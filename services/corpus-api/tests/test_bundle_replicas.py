"""T48: licensed offline originals stay bound to their source and finite licence."""

import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import respx
from sqlalchemy import select
from ddp_bundle_fixture import ORIGINAL
from ddp_core.bundle import digest, json_bytes, read_bundle
from ddp_corpus import bundle_source
from ddp_corpus.config import settings
from ddp_corpus.errors import APIError
from ddp_corpus.models import Document, Evidence, ParseJob, Resource, ResourceVersion, new_id
from ddp_corpus.routers import bundle_replicas
from ddp_corpus.storage import crop_key
from tests.conftest import CONTROL, actor_headers
from tests.test_bundles import import_one, sample_bundle, upload_headers
from tests.test_client_projection import headers as client_headers


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
TERM = NOW + timedelta(hours=1)
ORIGINAL_PATHS = ("licensed-source", "client-source", "document-download", "source-url", "internal-file-access", "crop", "cached-crop", "bundle-export")


def licensed_bundle(term):
    """Add source-issued terms without relying on the validator under test."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sample_bundle())) as original:
        with zipfile.ZipFile(output, "w") as archive:
            for name in original.namelist():
                content = original.read(name)
                if name == "manifest.json":
                    manifest = json.loads(content)
                    manifest["source"]["licence_valid_until"] = term
                    content = json_bytes(manifest)
                archive.writestr(name, content)
    return output.getvalue()


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    monkeypatch.setattr(bundle_source, "utcnow", lambda: current[0])
    monkeypatch.setattr(bundle_replicas, "utcnow", lambda: current[0])
    return current


def bundle_path(result):
    return f"/api/resources/{result['resource_id']}/versions/{result['source_version_id']}/bundle"


async def directory(client, result):
    response = await client.get(bundle_path(result) + "/replicas")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    rows = response.json()["replicas"]
    assert len(rows) == 1, response.text
    return rows[0]


async def revoke(client, result, replica, key="revoke-1", **kwargs):
    return await client.post(
        bundle_path(result) + f"/replicas/{replica['replica_id']}/revoke",
        headers={**actor_headers(), "Idempotency-Key": key},
        **kwargs,
    )


async def original_request(client, result, path, session, storage):
    headers = client_headers()
    if path == "licensed-source":
        endpoint = bundle_path(result) + "/licensed-source"
    elif path == "client-source":
        endpoint = f"/api/v1/client/versions/{result['source_version_id']}/source"
    elif path == "document-download":
        endpoint = f"/api/documents/{result['document_id']}/download?format=source"
    elif path == "source-url":
        endpoint = f"/api/documents/{result['document_id']}/source-url"
    elif path == "internal-file-access":
        endpoint = f"/internal/file-access/{result['document_id']}"
    elif path == "bundle-export":
        endpoint = bundle_path(result)
    else:
        version = await session.get(ResourceVersion, result["source_version_id"])
        await storage.put(crop_key(version.parse_job_id, 0, "fixed-bbox"), b"cached original crop", "image/png")
        endpoint = f"/api/documents/{result['document_id']}/crops/{version.parse_job_id}/0_fixed-bbox.png"
        if path == "cached-crop":
            headers["If-None-Match"] = '"fixed-bbox"'
    with respx.mock(assert_all_called=False) as upstream:
        upstream.post(f"{CONTROL}/internal/file-grants").respond(
            200, json={"token": "fixed-token", "url": f"{CONTROL}/files/fixed-token"}
        )
        return await client.get(endpoint, headers=headers, follow_redirects=False)


def assert_unavailable(response):
    assert response.status_code == 410, response.text
    assert response.json()["error"]["code"] == "source_unavailable"
    assert ORIGINAL not in response.content


def assert_source_redirect(response, *, availability, term=None):
    """Licensed/client originals never proxy bytes (invariant 6): 302 to a
    short-lived direct-read URL plus the X-DDP-Source-* authorization facts."""
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("memory://"), location
    assert "filename=" in location
    assert response.headers["x-ddp-source-availability"] == availability
    assert response.headers["cache-control"] == "private, no-store"
    if term is None:
        assert "x-ddp-source-licence-valid-until" not in response.headers
    else:
        assert datetime.fromisoformat(
            response.headers["x-ddp-source-licence-valid-until"]) == term


async def test_import_directory_and_real_offline_original(actor_client, clock):
    result = await import_one(actor_client)
    replica = await directory(actor_client, result)
    assert replica["availability"] == "licensed_copy"
    assert replica["revoked_at"] is None and replica["valid_until"] is None
    for key in ("resource_id", "source_version_id"):
        assert replica[key] == result[key]
    for key in ("origin_node_id", "authority_node_id", "source_digest", "policy_revision"):
        assert replica[key] == result["source"][key]
    response = await actor_client.get(bundle_path(result) + "/licensed-source")
    assert_source_redirect(response, availability="offline_snapshot")
    assert response.headers["x-ddp-source-digest"] == digest(ORIGINAL)


async def test_revoke_empty_body_idempotency_and_cross_replica_conflict(actor_client, clock):
    first = await import_one(actor_client)
    replica = await directory(actor_client, first)
    nonempty = await revoke(actor_client, first, replica, content=b"{}")
    assert nonempty.status_code == 400
    assert (await directory(actor_client, first))["availability"] == "licensed_copy"
    revoked = await revoke(actor_client, first, replica)
    assert revoked.status_code == 200, revoked.text
    replay = await revoke(actor_client, first, replica)
    assert replay.status_code == 200, replay.text
    assert replay.json() == revoked.json()
    assert revoked.json()["replica"]["availability"] == "unavailable"
    assert datetime.fromisoformat(revoked.json()["replica"]["revoked_at"]).replace(tzinfo=timezone.utc) == NOW
    second = await import_one(actor_client, key="import-second")
    second_replica = await directory(actor_client, second)
    conflict = await revoke(actor_client, second, second_replica)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert (await directory(actor_client, second))["availability"] == "licensed_copy"
    other_copy = await actor_client.get(bundle_path(second) + "/licensed-source")
    assert_source_redirect(other_copy, availability="offline_snapshot")
    assert other_copy.headers["x-ddp-source-digest"] == digest(ORIGINAL)


@pytest.mark.parametrize("path", ORIGINAL_PATHS)
async def test_revocation_blocks_every_original_path(actor_client, session, app_state, clock, path):
    result = await import_one(actor_client)
    replica = await directory(actor_client, result)
    assert (await revoke(actor_client, result, replica)).status_code == 200
    assert_unavailable(await original_request(actor_client, result, path, session, app_state.storage))
    assert (await directory(actor_client, result))["availability"] == "unavailable"
    evidence = await actor_client.get(bundle_path(result) + "/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"] == read_bundle(io.BytesIO(sample_bundle())).evidence
    assert evidence.json()["evidence"][0]["excerpt"] == "A fixed source"


@pytest.mark.parametrize("path", ORIGINAL_PATHS)
async def test_finite_licence_expires_at_exact_term(actor_client, session, app_state, clock, path):
    from ddp_corpus import node_identity
    # client-source answers through the bound node identity (never the request
    # header): bind the node the test client claims to be, mirroring
    # test_client_projection.py. settings.bundle_node_id stays unset here so
    # the bound value is authoritative and no mismatch trip fires.
    node_identity.bind_static_for_tests("node-" + "a" * 48)
    archive = licensed_bundle(TERM.isoformat())
    imported = await actor_client.post(
        "/api/bundles/import", content=archive, headers=upload_headers()
    )
    assert imported.status_code == 201, imported.text
    result = imported.json()
    # A signed URL needs at least one second of licence left (it must not outlive it).
    # All three 302 paths mint presigned URLs through licence_ttl; the rest serve
    # bytes/JSON and stay live until the exact term.
    clock[0] = TERM - (timedelta(seconds=1) if path in ("document-download", "licensed-source", "client-source")
                       else timedelta(microseconds=1))
    live = await original_request(actor_client, result, path, session, app_state.storage)
    expected_status = (302 if path in ("document-download", "licensed-source", "client-source")
                       else 304 if path == "cached-crop" else 200)
    assert live.status_code == expected_status, live.text
    if path in ("licensed-source", "client-source"):
        assert_source_redirect(live, availability="offline_snapshot", term=TERM)
    elif path == "document-download":
        # The plain download redirect carries no X-DDP-Source-* facts — just
        # the short-lived direct-read URL (see the source-download landmine test).
        assert live.headers["location"].startswith("memory://"), live.headers["location"]
        assert "filename=" in live.headers["location"]
    clock[0] = TERM
    assert_unavailable(await original_request(actor_client, result, path, session, app_state.storage))
    assert (await directory(actor_client, result))["availability"] == "unavailable"
    evidence = await actor_client.get(bundle_path(result) + "/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"][0]["excerpt"] == "A fixed source"


@pytest.fixture
async def native_bundle(session, app_state, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", "node-centre")
    document = Document(
        id=new_id(), uploaded_by="actor-alice", organization_id="org-test",
        doc_id=digest(ORIGINAL)[7:], filename="manual.pdf", mime="application/pdf",
        size_bytes=len(ORIGINAL), object_key="sources/native/source.pdf",
    )
    resource = Resource(
        id=new_id(), owner_id="actor-alice", uploaded_by="actor-alice",
        organization_id="org-test", display_name="manual.pdf",
    )
    session.add_all([document, resource])
    await session.flush()
    version = ResourceVersion(
        id=new_id(), resource_id=resource.id, document_id=document.id,
        source_digest=document.doc_id, filename="manual.pdf", size_bytes=len(ORIGINAL),
    )
    session.add(version)
    await session.commit()
    await app_state.storage.put(document.object_key, ORIGINAL, "application/pdf")
    return f"/api/resources/{resource.id}/versions/{version.id}/bundle"


async def test_native_finite_export_import_ledger_and_holder_cannot_override(
    actor_client, native_bundle, session, app_state, clock
):
    exported = await actor_client.get(native_bundle, params={"licence_valid_until": TERM.isoformat()})
    assert exported.status_code == 200, exported.text
    snapshot = read_bundle(io.BytesIO(exported.content))
    assert snapshot.source.get("licence_valid_until") == TERM.isoformat()
    imported = await actor_client.post("/api/bundles/import", content=exported.content, headers=upload_headers())
    assert imported.status_code == 201, imported.text
    result = imported.json()
    replica = await directory(actor_client, result)
    assert datetime.fromisoformat(replica["valid_until"]).replace(tzinfo=timezone.utc) == TERM
    source = await actor_client.get(bundle_path(result) + "/licensed-source")
    assert_source_redirect(source, availability="offline_snapshot", term=TERM)
    reexported = await actor_client.get(bundle_path(result))
    assert reexported.status_code == 200, reexported.text
    assert read_bundle(io.BytesIO(reexported.content)).source["licence_valid_until"] == TERM.isoformat()
    for override in (TERM.isoformat(), (TERM + timedelta(days=1)).isoformat(), ""):
        denied = await actor_client.get(bundle_path(result), params={"licence_valid_until": override})
        assert denied.status_code == 400, denied.text
    # Native export and its imported copy share content-addressed storage. Retire
    # the native resource so document-scoped reads select the licensed holder.
    deleted = await actor_client.delete(f"/api/resources/{snapshot.source['resource_id']}")
    assert deleted.status_code == 204, deleted.text
    # licensed-source mints a presigned URL: one full second of licence must remain.
    clock[0] = TERM - timedelta(seconds=1)
    before = await actor_client.get(bundle_path(result) + "/licensed-source")
    assert_source_redirect(before, availability="offline_snapshot", term=TERM)
    clock[0] = TERM
    for path in ORIGINAL_PATHS:
        assert_unavailable(await original_request(actor_client, result, path, session, app_state.storage))
    assert (await directory(actor_client, result))["availability"] == "unavailable"
    evidence = await actor_client.get(bundle_path(result) + "/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"] == snapshot.evidence


async def test_native_export_rejects_naive_licence_term(actor_client, native_bundle):
    response = await actor_client.get(native_bundle, params={"licence_valid_until": "2030-01-01T01:00:00"})
    assert response.status_code == 400, response.text


async def test_import_rejects_naive_licence_term(actor_client):
    response = await actor_client.post(
        "/api/bundles/import", content=licensed_bundle("2030-01-01T01:00:00"),
        headers=upload_headers(),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("state", ("live", "revoked"))
async def test_bob_cannot_probe_replica_directory_source_or_revoke(actor_client, clock, state):
    result = await import_one(actor_client)
    replica = await directory(actor_client, result)
    if state == "revoked":
        assert (await revoke(actor_client, result, replica)).status_code == 200
    missing = await actor_client.get("/api/resources/unknown/versions/unknown/bundle/replicas", headers=actor_headers("actor-bob"))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "resource_not_found"
    for suffix in ("/replicas", "/licensed-source", "/evidence", ""):
        denied = await actor_client.get(bundle_path(result) + suffix, headers=actor_headers("actor-bob"))
        assert denied.status_code == 404
        assert denied.json() == missing.json()
    denied = await actor_client.post(
        bundle_path(result) + f"/replicas/{replica['replica_id']}/revoke",
        headers={**actor_headers("actor-bob"), "Idempotency-Key": "bob-revoke"},
    )
    assert denied.status_code == 404
    assert denied.json() == missing.json()


@pytest.mark.parametrize("field,value", (
    ("resource_id", "c" * 32), ("source_version_id", "d" * 32),
    ("origin_node_id", "node-forged"), ("authority_node_id", "node-forged"),
    ("source_digest", "sha256:" + "e" * 64),
    ("policy_revision", "forged-policy"), ("licence_valid_until", TERM.isoformat()),
))
async def test_stored_manifest_identity_and_licence_tampering_cannot_release_original(
    actor_client, app_state, clock, field, value
):
    result = await import_one(actor_client)
    key = f"bundles/{result['source_version_id']}/manifest.json"
    manifest = json.loads(await app_state.storage.get(key))
    manifest["source"][field] = value
    await app_state.storage.put(key, json_bytes(manifest), "application/json")
    response = await actor_client.get(bundle_path(result) + "/licensed-source")
    assert response.status_code != 200, response.text
    assert ORIGINAL not in response.content


@pytest.mark.parametrize("tampering", ("remove", "null", "extend"))
async def test_stored_finite_term_cannot_be_removed_or_extended(
    actor_client, app_state, clock, tampering
):
    imported = await actor_client.post(
        "/api/bundles/import", content=licensed_bundle(TERM.isoformat()),
        headers=upload_headers(),
    )
    assert imported.status_code == 201, imported.text
    result = imported.json()
    key = f"bundles/{result['source_version_id']}/manifest.json"
    manifest = json.loads(await app_state.storage.get(key))
    if tampering == "remove":
        manifest["source"].pop("licence_valid_until")
    else:
        manifest["source"]["licence_valid_until"] = (
            None if tampering == "null" else (TERM + timedelta(days=1)).isoformat()
        )
    await app_state.storage.put(key, json_bytes(manifest), "application/json")
    for endpoint in (
        bundle_path(result) + "/licensed-source",
        f"/api/v1/client/versions/{result['source_version_id']}/source",
    ):
        response = await actor_client.get(endpoint, headers=client_headers())
        assert response.status_code != 200, response.text
        assert ORIGINAL not in response.content


@pytest.mark.parametrize("ending", ["revoke", "expiry"])
async def test_mcp_evidence_withholds_original_crop_after_licence_ends(
        actor_client, session, app_state, clock, ending):
    """MCP get_evidence ships crop pixels cut from the original: same gate, excerpt stays."""
    imported = await actor_client.post(
        "/api/bundles/import", content=licensed_bundle(TERM.isoformat()), headers=upload_headers())
    assert imported.status_code == 201, imported.text
    result = imported.json()
    version = await session.get(ResourceVersion, result["source_version_id"])
    evidence = await session.scalar(select(Evidence).where(
        Evidence.parse_job_id == version.parse_job_id))
    evidence.crop_key = crop_key(version.parse_job_id, 0, "mcp-bbox")
    await session.commit()
    await app_state.storage.put(evidence.crop_key, b"crop cut from original", "image/png")
    endpoint = f"/internal/mcp/evidence/{evidence.id}"

    live = (await actor_client.get(endpoint)).json()
    assert live["evidence"]["crop_degraded"] is None
    assert base64.b64decode(live["crop"]["data_base64"]) == b"crop cut from original"

    if ending == "revoke":
        assert (await revoke(actor_client, result, await directory(actor_client, result))).status_code == 200
    else:
        clock[0] = TERM
    ended = await actor_client.get(endpoint)
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert body["crop"] is None
    assert body["evidence"]["crop_degraded"] == "source_unavailable"
    assert body["evidence"]["content"] == "A fixed source"
    assert b"crop cut from original" not in ended.content


async def import_licensed(client, term=TERM):
    imported = await client.post(
        "/api/bundles/import", content=licensed_bundle(term.isoformat()), headers=upload_headers())
    assert imported.status_code == 201, imported.text
    return imported.json()


async def reparse_and_select(client, session, result):
    """A local reparse of the imported copy, selected as current: a version without a prefix."""
    version = await session.get(ResourceVersion, result["source_version_id"])
    imported_job = await session.get(ParseJob, version.parse_job_id)
    job = ParseJob(document_id=version.document_id, resource_id=result["resource_id"],
                   result_prefix=imported_job.result_prefix,
                   engine="borndigital", options={}, options_hash="r" * 64,
                   status="succeeded", index_status="ready", page_count=1,
                   document_version=2)
    session.add(job)
    await session.commit()
    selected = await client.put(f"/api/documents/{result['document_id']}/current-job",
                                json={"job_id": job.id})
    assert selected.status_code == 200, selected.text
    derived = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == result["resource_id"],
        ResourceVersion.parse_job_id == job.id))
    assert derived is not None and not derived.bundle_prefix
    return derived


@pytest.mark.parametrize("path", ["client-source", "document-download", "source-url",
                                  "internal-file-access"])
async def test_reparsed_licensed_copy_stays_bound_to_the_licence(
        actor_client, session, clock, path):
    from ddp_corpus import node_identity
    # client-source answers through the bound node identity (never the request
    # header): bind the node the test client claims to be, mirroring
    # test_client_projection.py.
    node_identity.bind_static_for_tests("node-" + "a" * 48)
    result = await import_licensed(actor_client)
    derived = await reparse_and_select(actor_client, session, result)
    endpoint = {
        "client-source": f"/api/v1/client/versions/{derived.id}/source",
        "document-download": f"/api/documents/{result['document_id']}/download?format=source",
        "source-url": f"/api/documents/{result['document_id']}/source-url",
        "internal-file-access": f"/internal/file-access/{result['document_id']}",
    }[path]

    async def request():
        with respx.mock(assert_all_called=False) as upstream:
            upstream.post(f"{CONTROL}/internal/file-grants").respond(
                200, json={"token": "fixed-token", "url": f"{CONTROL}/files/fixed-token"})
            return await actor_client.get(endpoint, headers=client_headers(), follow_redirects=False)

    # Both 302 paths mint presigned URLs through licence_ttl (one-second floor);
    # source-url/internal serve no URLs and stay live until the exact term.
    clock[0] = TERM - (timedelta(seconds=1) if path in ("document-download", "client-source")
                       else timedelta(microseconds=1))
    live = await request()
    assert live.status_code == (302 if path in ("document-download", "client-source")
                                else 200), live.text
    if path == "client-source":
        assert_source_redirect(live, availability="offline_snapshot", term=TERM)
    clock[0] = TERM
    assert_unavailable(await request())


async def test_reparsed_licensed_copy_cannot_be_exported_as_a_new_source(
        actor_client, session, clock):
    result = await import_licensed(actor_client)
    derived = await reparse_and_select(actor_client, session, result)
    for query in ("", "?licence_valid_until=2099-01-01T00:00:00Z"):
        exported = await actor_client.get(
            f"/api/resources/{result['resource_id']}/versions/{derived.id}/bundle{query}")
        assert exported.status_code == 409, exported.text
        assert exported.json()["error"]["code"] == "bundle_licensed_derivative"
        assert ORIGINAL not in exported.content
    snapshot = await actor_client.get(bundle_path(result))
    assert snapshot.status_code == 200, "the imported snapshot itself still re-exports"


async def test_original_urls_never_outlive_the_licence(actor_client, app_state, clock, monkeypatch):
    result = await import_licensed(actor_client)
    clock[0] = TERM - timedelta(seconds=10)
    lifetimes = []
    real = app_state.storage.presigned_get

    async def recording(key, expires_seconds=3600, **kwargs):
        lifetimes.append(expires_seconds)
        return await real(key, expires_seconds, **kwargs)

    monkeypatch.setattr(app_state.storage, "presigned_get", recording)
    download = await actor_client.get(
        f"/api/documents/{result['document_id']}/download?format=source",
        headers=client_headers(), follow_redirects=False)
    assert download.status_code == 302, download.text
    assert lifetimes == [10], f"URL must end with the licence, signed for {lifetimes}"
    access = await actor_client.get(f"/internal/file-access/{result['document_id']}",
                                    headers=client_headers())
    assert access.status_code == 200, access.text
    assert datetime.fromisoformat(access.json()["valid_until"]) == TERM, \
        "control signs /files and download-url URLs; it needs the term to cap them"


async def test_extraction_cuts_no_pixels_from_an_ended_licence(actor_client, session, app_state, clock):
    from ddp_corpus.deps import Actor
    from ddp_corpus.routers.extractions import original_authorizer
    from tests.conftest import ACTOR, ORG

    result = await import_licensed(actor_client)
    version = await session.get(ResourceVersion, result["source_version_id"])
    source_actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor",
                         resource_id=result["resource_id"], version_id=version.id)
    authorize = original_authorizer(source_actor, result["document_id"],
                                    resource_id=result["resource_id"],
                                    parse_job_id=version.parse_job_id, storage=app_state.storage)
    clock[0] = TERM - timedelta(microseconds=1)
    assert await authorize() == f"bundles/{version.id}/source.bin"
    clock[0] = TERM
    with pytest.raises(APIError) as ended:
        await authorize()
    assert ended.value.status_code == 410 and ended.value.code == "source_unavailable"


@pytest.mark.parametrize("reparsed", [False, True], ids=["snapshot", "reparse"])
async def test_indexing_cuts_no_pixels_from_an_ended_licence(
        actor_client, session, app_state, clock, monkeypatch, reparsed):
    """Compile renders crops and vision descriptions from the original: same licence gate."""
    from ddp_corpus import compilation, indexing

    result = await import_licensed(actor_client)
    version = await session.get(ResourceVersion, result["source_version_id"])
    job = await session.get(ParseJob, version.parse_job_id)
    if reparsed:
        job = await session.get(ParseJob, (await reparse_and_select(actor_client, session, result)).parse_job_id)
    job.index_status = "pending"
    await session.commit()
    rendered = []

    async def recording_crops(*args, **kwargs):
        rendered.append(kwargs.get("source_key"))
        return {}

    monkeypatch.setattr(compilation, "get_or_create_crops", recording_crops)
    clock[0] = TERM
    await indexing.index_document(session, app_state.storage, app_state.http,
                                  result["document_id"], job_id=job.id)
    job = await session.get(ParseJob, job.id, populate_existing=True)
    assert rendered == [], "no page of an ended licensed copy is rendered"




def org_b_headers(actor_id="actor-bob"):
    """A second organization importing the same bytes: isolation, not sharing."""
    return actor_headers(actor_id, organization_id="org-b")


async def test_cross_org_import_gets_its_own_document_row(actor_client, session):
    """Invariant 8: org B importing org A's bytes gets its own Document row; A's
    row (deleted_at/object_key/organization_id) is untouched and both copies read.

    Passes fully only with the per-org UNIQUE constraint: pre-migration the
    second import takes the visible 409 `bundle_content_shared` fallback
    instead of corrupting org A's row.
    """
    first = await import_one(actor_client)
    second = await import_one(actor_client, actor_id="actor-bob",
                              organization_id="org-b", key="import-org-b")
    assert second["resource_id"] != first["resource_id"]
    assert second["document_id"] != first["document_id"]
    mine = await session.get(Document, first["document_id"], populate_existing=True)
    theirs = await session.get(Document, second["document_id"], populate_existing=True)
    assert (mine.organization_id, theirs.organization_id) == ("org-test", "org-b")
    assert mine.deleted_at is None and theirs.deleted_at is None
    assert mine.object_key == f"bundles/{first['source_version_id']}/source.bin"
    assert theirs.object_key == f"bundles/{second['source_version_id']}/source.bin"
    assert mine.object_key != theirs.object_key
    for result, headers in ((first, actor_headers()),
                            (second, org_b_headers())):
        response = await actor_client.get(bundle_path(result) + "/licensed-source",
                                          headers=headers)
        assert_source_redirect(response, availability="offline_snapshot")
        assert response.headers["x-ddp-source-digest"] == digest(ORIGINAL)
    # Neither org can read through the other's resource/version identity.
    assert (await actor_client.get(bundle_path(first) + "/licensed-source",
                                   headers=org_b_headers())).status_code == 404


async def test_deleted_org_row_is_not_revived_by_other_org_reimport(
        actor_client, session):
    """Org A deletes its resource; org B importing the same digest must not
    revive or repoint A's Document row — B gets its own row instead."""
    first = await import_one(actor_client)
    deleted = await actor_client.delete(f"/api/resources/{first['resource_id']}")
    assert deleted.status_code == 204, deleted.text
    grave = await session.get(Document, first["document_id"], populate_existing=True)
    assert grave.deleted_at is not None
    old_key = grave.object_key
    second = await import_one(actor_client, actor_id="actor-bob",
                              organization_id="org-b", key="import-org-b-after-delete")
    assert second["document_id"] != first["document_id"]
    still_gone = await session.get(Document, first["document_id"],
                                   populate_existing=True)
    assert still_gone.deleted_at is not None, "another org's import revived our row"
    assert still_gone.object_key == old_key, "another org's import repointed our row"
    assert still_gone.organization_id == "org-test"
    fresh = await session.get(Document, second["document_id"], populate_existing=True)
    assert fresh.organization_id == "org-b" and fresh.deleted_at is None
    response = await actor_client.get(bundle_path(second) + "/licensed-source",
                                      headers=org_b_headers())
    assert_source_redirect(response, availability="offline_snapshot")


def test_no_unguarded_physical_replica_delete_path_exists():
    """副本行永不由应用层物理删除：可读性止于 tombstone/revoke/expiry，快照字节归
    引用安全 GC（`gc.py`）所有，账本行只随父资源/版本 CASCADE 消失；任何重引
    `session.delete(<BundleReplica>)` 的路径都会让本用例变红。
    """
    import pathlib
    import ddp_corpus.bundle_models as models
    assert not hasattr(models, "physical_delete_guard"), \
        "physical_delete_guard is dead: delete it instead of calling it"
    corpus = pathlib.Path(__file__).resolve().parent.parent / "ddp_corpus"
    offenders = []
    for path in sorted(corpus.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "BundleReplica" in text and "session.delete" in text:
            offenders.append(str(path.relative_to(corpus)))
    assert not offenders, \
        f"replica rows must not be physically deleted by application code: {offenders}"
