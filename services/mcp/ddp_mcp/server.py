"""MCP 平面 —— 五个语料工具 + deprecated ask_document。

传输：Streamable HTTP。对外经 control-api 代理（API key 鉴权在入口），
本服务只对内网开放。

## 本服务不做鉴权，但**必须验证"这次调用来自入口"**

入口验完 key 之后把 actor 上下文写成一组 `X-DDP-*` 头转发过来，并把客户端
传来的同名头无条件剥掉。本服务据此判断调用者是谁，判据是随请求一起来的
服务凭据（`Authorization: Bearer $SERVICE_TOKEN`）。**缺身份一律拒绝**，
细节与理由见 `ddp_mcp/corpus.py` 的模块说明。

设计要点：
- 五个语料工具保持小而正交，取数全在 corpus-api 的 `/internal/mcp/*`
  （授权与 `/api/*` 同一条链）；本模块只转发
- 旧 ask_document 只为兼容保留，且**不再直连模型网关的解析平面** ——
  它现在和对外 `/v1/parse` 走同一条语料域路径，见那个工具自己的说明
- 大文档解析耗时 -> "解析中即返回 + 请稍后重试"模式，不阻塞 MCP 同步调用
- 返回"证据 + 出处（页码/bbox）"而非只有结论
- v1 检索 = BM25（中文按二元组、英文按词切分）；v2 换向量检索 ——
  只改内部实现，工具签名永不变（铁律 6）
"""
import asyncio
import base64
import ipaddress
import json
import os
import re
import struct
import sys
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
import redis.asyncio as redis
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from rank_bm25 import BM25Okapi

from ddp_core.crops import render_crop

from ddp_mcp import corpus as corpus_plane
from ddp_mcp.corpus import (
    ask_impl, forwarded_identity, get_evidence_impl, graph_neighbors_impl,
    read_wiki_impl, search_impl,
)

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:9000")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "change-me")
# v2：配置 REDIS_URL 即启用向量检索（读 worker 建好的 chunks_idx）；缺省纯 BM25
REDIS_URL = os.environ.get("REDIS_URL", "")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
SHORT_DOC_CHARS = 3000   # 短于此的文档直接全文作证据
TOP_K = 3                # BM25 命中块数

mcp = FastMCP("DeepDocParse")

_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), trust_env=False)


def _headers() -> dict:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _tokenize(text: str) -> list[str]:
    """中英混排的轻量切分：英文/数字按词，CJK 按二元组（无分词依赖）。"""
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    cjk = re.findall(r"[一-鿿]", text)
    tokens += ["".join(p) for p in zip(cjk, cjk[1:])] or cjk[:1]
    return tokens


def _layout_blocks(layout_json: dict) -> list[dict]:
    """layout_json -> [{text, page_idx, bbox, page_size}]，检索与出处的统一数据源。

    输入是 DDP-Layout v1（字段清单与坐标系见 ../docs/layout-format.md）。
    **只读承诺字段**：`pdf_info[].page_idx / page_size / para_blocks[].bbox /
    lines[].spans[].content`。引擎附带的其它字段（type/index/angle…）不保证跨引擎存在，
    依赖它们会在换解析引擎时安静地失效。
    """
    blocks = []
    for page in layout_json.get("pdf_info", []):
        for blk in page.get("para_blocks", []):
            spans_text = []
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("content"):
                        spans_text.append(span["content"])
            text = " ".join(spans_text).strip()
            if text:
                blocks.append({
                    "text": text,
                    "page_idx": page.get("page_idx", 0),
                    "bbox": blk.get("bbox"),
                    "page_size": page.get("page_size"),
                })
    return blocks


_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        _redis = redis.from_url(REDIS_URL)
    return _redis


# **没有"本地算一个 doc_hash 兜底"了。** 文档身份只认 `/v1/parse/{id}` 返回的
# 那个值。语料域把外部提交的 doc_id 重写成了**按主体分域**的哈希
# （`routers/external.py`：猜中一个内容哈希不得命中别人的网关缓存），
# 于是 `sha256(file_url)` 已经不是任何人的身份 —— 拿它去查 Redis 分块索引，
# 命中的会是**上一轮、别的主体**建的索引。少一条兜底，多一条"取不到就退 BM25"。


# 与写入侧（model-gateway 的 task_store）共用 ddp_core 里的同一份命名规则。
# 两边各写一遍的后果是永久零命中且不报错 —— 见 ddp_core/vector_index.py
from ddp_core.vector_index import chunk_index_name  # noqa: E402,F401


async def _record_retrieval(mode: str) -> None:
    """记录本次走的检索路径（vector / bm25），供运维与 e2e 判别是否真的用上了向量检索。
    静默降级最怕的就是没人知道它降级了。"""
    r = _get_redis_safe()
    print(f"[ask_document] retrieval={mode}", file=sys.stderr)
    if r is not None:
        try:
            await r.incr(f"metrics:retrieval:{mode}")
        except Exception:
            pass


def _get_redis_safe() -> "redis.Redis | None":
    """连 REDIS_URL 写错（非法 scheme 等）也不能抛——否则整个工具挂掉而非退回 BM25。"""
    try:
        return _get_redis()
    except Exception:
        return None


async def _vector_retrieve(doc_hash: str, question: str, k: int = 3) -> list[dict] | None:
    """v2 检索：问题向量化（gateway /v1/embeddings）+ Redis FT KNN。
    任何一环不可用（未配/配错 REDIS_URL、未注册 embedding 模型、TEI 不可达、
    Redis 无 RediSearch、索引未建、零命中）都返回 None，调用方回退 BM25 ——
    工具签名与返回形态不变（铁律 6）。"""
    r = _get_redis_safe()
    if r is None:
        return None
    try:
        resp = await _http.post(f"{GATEWAY}/v1/embeddings", headers=_headers(),
                                json={"input": question})
        if resp.status_code != 200:
            return None
        vec = resp.json()["data"][0]["embedding"]
        blob = struct.pack(f"<{len(vec)}f", *vec)
        reply = await r.execute_command(
            "FT.SEARCH", chunk_index_name(len(vec)),
            f"(@doc_hash:{{{doc_hash}}})=>[KNN {k} @vec $BLOB AS score]",
            "PARAMS", "2", "BLOB", blob,
            "SORTBY", "score",
            "RETURN", "4", "text", "page_idx", "bbox", "page_size",
            "DIALECT", "2",
        )
        hits = []
        for item in reply[2::2]:
            fields = {}
            for name, value in zip(item[::2], item[1::2]):
                name = name.decode() if isinstance(name, bytes) else name
                value = value.decode() if isinstance(value, bytes) else value
                fields[name] = value
            hits.append({
                "text": fields.get("text", ""),
                "page_idx": int(fields.get("page_idx", 0)),
                "bbox": json.loads(fields["bbox"]) if fields.get("bbox") else None,
                # page_size 随 chunk 存下：缺它时裁剪要退回 pdfium 页尺寸，
                # 遇到 CropBox 偏移/旋转页会裁错区域（v1 路径本来是对的）
                "page_size": json.loads(fields["page_size"]) if fields.get("page_size") else None,
            })
        return hits or None
    except Exception:
        return None  # 检索增强失败绝不阻断 v1 路径


async def _vqa(image_data_uri: str, question: str) -> str:
    """经 gateway 调 VQA 运行时（模型走注册表 default）。"""
    resp = await _http.post(
        f"{GATEWAY}/v1/chat/completions",
        headers=_headers(),
        json={
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": question},
            ]}],
            "stream": False,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]




async def _crop_page_region(pdf_bytes: bytes, page_idx: int, bbox: list,
                            page_size: list | None) -> str | None:
    """共享核心裁图与 PDFium 串行锁；在线程池渲染，避免阻塞 MCP 事件循环。"""
    png = await asyncio.to_thread(render_crop, pdf_bytes, page_idx, bbox, page_size)
    if png is None:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode()


# --------------------------------------------------------------- ask_document 的边界
#
# `ask_document(file_url, …)` 的参数是一个**裸 URL**，没有任何能表达"我有权
# 读它"的东西。所以它只能受理**本部署之外**的文件：本部署自己的存储面
# （稳定文件 URL、对象存储）背后是受资源 ACL 保护的内容，而一个 URL 证明不了
# 授权 —— 拿它去取，等于用 MCP 的服务身份替调用方绕过 ACL。
#
# 要问已入库的文档，用 `search` / `get_evidence`：那条路带着 actor 上下文，
# 授权在语料域里判。

#: 本部署自己的地址从这些环境变量里来（部署侧本来就要配它们）。
_SELF_URL_ENVS = (
    "CORPUS_API_URL", "GATEWAY_URL", "CONTROL_API_URL", "MCP_PUBLIC_BASE_URL",
    "PUBLIC_BASE_URL", "INTERNAL_BASE_URL", "MINIO_ENDPOINT", "OBJECT_ENDPOINT",
    "OBJECT_PUBLIC_ENDPOINT",
)


def _self_hosts() -> set[str]:
    hosts = set()
    # 已生效的配置值排在前面：环境变量可能是空的（默认值兜底的那些），
    # 而这两个是本进程真正在用的地址
    values = [corpus_plane.CORPUS_URL, corpus_plane.PUBLIC_BASE_URL, GATEWAY]
    values += [os.environ.get(name) or "" for name in _SELF_URL_ENVS]
    for value in values:
        value = (value or "").strip()
        if not value:
            continue
        # MINIO_ENDPOINT 这类是裸 host:port，补个 scheme 才解析得出 hostname
        parsed = urlparse(value if "//" in value else f"//{value}", scheme="http")
        if parsed.hostname:
            hosts.add(parsed.hostname.lower())
    return hosts


def _require_external_url(file_url: str) -> None:
    """只放行**本部署之外**的 http(s) URL；其余一律拒绝并说清为什么。

    这条检查基于 URL 字面量，**不做 DNS 解析** —— 一个解析到内网地址的外部
    域名（DNS rebinding）挡不住。真正的纵深防御是"网关与语料侧各自只信任
    自己认识的地址"，这里挡的是最直接的那条：把内网地址直接写进参数。
    """
    parsed = urlparse(file_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ToolError("file_url 必须是 http(s) 绝对地址")
    if "@" in (parsed.netloc or ""):
        raise ToolError("file_url 不得携带用户名/密码")
    blocked = host in _self_hosts() or host in ("localhost", "::1") \
        or host.endswith((".localhost", ".internal", ".local"))
    if not blocked:
        try:
            blocked = not ipaddress.ip_address(host).is_global
        except ValueError:
            blocked = False
    if blocked:
        raise ToolError(
            "ask_document 只受理本部署之外的文件地址。这个地址指向本部署自己的"
            "服务或内网，而它背后的内容受资源授权保护 —— 一个 URL 证明不了你"
            "有权读它。已入库的文档请用 search / get_evidence（那条路带着你的"
            "身份，授权在语料侧判）。")


@mcp.tool()
async def search(query: str, limit: int = 10) -> dict:
    """跨整份共享语料混合检索，返回带 evidence、bbox 与裁图 URL 的结果。"""
    return await search_impl(query, limit)


@mcp.tool()
async def ask(question: str) -> dict:
    """基于语料证据回答，返回 DDP-Agent Assertion[]，不返回无类型长字符串。"""
    return await ask_impl(question)


@mcp.tool()
async def get_evidence(evidence_id: str):
    """按 ID 读取证据原文、定位信息与 MCP 原生裁图内容。"""
    return await get_evidence_impl(evidence_id)


@mcp.tool()
async def read_wiki(entry_id_or_title: str) -> dict:
    """读取 Wiki 条目；每个句子均带 evidence 或显式 unsupported。"""
    return await read_wiki_impl(entry_id_or_title)


@mcp.tool()
async def graph_neighbors(entity_id_or_name: str, depth: int = 1) -> dict:
    """读取实体 1~3 跳邻域；每条边均携带可复核 Evidence。"""
    return await graph_neighbors_impl(entity_id_or_name, depth)


@mcp.tool()
async def ask_document(file_url: str, question: str) -> str:
    """[deprecated] 对未入库文档或图片提问，返回带出处的答案。

    支持 PDF/DOCX/PPTX/XLSX/图片的 URL。首次询问大文档时会触发解析，
    若返回"解析中"，请稍后用相同参数重试。

    ## 两条边界（都是越权修复，不是风格改动）

    1. **只受理本部署之外的地址**（`_require_external_url`）。裸 URL 表达不了
       授权，本部署自己的存储面背后是受 ACL 保护的内容。
    2. **解析走语料域的 `/v1/parse*`，不再直连模型网关。** 直连那一版是拿
       MCP 的服务凭据去问网关，而网关按文档哈希做幂等复用 —— 于是"猜中一个
       已被解析过的地址"就能拿到**别人**那次解析的全文，一个授权判据都不经过。
       语料域这条路带着调用者的身份：它按主体分域重写 doc_id，取结果前还要
       求这次任务对本人可见（`routers/external.py`），顺带把归属与计量记上。
    """
    identity = forwarded_identity()      # 没有可信身份就什么都不做
    _require_external_url(file_url)
    ext = PurePosixPath(urlparse(file_url).path).suffix.lower()

    # ---- 图片：直接走 VQA，秒回 ----
    if ext in IMAGE_EXTS:
        img_resp = await _http.get(file_url, follow_redirects=True)
        img_resp.raise_for_status()
        mime = img_resp.headers.get("content-type", f"image/{ext.lstrip('.')}")
        data_uri = f"data:{mime};base64," + base64.b64encode(img_resp.content).decode()
        answer = await _vqa(data_uri, question)
        return f"{answer}\n\n---\n出处：整张图片（{file_url}）"

    # ---- 文档：提交解析（语料域按"主体 + 地址"幂等，重复调用复用自己的任务）----
    submit = await _http.post(f"{corpus_plane.CORPUS_URL}/v1/parse", headers=identity,
                              json={"file_url": file_url})
    if submit.status_code == 429:
        return "解析队列已满，请稍后用相同参数重试。"
    if submit.status_code in (401, 403):
        raise ToolError(f"这次调用无权提交解析：{submit.text[:200]}")
    submit.raise_for_status()
    task_id = submit.json()["task_id"]

    status_resp = await _http.get(f"{corpus_plane.CORPUS_URL}/v1/parse/{task_id}",
                                  headers=identity)
    status_resp.raise_for_status()
    status = status_resp.json()
    if status["status"] == "failed":
        return f"文档解析失败：{status.get('error') or '未知原因'}。请检查文件是否有效。"
    if status["status"] != "succeeded":
        return (f"文档正在解析中（任务 {task_id}，状态 {status['status']}），"
                "请稍后用完全相同的参数重试本工具。")

    result_resp = await _http.get(f"{corpus_plane.CORPUS_URL}/v1/parse/{task_id}/result",
                                  headers=identity)
    if result_resp.status_code == 409:  # 兜底：极小窗口内结果尚未归档完成
        return (f"文档解析已完成，结果归档中（任务 {task_id}），"
                "请稍后用完全相同的参数重试本工具。")
    if result_resp.status_code == 404:
        # 语料域说"这个任务对你不可见"。**不许退回直连网关再试一次** ——
        # 那正是这次要堵的那条路
        raise ToolError(f"解析任务 {task_id} 对当前身份不可见")
    result_resp.raise_for_status()
    result = result_resp.json()
    markdown: str = result.get("markdown", "")
    # 告诉调用方去哪取完整结果时要给**他能访问的**那个地址（入口），
    # 而不是内网的语料地址 —— 后者他连不上，等于没给
    result_url = (f"{corpus_plane.PUBLIC_BASE_URL or corpus_plane.CORPUS_URL}"
                  f"/v1/parse/{task_id}/result")

    # ---- 短文档：全文即证据，agent 自己的 LLM 综合 ----
    if len(markdown) <= SHORT_DOC_CHARS:
        return (f"文档全文（较短，直接给出）：\n\n{markdown}\n\n---\n"
                f"出处：{file_url} 全文；完整结果（markdown/版面/图片）：GET {result_url}")

    # ---- 长文档：v2 向量检索优先（worker 建好的向量索引），失败回退 BM25 ----
    # **身份只认状态响应里的 doc_hash**，没有就不查向量索引（退 BM25，
    # 而 BM25 只吃这次授权拿到的 layout_json）。理由见上面 _doc_hash 那段注释：
    # 自己算一个哈希会去命中别的主体建的索引
    doc_hash = status.get("doc_hash")
    hits = await _vector_retrieve(doc_hash, question, k=TOP_K) if doc_hash else None
    await _record_retrieval("vector" if hits is not None else "bm25")
    if hits is None:
        blocks = _layout_blocks(result.get("layout_json", {}))
        # 空 token 块不入索引：_tokenize 只认英数+CJK，其他文种/全符号块会为空，
        # 全空语料会让 BM25Okapi 除零崩溃（M3 验收回归项）
        indexed = [(tokens, b) for b in blocks if (tokens := _tokenize(b["text"]))]
        if not indexed:
            return (f"文档已解析但无可检索文本块，返回开头片段：\n\n{markdown[:SHORT_DOC_CHARS]}\n\n---\n"
                    f"完整结果：GET {result_url}")

        bm25 = BM25Okapi([tokens for tokens, _ in indexed])
        candidates = [b for _, b in indexed]
        scores = bm25.get_scores(_tokenize(question))
        top = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)[:TOP_K]
        hits = [candidates[i] for i in top if scores[i] > 0] or [candidates[top[0]]]

    # ---- 首个命中块：裁剪原图区域 -> VQA 针对性验证（仅 PDF 支持裁剪）----
    answer = None
    if ext == ".pdf" and hits[0].get("bbox"):
        try:
            pdf_resp = await _http.get(file_url, follow_redirects=True)
            pdf_resp.raise_for_status()
            data_uri = await _crop_page_region(
                pdf_resp.content, hits[0]["page_idx"], hits[0]["bbox"], hits[0].get("page_size"))
            if data_uri:
                answer = await _vqa(
                    data_uri, f"请仅根据这张文档区域截图回答：{question}")
        except httpx.HTTPError:
            pass  # 原文件不可达时退化为纯文本证据

    evidence = "\n\n".join(
        f"[第 {h['page_idx'] + 1} 页 bbox={h['bbox']}] {h['text']}" for h in hits)
    parts = []
    if answer:
        parts.append(f"答案（已对第 {hits[0]['page_idx'] + 1} 页命中区域做视觉验证）：{answer}")
    parts.append(f"相关证据：\n{evidence}")
    parts.append(f"完整结果（markdown/版面/图片）：GET {result_url}")
    return "\n\n---\n\n".join(parts)


if __name__ == "__main__":
    # Streamable HTTP，供入口反向代理。
    #
    # **监听地址必须可配。** 容器编排里要听 0.0.0.0（入口在另一个容器里，
    # 得连得上）；而裸进程部署（infra/autodl/stack.bash）与入口同机，就该只听
    # 回环 —— 本服务不做用户鉴权，只信任入口下发的 actor 上下文头，多监听
    # 一个地址就是多一条"任何人都能自称 admin"的路。写死 0.0.0.0 时，
    # 部署侧那个 MCP_PORT 旋钮也是假的：改了它只会让入口 502。
    #
    # **`path="/"` 不是可有可无的。** 入口（control-api）转发 `/mcp` 时会把
    # 这个前缀**剥掉**（`internal/proxy` 的 prefixTrim，`/mcp` -> `/`），
    # 而 FastMCP 缺省把自己挂在 `/mcp` —— 两边一叠，入口转过来的 `/` 落在
    # 一个没有路由的位置上：**整个 MCP 平面 404**，而 mcp 进程健康、
    # 入口健康、鉴权也正常放行，日志里只有一行 `"POST / HTTP/1.1" 404`。
    # 2026-09-03 第一次从公网打 `/mcp` 时才发现（e2e 一直没覆盖这个平面）。
    # 两边的配对由 tests/test_architecture_guards.py 钉着。
    mcp.run(transport="http",
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "9100")),
            path="/")
