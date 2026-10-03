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
    assert rebuilt.status_code == 201, rebuilt.text
    assert rebuilt.json()["revision"]["base_revision_id"] == revision_id
