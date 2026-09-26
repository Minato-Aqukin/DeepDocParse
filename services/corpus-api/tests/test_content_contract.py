"""中心内容契约测试：content-v1.yaml ←→ corpus-api 真实响应的形状对拍。

只覆盖 content-v1 声明的**读操作**（写操作在本文件里只做最小的建数手段，
不断言其形状 —— 写契约归 wiki-v1.yaml 等已有文件管）：

- resources：list（mine / site_public 空目录）、detail、delete
- documents：list、stats、detail（含 resource_id/source_version_id）、
  pages、layout、result、jobs、delete
- download-url：中心语料侧没有这条（control-api 自有），这里只钉住
  corpus `/source-url` 的稳定 URL 形状，保证"稳定 vs 短期"两条路不混
- search：q / doc 限定 / 空查询
- conversations：create、list、messages、ask SSE 全事件（meta/delta/
  citations/assertions/done）、delete
- evidence：detail（含 crop_url）、backlinks
- crops：crop_url 可取回 PNG（经过问答真实产出，不手造 key）
- wikis：create → read → read_revision → edit → rebuild → list，
  每一步都对 content-v1 的 WikiRevisionDocument 形状

校验方式：用 OpenAPI 里 `required` + 关键字段类型做结构断言（不用
jsonschema 全量校验 —— 契约里大量字段是宽松的 `type: object`，全量校验
的边际收益低，而 required 漂了是调用方立刻 500 的那种错）。
"""
import hashlib
import io
import json

import httpx
import pytest
import respx
from sqlalchemy import select

from ddp_corpus.models import Chunk, Document, Evidence, ParseJob, Resource, ResourceVersion
from tests.conftest import ACTOR, CHAT, ORG
from tests.test_documents import _callback, _embed_response, _mock_service
from tests.test_qa import (
    _ask,
    _conversation,
    _grounded_doc,
    _grounded_side_effect,
    _ready_document,
)
from tests.test_wiki_revisions import body as wiki_body
from tests.test_wiki_revisions import index_chunk, model as wiki_model, source as wiki_source


def _real_pdf() -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(2):
        doc.new_page(612, 792)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


PDF = _real_pdf()


def _require(body, *keys):
    missing = [k for k in keys if k not in body]
    assert not missing, f"content-v1 required 缺失 {missing}：{str(body)[:400]}"


@respx.mock
async def test_content_resources_list_detail_delete(actor_client, session):
    """GET /api/resources（mine + site_public）、GET 明细、DELETE。"""
    _mock_service()
    from tests.test_documents import _upload

    uploaded = await _upload(actor_client, PDF)
    listed = await actor_client.get("/api/resources", params={"scope": "mine"})
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, "刚传了一份文档，mine 列表不应为空"
    row = items[0]
    _require(row, "id", "versions")
    version = row["versions"][0]
    _require(version, "id", "resource_id", "version_no", "document_id",
             "source_digest", "filename")
    assert version["source_digest"] == uploaded["doc_id"], \
        "版本的 source_digest 必须等于上传字节的内容摘要（去重与版本追踪的命门）"
    assert version["document_id"] == uploaded["id"], "版本必须指回上传的那份文档"

    public = await actor_client.get("/api/resources", params={"scope": "site_public"})
    assert public.status_code == 200, public.text
    assert public.json()["items"] == [], "私有资源不应出现在 site_public 目录"

    detail = await actor_client.get(f"/api/resources/{row['id']}")
    assert detail.status_code == 200, detail.text
    _require(detail.json(), "id", "versions")

    removed = await actor_client.delete(f"/api/resources/{row['id']}")
    assert removed.status_code == 204, removed.text
    gone = await actor_client.get(f"/api/resources/{row['id']}")
    assert gone.status_code == 404, "删除后明细应 404"


@respx.mock
async def test_content_documents_list_stats_detail_pages_layout_result_jobs_delete(
    actor_client,
):
    """documents 全套读 + jobs + delete：每个响应都对 required。"""
    document = await _ready_document(actor_client)

    listed = await actor_client.get("/api/documents")
    assert listed.status_code == 200, listed.text
    assert listed.json(), "刚解析完一份文档，列表不应为空"
    _require(listed.json()[0], "id", "resource_id", "filename",
             "status", "compile_fingerprint")
    assert listed.json()[0]["resource_id"], "资源化上传的文档必须带 resource_id"

    stats = await actor_client.get("/api/documents/stats/summary")
    assert stats.status_code == 200, stats.text
    _require(stats.json(), "documents", "pages", "askable")
    assert stats.json()["documents"] >= 1 and stats.json()["askable"] >= 1

    detail = await actor_client.get(f"/api/documents/{document['id']}")
    assert detail.status_code == 200, detail.text
    info = detail.json()
    _require(info, "id", "resource_id", "source_version_id", "filename",
             "status", "compile_fingerprint")
    assert info["resource_id"] and info["source_version_id"], \
        "文档详情必须带 resource_id/source_version_id（本地 version≈document 映射靠它）"

    pages = await actor_client.get(f"/api/documents/{document['id']}/pages")
    assert pages.status_code == 200, pages.text
    _require(pages.json(), "document_id", "job_id", "page_count", "pages")
    first_page = pages.json()["pages"][0]
    _require(first_page, "page_idx", "blocks")
    block = first_page["blocks"][0]
    _require(block, "seq", "page_idx", "text")
    assert block["page_idx"] == first_page["page_idx"]

    layout = await actor_client.get(f"/api/documents/{document['id']}/layout")
    assert layout.status_code == 200, layout.text
    assert isinstance(layout.json(), dict), "layout 原样返回 JSON 对象"

    result = await actor_client.get(f"/api/documents/{document['id']}/result")
    assert result.status_code == 200, result.text
    _require(result.json(), "document_id", "job_id", "filename",
             "page_count", "markdown", "images")

    jobs = await actor_client.get(f"/api/documents/{document['id']}/jobs")
    assert jobs.status_code == 200, jobs.text
    assert jobs.json(), "解析过的文档至少有一条 job"
    _require(jobs.json()[0], "id", "status")

    stable = await actor_client.get(f"/api/documents/{document['id']}/source-url")
    assert stable.status_code == 200, stable.text
    _require(stable.json(), "url", "path", "mime")

    removed = await actor_client.delete(f"/api/documents/{document['id']}")
    assert removed.status_code == 204, removed.text
    gone = await actor_client.get(f"/api/documents/{document['id']}")
    assert gone.status_code == 404, gone.text


@respx.mock
async def test_content_search(actor_client):
    """/api/search：命中形状、doc 限定、空查询。"""
    document = await _ready_document(actor_client)

    hit = await actor_client.get("/api/search", params={"q": "第二页的表格"})
    assert hit.status_code == 200, hit.text
    body = hit.json()
    _require(body, "query", "groups")
    assert body["groups"], "应命中刚解析的文档"
    group = body["groups"][0]
    _require(group, "document_id", "filename", "hits")
    _require(group["hits"][0], "chunk_id", "page_idx", "score", "snippet")

    scoped = await actor_client.get(
        "/api/search", params={"q": "第二页的表格", "doc": document["id"]})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["groups"], "doc 限定后仍应命中"
    assert {g["document_id"] for g in scoped.json()["groups"]} == {document["id"]}

    empty = await actor_client.get("/api/search", params={"q": ""})
    assert empty.status_code == 200, empty.text
    assert empty.json()["groups"] == [], "空查询返回空组"


@respx.mock
async def test_content_conversations_and_ask_sse(actor_client, session):
    """会话 CRUD + ask SSE 全事件：meta/delta/citations/assertions/done。"""
    document = await _ready_document(actor_client)
    resp = await actor_client.post(f"/api/documents/{document['id']}/conversations")
    assert resp.status_code == 201, resp.text
    _require(resp.json(), "id", "document_id")
    cid = resp.json()["id"]

    listed = await actor_client.get("/api/conversations",
                                    params={"document": document["id"]})
    assert listed.status_code == 200, listed.text
    assert any(c["id"] == cid for c in listed.json()), "新建的会话应在列表里"
    _require(listed.json()[0], "id", "document_id")

    respx.post(CHAT).mock(side_effect=_grounded_side_effect(
        _grounded_doc(("第二页讲的是表格数据。", [1]))))
    events = await _ask(actor_client, cid)
    names = [name for name, _ in events]
    assert names[0] == "meta" and names[-1] == "done", names
    by_name = dict(events)
    _require(by_name["meta"], "query_decision", "retrieval")
    assert any(n == "delta" for n, _ in events), "答案文本必须走 delta 帧"
    _require({"citations": by_name["citations"]["citations"]}, "citations")
    assert by_name["citations"]["citations"], "带引用的回答必须有 citations 帧"
    _require({"assertions": by_name["assertions"]["assertions"]}, "assertions")
    assertion = by_name["assertions"]["assertions"][0]
    _require(assertion, "text", "evidence_ids", "unsupported")
    assert by_name["done"]["message_id"], "done 帧必须带 message_id"
    _require(by_name["done"], "message_id", "verified", "confidence")

    messages = await actor_client.get(f"/api/conversations/{cid}/messages")
    assert messages.status_code == 200, messages.text
    assert messages.json(), "问答后应有落库消息"
    stored = messages.json()[-1]
    _require(stored, "id", "role", "content", "verified", "created_at")
    assert stored["assertions"], "落库消息必须带断言"

    removed = await actor_client.delete(f"/api/conversations/{cid}")
    assert removed.status_code == 204, removed.text
    gone = await actor_client.get(f"/api/conversations/{cid}/messages")
    assert gone.status_code == 404, gone.text


@respx.mock
async def test_content_evidence_detail_backlinks_and_crop(actor_client, session):
    """证据详情（crop_url 可取回 PNG）+ backlinks（含 wiki_claim 扩展位）。"""
    document = await _ready_document(actor_client)
    cid = await _conversation(actor_client, document["id"])
    respx.post(CHAT).mock(side_effect=_grounded_side_effect(
        _grounded_doc(("第二页讲的是表格数据。", [1]))))
    events = await _ask(actor_client, cid)
    citations = dict(events)["citations"]["citations"]
    assert citations, "前提不成立：这一轮没有产出引用"
    evidence_id = citations[0]["evidence_id"]
    assert evidence_id, "引用必须落到真实 evidence 行"

    detail = await actor_client.get(f"/api/evidence/{evidence_id}")
    assert detail.status_code == 200, detail.text
    info = detail.json()
    _require(info, "id", "document", "content")
    _require(info["document"], "id", "filename")

    crop_url = next((c.get("crop_url") for c in citations if c.get("crop_url")), None)
    assert crop_url, "真 PDF 问答应产出裁图"
    crop = await actor_client.get(crop_url)
    assert crop.status_code == 200, crop.text[:200] if hasattr(crop, "text") else crop
    assert crop.headers["content-type"].startswith("image/png"), crop.headers

    links = await actor_client.get(f"/api/evidence/{evidence_id}/backlinks")
    assert links.status_code == 200, links.text
    _require(links.json(), "evidence_id", "backlinks")
    assert links.json()["evidence_id"] == evidence_id
    kinds = {b["source_kind"] for b in links.json()["backlinks"]}
    assert "assertion" in kinds, f"问答引用应出现在 backlinks 里：{kinds}"
    for backlink in links.json()["backlinks"]:
        _require(backlink, "source_kind", "source_id", "role", "label")


@respx.mock
async def test_content_wikis_crud_shapes(actor_client, session):
    """版本化 Wiki：create → read → read_revision → edit → rebuild → list。"""
    resource, version, evidence, _ = await wiki_source(session)
    calls = wiki_model(evidence)
    created = await actor_client.post("/api/wikis", json=wiki_body(resource, version),
                                      headers={"Idempotency-Key": "content-create"})
    assert created.status_code == 201, created.text
    first = created.json()
    _require(first, "wiki", "revision")
    _require(first["wiki"], "id", "title", "current_revision_id")
    _require(first["revision"], "id", "wiki_id")
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]

    read = await actor_client.get(f"/api/wikis/{wiki_id}")
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["id"] == revision_id

    historical = await actor_client.get(f"/api/wikis/{wiki_id}/revisions/{revision_id}")
    assert historical.status_code == 200, historical.text
    _require(historical.json(), "wiki", "revision")

    page_key = first["revision"]["pages"][0]["page_key"]
    edited = await actor_client.patch(
        f"/api/wikis/{wiki_id}/pages/{page_key}",
        json={"base_revision_id": revision_id,
              "paragraphs": [{"id": "note", "text": "人工注记。"}]},
        headers={"Idempotency-Key": "content-edit"})
    assert edited.status_code == 201, edited.text
    second_id = edited.json()["revision"]["id"]
    assert second_id != revision_id, "人工编辑应产生新修订"

    rebuilt = await actor_client.post(
        f"/api/wikis/{wiki_id}/revisions",
        json={**wiki_body(resource, version), "base_revision_id": second_id},
        headers={"Idempotency-Key": "content-rebuild"})
    assert rebuilt.status_code == 201, rebuilt.text
    assert rebuilt.json()["revision"]["pages"][0]["human_paragraphs"][0]["text"] == \
        "人工注记。", "重建必须保留人工段落"

    listed = await actor_client.get("/api/wikis")
    assert listed.status_code == 200, listed.text
    assert any(w["wiki"]["id"] == wiki_id for w in listed.json()), "新建 Wiki 应在列表里"
    assert calls.call_count >= 1
