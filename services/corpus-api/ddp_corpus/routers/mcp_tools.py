"""语料级 MCP 五工具的**域内实现**。MCP 服务只剩一层 HTTP 适配。

## 为什么这些实现从 `services/mcp` 搬到了这里

合仓后 `ddp_mcp/corpus.py` 自己连 PostgreSQL 与对象存储，用的是**一条没有
actor 的连接**：`search` 直接 `SELECT` 全库 evidence、`get_evidence` 直接从
MinIO 取裁图，谁能连上 MCP 端口谁就读得到整份语料。资源层（迁移 0015）之后
这是一个真实的越权面 —— 别人私有资源里的原文与裁图会被原样吐给外部 agent。

取数因此全部收回语料域：与 `/api/*` 走**同一条** `current_actor` +
`ddp_corpus.policy` + `ddp_corpus.document_context` 的授权链，不在本文件里
另写一套判据（铁律 4）。MCP 那一侧只负责把可信的 actor 上下文转过来。

## 授权的单位是**资产的那次固定解析**，不是"这份内容"

去重是全局的：两个人上传相同字节共用**一条 Document**。所以
"这份 Document 对你可见"**不等于**"另一个资产对它做的那次解析对你可见"。
把可见性停在 Document 上会漏三样东西：

    别人那次解析的原文块 · 别人那次编译出的生成物证据 · 别人给资产起的文件名

`document_context.search_contexts` 给的正是"你有权读的那些固定 parse id"，
以及每个 id 背后的资产身份（resource / version / filename）。所以：

- **检索前**把 `authorized_parse_job_ids` 下推给检索层（排序前收作用域）；
- **出参**里的文件名一律来自资产上下文，**永不读 `Document.filename`**；
- 同一份内容你有好几份资产时，全部列在 `copies` 里，不替你挑一份。

## 每一次 await 之后都要复核

检索、模型生成、对象存储读取都是 await。期间资产可能被撤销/删除/换版，
而手里那份候选是**上一刻**的授权结果。所以模型出网之前复核一次、
拿到结果之后再复核一次 —— 这也是 `/api/search` 的做法。

## 路径为什么在 `/internal/`

入口（control-api）只转发 `corpusPrefixes` 里的 `/api/*`，所以
`/internal/mcp/*` 从公网到不了；它只给内网的 MCP 服务用。但**它不是
service-only**：这些工具要的正是"某个用户/某把 key"的身份，
所以挂 `current_actor` 而不是 `require_service_actor`。
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.config import settings
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.document_context import DocumentContext, search_contexts
from ddp_corpus.errors import APIError
from ddp_corpus.evidence import citation_out
from ddp_corpus.models import Chunk, Document, Evidence
from ddp_corpus.knowledge_policy import accessible_knowledge
from ddp_corpus.policy import authorized_document_ids, visible_document_condition
from ddp_corpus.routers import knowledge as knowledge_plane
from ddp_corpus.upstream import chat_request, embed_one
from ddp_core.agent import assertions_from_text
from ddp_core.knowledge import normalize_entity_name

router = APIRouter(prefix="/internal/mcp")

#: 一次 search 最多返回多少条。与工具签名里的 `limit` 上限一致（契约 v1）。
MAX_LIMIT = 50
#: `ask` 拿多少条证据进 prompt。沿用搬迁前的值，别顺手调 —— 它是契约行为。
ASK_EVIDENCE = 8


# --------------------------------------------------------------------- 资产身份

def _asset_fields(contexts: list[DocumentContext]) -> dict:
    """一条证据对外的**资产身份**。

    `contexts` 是"这个 actor 有权通过哪些资产读到这次解析"。多于一条时
    **全列出来**：同字节去重之后，同一次解析可能同时属于你的两份资产
    （两次上传、或同一资产的两个版本指向同一次解析）。替调用方挑一份的话，
    另一份在界面上就凭空消失了 —— 而那正是"多份同字节资产"的用户会遇到的事。

    排序按 (创建时间, version_id)，保证同一份数据每次的主选一致。
    """
    ordered = sorted(contexts, key=lambda c: (c.created_at, c.version_id or ""))
    primary = ordered[0]
    return {
        "resource_id": primary.resource_id,
        "source_version_id": primary.version_id,
        # **文件名来自资产，不是 Document**：别人给同一份字节起的名字是他的私事
        "filename": primary.filename,
        "copies": [{"resource_id": item.resource_id, "source_version_id": item.version_id,
                    "filename": item.filename} for item in ordered],
    }


# --------------------------------------------------------------------- 证据形状

async def _resolve_chunk(session: AsyncSession, evidence: Evidence) -> Chunk | None:
    """这条证据现在还接得回哪个 chunk（接不回就是 `resolved=false`）。

    判据与 `evidence.load_citations` 一致：稳定定位键是 `(parse_job_id, seq)`，
    而 `chunk_id` 每次 reindex 都重铸，所以只能现算、不能落库。
    """
    return await session.scalar(select(Chunk).where(
        Chunk.parse_job_id == evidence.parse_job_id, Chunk.seq == evidence.seq,
        or_(Chunk.evidence_id == evidence.id, Chunk.derived_evidence_id == evidence.id)))


async def _evidence_payload(session: AsyncSession, evidence: Evidence, *,
                            contexts: list[DocumentContext],
                            live_chunk_id: str | None = None,
                            score: float | None = None,
                            similarity: float | None = None) -> dict:
    """MCP 契约里的 evidence 形状。

    `contexts` 是调用方已经拿到的授权证明（这次解析对他开放的资产）。
    **空列表在这里是程序错误，不是"没有资产"** —— 它意味着调用方跳过了
    授权判定，所以直接拒绝构造，而不是悄悄返回一条没有出处身份的证据。

    `live_chunk_id` 是调用方作出的断言："这条证据现在就接在这个 chunk 上"——
    检索命中天然如此，那条路不必再查一次库。不传就自己查。**不要改成
    "传了 None 就当接不回去"**：那会让忘了传的路径静默地把出处都标成失效。
    """
    if not contexts:
        raise APIError(500, "evidence payload built without an authorized asset",
                       "api_error", "internal_error")
    chunk_id = live_chunk_id or (
        chunk.id if (chunk := await _resolve_chunk(session, evidence)) else None)
    payload = {
        "evidence_id": evidence.id, "document_id": evidence.document_id,
        # 这条证据出自哪一次固定解析。它既是出处的一部分，也是**复核授权的键**
        "parse_revision": evidence.parse_job_id,
        **_asset_fields(contexts),
        "page_idx": evidence.page_idx, "seq": evidence.seq, "bbox": evidence.bbox,
        "page_size": evidence.page_size, "kind": evidence.kind,
        "content": evidence.content, "snippet": (evidence.content or "")[:500],
        "source_type": "generated" if evidence.derived_from else "source",
        "derived_from": evidence.derived_from, "review_state": evidence.review_state,
        "resolved": chunk_id is not None, "chunk_id": chunk_id,
        "crop_key": evidence.crop_key,
        "score": score, "similarity": similarity,
    }
    # 对象键 -> 受鉴权保护的稳定裁图路径。**两个平面共用的唯一一份映射**
    # （`evidence.citation_out`）—— MCP 曾经自己拼一遍，于是它的键分段规则
    # 与产品层漂开了。返回的是相对路径，绝对化是 MCP 适配层的事（它才知道
    # 自己那份对外基址）。
    return citation_out(evidence.document_id, payload)


# --------------------------------------------------------------------- search

class SearchIn(BaseModel):
    query: str = Field(default="", max_length=2000)
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)


def _scope(contexts: dict[str, list[DocumentContext]]) -> dict:
    """这次检索的作用域有多大。

    "你的资产一次固定解析都没有"与"检索没命中"是两件事，而契约的 `degraded`
    词汇表里没有前者对应的取值（`enums.yaml` 不在本轮的改动面里，自造一个
    没有用户可见文案的值比不报更糟）。所以把它做成一个显式的计数字段：
    结果为空时，调用方看得出来是"作用域为零"还是"真的没命中"。
    """
    return {"authorized_parse_revisions": len(contexts)}


async def _search(request: Request, session: AsyncSession, actor: Actor, *,
                  query: str, limit: int, version_ids: list[str] | None = None) -> dict:
    if not query.strip():
        return {"results": [], "degraded": "empty_query", "scope": {"authorized_parse_revisions": 0}}
    permitted = await authorized_document_ids(session, actor)
    contexts = await search_contexts(session, actor, version_ids=version_ids)
    if not permitted or not contexts:
        # 没有可读的固定解析 —— 结果为空是事实，而"为什么空"写在 scope 里
        return {"results": [], "degraded": "no_hits", "scope": _scope(contexts)}

    http = request.app.state.http
    index = request.app.state.search_index
    degraded: str | None = None
    try:
        vector = await embed_one(http, query)
    except Exception:
        # 零向量顶上会让"语义检索还在工作"变成一句假话（不变式 2）
        vector, degraded = None, "embedding_unavailable"

    hits = await index.search(
        session, vector=vector, query=query, document_id=None, limit=limit,
        candidates=max(settings.qa_candidates, limit * 3),
        min_similarity=settings.qa_min_similarity,
        # **授权进 SQL，排序之前**：文档一层收内容范围，parse 一层收"哪一次解析"。
        # 只给文档那一层的话，同字节去重会让你读到别人那次解析的块
        authorized_document_ids=permitted,
        authorized_parse_job_ids=list(contexts))
    if not hits:
        return {"results": [], "degraded": degraded or "no_hits", "scope": _scope(contexts)}

    ids = [hit.get("derived_evidence_id") or hit.get("evidence_id") for hit in hits]
    # 纵深防御：证据行也按可见性 join 一次。检索层已经收过作用域，这一条防的是
    # "以后谁给检索加了一条旁路" —— 出处是这个系统对外的原文出口，不能只有一道门
    rows = (await session.execute(
        select(Evidence).join(Document, Document.id == Evidence.document_id).where(
            Evidence.id.in_([value for value in ids if value]),
            visible_document_condition(actor)))).scalars().all()
    evidence = {row.id: row for row in rows}

    # **await 之后复核。** 上面那几步之间资产可能被撤销或换版；拿着上一刻的
    # 授权结果往外发，等于把一次已经撤销的许可延续到这一次响应里
    contexts = await search_contexts(session, actor, version_ids=version_ids)
    results = []
    for hit, evidence_id in zip(hits, ids):
        row = evidence.get(evidence_id)
        if row is None:
            continue
        # 判据是**证据自己的 parse_job_id**，不是命中里的那个：要发出去的是它
        allowed = contexts.get(row.parse_job_id)
        if not allowed:
            continue
        results.append(await _evidence_payload(
            session, row, contexts=allowed, live_chunk_id=hit.get("chunk_id"),
            score=hit.get("score"), similarity=hit.get("similarity")))
    results = await _still_authorized(session, actor, results, version_ids=version_ids)
    return {"results": results, "degraded": degraded, "scope": _scope(contexts)}


@router.post("/search")
async def search(body: SearchIn, request: Request, actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session)) -> dict:
    return await _search(request, session, actor, query=body.query, limit=body.limit)


# --------------------------------------------------------------------- ask

class AskIn(BaseModel):
    question: str = Field(default="", max_length=4000)


async def _still_authorized(session: AsyncSession, actor: Actor,
                            items: list[dict], *, version_ids: list[str] | None = None) -> list[dict]:
    """按**当下**的授权集合过一遍证据列表。

    两个用处：模型出网前（别把刚刚被撤销的原文送出去）与拿到回答之后
    （别把它当成还成立的出处）。两次都要做 —— 生成本身就是一次长 await。
    """
    contexts = await search_contexts(session, actor, version_ids=version_ids)
    # Refresh the asset aliases too: a second copy may have been withdrawn while
    # the parse remains readable through another asset.
    return [{**item, **_asset_fields(contexts[item["parse_revision"]])}
            for item in items if item.get("parse_revision") in contexts]


def _no_evidence(degraded: str | None, contexts_scope: dict) -> dict:
    return {"assertions": [{"text": "语料中未找到可支持的证据。", "evidence_ids": [],
                            "verification": {"state": "unverified", "mode": None},
                            "unsupported": True, "citations": []}],
            "degraded": degraded or "no_hits", "scope": contexts_scope}


@router.post("/ask")
async def ask(body: AskIn, request: Request, actor: Actor = Depends(current_actor),
              session: AsyncSession = Depends(get_session)) -> dict:
    found = await _search(request, session, actor,
                          query=body.question, limit=ASK_EVIDENCE)
    evidence = found["results"]
    if not evidence:
        return _no_evidence(found["degraded"], found["scope"])

    # **出网前复核。** 下一行就要把原文拼进 prompt 发给模型 —— 那是一次
    # 不可撤回的外发，检索与这一刻之间的任何撤销都必须在这里生效
    evidence = await _still_authorized(session, actor, evidence)
    if not evidence:
        return _no_evidence(found["degraded"], found["scope"])

    sources = "\n\n".join(f"[{i}] {item['content']}" for i, item in enumerate(evidence, 1))
    http = request.app.state.http
    upstream = chat_request(http, [
        {"role": "system", "content": "只依据资料回答并用 [n] 引用。"},
        {"role": "user", "content": f"【资料】\n{sources}\n【问题】\n{body.question}"},
    ], stream=False)
    try:
        response = await http.send(upstream)
    except Exception:
        return {"assertions": [], "degraded": "answer_unavailable", "scope": found["scope"]}
    if response.status_code != 200:
        # 证据已经检索到了，但结论生成不出来。**不许把它渲染成"没有答案"** ——
        # 那会让一次上游故障看起来像"文档里没有"
        return {"assertions": [], "degraded": "answer_unavailable", "scope": found["scope"]}

    try:
        text = (response.json()["choices"][0]["message"]["content"] or "")
    except (ValueError, KeyError, IndexError, TypeError):
        # 200 但形状不对（配错的兼容服务会这样）。这仍然是"生成不可用"，
        # 不是 500：证据已经有了，把它当成一次可见的降级报出去
        return {"assertions": [], "degraded": "answer_unavailable", "scope": found["scope"]}

    # 生成是一次很长的 await：回来之后**再复核一次**才能把这些出处发出去
    authorized = await _still_authorized(session, actor, evidence)
    if len(authorized) != len(evidence):
        # Generated prose can contain any prompt source even without a citation.
        # Removing citations cannot redact it, and re-numbering would misbind [n].
        raise APIError(404, "source unavailable", "invalid_request_error", "not_found")
    evidence = authorized
    by_id = {item["evidence_id"]: item for item in evidence}
    assertions = []
    for item in assertions_from_text(text, [item["evidence_id"] for item in evidence]):
        citations = [by_id[value] for value in item["evidence_ids"] if value in by_id]
        assertions.append({
            "text": item["text"],
            # 复核掉的那条不能留在 evidence_ids 里：它会变成一条指不回任何
            # 证据的"有支撑"结论
            "evidence_ids": [citation["evidence_id"] for citation in citations],
            "verification": {"state": "unverified", "mode": None},
            "unsupported": not bool(citations),
            "citations": citations,
        })
    return {"assertions": assertions, "degraded": found["degraded"], "scope": found["scope"]}


# --------------------------------------------------------------------- get_evidence

@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str, request: Request,
                       actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session)) -> dict:
    """证据原文 + 定位信息 + 裁图字节（base64）。

    裁图**随响应回**而不是给个 URL：MCP 要把它做成原生 image content 交给
    外部 agent 自己核对，而那张图受鉴权保护，agent 手里没有可用的凭据。

    **授权判的是"这次解析对你开放吗"，不是"这份内容你看得见吗"。**
    同字节去重让两个人共用一条 Document；读得到那条 Document 不等于读得到
    别人那次解析产出的块、生成物证据与文件名。
    """
    evidence = await session.get(Evidence, evidence_id)
    if evidence is None:
        raise APIError(404, "evidence not found", "invalid_request_error", "not_found")
    contexts = await search_contexts(session, actor, evidence.document_id)
    allowed = contexts.get(evidence.parse_job_id)
    if not allowed:
        # 查不到、内容看不见、以及"看得见内容但这不是你那次解析"三者同形。
        # 分开报等于给出一个"这条证据存不存在"的探测口
        raise APIError(404, "evidence not found", "invalid_request_error", "not_found")

    payload = await _evidence_payload(session, evidence, contexts=allowed)
    image, payload["crop_degraded"] = await _crop_bytes(
        getattr(request.app.state, "storage", None), evidence.crop_key)
    # 取像素是一次外部 await；发出去之前按当下的授权再确认一次
    current = await search_contexts(session, actor, evidence.document_id)
    if evidence.parse_job_id not in current:
        raise APIError(404, "evidence not found", "invalid_request_error", "not_found")
    payload.update(_asset_fields(current[evidence.parse_job_id]))
    return {"status": "ok", "evidence": payload,
            "crop": None if image is None else {
                "mime": "image/png", "data_base64": base64.b64encode(image).decode()}}


async def _crop_bytes(storage, key: str | None) -> tuple[bytes | None, str | None]:
    """返回 (裁图字节, 降级原因)。**"没有图"与"取不到图"必须分开。**

    外部 agent 拿不到像素就核对不了这条证据；把两者都渲染成"没有图"，等于把
    一次配置缺失伪装成"这条证据本来就没裁图"（不变式 2）。
    """
    if not key:
        return None, None
    if storage is None:
        return None, "crop_store_unavailable"
    try:
        return await storage.get(key), None
    except Exception:
        return None, "crop_read_failed"


# --------------------------------------------------------------------- 知识平面

def _reattach(row: dict, allowed_revisions: set[str]) -> dict:
    """把一条 wiki 句子 / 图谱边的出处收敛到"这个人有权读的那几次解析"。

    知识层的行是**全语料生成物**（一句话可能引三份文档的证据），所以
    "这条行对他可见"与"这条行的每个出处对他可见"是两件事。判据用
    `parse_revision` 而不是 `document_id`：同字节共用一条 Document，
    按文档判会把别人那次解析的 snippet 当成"你反正看得见这份内容"放过去。

    **第一道门在知识平面**（`knowledge_policy.accessible_knowledge`），而它今天
    是**全有或全无**的：依赖文档没有全部可见，整条行就不出现。所以这一层在当前
    实现下拦不到整行，它是出口上的第二道门 —— 防的是那条判据哪天放宽成
    "有一条可见出处就算可见"：那时私有原文会经 `citations[].snippet` 漏出去，
    而 MCP 正是把整段 JSON 交给外部 agent 的那个出口。

    去掉出处之后 `evidence_ids` 必须跟着重算，没有剩下的就标 `unsupported`
    —— 不变式 1/2：指不回证据必须明确说出来，不能留一个看起来有支撑的句子。
    """
    citations = [item for item in (row.get("citations") or [])
                 # 没有 parse_job_id 的出处一律不放行（失败闭合）
                 if item.get("parse_job_id") in allowed_revisions]
    live = [item for item in citations if item.get("resolved")]
    return {**row, "citations": citations,
            "evidence_ids": [item["evidence_id"] for item in live],
            "unsupported": bool(row.get("unsupported")) or not live}


@router.get("/wiki")
async def read_wiki(value: str = Query(..., min_length=1, max_length=255),
                    actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)) -> dict:
    """Wiki 条目。**实现委托给知识平面**，不在这里另读一遍全局行。"""
    knowledge_plane.require_knowledge_enabled()   # /api/wiki 的路由级依赖，直调时要自己带上
    # 查无此条目就让知识平面的 404 原样冒上去 —— MCP 适配层把它还原成
    # `{"status": "not_found"}`。在这里改写成 200 + not_found 的话，
    # "这个端点 404 了"与"这个条目不存在"就再也分不开
    data = await knowledge_plane.read_wiki(value, actor=actor, session=session)
    # 委托调用是 await：出处的收敛用**回来之后**的授权集合
    allowed = set(await search_contexts(session, actor))
    access = await accessible_knowledge(session, actor)
    if data["entry"]["id"] not in access["entries"] or any(
            sentence["id"] not in access["sentences"]
            for section in data["sections"] for sentence in section["sentences"]):
        raise APIError(404, "wiki not found", "invalid_request_error", "not_found")
    return {
        "status": "ok",
        "entry": {"id": data["entry"]["id"], "title": data["entry"]["title"]},
        "sections": [{"heading": section["heading"],
                      "sentences": [_reattach(sentence, allowed)
                                    for sentence in section["sentences"]]}
                     for section in data["sections"]],
    }


@router.get("/graph/neighbors")
async def graph_neighbors(value: str = Query(..., min_length=1, max_length=255),
                          depth: int = Query(default=1),
                          actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session)) -> dict:
    """实体邻域。同样委托知识平面；`depth` 越界照旧是 `invalid_depth`。"""
    if not 1 <= depth <= 3:
        raise APIError(400, "depth must be between 1 and 3", "invalid_request_error",
                       "invalid_depth")
    knowledge_plane.require_knowledge_enabled()
    # **先让知识平面判授权**（它对中心实体不可见时抛 404），再解析中心 id。
    # 反过来写也"能跑"，但那样就有一次发生在鉴权之前的实体解析 ——
    # 以后谁在那中间加一行日志或计数，就成了一个可用来探测实体是否存在的接口
    data = await knowledge_plane.graph(entity=value, depth=depth,
                                       actor=actor, session=session)
    # 中心节点**从已经过授权的结果里认**，不另外查一次库：
    # 查库那一版要么复制一份实体解析规则（铁律 4），要么调知识平面的私有函数
    # （它的签名已经变过一次）。而且那次查询发生在鉴权之前，本身就是一个
    # "这个实体存不存在"的探测口。归一化用的是两侧共用的那一份。
    target = normalize_entity_name(value)
    center = next((row for row in data["entities"]
                   if row["id"] == value or row.get("normalized_name") == target), None)
    if center is None:
        raise APIError(404, "entity not found", "invalid_request_error", "not_found")
    allowed = set(await search_contexts(session, actor))
    access = await accessible_knowledge(session, actor)
    if (any(row["id"] not in access["entities"] for row in data["entities"])
            or any(row["id"] not in access["edges"] for row in data["edges"])):
        raise APIError(404, "entity not found", "invalid_request_error", "not_found")
    return {
        "status": "ok", "center_id": center["id"],
        "entities": [{**row, "name": row["canonical_name"]} for row in data["entities"]],
        "edges": [_reattach(edge, allowed) for edge in data["edges"]],
    }
