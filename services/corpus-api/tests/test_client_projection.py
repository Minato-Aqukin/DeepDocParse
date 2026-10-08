"""Real SQLite authorization, durable revisions, paging, and accepted-upload reconciliation."""
from datetime import timedelta
import hashlib
import json

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from conftest import ACTOR, ORG, SERVICE, CONTROL, actor_headers, submit_document
from ddp_corpus import client_projection as projection
from ddp_corpus.client_models import ClientReceipt, ClientSnapshot
from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion, new_id, utcnow
from ddp_corpus.routers import client as client_router


def headers(who=ACTOR, **kwargs):
    return {**actor_headers(who, **kwargs), "X-DDP-Client-Scope": "sha256:" + hashlib.sha256(who.encode()).hexdigest(), "X-DDP-Authority-Node": "node-"+"a"*48}


@pytest.fixture
async def client_caps(monkeypatch):
    value = {"capabilities": [], "capability_status": "unknown"}
    async def current(_):
        return value
    monkeypatch.setattr(client_router, "capabilities", current)
    return value


async def asset(session, owner=ACTOR, *, publication="private", document=None):
    if document is None:
        document = Document(id=new_id(), uploaded_by=owner, organization_id=ORG, doc_id=new_id()*2,
            origin="web", filename="manual.pdf", mime="application/pdf", size_bytes=100,
            object_key="private-object-key")
        session.add(document)
        await session.flush()
    resource = Resource(id=new_id(), organization_id=ORG, owner_id=owner, uploaded_by=owner,
        display_name="Manual", publication=publication)
    session.add(resource)
    await session.flush()
    number = await session.scalar(select(func.count(ParseJob.id)).where(ParseJob.document_id == document.id))
    job = ParseJob(id=new_id(), document_id=document.id, resource_id=resource.id, initiated_by=owner,
        engine="borndigital", options_hash=new_id(), document_version=number+1, status="succeeded", index_status="ready")
    session.add(job)
    await session.flush()
    version = ResourceVersion(id=new_id(), resource_id=resource.id, version_no=1, document_id=document.id,
        source_digest=document.doc_id, filename="manual.pdf", size_bytes=document.size_bytes, parse_job_id=job.id)
    session.add(version)
    await session.flush()
    return resource, version, job, document


async def snapshot(client, who=ACTOR):
    response = await client.get("/api/v1/client/snapshot", headers=headers(who))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def events(client, cursor, who=ACTOR):
    return await client.get("/api/v1/client/events", params={"after": cursor}, headers=headers(who))


async def test_snapshot_ack_hidden_mutations_and_capability_events(client, session, client_caps):
    first = await snapshot(client)
    assert first["sequence"] == 1 and first["state"]["cache_complete"]
    same = (await events(client, first["cursor"])).json()["events"][0]
    assert same == {**first, "previous_sequence": 1}
    resource, version, job, _ = await asset(session)
    await session.commit()
    changed = (await events(client, first["cursor"])).json()["events"][0]
    assert changed["previous_sequence"] == 1 and changed["sequence"] == 2
    assert changed["state"]["resources"][0]["version_id"] == version.id
    assert changed["state"]["resources"][0]["parse_revision"] == job.id
    assert "private-object-key" not in json.dumps(changed)
    await asset(session, "bob")
    await session.execute(update(ParseJob).where(ParseJob.id == job.id).values(index_lease_until=utcnow()))
    await session.commit()
    same = (await events(client, changed["cursor"])).json()["events"][0]
    assert same == {**changed, "previous_sequence": changed["sequence"]}
    client_caps.update({"capabilities": [{"operation":"doc.parse", "readiness":"unhealthy", "configured":True}]})
    model_event = (await events(client, changed["cursor"])).json()["events"][0]
    assert model_event["sequence"] == 3 and model_event["previous_sequence"] == 2
    assert model_event["state"]["capabilities"][0]["readiness"] == "unhealthy"


async def test_window_counts_fixed_pages_and_acl_revocation(client, session, client_caps):
    for _ in range(105):
        await asset(session, publication="published")
    await asset(session, "other-private")
    await session.commit()
    head = await snapshot(client, "bob")
    state = head["state"]
    assert state["snapshot_complete"] and not state["cache_complete"]
    assert state["windows"]["resources"]["visible_total"] == 105
    assert state["windows"]["resources"]["items_loaded"] == 100
    body = {"name":"resource.page", "payload":{"snapshot_id":state["snapshot_id"],
        "cursor":state["windows"]["resources"]["next_cursor"]}}
    page = await client.post("/api/v1/client/query", headers=headers("bob"), json=body)
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 5 and page.json()["page_index"] == 1
    assert page.json()["sequence"] == head["sequence"] and not page.json()["has_more"]
    assert (await client.post("/api/v1/client/query", headers=headers(ACTOR), json=body)).status_code == 410
    wrong_kind = {**body, "name":"task.page"}
    assert (await client.post("/api/v1/client/query", headers=headers("bob"), json=wrong_kind)).status_code == 410
    # Revocation of an item on the already cached first page invalidates every old page.
    denied = state["resources"][0]["resource_id"]
    await session.execute(update(Resource).where(Resource.id == denied).values(publication="withdrawn"))
    await session.commit()
    assert (await client.post("/api/v1/client/query", headers=headers("bob"), json=body)).status_code == 410
    after = (await events(client, head["cursor"], "bob")).json()["events"][0]
    assert after["sequence"] == head["sequence"]+1
    assert after["state"]["windows"]["resources"]["visible_total"] == 104
    assert denied not in json.dumps(after)
    # The owner still has its own withdrawn asset; privilege is not inferred from publication alone.
    owner = await snapshot(client)
    assert owner["state"]["windows"]["resources"]["visible_total"] == 105


async def test_scope_cursor_expiry_and_explicit_size_failure(client, session, client_caps, monkeypatch):
    own = await snapshot(client)
    assert (await events(client, own["cursor"], "bob")).status_code == 410
    assert (await events(client, "unknown")).status_code == 410
    await session.execute(update(ClientSnapshot).where(ClientSnapshot.id == own["cursor"]).values(
        expires_at=utcnow()-timedelta(seconds=1)))
    await session.commit()
    assert (await events(client, own["cursor"])).status_code == 410
    fresh = await snapshot(client)
    assert fresh["sequence"] > own["sequence"]
    await asset(session)
    await session.commit()
    monkeypatch.setattr(projection, "MAX_ITEMS", 0)
    failed = await client.get("/api/v1/client/snapshot", headers=headers())
    assert failed.status_code == 507 and failed.json()["error"]["code"] == "projection_too_large"
    # Failure cannot advance the last committed view or acknowledge partial data.
    monkeypatch.setattr(projection, "MAX_ITEMS", 20000)
    last = (await events(client, fresh["cursor"])).json()["events"][0]
    assert last["sequence"] == fresh["sequence"] + 1


async def test_read_authentication_and_fixed_query_names(client, client_caps):
    assert (await client.get("/api/v1/client/snapshot")).status_code == 401
    assert (await client.get("/api/v1/client/snapshot", headers=actor_headers())).status_code == 401
    for body in ({"name":"https://evil.example", "payload":{}},
                 {"name":"resource.page", "payload":{"snapshot_id":"x", "cursor":"y", "url":"https://evil.example"}}):
        assert (await client.post("/api/v1/client/query", headers=headers(), json=body)).status_code == 400
    # User context cannot access a service-only capability/protocol producer.
    assert (await client.get("/internal/client/protocol", headers=headers())).status_code == 403
    producer = await client.get("/internal/client/protocol", headers=actor_headers("control-api", kind="service"))
    assert producer.status_code == 200 and "client.receipt" in producer.json()["capabilities"]


@respx.mock
async def test_upload_acceptance_receipt_is_real_durable_and_never_redispatches(client, session, app_state, client_caps):
    respx.post(f"{CONTROL}/internal/file-grants").mock(return_value=httpx.Response(200, json={"url":"http://files/upload.pdf"}))
    # Existing ControlClient endpoint contract can vary; match its fixed route prefix.
    respx.post(url__regex=rf"{CONTROL}/internal/.*").mock(return_value=httpx.Response(200, json={"url":"http://files/upload.pdf", "file_url":"http://files/upload.pdf"}))
    submit = respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(202, json={"task_id":"upstream-one"}))
    event_id = new_id()
    result = await submit_document(client, app_state.storage, b"test uploaded PDF", event_id=event_id)
    assert result.status_code == 200, result.text
    assert submit.call_count == 1, "positive path must submit exactly one real accepted parse request"
    receipt = await client.get("/api/v1/client/receipts/"+event_id, headers=headers())
    assert receipt.status_code == 200, receipt.text
    accepted = receipt.json()
    row = await session.get(ClientReceipt, projection.receipt_hash(ORG, ACTOR, event_id))
    assert row is not None and row.parse_job_id == accepted["task_id"]
    assert accepted["accepted"] and accepted["operation"] == "document.upload"
    before = submit.call_count
    for _ in range(3):
        assert (await client.get("/api/v1/client/receipts/"+event_id, headers=headers())).json() == accepted
    assert submit.call_count == before
    assert (await client.get("/api/v1/client/receipts/"+event_id, headers=headers("bob"))).status_code == 404
    assert (await client.get("/api/v1/client/receipts/unknown", headers=headers())).status_code == 404
    await session.execute(update(Resource).where(Resource.id == accepted["resource_id"]).values(deleted_at=utcnow()))
    await session.commit()
    assert (await client.get("/api/v1/client/receipts/"+event_id, headers=headers())).status_code == 404


@respx.mock
async def test_query_fixed_versions_authorized_before_topk_and_evidence_identity(client, session, app_state, client_caps, monkeypatch):
    from test_mcp_tools import _evidence
    from ddp_corpus import node_identity
    # 资产头与证据信封只认绑定身份：把绑定设成 headers() 声明的那个节点。
    node_identity.bind_static_for_tests("node-" + "a" * 48)
    own, version, job, document = await asset(session)
    other, version2, job2, document2 = await asset(session)
    secret, secret_version, secret_job, _ = await asset(session, "bob", document=document)
    first = await _evidence(session, document, job, seq=0, text="needle own public fact")
    await _evidence(session, document2, job2, seq=0, text="needle second permitted fact")
    denied = await _evidence(session, document, secret_job, seq=0, text="needle needle needle SALARY_SECRET")
    await session.commit()
    respx.post(f"{SERVICE}/v1/embeddings").mock(return_value=httpx.Response(503))
    real_search = app_state.search_index.search
    observed = []
    async def scoped_search(*args, **kwargs):
        observed.append(set(kwargs["authorized_parse_job_ids"]))
        assert set(kwargs["authorized_parse_job_ids"]) == {job.id, job2.id}
        return await real_search(*args, **kwargs)
    monkeypatch.setattr(app_state.search_index, "search", scoped_search)
    body = {"name":"corpus.search", "payload":{"query":"needle", "limit":2,
        "version_ids":[version.id, version2.id]}}
    result = await client.post("/api/v1/client/query", headers=headers(), json=body)
    assert result.status_code == 200, result.text
    assert observed and len(result.json()["hits"]) == 2
    assert {r["version_id"] for r in result.json()["hits"]} == {version.id, version2.id}
    assert "SALARY_SECRET" not in result.text
    body["payload"]["version_ids"] = []
    empty = await client.post("/api/v1/client/query", headers=headers(), json=body)
    assert empty.status_code == 200 and empty.json()["hits"] == []
    assert len(observed) == 1
    body["payload"]["version_ids"] = [secret_version.id]
    assert (await client.post("/api/v1/client/query", headers=headers(), json=body)).status_code == 404
    body["payload"]["resource_id"] = own.id
    assert (await client.post("/api/v1/client/query", headers=headers(), json=body)).status_code == 400
    evidence = await client.post("/api/v1/client/query", headers=headers(), json={"name":"evidence.get",
        "payload":{"evidence_id":first.id, "version_id":version.id}})
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["version_id"] == version.id
    assert evidence.json()["evidence"]["source_version_id"] == version.id
    forbidden = await client.post("/api/v1/client/query", headers=headers(), json={"name":"evidence.get", "payload":{"evidence_id":denied.id}})
    assert forbidden.status_code == 404 and "SALARY_SECRET" not in forbidden.text


async def test_unknown_health_still_records_model_configuration_changes(client, monkeypatch):
    from ddp_corpus.config import settings
    async def unavailable(_):
        return [], "unknown"
    monkeypatch.setattr(client_router, "collect_capability_profiles", unavailable)
    first = await snapshot(client)
    assert first["state"]["capability_status"] == "unknown"
    monkeypatch.setattr(settings, "chat_model", "changed-offline-model")
    changed = (await events(client, first["cursor"])).json()["events"][0]
    assert changed["sequence"] == first["sequence"]+1
    assert changed["state"]["model_configuration"]["chat_model"] == "changed-offline-model"
    assert changed["state"]["capability_status"] == "unknown"


async def test_query_revocation_does_not_leak_old_selected_scope(client, session, app_state, client_caps, monkeypatch):
    resource, version, _, _ = await asset(session, publication="published")
    await session.commit()
    async def withdraw_during_search(*args, **kwargs):
        await session.execute(update(Resource).where(Resource.id == resource.id).values(publication="withdrawn"))
        await session.commit()
        return {"results":[], "degraded":None, "scope":{"authorized_parse_revisions":0}}
    monkeypatch.setattr(client_router.mcp_tools, "_search", withdraw_during_search)
    result = await client.post("/api/v1/client/query", headers=headers("bob"), json={"name":"corpus.search",
        "payload":{"query":"needle", "version_ids":[version.id]}})
    assert result.status_code == 404
    assert version.id not in result.text


def test_metadata_above_four_mib_remains_explicit_bounded_windows():
    values = {"resources":[{"id":str(n), "version_id":f"version-{n}", "name":"中"*250,
                             "filename":"文"*250} for n in range(5000)],
              "tasks":[], "capabilities":[], "capability_status":"unknown"}
    assert len(projection.encoded(values)) > 4*1024*1024
    head, pages = projection.make_snapshot("scope", 1, values, [])
    assert len(projection.encoded(projection.frame(head))) < 4*1024*1024
    assert head.state["windows"]["resources"]["visible_total"] == 5000
    assert head.state["windows"]["resources"]["items_loaded"] == 100
    assert head.state["snapshot_complete"] and not head.state["cache_complete"]
    all_ids = [item["id"] for page in pages if page.kind == "resources" for item in page.body["items"]]
    assert all_ids == [str(n) for n in range(5000)]
    assert all(len(projection.encoded(page.body)) < 4*1024*1024 for page in pages)
    values["resources"] = [{"id":"oversized", "name":"x"*(projection.PAGE_BYTES+1)}]
    with pytest.raises(projection.APIError) as raised:
        projection.make_snapshot("scope", 2, values, [])
    assert raised.value.code == "projection_item_too_large"


async def test_fixed_original_read_authorizes_each_logical_version(client, session, app_state):
    from ddp_corpus import node_identity
    # 同上：source 资产头要绑定身份，没有它就是 503。
    node_identity.bind_static_for_tests("node-" + "a" * 48)
    original = b"%PDF-1.4\noriginal bytes shared by two separately owned resources\n"
    resource, version, _, document = await asset(session)
    document.doc_id = version.source_digest = hashlib.sha256(original).hexdigest()
    document.size_bytes = version.size_bytes = len(original)
    _, bob_version, _, _ = await asset(session, owner="bob", document=document)
    await app_state.storage.put(document.object_key, original, "application/pdf")
    await session.commit()

    route = f"/api/v1/client/versions/{version.id}/source"
    accepted = await client.get(route, headers=headers())
    assert accepted.status_code == 302, accepted.text
    assert accepted.headers["location"].startswith("memory://"), accepted.headers["location"]
    assert "filename=" in accepted.headers["location"]
    assert accepted.headers["x-ddp-source-digest"] == "sha256:" + version.source_digest
    assert accepted.headers["x-ddp-actor-subject"] == ACTOR
    assert accepted.headers["x-ddp-source-availability"] == "online"
    assert accepted.headers["cache-control"] == "private, no-store"
    assert original not in accepted.content
    # Possessing another resource for the same physical document is not an ACL bypass.
    denied = await client.get(route, headers=headers("bob"))
    missing = await client.get("/api/v1/client/versions/unknown/source", headers=headers("bob"))
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    own_copy = await client.get(f"/api/v1/client/versions/{bob_version.id}/source", headers=headers("bob"))
    assert own_copy.status_code == 302, own_copy.text
    assert own_copy.headers["location"].startswith("memory://")
    # Existing caller-scoped metadata is never enough after resource deletion.
    resource.deleted_at = utcnow()
    await session.commit()
    assert (await client.get(route, headers=headers())).status_code == 404
async def test_asset_authority_header_emits_bound_node_never_echo(client, session, app_state):
    """资产头的 authority 节点必须是绑定身份，绝不回显请求头。

    变异确认：把 `_asset_identity` 改回 `request.headers.get(...)`，
    本用例必须红（evil 节点会被原样回显）。
    """
    from ddp_corpus import node_identity
    node_identity.bind_static_for_tests("node-" + "b" * 48)
    original = b"%PDF-1.4\nbound identity bytes\n"
    _, version, _, document = await asset(session)
    document.doc_id = version.source_digest = hashlib.sha256(original).hexdigest()
    document.size_bytes = version.size_bytes = len(original)
    await app_state.storage.put(document.object_key, original, "application/pdf")
    await session.commit()
    bound = node_identity.local_node_id()
    assert bound.startswith("node-")
    route = f"/api/v1/client/versions/{version.id}/source"
    # 调用方自称另一个节点：响应头必须是绑定身份，不是他声称的那个
    evil = await client.get(route, headers={**headers(), "X-DDP-Authority-Node": "node-evil"})
    assert evil.status_code == 302, evil.text
    assert evil.headers["x-ddp-authority-node"] == bound
    assert evil.headers["x-ddp-authority-node"] != "node-evil"
    # 连头都不带：同样是绑定身份（不是空字符串）
    bare = {k: v for k, v in headers().items() if k != "X-DDP-Authority-Node"}
    plain = await client.get(route, headers=bare)
    assert plain.status_code == 302, plain.text
    assert plain.headers["x-ddp-authority-node"] == bound


async def test_evidence_mismatched_claim_rejected(client, session, app_state, client_caps,
                                                  monkeypatch):
    """证据信封用绑定节点；请求头声明了不一致的节点 -> 409。

    变异确认：把 `client_evidence` 改回用请求头拼信封，本用例必须红
    （evil 节点会进 origin/authority 字段）。
    """
    from test_mcp_tools import _evidence
    from ddp_corpus import node_identity
    node_identity.bind_static_for_tests("node-" + "b" * 48)
    _, version, job, document = await asset(session)
    document.doc_id = version.source_digest = hashlib.sha256(b"bound node fact bytes").hexdigest()
    first = await _evidence(session, document, job, seq=0, text="bound node fact")
    await session.commit()
    bound = node_identity.local_node_id()
    good = await client.post("/api/v1/client/query", headers={**headers(), "X-DDP-Authority-Node": bound},
                             json={"name": "evidence.get",
                                   "payload": {"evidence_id": first.id,
                                               "version_id": version.id}})
    assert good.status_code == 200, good.text
    envelope = good.json()["evidence"]
    assert envelope["origin_node_id"] == bound == envelope["authority_node_id"]
    evil = await client.post(
        "/api/v1/client/query",
        headers={**headers(), "X-DDP-Authority-Node": "node-" + "e" * 48},
        json={"name": "evidence.get",
              "payload": {"evidence_id": first.id, "version_id": version.id}})
    assert evil.status_code == 409, evil.text
    assert evil.json()["error"]["code"] == "evidence_provenance_unavailable"

async def test_evidence_locator_is_paragraph_for_non_pdf(
    client, session, app_state, client_caps):
    """非 PDF 文档的 client 证据 locator 不伪造页码。

    与 federation.evidence_locator 同源（client_evidence 直接复用它）；PDF 路径形状
    由既有 locator 断言覆盖，这里钉住非 PDF 不带页序。
    """
    from test_mcp_tools import _evidence
    from ddp_corpus import node_identity
    node_identity.bind_static_for_tests("node-" + "b" * 48)
    _, version, job, document = await asset(session)
    document.mime = "text/markdown"
    document.doc_id = version.source_digest = hashlib.sha256(b"non pdf fact bytes").hexdigest()
    first = await _evidence(session, document, job, seq=4, text="non pdf fact")
    await session.commit()
    # 不带 authority 声明：client_evidence 直接用绑定身份（有声明且不一致 -> 409）。
    bare = {k: v for k, v in headers().items() if k != "X-DDP-Authority-Node"}
    good = await client.post("/api/v1/client/query", headers=bare,
                             json={"name": "evidence.get",
                                   "payload": {"evidence_id": first.id,
                                               "version_id": version.id}})
    assert good.status_code == 200, good.text
    locator = good.json()["evidence"]["locator"]
    assert locator["kind"] == "paragraph"
    assert "physical_page_index" not in locator
    assert locator["seq"] == 4
    assert "printed_page_label" not in locator


async def test_fixed_original_rechecks_permission_after_storage_read(client, session, app_state, monkeypatch):
    original = b"%PDF-1.4\nfixed original\n"
    resource, version, _, document = await asset(session, publication="published")
    document.doc_id = version.source_digest = hashlib.sha256(original).hexdigest()
    document.size_bytes = version.size_bytes = len(original)
    await app_state.storage.put(document.object_key, original, "application/pdf")
    await session.commit()
    read = app_state.storage.get_limited

    async def withdraw_during_read(key, maximum):
        content = await read(key, maximum)
        resource.publication = "withdrawn"
        await session.commit()
        return content

    monkeypatch.setattr(app_state.storage, "get_limited", withdraw_during_read)
    response = await client.get(
        f"/api/v1/client/versions/{version.id}/source", headers=headers("bob"))
    assert response.status_code == 404
    assert original not in response.content


@respx.mock
async def test_center_wiki_windows_fixed_revisions_and_revoked_public_history(client, session):
    from test_wiki_revisions import body, model, source

    resource, version, evidence, _ = await source(session, publication="published")
    calls = model(evidence)

    async def create(key):
        result = await client.post("/api/wikis", json=body(resource, version),
            headers={**headers(), "Idempotency-Key": key})
        assert result.status_code == 201, result.text
        return result.json()

    async def query(name, payload, who=ACTOR):
        return await client.post("/api/v1/client/query", headers=headers(who),
                                 json={"name": name, "payload": payload})

    first = await create("wiki-first")
    second = await create("wiki-second")
    window = await query("wiki.list", {"limit": 1})
    assert window.status_code == 200, window.text
    page = window.json()
    assert page["visible_total"] == 2 and page["has_more"]
    assert "pages" not in page["items"][0]["revision"]
    assert "dependency_manifest" not in page["items"][0]["revision"]
    await create("wiki-created-after-anchor")
    following = (await query("wiki.list", {"limit": 1, "cursor": page["next_cursor"]})).json()
    assert following["visible_total"] == 2 and not following["has_more"]
    assert {page["items"][0]["wiki"]["id"], following["items"][0]["wiki"]["id"]} == {
        first["wiki"]["id"], second["wiki"]["id"]}
    assert (await query("wiki.list", {}, "bob")).json()["visible_total"] == 0
    assert (await query("wiki.list", {"cursor": page["next_cursor"]}, "bob")).status_code == 409

    wiki_id, revision_id = first["wiki"]["id"], first["revision"]["id"]
    assert (await query("wiki.get", {"wiki_id": wiki_id}, "bob")).status_code == 404
    published = await client.post(f"/api/wikis/{wiki_id}/publish", headers=headers(),
                                  json={"base_revision_id": revision_id})
    assert published.status_code == 200, published.text
    page_key = first["revision"]["pages"][0]["page_key"]
    edited = await client.patch(f"/api/wikis/{wiki_id}/pages/{page_key}",
        headers={**headers(), "Idempotency-Key": "private-human-edit"},
        json={"base_revision_id": revision_id,
              "paragraphs": [{"id": "private-note", "text": "PRIVATE_UNPUBLISHED_NOTE"}]})
    assert edited.status_code == 201, edited.text
    edited_revision_id = edited.json()["revision"]["id"]
    count_before_reads = calls.call_count
    old = await query("wiki.get", {"wiki_id": wiki_id, "revision_id": revision_id})
    current = await query("wiki.get", {"wiki_id": wiki_id})
    assert old.status_code == current.status_code == 200
    assert old.json()["revision"]["pages"][0]["human_paragraphs"] == []
    assert "PRIVATE_UNPUBLISHED_NOTE" in current.text
    history = (await query("wiki.revisions", {"wiki_id": wiki_id, "limit": 1})).json()
    assert history["visible_total"] == 2 and history["has_more"]
    historical_page = (await query("wiki.revisions", {
        "wiki_id": wiki_id, "limit": 1, "cursor": history["next_cursor"]})).json()
    assert historical_page["items"][0]["id"] == revision_id and not historical_page["has_more"]
    assert (await query("wiki.revisions", {
        "wiki_id": second["wiki"]["id"], "cursor": history["next_cursor"]})).status_code == 409
    public = await query("wiki.get", {"wiki_id": wiki_id}, "bob")
    public_history = await query("wiki.revisions", {"wiki_id": wiki_id}, "bob")
    assert public.status_code == public_history.status_code == 200
    assert public_history.json()["visible_total"] == 1
    assert public_history.json()["items"][0]["id"] == revision_id
    assert "PRIVATE_UNPUBLISHED_NOTE" not in public.text + public_history.text
    assert (await query("wiki.get", {
        "wiki_id": wiki_id, "revision_id": edited_revision_id}, "bob")).status_code == 404
    resource.publication = "private"
    await session.commit()
    assert (await query("wiki.get", {"wiki_id": wiki_id}, "bob")).status_code == 404
    assert (await query("wiki.revisions", {"wiki_id": wiki_id}, "bob")).status_code == 404
    assert calls.call_count == count_before_reads
