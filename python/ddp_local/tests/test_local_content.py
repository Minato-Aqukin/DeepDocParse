"""Local content-subset contract test: content-v1 shapes over ddp_local HTTP.

Mirrors services/corpus-api/tests/test_content_contract.py (required-keys
style): every implemented operation is driven through HTTP and asserted
against the contract's required keys. Upload sessions use center UploadSession
shapes where they exist (id/status/object_key/filename/mime/declared_size/
part_size/parts/completed_parts/expires_at/target_resource_id/ingest_status/
ingest_error); part URLs are same-origin relative paths. Ask SSE emits the
center event sequence meta → delta → citations → assertions → done with one
documented delta. Wiki revisions come back in wiki-v1 shape.
"""

import httpx
import pytest

from ddp_local.http import create_app
from ddp_local.providers import ModelSelection
from ddp_local.runtime import LocalRuntime

from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
TOKEN = "c" * 48


def _require(body, *keys):
    missing = [k for k in keys if k not in body]
    assert not missing, f"content-v1 required 缺失 {missing}：{str(body)[:400]}"


@pytest.fixture
async def client(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    app = create_app(runtime, session_token=TOKEN,
                     allowed_hosts={"127.0.0.1:18763"}, start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18763",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as handle:
        yield handle, runtime
    runtime.close()


def _grounded_model(runtime):
    runtime.provider.model = ModelSelection("http://127.0.0.1:18761/v1", "fixture")
    calls = []

    async def generate(messages, **kwargs):
        calls.append((messages, kwargs))
        return "The answer is 42 [1]", {"name": "fixture", "location": "local"}

    runtime.provider.generate = generate
    return calls


async def _ready_version(client, runtime, filename="sample.pdf", key="chain",
                         target=None):
    data = (FIXTURES / filename).read_bytes()
    body = {"filename": filename, "size": len(data), "mime": "application/pdf"}
    if target:
        body["target_resource_id"] = target
    created = await client.post("/api/uploads", json=body,
                                headers={"Idempotency-Key": key})
    assert created.status_code == 201, created.text
    session = created.json()
    _require(session, "id", "status", "object_key", "filename", "mime",
             "declared_size", "part_size", "parts", "completed_parts", "expires_at",
             "target_resource_id", "ingest_status")
    assert session["ingest_status"] is None or session["ingest_status"] == "pending"
    part = session["parts"][0]
    assert part["url"].startswith("/api/uploads/") and "://" not in part["url"], \
        "part URLs must be same-origin relative paths"
    assert part["url"] == f"/api/uploads/{session['id']}/parts/1"
    put = await client.put(part["url"], content=data)
    assert put.status_code == 200, put.text
    _require(put.json(), "part_number", "etag", "size")
    finalized = await client.post(f"/api/uploads/{session['id']}/finalize")
    assert finalized.status_code == 200, finalized.text
    done = finalized.json()
    assert done["ingest_status"] == "ready", done
    assert done["ingest_error"] is None
    assert (await client.get(f"/api/uploads/{session['id']}")).json()["ingest_status"] == "ready"
    await runtime.work_once()
    return done["version_id"], done["resource_id"]


def _ask_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        event = next((line[7:].strip() for line in lines if line.startswith("event: ")), None)
        raw = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and raw is not None:
            import json as _json

            events.append((event, _json.loads(raw)))
    return events


async def test_local_content_auth_me(client):
    handle, _ = client
    me = await handle.get("/api/auth/me")
    assert me.status_code == 200, me.text
    _require(me.json(), "id", "username", "role")


async def test_local_content_resources_list_detail_delete(client):
    handle, runtime = client
    version_id, resource_id = await _ready_version(handle, runtime)
    listed = await handle.get("/api/resources", params={"scope": "mine"})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    _require(body, "items", "has_more")
    assert body["items"], "刚传了一份文档，mine 列表不应为空"
    row = next(item for item in body["items"] if item["id"] == resource_id)
    _require(row, "id", "versions")
    version = next(item for item in row["versions"] if item["id"] == version_id)
    _require(version, "id", "resource_id", "version_no", "document_id",
             "source_digest", "filename")

    public = await handle.get("/api/resources", params={"scope": "site_public"})
    assert public.status_code == 200, public.text
    assert public.json()["items"] == [], "本机无公开目录"

    detail = await handle.get(f"/api/resources/{resource_id}")
    assert detail.status_code == 200, detail.text
    _require(detail.json(), "id", "versions")

    patch = await handle.patch(f"/api/resources/{resource_id}", json={"publication": "published"})
    assert patch.status_code == 404, patch.text
    assert patch.json()["error"]["code"] == "not_supported_locally"

    removed = await handle.delete(
        f"/api/resources/{resource_id}", headers={"Idempotency-Key": "chain-delete"})
    assert removed.status_code == 204, removed.text
    gone = await handle.get(f"/api/resources/{resource_id}")
    assert gone.status_code == 404, "删除后明细应 404"


async def test_resource_import_lineage_and_egress_permission_survive_listing(client, tmp_path):
    import io

    handle, runtime = client
    local_version, local_resource = await _ready_version(handle, runtime)
    remote = LocalRuntime(tmp_path / "remote-workspace")
    try:
        original = remote.upload_stream(io.BytesIO((FIXTURES / "sample.pdf").read_bytes()),
                                        filename="sample.pdf", operation_key="remote-file")
        await remote.work_once()
        imported = runtime.import_bundle(io.BytesIO(remote.export_bundle(original["version_id"])),
                                         operation_key="remote-delivery")
        rows = (await handle.get("/api/resources", params={"scope": "mine"})).json()["items"]
        local = next(row for row in rows if row["id"] == local_resource)
        copy_resource = runtime.store.version(imported["version_id"])["resource_id"]
        original_resource = remote.store.version(original["version_id"])["resource_id"]
        copy = next(row for row in rows if row["id"] == copy_resource)
        assert local["copied_from"] is None
        assert copy["copied_from"] == f"remote:{remote.store.environment_id}:{original_resource}"
        assert local["versions"][0]["federation_input_allowed"] is True
        assert copy["versions"][0]["federation_input_allowed"] is False
        assert local["versions"][0]["filename"] == copy["versions"][0]["filename"]
        assert local_version != imported["version_id"]
        version = copy["versions"][0]
        body = {"center": {
            "recipient_node_id": "node-center", "environment_id": "node-center", "workspace_id": "org",
            "profile_id": "profile-owner", "issuer": "node-center", "subject": "owner",
            "endpoint": "https://center.example",
        }, "inputs": [{"ref": version["id"], "digest": "sha256:" + version["source_digest"],
                      "size_bytes": version["size_bytes"]}],
            "retention": "temporary", "valid_seconds": 3600}
        query = await handle.post("/api/v1/plans/propose", json={**body, "query": "资料可用吗？"},
                                  headers={"Idempotency-Key": "lock-imported-query"})
        assert query.status_code == 400
        assert query.json()["error"]["code"] == "policy_denied"
        file = await handle.post("/api/v1/plans/propose-file", json={**body, "filename": version["filename"]},
                                 headers={"Idempotency-Key": "send-imported-file"})
        assert file.status_code == 400
        assert file.json()["error"]["code"] == "policy_denied"
    finally:
        remote.close()


async def test_local_content_documents_full_chain(client):
    handle, runtime = client
    version_id, resource_id = await _ready_version(handle, runtime)

    listed = await handle.get("/api/documents")
    assert listed.status_code == 200, listed.text
    assert listed.json(), "刚解析完一份文档，列表不应为空"
    _require(listed.json()[0], "id", "resource_id", "filename",
             "status", "compile_fingerprint")
    assert listed.json()[0]["resource_id"], "本地 version≈document 映射必须带 resource_id"

    filtered = await handle.get("/api/documents", params={"q": "sample"})
    assert filtered.status_code == 200 and filtered.json(), "文件名过滤应命中"

    stats = await handle.get("/api/documents/stats/summary")
    assert stats.status_code == 200, stats.text
    _require(stats.json(), "documents", "pages", "askable")
    assert stats.json()["documents"] >= 1 and stats.json()["askable"] >= 1

    detail = await handle.get(f"/api/documents/{version_id}")
    assert detail.status_code == 200, detail.text
    info = detail.json()
    _require(info, "id", "resource_id", "source_version_id", "filename",
             "status", "compile_fingerprint")
    assert info["resource_id"] == resource_id and info["source_version_id"] == version_id
    # Enum-typed fields must carry contract values, or the page shows raw codes
    # (2026-09-26 desktop walkthrough: "embedding_unavailable" leaked as a compile degradation).
    from ddp_contracts.enums import (
        CODE_DETECTION_VALUES, COMPILE_DEGRADED_VALUES, COMPILE_STATUS_VALUES, INDEX_STATUS_VALUES,
        PARSE_STATUS_VALUES)
    assert info["status"] in PARSE_STATUS_VALUES and info["index_status"] in INDEX_STATUS_VALUES
    assert info["compile_status"] in COMPILE_STATUS_VALUES
    assert info["code_detection"] in CODE_DETECTION_VALUES
    assert set(info["compile_degraded"]) <= set(COMPILE_DEGRADED_VALUES), info["compile_degraded"]

    pages = await handle.get(f"/api/documents/{version_id}/pages")
    assert pages.status_code == 200, pages.text
    _require(pages.json(), "document_id", "job_id", "page_count", "pages")
    first_page = pages.json()["pages"][0]
    _require(first_page, "page_idx", "blocks")
    block = first_page["blocks"][0]
    _require(block, "seq", "page_idx", "text")
    assert block["page_idx"] == first_page["page_idx"]

    layout = await handle.get(f"/api/documents/{version_id}/layout")
    assert layout.status_code == 200, layout.text
    assert isinstance(layout.json(), dict), "layout 原样返回 JSON 对象"

    result = await handle.get(f"/api/documents/{version_id}/result")
    assert result.status_code == 200, result.text
    _require(result.json(), "document_id", "job_id", "filename",
             "page_count", "markdown", "images")

    jobs = await handle.get(f"/api/documents/{version_id}/jobs")
    assert jobs.status_code == 200, jobs.text
    assert jobs.json(), "解析过的文档至少有一条 job"
    _require(jobs.json()[0], "id", "status")

    download = await handle.get(f"/api/documents/{version_id}/download-url")
    assert download.status_code == 200, download.text
    _require(download.json(), "url", "expires_at", "supports_range")
    assert download.json()["url"] == f"/api/documents/{version_id}/source", \
        "本机 download-url 必须是同源相对地址"

    source = await handle.get(f"/api/documents/{version_id}/source")
    assert source.status_code == 200, source.text[:100] if hasattr(source, "text") else source
    assert source.headers["content-type"] == "application/pdf"
    assert source.content == (FIXTURES / "sample.pdf").read_bytes()

    for path, method in [
        (f"/api/documents/{version_id}/reparse", "post"),
        (f"/api/documents/{version_id}/reindex", "post"),
    ]:
        denied = await getattr(handle, method)(path)
        assert denied.status_code == 404, denied.text
        assert denied.json()["error"]["code"] == "not_supported_locally"

    scoped = await handle.get("/api/search", params={"q": "contract", "doc": version_id})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["groups"], "doc 限定后仍应命中"

    removed = await handle.delete(
        f"/api/documents/{version_id}", headers={"Idempotency-Key": "doc-delete"})
    assert removed.status_code == 204, removed.text
    assert (await handle.get(f"/api/documents/{version_id}")).status_code == 404


async def test_local_content_conversations_and_ask_sse(client):
    handle, runtime = client
    version_id, _ = await _ready_version(handle, runtime)
    _grounded_model(runtime)

    created = await handle.post(f"/api/documents/{version_id}/conversations")
    assert created.status_code == 201, created.text
    _require(created.json(), "id", "document_id")
    cid = created.json()["id"]

    listed = await handle.get("/api/conversations", params={"document": version_id})
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == cid for item in listed.json()), "新建的会话应在列表里"
    _require(listed.json()[0], "id", "document_id")

    asked = await handle.post(f"/api/conversations/{cid}/ask", json={"question": "contract"})
    assert asked.status_code == 200, asked.text
    events = _ask_events(asked.text)
    names = [name for name, _ in events]
    assert names[0] == "meta" and names[-1] == "done", names
    by_name = dict(events)
    _require(by_name["meta"], "query_decision", "retrieval")
    deltas = [payload for name, payload in events if name == "delta"]
    assert len(deltas) == 1, f"本机一次产出答案，必须恰好一个 delta：{names}"
    _require({"citations": by_name["citations"]["citations"]}, "citations")
    assert by_name["citations"]["citations"], "带引用的回答必须有 citations 帧"
    assertion = by_name["assertions"]["assertions"][0]
    _require(assertion, "text", "evidence_ids", "unsupported")
    assert assertion["evidence_ids"], "断言必须绑定本轮供给的证据"
    assert by_name["done"]["message_id"], "done 帧必须带 message_id"
    _require(by_name["done"], "message_id", "verified", "confidence")

    messages = await handle.get(f"/api/conversations/{cid}/messages")
    assert messages.status_code == 200, messages.text
    assert messages.json(), "问答后应有落库消息"
    stored = messages.json()[-1]
    _require(stored, "id", "role", "content", "verified", "created_at")
    assert stored["assertions"], "落库消息必须带断言"
    assert stored["assertions"][0]["evidence_ids"], "落库断言必须保留证据绑定"

    removed = await handle.delete(f"/api/conversations/{cid}")
    assert removed.status_code == 204, removed.text
    assert (await handle.get(f"/api/conversations/{cid}/messages")).status_code == 404


async def test_local_content_evidence_detail_backlinks_no_crop(client):
    handle, runtime = client
    version_id, _ = await _ready_version(handle, runtime)
    _grounded_model(runtime)
    cid = (await handle.post(f"/api/documents/{version_id}/conversations")).json()["id"]
    events = _ask_events(
        (await handle.post(f"/api/conversations/{cid}/ask", json={"question": "contract"})).text)
    citations = dict(events)["citations"]["citations"]
    assert citations, "前提不成立：这一轮没有产出引用"
    evidence_id = citations[0]["evidence_id"]
    assert evidence_id, "引用必须落到真实 evidence 行"

    detail = await handle.get(f"/api/evidence/{evidence_id}")
    assert detail.status_code == 200, detail.text
    info = detail.json()
    _require(info, "id", "document", "content")
    _require(info["document"], "id", "filename")
    assert info["crop_url"] is None, "本机无裁图：crop_url 必须为 null（降级展示原文与定位）"
    crop = await handle.get(f"/api/documents/{version_id}/crops/job/crop.png")
    assert crop.status_code == 404 and crop.json()["error"]["code"] == "not_supported_locally"

    links = await handle.get(f"/api/evidence/{evidence_id}/backlinks")
    assert links.status_code == 200, links.text
    _require(links.json(), "evidence_id", "backlinks")
    assert links.json()["evidence_id"] == evidence_id
    kinds = {item["source_kind"] for item in links.json()["backlinks"]}
    assert "assertion" in kinds, f"问答引用应出现在 backlinks 里：{kinds}"
    for backlink in links.json()["backlinks"]:
        _require(backlink, "source_kind", "source_id", "role", "label")


async def test_local_content_wiki_build_and_append_version(client):
    handle, runtime = client
    version_id, resource_id = await _ready_version(handle, runtime)
    _grounded_model(runtime)
    runtime.provider.generate = _wiki_protocol(runtime)

    created = await handle.post(
        "/api/wikis",
        json={"title": "合同", "sources": [
            {"resource_id": resource_id, "source_version_id": version_id}]},
        headers={"Idempotency-Key": "wiki-create"})
    assert created.status_code == 201, created.text
    first = created.json()
    _require(first, "wiki", "revision")
    _require(first["wiki"], "id", "title", "current_revision_id")
    _require(first["revision"], "id", "wiki_id")
    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    assert first["revision"]["pages"], "Wiki 修订必须有页面"

    read = await handle.get(f"/api/wikis/{wiki_id}")
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["id"] == revision_id

    historical = await handle.get(f"/api/wikis/{wiki_id}/revisions/{revision_id}")
    assert historical.status_code == 200, historical.text
    _require(historical.json(), "wiki", "revision")

    listed = await handle.get("/api/wikis")
    assert listed.status_code == 200, listed.text
    assert any(item["wiki"]["id"] == wiki_id for item in listed.json()), "新建 Wiki 应在列表里"

    appended_id, _ = await _ready_version(
        handle, runtime, key="append", target=resource_id)
    assert appended_id != version_id, "追加版本必须产生新的 version id"
    await runtime.work_once()
    versions = (await handle.get(f"/api/resources/{resource_id}")).json()["versions"]
    assert {item["id"] for item in versions} == {version_id, appended_id}
    # The Wiki pinned v1 now has a newer ready version on the same resource: it must read
    # as stale with the contract reason (desktop walkthrough 2026-09-26 showed no stale state).
    from ddp_contracts.enums import WIKI_STALE_REASON_VALUES
    revision = (await handle.get(f"/api/wikis/{wiki_id}")).json()["revision"]
    assert revision["stale"] is True
    reasons = {reason for page in revision["stale_reasons"].values() for reason in page}
    assert reasons == {"source_version_changed"}
    assert reasons <= set(WIKI_STALE_REASON_VALUES)
    rebuilt = await handle.post(
        f"/api/wikis/{wiki_id}/revisions",
        json={"title": "合同", "base_revision_id": revision_id, "sources": [
            {"resource_id": resource_id, "source_version_id": appended_id}]},
        headers={"Idempotency-Key": "wiki-rebuild-latest"})
    assert rebuilt.status_code == 201, rebuilt.text
    current = (await handle.get(f"/api/wikis/{wiki_id}")).json()["revision"]
    assert current["id"] == rebuilt.json()["revision"]["id"]
    assert current["stale"] is False and current["stale_reasons"] == {}

    publish = await handle.post(f"/api/wikis/{wiki_id}/publish",
                                json={"base_revision_id": revision_id})
    assert publish.status_code == 404
    assert publish.json()["error"]["code"] == "not_supported_locally"


async def test_append_source_marks_edited_wiki_stale_and_rebuild_preserves_human_paragraph(client):
    handle, runtime = client
    version_id, resource_id = await _ready_version(handle, runtime)
    _grounded_model(runtime)
    runtime.provider.generate = _wiki_protocol(runtime)
    body = {"title": "合同", "sources": [
        {"resource_id": resource_id, "source_version_id": version_id}]}
    created = await handle.post("/api/wikis", json=body,
                                headers={"Idempotency-Key": "human-append-build"})
    assert created.status_code == 201, created.text
    wiki_id = created.json()["wiki"]["id"]
    original = created.json()["revision"]
    page_key = original["pages"][0]["page_key"]
    paragraph = {"id": "editor-note", "text": "人工说明：保留原文，不要改写。\n第二行含 café、42 和 [手工备注]。"}
    edited = await handle.patch(
        f"/api/wikis/{wiki_id}/pages/{page_key}",
        json={"base_revision_id": original["id"], "paragraphs": [paragraph]},
        headers={"Idempotency-Key": "human-append-edit"})
    assert edited.status_code == 201, edited.text
    edited_revision = edited.json()["revision"]
    human = edited_revision["pages"][0]["human_paragraphs"]
    assert [(item["id"], item["text"]) for item in human] == [
        (paragraph["id"], paragraph["text"])]

    appended_id, appended_resource = await _ready_version(
        handle, runtime, key="human-append-pdf", target=resource_id)
    assert appended_resource == resource_id and appended_id != version_id
    stale = (await handle.get(f"/api/wikis/{wiki_id}")).json()["revision"]
    assert stale["id"] == edited_revision["id"]
    assert stale["stale"] is True
    assert stale["stale_reasons"] == {page_key: ["source_version_changed"]}
    assert stale["pages"][0]["stale"] is True
    assert stale["pages"][0]["human_paragraphs"] == human

    rebuilt = await handle.post(
        f"/api/wikis/{wiki_id}/revisions",
        json={**body, "base_revision_id": edited_revision["id"], "sources": [
            {"resource_id": resource_id, "source_version_id": appended_id}]},
        headers={"Idempotency-Key": "human-append-rebuild"})
    assert rebuilt.status_code == 201, rebuilt.text
    current = (await handle.get(f"/api/wikis/{wiki_id}")).json()
    draft = current["revision"]
    assert draft["id"] == rebuilt.json()["revision"]["id"]
    assert draft["id"] != edited_revision["id"]
    assert draft["base_revision_id"] == edited_revision["id"]
    assert current["wiki"]["published_revision_id"] is None
    assert draft["semantic_review"] == "needs_review"
    assert draft["stale"] is False and draft["stale_reasons"] == {}
    assert draft["merge_conflicts"] == []
    page = next(item for item in draft["pages"] if item["page_key"] == page_key)
    assert page["human_paragraphs"] == human
    cited_versions = set()
    for section in page["generated_sections"]:
        for sentence in section["sentences"]:
            for evidence_id in sentence["evidence_ids"]:
                evidence = await handle.get(f"/api/evidence/{evidence_id}")
                assert evidence.status_code == 200, evidence.text
                cited_versions.add(evidence.json()["document"]["id"])
    assert cited_versions == {appended_id}

    history = await handle.get(
        f"/api/wikis/{wiki_id}/revisions/{edited_revision['id']}")
    assert history.status_code == 200, history.text
    assert history.json()["revision"]["stale"] is True
    assert history.json()["revision"]["pages"][0]["human_paragraphs"] == human


async def test_local_content_unsupported_fallback_shape(client):
    handle, _ = client
    for method, path, kwargs in [
        ("get", "/api/knowledge/graph", {}),
        ("post", "/api/documents/x/reparse", {}),
        ("get", "/api/reviews", {}),
    ]:
        response = await getattr(handle, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.text)
        assert response.json()["error"]["code"] == "not_supported_locally", (method, path)
    capabilities = await handle.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    # Shared Web views read the authority from the center's capabilities shape (discovery-v1);
    # without it the Wiki page showed a raw TypeError on the local source (2026-09-26).
    body = capabilities.json()
    assert body["identity"]["authority_node_id"] and body["identity"]["workspace_id"]
    assert body["profile"]["issuer"] and body["profile"]["subject"]


def _wiki_protocol(runtime):
    async def generate(messages, **kwargs):
        import json as _json

        stage = _json.loads(messages[1]["content"])["stage"]
        if stage == "plan":
            return _json.dumps({"pages": [{"title": "合同", "references": [1]}]}), \
                {"name": "fixture", "location": "local"}
        if stage == "relations":
            return _json.dumps({"selected_relations": []}), \
                {"name": "fixture", "location": "local"}
        return _json.dumps({"pages": [{"page": 1, "sections": [{"heading": "事实", "sections": [],
            "sentences": [{"text": "答案是 42。", "references": [1]}]}]}]}), \
            {"name": "fixture", "location": "local"}

    return generate


async def test_wiki_app_dependencies_rebuild_fixed_source_and_locate_original(client):
    handle, runtime = client
    version_id, resource_id = await _ready_version(handle, runtime)
    _grounded_model(runtime)
    runtime.provider.generate = _wiki_protocol(runtime)
    created = await handle.post("/api/wikis", json={
        "title": "合同", "sources": [
            {"resource_id": resource_id, "source_version_id": version_id}]},
        headers={"Idempotency-Key": "app-dependency-build"})
    assert created.status_code == 201, created.text
    built = created.json()
    wiki_id, revision_id = built["wiki"]["id"], built["revision"]["id"]
    listed = (await handle.get("/api/wikis")).json()
    for document in [built, next(row for row in listed if row["wiki"]["id"] == wiki_id),
                     (await handle.get(f"/api/wikis/{wiki_id}")).json(),
                     (await handle.get(f"/api/wikis/{wiki_id}/revisions/{revision_id}")).json()]:
        dependency = document["revision"]["dependency_manifest"][0]
        assert dependency["resource_id"] == resource_id
        assert dependency["source_version_id"] == version_id
        assert dependency["document_id"] == version_id
        evidence = runtime.store.evidence(dependency["evidence_id"])
        assert dependency["locator"] == evidence["evidence"]["locator"]
        assert dependency["source_digest"] == evidence["evidence"]["source_digest"]
        assert dependency["parse_revision"] == evidence["evidence"]["parse_revision"]
        assert dependency["excerpt_digest"] == evidence["evidence"]["excerpt_digest"]
    rebuilt = await handle.post(f"/api/wikis/{wiki_id}/revisions", json={
        "title": "合同", "base_revision_id": revision_id, "sources": [
            {"resource_id": dependency["resource_id"],
             "source_version_id": dependency["source_version_id"]}]},
        headers={"Idempotency-Key": "app-dependency-rebuild"})
    assert rebuilt.json()["revision"]["base_revision_id"] == revision_id


async def _upload_session(handle, key, data, sha=True):
    import hashlib as _hashlib

    body = {"filename": "sample.pdf", "size": len(data), "mime": "application/pdf"}
    if sha:
        body["sha256"] = _hashlib.sha256(data).hexdigest()
    created = await handle.post("/api/uploads", json=body,
                                headers={"Idempotency-Key": key})
    assert created.status_code == 201, created.text
    return created.json()


async def _put_part(handle, session_id, number, data):
    put = await handle.put(f"/api/uploads/{session_id}/parts/{number}", content=data)
    assert put.status_code == 200, put.text
    return put.json()


async def test_finalize_rejects_oversize_part_and_ninth_part(client):
    handle, _ = client
    session = await _upload_session(handle, "cap-part", b"%PDF-tiny")
    big = await handle.put(f"/api/uploads/{session['id']}/parts/1", content=b"x" * (8 * 1024 * 1024 + 1))
    assert big.status_code == 400, big.text
    assert big.json()["error"]["code"] == "input_too_large"
    for number in range(1, 9):
        await _put_part(handle, session["id"], number, b"%PDF-" + bytes([number]))
    extra = await handle.put(f"/api/uploads/{session['id']}/parts/9", content=b"%PDF-9")
    assert extra.status_code == 400, extra.text
    assert extra.json()["error"]["code"] == "invalid_part"


async def test_finalize_rejects_total_over_budget_size_sha_magic(client):
    import hashlib as _hashlib

    handle, _ = client
    part = b"%PDF-" + b"a" * (8 * 1024 * 1024 - 5)
    created = await handle.post("/api/uploads",
                                json={"filename": "sample.pdf", "size": 32 * 1024 * 1024,
                                      "mime": "application/pdf"},
                                headers={"Idempotency-Key": "cap-total"})
    assert created.status_code == 201, created.text
    session = created.json()
    for number in range(1, 9):
        await handle.put(f"/api/uploads/{session['id']}/parts/{number}", content=part)
    finalized = await handle.post(f"/api/uploads/{session['id']}/finalize")
    assert finalized.status_code == 400, finalized.text
    assert finalized.json()["error"]["code"] == "input_too_large"
    assert (await handle.get(f"/api/uploads/{session['id']}")).json()["ingest_status"] == "rejected"

    data = b"%PDF-ok"
    session = await _upload_session(handle, "cap-size", data)
    await _put_part(handle, session["id"], 1, data + b"trailing")
    finalized = await handle.post(f"/api/uploads/{session['id']}/finalize")
    assert finalized.status_code == 400, finalized.text
    assert finalized.json()["error"]["code"] == "size_mismatch"

    session = await _upload_session(handle, "cap-sha", data)
    await _put_part(handle, session["id"], 1, data)
    runtime_session = (await handle.get(f"/api/uploads/{session['id']}")).json()
    assert runtime_session["ingest_status"] == "pending"
    tampered = await handle.post("/api/uploads", json={
        "filename": "sample.pdf", "size": len(data), "mime": "application/pdf",
        "sha256": "0" * 64}, headers={"Idempotency-Key": "cap-sha-tampered"})
    assert tampered.status_code in (200, 201)
    tampered_id = tampered.json()["id"]
    await _put_part(handle, tampered_id, 1, data)
    finalized = await handle.post(f"/api/uploads/{tampered_id}/finalize")
    assert finalized.status_code == 400, finalized.text
    assert finalized.json()["error"]["code"] == "digest_mismatch"
    assert _hashlib.sha256(data).hexdigest() != "0" * 64

    session = await _upload_session(handle, "cap-magic", b"NOTPDF!!", sha=False)
    await _put_part(handle, session["id"], 1, b"NOTPDF!!")
    finalized = await handle.post(f"/api/uploads/{session['id']}/finalize")
    assert finalized.status_code == 400, finalized.text
    assert finalized.json()["error"]["code"] == "invalid_pdf"


async def test_finalize_streams_without_full_join(client, monkeypatch):
    handle, runtime = client
    data = (FIXTURES / "sample.pdf").read_bytes()
    session = await _upload_session(handle, "stream-ok", data)

    def _spy_read(key, maximum):
        raise AssertionError("streaming finalize must not call read_blob per part")

    monkeypatch.setattr(runtime.blobs, "read", _spy_read)
    part = await handle.put(f"/api/uploads/{session['id']}/parts/1", content=data)
    assert part.status_code == 200, part.text
    finalized = await handle.post(f"/api/uploads/{session['id']}/finalize")
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["ingest_status"] == "ready"


async def test_finalize_store_level_streams_parts_without_join(tmp_path):
    import hashlib as _hashlib
    import io as _io

    from ddp_local.runtime import LocalRuntime as _Runtime

    runtime = _Runtime(tmp_path / "workspace")
    try:
        payload = b"%PDF-" + b"0123456789abcdef" * 64
        session = runtime.store.create_upload_session(
            filename="sample.pdf", mime="application/pdf", declared_size=len(payload),
            declared_sha256=_hashlib.sha256(payload).hexdigest(),
            target_resource_id=None, idempotency_key="store-stream")
        key, size = runtime.blobs.put_stream(_io.BytesIO(payload),
                                             maximum=8 * 1024 * 1024)
        runtime.store.store_upload_part(session["id"], 1, key, size)
        opened = []

        def _limited_open(blob_key):
            stream = runtime.blobs.open(blob_key)
            opened.append(stream)
            original = stream.read

            def _bounded(size=-1):
                return original(16) if size is None or size < 0 else original(min(size, 16))

            stream.read = _bounded
            return stream

        done = runtime.store.finalize_upload_session(
            session["id"], _limited_open, runtime.blobs.put_stream)
        assert done["status"] == "ready", done
        assert opened and all(getattr(stream, "closed", True) for stream in opened)
        stored = runtime.blobs.read(runtime.store.version(
            done["version_id"])["blob_key"], 32 * 1024 * 1024)
        assert stored == payload
    finally:
        runtime.close()


async def test_documents_list_uses_columns_and_surfaces_corruption(client, monkeypatch):

    handle, runtime = client
    version_id, _ = await _ready_version(handle, runtime)
    listed = await handle.get("/api/documents")
    assert listed.status_code == 200, listed.text
    info = next(item for item in listed.json() if item["id"] == version_id)
    assert info["page_count"] >= 1
    assert "layout_unavailable" not in info["compile_degraded"]

    calls = []
    original_read = runtime.blobs.read
    original_open = runtime.blobs.open

    def _spy_read(key, maximum):
        calls.append(("read", key))
        return original_read(key, maximum)

    def _spy_open(key):
        calls.append(("open", key))
        return original_open(key)

    monkeypatch.setattr(runtime.blobs, "read", _spy_read)
    monkeypatch.setattr(runtime.blobs, "open", _spy_open)
    listed = await handle.get("/api/documents")
    assert listed.status_code == 200, listed.text
    assert (await handle.get("/api/documents/stats/summary")).json()["pages"] >= 1
    assert calls == [], f"list/stats must not touch layout blobs: {calls}"

    corrupt = runtime.blobs.write(b"not a layout")
    runtime.store.db.execute(
        "UPDATE versions SET layout_key=?,page_count=NULL,layout_error='layout_unreadable' "
        "WHERE id=?", (corrupt, version_id))
    runtime.store.db.commit()
    listed = await handle.get("/api/documents")
    assert listed.status_code == 200, listed.text
    info = next(item for item in listed.json() if item["id"] == version_id)
    assert info["page_count"] == 0
    assert "layout_unavailable" in info["compile_degraded"]
    assert info["compile_status"] == "failed"
    assert calls == [], f"corrupt list must stay column-only: {calls}"
    detail = await handle.get(f"/api/documents/{version_id}")
    assert detail.status_code == 200, detail.text
    assert "layout_unavailable" in detail.json()["compile_degraded"]


async def test_uploads_create_rejects_duplicate_idempotency_key(client):
    handle, _ = client
    body = {"filename": "sample.pdf", "size": 8, "mime": "application/pdf"}
    duplicated = await handle.post(
        "/api/uploads", json=body,
        headers=[("Idempotency-Key", "dup-first"), ("Idempotency-Key", "dup-second")])
    assert duplicated.status_code == 400, duplicated.text
    assert duplicated.json()["error"]["code"] == "invalid_key"
    single = await handle.post("/api/uploads", json=body, headers={"Idempotency-Key": "dup-first"})
    assert single.status_code == 201, single.text
    missing = await handle.post("/api/uploads", json={**body, "size": 9})
    assert missing.status_code == 201, missing.text


async def test_content_key_rejects_duplicate_idempotency_key(client):
    handle, runtime = client
    _, resource_id = await _ready_version(handle, runtime, key="dup-key-chain")
    duplicated = await handle.request(
        "DELETE", f"/api/resources/{resource_id}",
        headers=[("Idempotency-Key", "dup-first"), ("Idempotency-Key", "dup-second")])
    assert duplicated.status_code == 400, duplicated.text
    assert duplicated.json()["error"]["code"] == "invalid_key"
    removed = await handle.request(
        "DELETE", f"/api/resources/{resource_id}", headers={"Idempotency-Key": "dup-single"})
    assert removed.status_code == 204, removed.text


async def test_body_budget_rejects_over_64kib_conversations_ask(client):
    handle, runtime = client
    version_id, _ = await _ready_version(handle, runtime, key="tier-ask-chain")
    cid = (await handle.post(f"/api/documents/{version_id}/conversations")).json()["id"]
    big = await handle.post(
        f"/api/conversations/{cid}/ask", content=b"x" * (65536 + 1),
        headers={"Content-Type": "application/json"})
    assert big.status_code == 400, big.text
    assert big.json()["error"]["code"] == "input_too_large"


async def test_body_budget_rejects_over_64kib_model_start(client):
    handle, _ = client
    big = await handle.post(
        "/api/v1/models/fixture/start", content=b"x" * (65536 + 1),
        headers={"Content-Type": "application/json", "Idempotency-Key": "tier-model-start"})
    assert big.status_code == 400, big.text
    assert big.json()["error"]["code"] == "input_too_large"


async def test_part_spool_raises_before_consuming_the_tail():
    from ddp_core.application.ports import ApplicationError as _ApplicationError
    from ddp_local.content_http import _part_spool as _spool
    from ddp_local.store import MAX_UPLOAD_PART as _PART_CAP

    pulled = []

    class _Stream:
        async def stream(self):
            for chunk in (b"a" * _PART_CAP, b"b", b"c"):
                pulled.append(chunk)
                yield chunk

    with pytest.raises(_ApplicationError) as rejected:
        await _spool(_Stream())
    assert rejected.value.code == "input_too_large"
    assert pulled == [b"a" * _PART_CAP, b"b"]

    class _Exact:
        async def stream(self):
            yield b"a" * _PART_CAP

    with await _spool(_Exact()) as kept:
        assert kept.read() == b"a" * _PART_CAP


def test_center_file_scope_rejects_zero_and_two_inputs():
    import time as _time

    from ddp_core.application.ports import ApplicationError as _ApplicationError
    from ddp_local.plan_templates import center_file_scope as _file_scope

    center = {"recipient_node_id": "center", "environment_id": "center", "workspace_id": "org-1",
              "profile_id": "profile-alice", "issuer": "center", "subject": "user-alice",
              "endpoint": "https://center.invalid"}
    one = {"ref": "v-1", "digest": "sha256:" + "0" * 64, "size_bytes": 8}
    base = {"center": center, "filename": "manual.pdf", "retention": "temporary",
            "valid_seconds": 3600}
    now = _time.time()
    ok = _file_scope({**base, "inputs": [one]},
                     local_node_id="local", workspace_id="ws", now=now)
    assert ok["input_manifest"] == [one]
    for inputs in ([], [one, dict(one, ref="v-2")]):
        with pytest.raises(_ApplicationError) as rejected:
            _file_scope({**base, "inputs": inputs},
                        local_node_id="local", workspace_id="ws", now=now)
        assert rejected.value.code == "invalid_plan"


async def test_documents_source_streams_chunks_and_refuses_corrupt_blob(client, monkeypatch):
    import hashlib as _hashlib
    import io as _io

    from ddp_local.blobs import STREAM_CHUNK as _CHUNK

    handle, runtime = client
    payload = b"%PDF-" + bytes(((index * 251 + 17) % 245) + 11 for index in range(3 * _CHUNK))
    assert b"\n" not in payload
    key, _ = runtime.blobs.put_stream(_io.BytesIO(payload))
    created = runtime.store.create_resource(
        filename="padded.pdf", blob_key=key, size=len(payload),
        operation_key="stream-chunk-chain", records=[], layout_key=None)
    version_id = created["version_id"]
    # The emitted body chunks are the property under test: a server-side spy
    # wraps the StreamingResponse body iterator, so every yielded piece is
    # sized exactly as delivered. Spying os.read instead only records the
    # pre-hash pass, and the HTTP client re-chunks whatever the server sent,
    # so neither can see a generator that yields whole lines.
    from starlette.responses import StreamingResponse as _StreamingResponse

    sizes = []
    original_init = _StreamingResponse.__init__

    def _spy_init(self, content, *args, **kwargs):
        original_init(self, content, *args, **kwargs)
        inner = self.body_iterator

        async def _recorded():
            async for piece in inner:
                sizes.append(len(piece))
                yield piece

        self.body_iterator = _recorded()

    monkeypatch.setattr(_StreamingResponse, "__init__", _spy_init)
    response = await handle.get(f"/api/documents/{version_id}/source")
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert len(sizes) >= 3, sizes
    assert max(sizes) <= _CHUNK, sizes
    assert sum(sizes) == len(payload)

    pieces, total = [], 0
    async with handle.stream("GET", f"/api/documents/{version_id}/source") as streamed:
        assert streamed.status_code == 200, await streamed.aread()
        async for piece in streamed.aiter_bytes(chunk_size=_CHUNK):
            pieces.append(piece)
            total += len(piece)
            if total >= _CHUNK:
                break
    assert pieces and pieces[0] == payload[:len(pieces[0])]
    assert total <= 2 * _CHUNK

    stored = runtime.blobs.directory / key
    with stored.open("r+b") as tampered:
        tampered.seek(len(payload) - 1)
        tampered.write(b"\x00" if payload[-1:] != b"\x00" else b"\x01")
    corrupt = await handle.get(f"/api/documents/{version_id}/source")
    assert corrupt.status_code != 200, corrupt.content[:64]
    assert corrupt.json()["error"]["code"] == "blob_corrupt"
    assert _hashlib.sha256(corrupt.content).hexdigest() != key


async def test_documents_source_rejects_non_pdf_magic(client):
    import io as _io

    handle, runtime = client
    key, _ = runtime.blobs.put_stream(_io.BytesIO(b"NOTPD" + b"x" * 64))
    created = runtime.store.create_resource(
        filename="fake.pdf", blob_key=key, size=69,
        operation_key="stream-magic-chain", records=[], layout_key=None)
    rejected = await handle.get(f"/api/documents/{created['version_id']}/source")
    assert rejected.status_code != 200, rejected.content[:64]
    assert rejected.json()["error"]["code"] == "source_invalid"


async def test_proven_layout_corruption_overwrites_cached_count(client):
    handle, runtime = client
    version_id, _ = await _ready_version(handle, runtime, key="mark-corrupt-chain")
    before = runtime.store.version(version_id)
    assert before["page_count"] and not before["layout_error"]
    stored = runtime.blobs.directory / before["layout_key"]
    with stored.open("r+b") as tampered:
        tampered.seek(0)
        tampered.write(b"\x00")
    detail = await handle.get(f"/api/documents/{version_id}/layout")
    assert detail.status_code != 200, detail.content[:64]
    assert detail.json()["error"]["code"] == "result_not_ready"
    after = runtime.store.version(version_id)
    assert after["page_count"] is None
    assert after["layout_error"] == "layout_unreadable"
    listed = await handle.get("/api/documents")
    assert listed.status_code == 200, listed.text
    info = next(item for item in listed.json() if item["id"] == version_id)
    assert info["page_count"] == 0
    assert "layout_unavailable" in info["compile_degraded"]
    assert info["compile_status"] == "failed"
    runtime.store.mark_layout_error(version_id, "layout_too_large")
    assert runtime.store.version(version_id)["layout_error"] == "layout_unreadable"


async def test_finalize_closes_opened_parts_when_a_later_open_fails(tmp_path):
    import hashlib as _hashlib
    import io as _io

    from ddp_core.application.ports import ApplicationError as _ApplicationError

    runtime = LocalRuntime(tmp_path / "workspace")
    try:
        first = b"%PDF-" + b"a" * 64
        second = b"b" * 64
        first_key, _ = runtime.blobs.put_stream(_io.BytesIO(first))
        second_key, _ = runtime.blobs.put_stream(_io.BytesIO(second))
        session = runtime.store.create_upload_session(
            filename="two.pdf", mime="application/pdf",
            declared_size=len(first) + len(second),
            declared_sha256=_hashlib.sha256(first + second).hexdigest(),
            target_resource_id=None, idempotency_key="finalize-leak")
        runtime.store.store_upload_part(session["id"], 1, first_key, len(first))
        runtime.store.store_upload_part(session["id"], 2, second_key, len(second))
        opened = []

        def _flaky_open(blob_key):
            if blob_key == second_key:
                raise _ApplicationError("blob_unreadable", "boom")
            stream = runtime.blobs.open(blob_key)
            opened.append(stream)
            return stream

        import pytest as _pytest

        with _pytest.raises(_ApplicationError) as failed:
            runtime.store.finalize_upload_session(
                session["id"], _flaky_open, runtime.blobs.put_stream)
        assert failed.value.code == "blob_unreadable"
        assert opened and all(stream.closed for stream in opened)
    finally:
        runtime.close()
