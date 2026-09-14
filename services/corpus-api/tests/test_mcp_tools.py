"""语料级 MCP 五工具在**语料域里**的授权。

## 这组用例存在的理由

搬迁前这五个工具的实现住在 `services/mcp/ddp_mcp/corpus.py`，自己连
PostgreSQL 与 MinIO，**一条没有 actor 的连接**：`search` SELECT 全库
evidence、`get_evidence` 直接按 crop_key 取像素。资源层上线后这是一个
实打实的越权面。现在实现收在 `routers/mcp_tools.py`，授权与 `/api/*`
共用 `ddp_corpus.policy`。

所以每条用例都成对写：**一个有权的人拿得到**（否则"全都拒绝"也能让
测试变绿，那种绿是假的），**一个无权的人拿不到，且拿不到的是内容本身**
—— 只断言 `results == []` 不够，还要断言那段私有原文一个字都没出现在
响应体里（出处的 snippet 是最容易漏的那条泄漏路径）。
"""
import json

import pytest
import respx
from httpx import Response

from ddp_corpus.models import (
    Chunk, Citation, Document, Evidence, GraphEdge, KnowledgeEntity, ParseJob, Resource,
    ResourceVersion, WikiEntry, WikiSection, WikiSentence, new_id,
)
from ddp_core.anchor import digest_of
from ddp_core.tokenize import tokenized

from tests.conftest import CHAT, EMBEDDINGS, ORG, actor_headers

ALICE = "actor-alice"          # conftest 的默认 actor
BOB = "actor-bob"


SECRET = "薪酬总额为四千二百万元"     # 只出现在 alice 的文档里
PUBLIC_TEXT = "公开手册的安装步骤说明"


def bob_headers(**over) -> dict:
    return actor_headers(BOB, **over)


async def _document(session, *, uploader: str, doc_id: str, filename: str,
                    organization_id: str = ORG) -> tuple[Document, ParseJob]:
    document = Document(id=new_id(), uploaded_by=uploader, organization_id=organization_id,
                        doc_id=doc_id, origin="web", filename=filename,
                        mime="application/pdf", object_key=f"uploads/{filename}",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash=doc_id[:8], status="succeeded", index_status="ready")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    return document, job


async def _evidence(session, document: Document, job: ParseJob, *, seq: int, text: str,
                    crop_key: str | None = None) -> Evidence:
    """一条证据 + 它当前接得回的 chunk（检索命中的就是这个 chunk）。"""
    evidence = Evidence(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=seq,
                        atom_key=f"source:{seq}", page_idx=seq, bbox=[10, 20, 300, 60],
                        page_size=[612, 792], kind="text", content=text,
                        content_digest=digest_of(text), crop_key=crop_key)
    session.add(evidence)
    await session.flush()
    session.add(Chunk(id=new_id(), document_id=document.id, parse_job_id=job.id, seq=seq,
                      page_idx=seq, bbox=[10, 20, 300, 60], page_size=[612, 792],
                      text=text, char_len=len(text), block_type="text",
                      text_tokenized=tokenized(text), evidence_id=evidence.id))
    await session.flush()
    return evidence


async def _claim(session, document: Document, owner: str, *, job: ParseJob | None = None,
                 publication: str = "private") -> tuple[Resource, ResourceVersion]:
    resource = Resource(id=new_id(), organization_id=document.organization_id,
                        owner_id=owner, uploaded_by=owner, display_name=document.filename,
                        publication=publication)
    session.add(resource)
    await session.flush()
    version = ResourceVersion(id=new_id(), resource_id=resource.id, version_no=1,
                              document_id=document.id, source_digest=document.doc_id,
                              parse_job_id=job.id if job else None,
                              filename=document.filename)
    session.add(version)
    if job and job.resource_id is None:
        job.resource_id = resource.id
    await session.flush()
    return resource, version


def _provider(owner: str, resource: Resource, version: ResourceVersion) -> dict:
    """知识层生成物的归属与来源绑定。

    `knowledge_policy.provider_allowed` 要求它齐全：没有归属的历史生成物一律
    被隔离（谁也看不到）。所以**正面用例必须把它写对**，否则"有权的人也看不到"，
    这组用例就成了假绿。
    """
    return {"generated_by": owner, "organization_id": resource.organization_id,
            "source_bindings": [{"resource_id": resource.id,
                                 "source_version_id": version.id,
                                 "document_id": version.document_id,
                                 "parse_revision": version.parse_job_id}]}


@pytest.fixture
async def corpus(session, app_state):
    """alice 的私有文档 + 一份已发布的文档。两者都真的能被检索到。"""
    private_doc, private_job = await _document(
        session, uploader=ALICE, doc_id="a" * 64, filename="salary.pdf")
    resource, version = await _claim(session, private_doc, ALICE, job=private_job)
    private_evidence = await _evidence(session, private_doc, private_job, seq=0,
                                       text=SECRET, crop_key=f"crops/{private_job.id}/0.png")

    public_doc, public_job = await _document(
        session, uploader=ALICE, doc_id="b" * 64, filename="handbook.pdf")
    public_resource, public_version = await _claim(session, public_doc, ALICE, job=public_job, publication="published")
    public_evidence = await _evidence(session, public_doc, public_job, seq=0,
                                      text=PUBLIC_TEXT)
    await session.commit()
    return {"private": private_doc, "private_evidence": private_evidence,
            "public": public_doc, "public_evidence": public_evidence,
            "private_job": private_job, "public_job": public_job,
            "public_resource": public_resource, "public_version": public_version,
            "provider": _provider(ALICE, resource, version)}


async def _search(client, query: str, **kw) -> Response:
    return await client.post("/internal/mcp/search", json={"query": query, **kw})


# ------------------------------------------------------------------ search

@respx.mock
async def test_search_returns_only_evidence_the_actor_may_read(actor_client, client, corpus):
    """无权的人**一个字**都拿不到；有权的人照常拿得到（成对断言，防假绿）。"""
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}))

    mine = await _search(actor_client, SECRET)
    assert mine.status_code == 200
    assert [item["evidence_id"] for item in mine.json()["results"]] == \
        [corpus["private_evidence"].id], "有权的人必须照常检索到自己的证据"

    client.headers.update(bob_headers())
    theirs = await _search(client, SECRET)
    assert theirs.status_code == 200
    body = theirs.json()
    assert body["results"] == []
    # **原文不许经 snippet/content 漏出去** —— 只断言空列表的话，
    # 一个"结果为空但带着 snippet 预览"的实现照样能过
    assert SECRET not in theirs.text
    assert body["degraded"] == "no_hits"


@respx.mock
async def test_published_resource_is_reachable_by_another_actor(client, corpus):
    """反向对照：如果什么都看不见，上面那条用例就是假绿。"""
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}))
    client.headers.update(bob_headers())
    found = await _search(client, PUBLIC_TEXT)
    assert [item["evidence_id"] for item in found.json()["results"]] == \
        [corpus["public_evidence"].id]


@respx.mock
async def test_search_scopes_before_ranking_not_after(client, corpus, app_state, monkeypatch):
    """ACL 必须作为检索参数下推，**不能**在命中列表上事后过滤。

    事后过滤的形态是"按全语料取 top-k 再删掉不该看的"：结果被悄悄挖空，
    而且相关性最高的那几条永远被别人的文档占着。这里直接盯住传给检索层
    的参数 —— 行为断言在单库单测里做不出来（MemoryIndex 与 PgVectorIndex
    的候选池不一样），所以钉参数。
    """
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}))
    seen: dict = {}
    original = app_state.search_index.search

    async def spy(session, **kwargs):
        seen.update(kwargs)
        return await original(session, **kwargs)

    monkeypatch.setattr(app_state.search_index, "search", spy)
    client.headers.update(bob_headers())
    await _search(client, PUBLIC_TEXT)
    assert seen["authorized_parse_job_ids"] == [corpus["public_job"].id]
    assert "authorized_document_ids" in seen, "授权没有下推给检索层"
    assert seen["authorized_document_ids"] == [corpus["public"].id], \
        "下推的应当正好是这个人看得见的那些文档"


async def test_search_without_identity_is_rejected(client, corpus):
    """没有 actor 上下文头 = 没有身份，401，而不是"按公共语料处理"。"""
    response = await _search(client, SECRET)
    assert response.status_code == 401
    assert SECRET not in response.text


async def test_empty_query_is_a_named_degradation(actor_client, corpus):
    body = (await _search(actor_client, "   ")).json()
    assert body == {"results": [], "degraded": "empty_query", "scope": {"authorized_parse_revisions": 0}}


# ------------------------------------------------------------------ ask

@respx.mock
async def test_ask_answers_only_from_evidence_the_actor_may_read(actor_client, client, corpus):
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}))
    chat = respx.post(CHAT).mock(return_value=Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "共四千二百万元。[1]"}}]}))

    mine = (await actor_client.post("/internal/mcp/ask", json={"question": SECRET})).json()
    assert mine["assertions"][0]["evidence_ids"] == [corpus["private_evidence"].id]
    assert SECRET in chat.calls[0].request.content.decode(), "有权的人这条路要真的跑通"

    sent = len(chat.calls)
    client.headers.update(bob_headers())
    theirs = await client.post("/internal/mcp/ask", json={"question": SECRET})
    body = theirs.json()
    assert body["assertions"][0]["unsupported"] is True
    assert body["degraded"] == "no_hits"
    assert SECRET not in theirs.text
    # **私有原文不能被送进模型**：只看返回值的话，"先把别人的文档塞进 prompt、
    # 再把答案吐回来"照样能过 —— 那仍然是一次泄漏
    assert len(chat.calls) == sent, "无权的问题不该产生任何上游调用"


@respx.mock
async def test_ask_reports_answer_unavailable_instead_of_empty(actor_client, corpus):
    """上游生成挂了要说出来，不能渲染成"没有答案"（不变式 2）。"""
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}))
    respx.post(CHAT).mock(return_value=Response(503, json={"error": "down"}))
    body = (await actor_client.post("/internal/mcp/ask", json={"question": SECRET})).json()
    assert body == {"assertions": [], "degraded": "answer_unavailable", "scope": {"authorized_parse_revisions": 2}}


# ------------------------------------------------------------------ get_evidence

async def test_get_evidence_requires_authorization(actor_client, client, corpus, app_state):
    await app_state.storage.put(corpus["private_evidence"].crop_key, b"PNG-bytes", "image/png")

    mine = await actor_client.get(f"/internal/mcp/evidence/{corpus['private_evidence'].id}")
    assert mine.status_code == 200
    body = mine.json()
    assert body["evidence"]["content"] == SECRET
    assert body["evidence"]["crop_degraded"] is None
    assert body["crop"]["mime"] == "image/png"
    # 裁图 URL 用两个平面共用的那一份映射，且是**相对**路径（绝对化是 MCP 的事）
    assert body["evidence"]["crop_url"].startswith(
        f"/api/documents/{corpus['private'].id}/crops/")

    client.headers.update(bob_headers())
    theirs = await client.get(f"/internal/mcp/evidence/{corpus['private_evidence'].id}")
    assert theirs.status_code == 404, "无权必须与不存在同形，否则它是一个存在性探测器"
    assert SECRET not in theirs.text


async def test_get_evidence_without_identity_is_rejected(client, corpus):
    response = await client.get(f"/internal/mcp/evidence/{corpus['private_evidence'].id}")
    assert response.status_code == 401
    assert SECRET not in response.text


async def test_unreadable_crop_is_not_reported_as_no_crop(actor_client, corpus, app_state):
    """"取不到图"与"本来就没图"必须分开 —— 外部 agent 据此判断能不能复核。"""
    class Broken:
        async def get(self, key):
            raise RuntimeError("object store down")

    app_state.storage = Broken()
    body = (await actor_client.get(
        f"/internal/mcp/evidence/{corpus['private_evidence'].id}")).json()
    assert body["crop"] is None
    assert body["evidence"]["crop_degraded"] == "crop_read_failed"


# ------------------------------------------------------------------ wiki / graph

@pytest.fixture
async def knowledge(session, corpus):
    """一条 wiki 句子与一条图谱边，各引一条**私有**证据。"""
    provider = corpus["provider"]
    entity = KnowledgeEntity(id=new_id(), canonical_name="北极星", normalized_name="北极星",
                             entity_type="organization", aliases=[], provider=provider)
    other = KnowledgeEntity(id=new_id(), canonical_name="子公司", normalized_name="子公司",
                            entity_type="organization", aliases=[], provider=provider)
    session.add_all([entity, other])
    await session.flush()
    entry = WikiEntry(id=new_id(), entity_id=entity.id, title="北极星", outline=[],
                      provider=provider)
    session.add(entry)
    await session.flush()
    section = WikiSection(id=new_id(), entry_id=entry.id, heading="概况", position=0)
    session.add(section)
    await session.flush()
    sentence = WikiSentence(id=new_id(), section_id=section.id, position=0,
                            text="它的薪酬总额很高。", unsupported=False, provider=provider)
    edge = GraphEdge(id=new_id(), subject_id=entity.id, predicate="控股", object_id=other.id,
                     confidence=0.9, unsupported=False, provider=provider)
    session.add_all([sentence, edge])
    await session.flush()
    for kind, source_id in (("wiki_sentence", sentence.id), ("graph_edge", edge.id)):
        # content_digest 必须是**真**指纹：`load_citations` 靠它判断这条出处
        # 还接不接得回当前 chunk，随手填一个假值会让出处一律 resolved=false，
        # 于是"有权的人也看不到"，这组用例就变成假绿
        session.add(Citation(id=new_id(), evidence_id=corpus["private_evidence"].id,
                             source_kind=kind, source_id=source_id, role="primary",
                             snippet=SECRET, rank=0, content_digest=digest_of(SECRET)))
    await session.commit()
    return {"entry": entry, "entity": entity, "other": other, "sentence": sentence, "edge": edge}


async def test_wiki_is_hidden_when_it_depends_on_unreadable_documents(
        actor_client, client, corpus, knowledge):
    """委托给知识平面之后，条目的可见性判据就是它那一条（全有或全无）。

    这里钉的是 **MCP 这条路真的经过了那道门** —— 搬迁前它自己 SELECT
    `wiki_entries`，与知识平面的授权毫无关系。
    """
    mine = (await actor_client.get("/internal/mcp/wiki",
                                   params={"value": "北极星"})).json()
    cited = mine["sections"][0]["sentences"][0]
    assert cited["evidence_ids"] == [corpus["private_evidence"].id]
    assert cited["unsupported"] is False

    client.headers.update(bob_headers())
    theirs = await client.get("/internal/mcp/wiki", params={"value": "北极星"})
    assert theirs.status_code == 404
    assert SECRET not in theirs.text


async def test_graph_is_hidden_when_it_depends_on_unreadable_documents(
        actor_client, client, corpus, knowledge):
    mine = (await actor_client.get("/internal/mcp/graph/neighbors",
                                   params={"value": "北极星"})).json()
    assert mine["status"] == "ok" and mine["center_id"] == knowledge["entity"].id
    assert mine["edges"][0]["evidence_ids"] == [corpus["private_evidence"].id]

    client.headers.update(bob_headers())
    theirs = await client.get("/internal/mcp/graph/neighbors", params={"value": "北极星"})
    assert theirs.status_code == 404
    assert SECRET not in theirs.text


def test_reattach_drops_unreadable_citations_and_marks_unsupported():
    """出口上的第二道门（`_reattach`）。

    **没有端到端用例，是因为构造不出那个形态**：知识平面今天是全有或全无，
    "行可见但混着一条私有出处"到不了这里。所以直接调函数 —— 它守的是那条判据
    哪天放宽成"有一条可见出处就算可见"时，私有原文不会经 snippet 漏出去。
    """
    from ddp_corpus.routers.mcp_tools import _reattach

    row = {"id": "s1", "text": "混着两份文档的句子。", "unsupported": False,
           "evidence_ids": ["ev-pub", "ev-priv"],
           "citations": [{"evidence_id": "ev-pub", "document_id": "doc-pub",
                          "parse_job_id": "parse-pub", "resolved": True, "snippet": PUBLIC_TEXT},
                         {"evidence_id": "ev-priv", "document_id": "doc-priv",
                          "parse_job_id": "parse-priv", "resolved": True, "snippet": SECRET}]}

    kept = _reattach(row, {"parse-pub"})
    assert [c["document_id"] for c in kept["citations"]] == ["doc-pub"]
    assert kept["evidence_ids"] == ["ev-pub"]
    assert SECRET not in json.dumps(kept, ensure_ascii=False)
    assert kept["unsupported"] is False, "还剩一条可见出处，不该标成无支撑"

    # 一条都不剩时必须显式标无支撑：留一个"看起来有依据"的句子比说不出来更糟
    none_left = _reattach(row, set())
    assert none_left["citations"] == [] and none_left["evidence_ids"] == []
    assert none_left["unsupported"] is True


async def test_graph_depth_is_validated(actor_client, knowledge):
    response = await actor_client.get("/internal/mcp/graph/neighbors",
                                      params={"value": "北极星", "depth": 9})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_depth"


async def test_wiki_and_graph_without_identity_are_rejected(client, knowledge):
    for path, params in (("/internal/mcp/wiki", {"value": "北极星"}),
                         ("/internal/mcp/graph/neighbors", {"value": "北极星"})):
        assert (await client.get(path, params=params)).status_code == 401


async def test_unknown_entities_are_not_found_not_empty_shells(actor_client, knowledge):
    """查无此物要明说，不许返回一个空壳（契约「错误与降级」一节）。"""
    assert (await actor_client.get("/internal/mcp/wiki",
                                   params={"value": "不存在的条目"})).status_code == 404
    assert (await actor_client.get("/internal/mcp/graph/neighbors",
                                   params={"value": "不存在的实体"})).status_code == 404


async def test_service_credentials_are_required_even_with_identity_headers(client, corpus):
    """光有 X-DDP-* 头不够：没有服务凭据的话，那组头是谁写的都不知道。"""
    headers = actor_headers()
    headers.pop("Authorization")
    client.headers.update(headers)
    response = await _search(client, SECRET)
    assert response.status_code == 401
    assert json.loads(response.text)["error"]["code"] == "missing_token"


def test_all_five_mcp_routes_are_mounted_in_the_production_application():
    from ddp_corpus.main import app
    paths = app.openapi()["paths"]
    assert {"/internal/mcp/search", "/internal/mcp/ask", "/internal/mcp/evidence/{evidence_id}",
            "/internal/mcp/wiki", "/internal/mcp/graph/neighbors"} <= paths.keys()


@respx.mock
async def test_shared_bytes_do_not_grant_another_assets_parse_or_filename(client, corpus, session, app_state, monkeypatch):
    doc, secret_job = corpus["private"], corpus["private_job"]
    bob_job = ParseJob(document_id=doc.id, engine="borndigital", options_hash="bob-options",
        initiated_by=BOB, document_version=2, status="succeeded", index_status="ready")
    session.add(bob_job)
    await session.flush()
    resource, version = await _claim(session, doc, BOB, job=bob_job)
    resource.display_name = version.filename = "bob-private.pdf"
    own = await _evidence(session, doc, bob_job, seq=0, text="common Bob permitted annotation")
    await _evidence(session, doc, secret_job, seq=1, text=f"common common common {SECRET}")
    await session.commit()
    client.headers.update(bob_headers())
    respx.post(EMBEDDINGS).mock(return_value=Response(503))
    original_search = app_state.search_index.search
    scopes = []

    async def search_with_scope(session, **kwargs):
        scopes.append(set(kwargs["authorized_parse_job_ids"]))
        return await original_search(session, **kwargs)

    monkeypatch.setattr(app_state.search_index, "search", search_with_scope)
    searched = await _search(client, "common", limit=1)
    assert [row["evidence_id"] for row in searched.json()["results"]] == [own.id]
    assert scopes == [{bob_job.id, corpus["public_job"].id}]
    assert SECRET not in searched.text and "salary.pdf" not in searched.text
    item = searched.json()["results"][0]
    assert item["parse_revision"] == bob_job.id
    assert item["resource_id"] == resource.id and item["source_version_id"] == version.id
    assert item["filename"] == "bob-private.pdf"
    assert item["copies"] == [{"resource_id": resource.id, "source_version_id": version.id,
                               "filename": "bob-private.pdf"}]
    denied = await client.get(f"/internal/mcp/evidence/{corpus['private_evidence'].id}")
    unknown = await client.get("/internal/mcp/evidence/not-present")
    assert denied.status_code == unknown.status_code == 404 and denied.json() == unknown.json()
    permitted = await client.get(f"/internal/mcp/evidence/{own.id}")
    assert permitted.status_code == 200 and "salary.pdf" not in permitted.text

    def model(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        assert "Bob permitted annotation" in prompt and SECRET not in prompt
        return Response(200, json={"choices": [{"message": {"content": "Bob permitted annotation.[1]"}}]})

    chat = respx.post(CHAT).mock(side_effect=model)
    answer = await client.post("/internal/mcp/ask", json={"question": "common"})
    assert chat.call_count == 1 and SECRET not in answer.text
    assert answer.json()["assertions"][0]["evidence_ids"] == [own.id]


@respx.mock
async def test_revocation_during_generation_withholds_source_derived_answer(client, corpus, session):
    client.headers.update(bob_headers())
    respx.post(EMBEDDINGS).mock(return_value=Response(503))

    async def revoke(request):
        corpus["public_resource"].publication = "withdrawn"
        await session.commit()
        return Response(200, json={"choices": [{"message": {"content": PUBLIC_TEXT + "。[1]"}}]})

    chat = respx.post(CHAT).mock(side_effect=revoke)
    result = await client.post("/internal/mcp/ask", json={"question": PUBLIC_TEXT})
    assert chat.call_count == 1
    assert PUBLIC_TEXT not in result.text, "removing citations cannot authorize returning the revoked source's answer"
    assert corpus["public_evidence"].id not in result.text


async def test_crop_read_rechecks_revocation_before_returning_pixels(client, corpus, app_state, session):
    client.headers.update(bob_headers())
    evidence = corpus["public_evidence"]
    evidence.crop_key = "crops/public/0_digest.png"
    await session.commit()

    class RevokingStore:
        async def get(self, key):
            corpus["public_resource"].publication = "withdrawn"
            await session.commit()
            return b"secret-pixels"

    app_state.storage = RevokingStore()
    response = await client.get(f"/internal/mcp/evidence/{evidence.id}")
    assert response.status_code == 404
    assert "secret-pixels" not in response.text and PUBLIC_TEXT not in response.text


async def test_crop_read_refreshes_aliases_when_one_copy_is_revoked(client, corpus, session, app_state):
    resource, version = await _claim(session, corpus["public"], BOB, job=corpus["public_job"])
    version.filename = "bob-only-copy.pdf"
    evidence = corpus["public_evidence"]
    evidence.crop_key = "crops/shared/0_digest.png"
    await session.commit()
    client.headers.update(bob_headers())

    class RevokingStore:
        async def get(self, key):
            corpus["public_resource"].publication = "withdrawn"
            await session.commit()
            return b"readable-through-bob-copy"

    app_state.storage = RevokingStore()
    response = await client.get(f"/internal/mcp/evidence/{evidence.id}")
    assert response.status_code == 200
    payload = response.json()["evidence"]
    assert payload["resource_id"] == resource.id and payload["source_version_id"] == version.id
    assert payload["filename"] == "bob-only-copy.pdf"
    assert payload["copies"] == [{"resource_id": resource.id, "source_version_id": version.id,
                                   "filename": "bob-only-copy.pdf"}]
    assert "handbook.pdf" not in response.text


@pytest.mark.parametrize("tool", ["wiki", "graph"])
async def test_knowledge_revocation_after_delegate_withholds_the_generated_text(client, corpus, knowledge, session, monkeypatch, tool):
    from ddp_corpus.routers import knowledge as plane
    source = await session.get(Resource, corpus["provider"]["source_bindings"][0]["resource_id"])
    source.publication = "published"
    for row in (knowledge["entry"], knowledge["entity"], knowledge["other"], knowledge["sentence"], knowledge["edge"]):
        row.provider = {**row.provider, "generated_by": BOB}
    await session.commit()
    client.headers.update(bob_headers())
    function = "read_wiki" if tool == "wiki" else "graph"
    original = getattr(plane, function)
    reached = []

    async def revoke_after_read(*args, **kwargs):
        data = await original(*args, **kwargs)
        reached.append(True)
        source.publication = "withdrawn"
        await session.commit()
        return data

    monkeypatch.setattr(plane, function, revoke_after_read)
    path = "/internal/mcp/wiki" if tool == "wiki" else "/internal/mcp/graph/neighbors"
    response = await client.get(path, params={"value": "北极星"})
    assert reached, "the positive read must succeed before revocation is tested"
    assert response.status_code == 404
    assert "它的薪酬总额很高" not in response.text and SECRET not in response.text


@respx.mock
async def test_partial_prompt_revocation_does_not_renumber_or_release_answer(client, corpus, session):
    await _evidence(session, corpus["public"], corpus["public_job"], seq=1,
                    text="racequery revocable confidential figure")
    other_doc, other_job = await _document(session, uploader=ALICE, doc_id="c"*64, filename="retained.pdf")
    await _claim(session, other_doc, ALICE, job=other_job, publication="published")
    retained = await _evidence(session, other_doc, other_job, seq=0, text="racequery retained public fact")
    await session.commit()
    client.headers.update(bob_headers())
    respx.post(EMBEDDINGS).mock(return_value=Response(503))

    async def revoke_one(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        assert "revocable confidential figure" in prompt and "retained public fact" in prompt
        corpus["public_resource"].publication = "withdrawn"
        await session.commit()
        return Response(200, json={"choices": [{"message": {
            "content": "confidential generated answer.[1] retained answer.[2]"}}]})

    chat = respx.post(CHAT).mock(side_effect=revoke_one)
    response = await client.post("/internal/mcp/ask", json={"question": "racequery"})
    assert chat.call_count == 1 and response.status_code == 404
    assert "confidential generated answer" not in response.text and retained.id not in response.text
    assert (await client.get(f"/internal/mcp/evidence/{retained.id}")).status_code == 200
