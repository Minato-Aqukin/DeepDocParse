"""Verified byte uploads append fixed revisions, not metadata-copy ancestry."""
import hashlib

import pytest
import respx
from sqlalchemy import select

from ddp_corpus.models import Document, Resource
from tests.conftest import ACTOR, ORG, actor_headers, submit_document
from tests.test_documents import _mock_service


@respx.mock
async def test_target_upload_appends_three_versions_and_replay_preserves_history(actor_client, session, app_state):
    _mock_service()
    first = await submit_document(actor_client, app_state.storage, b"%PDF-1.4 revision one")
    assert first.status_code == 200, first.text
    resource_id = await session.scalar(select(Resource.id))
    initial = (await actor_client.get(f"/api/resources/{resource_id}")).json()
    original = initial["versions"][0]

    for content in (b"%PDF-1.4 revision two", b"%PDF-1.4 revision three"):
        appended = await submit_document(actor_client, app_state.storage, content,
                                         target_resource_id=resource_id)
        assert appended.status_code == 200, appended.text

    current = (await actor_client.get(f"/api/resources/{resource_id}")).json()
    assert sorted(version["version_no"] for version in current["versions"]) == [1, 2, 3]
    assert next(version for version in current["versions"] if version["id"] == original["id"]) == original
    assert current["copied_from"] == initial["copied_from"]
    listing = await actor_client.get("/api/resources", params={"scope": "mine"})
    assert {resource["id"] for resource in listing.json()["items"]} == {resource_id}

    replay = await actor_client.post("/internal/events", content=appended.request.content,
                                     headers=appended.request.headers)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "duplicate_event"
    after_replay = (await actor_client.get(f"/api/resources/{resource_id}")).json()
    assert after_replay["versions"] == current["versions"]


@respx.mock
async def test_target_upload_rejects_duplicate_bytes_from_a_new_event(actor_client, session, app_state):
    _mock_service()
    content = b"%PDF-1.4 same fixed source"
    first = await submit_document(actor_client, app_state.storage, content)
    assert first.status_code == 200, first.text
    resource_id = await session.scalar(select(Resource.id))
    original = (await actor_client.get(f"/api/resources/{resource_id}")).json()

    duplicate = await submit_document(
        actor_client, app_state.storage, content, target_resource_id=resource_id)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "resource_version_exists"
    current = (await actor_client.get(f"/api/resources/{resource_id}")).json()
    assert current["versions"] == original["versions"]


@pytest.mark.parametrize("boundary", ["other_owner", "other_organization", "deleted"])
@respx.mock
async def test_target_upload_rejects_unavailable_authority_before_creating_content(
        actor_client, session, app_state, boundary):
    routes = _mock_service()
    first = await submit_document(actor_client, app_state.storage, b"%PDF-1.4 original")
    assert first.status_code == 200, first.text
    resource_id = await session.scalar(select(Resource.id))
    if boundary == "deleted":
        deleted = await actor_client.delete(f"/api/resources/{resource_id}")
        assert deleted.status_code == 204, deleted.text
    dispatched = routes["submit"].call_count
    content = b"%PDF-1.4 forbidden target update"
    response = await submit_document(
        actor_client, app_state.storage, content, target_resource_id=resource_id,
        actor_id="another-owner" if boundary == "other_owner" else ACTOR,
        organization_id="another-organization" if boundary == "other_organization" else ORG,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "resource_not_found"
    assert routes["submit"].call_count == dispatched
    assert await session.scalar(select(Document.id).where(
        Document.doc_id == hashlib.sha256(content).hexdigest())) is None


@respx.mock
async def test_admission_uses_the_consumer_predicate_for_owner_liveness_and_digest(
        actor_client, session, app_state):
    """control asks this before allocating storage; it must agree with the consumer."""
    _mock_service()
    content = b"%PDF-1.4 admitted original"
    first = await submit_document(actor_client, app_state.storage, content)
    assert first.status_code == 200, first.text
    resource_id = await session.scalar(select(Resource.id))
    path = f"/internal/upload-target/{resource_id}"

    assert (await actor_client.get(path)).status_code == 200
    assert (await actor_client.get(path, params={
        "sha256": hashlib.sha256(b"%PDF-1.4 next").hexdigest()})).status_code == 200
    duplicate = await actor_client.get(path, params={"sha256": hashlib.sha256(content).hexdigest()})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "resource_version_exists"

    for headers in (actor_headers("another-owner"),
                    actor_headers(organization_id="another-organization")):
        denied = await actor_client.get(path, headers=headers)
        assert denied.status_code == 404 and denied.json()["error"]["code"] == "resource_not_found"

    withdrawn = await actor_client.patch(f"/api/resources/{resource_id}",
                                         json={"publication": "withdrawn"})
    assert withdrawn.status_code == 200, withdrawn.text
    assert (await actor_client.get(path)).status_code == 404
    rejected = await submit_document(actor_client, app_state.storage, b"%PDF-1.4 after withdraw",
                                     target_resource_id=resource_id)
    assert rejected.status_code == 404 and rejected.json()["error"]["code"] == "resource_not_found"
