"""Real HTTP/SQL publication, privacy, stable pagination and source authorization."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from sqlalchemy import select, update
from conftest import ACTOR, ORG, actor_headers
from ddp_corpus.collection_models import CollectionCatalogSnapshot, CollectionMember
from ddp_corpus.config import settings
from ddp_corpus.models import Chunk, ParseJob, Resource, ResourceVersion, utcnow
from test_client_projection import asset

NODE = "node-"+"a"*48
BASE = "/api/v1/collections"
INTERNAL = "/internal/federation/collections"
PEER = "/internal/federation/published-collections"


def internal(who="bob", org=ORG):
    return {**actor_headers("control-api", kind="service", organization_id=org, role="admin"),
        "X-DDP-Caller-Actor": who, "X-DDP-Caller-Kind": "user", "X-DDP-Caller-Role": "contributor",
        "X-DDP-Caller-Scope": "sha256:"+hashlib.sha256((org+who).encode()).hexdigest(),
        "X-DDP-Authority-Node": NODE}


async def source(session, owner=ACTOR, publication="published"):
    r, v, p, d = await asset(session, owner, publication=publication)
    session.add(Chunk(document_id=d.id, parse_job_id=p.id, seq=0, text="SECRET source text", char_len=18))
    await session.commit()
    return r, v, p, d


def body(version):
    return {"name": "Explicit manual collection", "licence": "CC-BY-4.0", "languages": ["en"],
            "topics": ["selected-by-owner"], "version_ids": [version.id]}


async def create(client, version, key="create", who=ACTOR, org=ORG, **fields):
    return await client.post(BASE, headers={**actor_headers(who, organization_id=org), "Idempotency-Key": key},
                             json={**body(version), **fields})


async def publish(client, collection, key="publish", who=ACTOR, org=ORG):
    return await client.post(f"{BASE}/{collection['collection_id']}/publish",
        headers={**actor_headers(who, organization_id=org), "Idempotency-Key": key},
        json={"expected_revision": collection["revision"]})


async def snapshot(client, *, who="bob", org=ORG, scope="scope-one", **params):
    return await client.get(INTERNAL, params={"scope_id": scope, **params}, headers=internal(who, org))


async def test_explicit_publication_is_only_catalog_source(client, session):
    public, version, _, _ = await source(session)
    private, private_v, _, _ = await source(session, "secret-owner", "private")
    empty = (await snapshot(client)).json()
    assert empty["collections"] == [] and empty["total"] == 0 and empty["complete"]
    own = await create(client, version)
    assert own.status_code == 201, own.text
    collection = own.json()
    assert collection["collection_id"] not in (public.id, version.id)
    assert (await snapshot(client)).json()["total"] == 0
    assert (await client.get(f"{BASE}/{collection['collection_id']}", headers=actor_headers("third"))).status_code == 404
    assert (await create(client, private_v, key="stolen")).status_code == 404
    private_draft = await create(client, private_v, key="private", who="secret-owner", topics=["TOP SECRET"])
    assert private_draft.status_code == 201
    assert (await publish(client, private_draft.json(), who="secret-owner")).status_code == 409
    assert (await snapshot(client)).json()["registry_revision"] == empty["registry_revision"]
    ready = await publish(client, collection)
    assert ready.status_code == 200, ready.text
    page = (await snapshot(client)).json()
    assert page["total"] == 1 and not page["complete"] and not page["content_snapshot_complete"]
    assert page["index_readiness"] == {collection["collection_id"]: "ready"}
    descriptor = page["collections"][0]
    assert descriptor["origin_node_id"] == NODE and descriptor["revision"] == 2
    schema = json.loads((Path(__file__).resolve().parents[3]/"packages/contracts/schemas/ddp-discovery/v1.json").read_text())
    from jsonschema import Draft202012Validator
    Draft202012Validator({"$ref": "#/$defs/CollectionDescriptor", "$defs": schema["$defs"]}).validate(descriptor)
    for secret in (private.id, private_v.id, public.id, version.id, "SECRET", "Explicit manual", "private-object-key"):
        assert secret not in json.dumps(page)
    terminal = await snapshot(client, snapshot_id=page["snapshot_id"], cursor=page["terminal_cursor"])
    assert terminal.status_code == 200
    assert terminal.json()["complete"] and terminal.json()["next_cursor"] is None
    assert terminal.json()["collections"] == []
    other = (await snapshot(client, org="other-org")).json()
    assert other["total"] == 0 and collection["collection_id"] not in json.dumps(other)


async def test_writes_cas_idempotency_and_admin_boundary(client, session):
    _, version, _, _ = await source(session)
    first = await create(client, version)
    assert first.status_code == 201, first.text
    row = first.json()
    assert (await create(client, version)).json()["collection_id"] == row["collection_id"]
    conflict = await create(client, version, topics=["DIFFERENT"])
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    other = await create(client, version, who="bob")
    assert other.status_code == 201 and other.json()["collection_id"] != row["collection_id"]
    assert (await publish(client, row, who="bob")).status_code == 404
    assert (await publish(client, row)).status_code == 200
    assert (await publish(client, row, key="stale")).json()["error"]["code"] == "collection_revision_conflict"
    assert (await publish(client, {**row, "revision": 2})).json()["error"]["code"] == "idempotency_conflict"
    admin = await client.post(f"{BASE}/{row['collection_id']}/withdraw",
        headers={**actor_headers("admin", role="admin"), "Idempotency-Key": "withdraw"},
        json={"expected_revision": 2})
    assert admin.status_code == 200 and admin.json()["revision"] == 3
    replay = await publish(client, row)
    assert replay.status_code == 200 and replay.json()["publication"] == "withdrawn"
    assert replay.json()["operation_revision"] == 2
    foreign = await client.post(f"{BASE}/{row['collection_id']}/withdraw",
        headers={**actor_headers("admin", role="admin", organization_id="foreign"), "Idempotency-Key": "withdraw"},
        json={"expected_revision": 3})
    assert foreign.status_code == 404


async def test_fixed_pages_caller_scope_and_new_arrivals(client, session):
    _, version, _, _ = await source(session)
    ids = []
    for i in range(3):
        created = (await create(client, version, key=str(i))).json()
        assert (await publish(client, created, key=str(i))).status_code == 200
        ids.append(created["collection_id"])
    first = (await snapshot(client, limit=1)).json()
    added = (await create(client, version, key="new")).json()
    await publish(client, added, key="new")
    result, page = [], first
    while not page["complete"]:
        result.extend(c["collection_id"] for c in page["collections"])
        response = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=page["next_cursor"])
        assert response.status_code == 200, response.text
        page = response.json()
    assert sorted(result) == sorted(ids) and page["total"] == 3
    for field in ("created_at", "valid_until", "registry_revision", "first_cursor", "terminal_cursor"):
        assert page[field] == first[field]
    another = (await snapshot(client)).json()
    for kwargs in ({"who": "mallory"}, {"org": "wrong-org"}, {"scope": "other-scope"},
                   {"cursor": another["terminal_cursor"]}):
        args = {"snapshot_id": first["snapshot_id"], "cursor": first["terminal_cursor"], **kwargs}
        assert (await snapshot(client, **args)).status_code == 410
    assert (await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"], limit=2)).status_code == 400
    await session.execute(update(CollectionCatalogSnapshot).where(CollectionCatalogSnapshot.id == first["snapshot_id"])
        .values(valid_until=utcnow()-timedelta(seconds=1)))
    await session.commit()
    expired = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"])
    assert expired.json()["error"]["code"] == "catalog_snapshot_expired"


async def test_expired_snapshot_wrong_binding_is_invalid_not_expired(client, session):
    """Wrong caller/scope/cursor binding wins over expiry, with no revoked IDs.

    Reviewer counterexample: an expired snapshot plus a cursor that never
    belonged to it must stay the generic catalog_snapshot_invalid, not reveal
    that the snapshot is merely expired.
    """
    _, version, _, _ = await source(session)
    collection = (await create(client, version)).json()
    await publish(client, collection)
    first = (await snapshot(client, limit=1)).json()
    await session.execute(update(CollectionCatalogSnapshot).where(CollectionCatalogSnapshot.id == first["snapshot_id"])
        .values(valid_until=utcnow()-timedelta(seconds=1)))
    await session.commit()
    wrong_cursor = await snapshot(client, snapshot_id=first["snapshot_id"], cursor="incorrect-cursor")
    assert wrong_cursor.status_code == 410
    assert wrong_cursor.json()["error"]["code"] == "catalog_snapshot_invalid"
    assert "revoked_collection_ids" not in wrong_cursor.json()
    for kwargs in ({"who": "mallory"}, {"org": "other-org"}, {"scope": "other-scope"}):
        denied = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"], **kwargs)
        assert denied.status_code == 410, denied.text
        assert denied.json()["error"]["code"] == "catalog_snapshot_invalid", denied.text
        assert "revoked_collection_ids" not in denied.json()
    # Only a legitimately bound caller+cursor may learn the snapshot expired.
    expired = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"])
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "catalog_snapshot_expired"


async def test_withdrawn_ancestor_invalidates_reads_and_terminal_proof(client, session):
    ancestor, _, _, _ = await source(session)
    resource, version, _, _ = await source(session, "copy-owner")
    resource.copied_from = ancestor.id
    await session.commit()
    collection = (await create(client, version, who="copy-owner")).json()
    await publish(client, collection, who="copy-owner")
    first = (await snapshot(client)).json()
    await session.execute(update(Resource).where(Resource.id == ancestor.id).values(publication="private"))
    await session.commit()
    terminal = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"])
    assert terminal.status_code == 410 and terminal.json()["error"]["code"] == "catalog_snapshot_invalid"
    assert (await snapshot(client)).json()["total"] == 0
    assert (await client.get(f"{BASE}/{collection['collection_id']}", headers=actor_headers("third"))).status_code == 404


async def test_unready_indexes_remain_enumerable_without_content_completeness(client, session):
    _, version, job, _ = await source(session)
    collection = (await create(client, version)).json()
    await publish(client, collection)
    first = (await snapshot(client)).json()
    await session.execute(update(ParseJob).where(ParseJob.id == job.id).values(index_status="failed", index_generation=1))
    await session.commit()
    changed = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"])
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "catalog_snapshot_changed"
    after = (await snapshot(client)).json()
    assert after["total"] == 1 and after["index_readiness"][collection["collection_id"]] == "unavailable"
    assert after["collections"][0]["index_revision"] != first["collections"][0]["index_revision"]
    assert not after["content_snapshot_complete"]
    other = (await create(client, version, key="unready")).json()
    assert (await publish(client, other, key="unready")).status_code == 409


async def test_replace_retracts_and_members_do_not_follow_new_parse(client, session):
    _, version, _, _ = await source(session)
    created = (await create(client, version)).json()
    await publish(client, created)
    response = await client.put(f"{BASE}/{created['collection_id']}",
        json={**body(version), "expected_revision": 2, "topics": ["revised"]},
        headers={**actor_headers(), "Idempotency-Key": "replace"})
    assert response.status_code == 200 and response.json()["publication"] == "draft"
    assert (await snapshot(client)).json()["total"] == 0
    pin = await session.scalar(select(CollectionMember).where(CollectionMember.collection_id == created["collection_id"]))
    await session.execute(update(ResourceVersion).where(ResourceVersion.id == version.id).values(parse_job_id=None))
    await session.commit()
    assert pin.parse_job_id is not None
    assert (await client.get(f"{BASE}/{created['collection_id']}", headers=actor_headers())).status_code == 404


async def test_internal_producer_requires_service_and_true_caller(client):
    assert (await client.get(INTERNAL, params={"scope_id": "s"})).status_code == 401
    headers = {**internal(), **actor_headers("bob")}
    assert (await client.get(INTERNAL, params={"scope_id": "s"}, headers=headers)).status_code == 403
    headers = internal()
    del headers["X-DDP-Caller-Scope"]
    assert (await client.get(INTERNAL, params={"scope_id": "s"}, headers=headers)).status_code == 401


async def test_source_republication_does_not_revive_an_old_snapshot(client, session):
    ancestor, _, _, _ = await source(session)
    resource, version, _, _ = await source(session)
    resource.copied_from = ancestor.id
    await session.commit()
    collection = (await create(client, version)).json()
    await publish(client, collection)
    first = (await snapshot(client)).json()
    await session.execute(update(Resource).where(Resource.id == ancestor.id).values(publication="private"))
    await session.commit()
    await session.execute(update(Resource).where(Resource.id == ancestor.id).values(publication="published"))
    await session.commit()
    stale = await snapshot(client, snapshot_id=first["snapshot_id"], cursor=first["terminal_cursor"])
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "catalog_snapshot_changed"


async def test_catalog_limits_count_only_visible_authorized_metadata(client, session, monkeypatch):
    from ddp_corpus import catalog
    _, version, _, _ = await source(session)
    created = (await create(client, version)).json()
    await publish(client, created)
    # Bound the actual disclosed bytes, rather than allowing metadata size times row count
    # to defeat the otherwise bounded page interface.
    monkeypatch.setattr(catalog, "MAX_SNAPSHOT_BYTES", 20)
    assert (await snapshot(client)).status_code == 507
    # An unauthorized published-but-withdrawn source cannot trigger that error for a reader.
    await session.execute(update(Resource).where(Resource.id == version.resource_id).values(publication="private"))
    await session.commit()
    empty = await snapshot(client)
    assert empty.status_code == 200 and empty.json()["total"] == 0


def service(org=ORG, who="control-api"):
    return actor_headers(who, kind="service", organization_id=org, role="admin")


async def test_node_published_snapshot_is_service_only_and_scope_is_server_derived(
        client, session, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", NODE)
    _, version, _, _ = await source(session)
    collection = (await create(client, version)).json()
    await publish(client, collection)
    assert (await client.get(PEER)).status_code == 401
    assert (await client.get(PEER, headers=actor_headers("bob"))).status_code == 403

    first = await client.get(PEER, headers=service(), params={"scope_id": "attacker-chosen"})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["scope_id"] == "peer-directory"          # fixed server-side
    assert page["origin_node_id"] == NODE
    assert page["total"] == 1 and page["complete"] is False
    descriptor = page["collections"][0]
    assert descriptor["collection_id"] == collection["collection_id"]
    assert descriptor["origin_node_id"] == NODE

    # The snapshot binds the authenticated service identity: another service
    # actor cannot continue someone else's cursor, and no request parameter can
    # pick the caller scope.
    foreign = await client.get(PEER, headers=service(org="other-org"),
        params={"snapshot_id": page["snapshot_id"], "cursor": page["first_cursor"]})
    assert foreign.status_code == 410 and foreign.json()["error"]["code"] == "catalog_snapshot_invalid"

    terminal = await client.get(PEER, headers=service(),
        params={"snapshot_id": page["snapshot_id"], "cursor": page["terminal_cursor"]})
    assert terminal.status_code == 200
    assert terminal.json()["complete"] and terminal.json()["next_cursor"] is None
    assert terminal.json()["collections"] == []

    added = (await create(client, version, key="peer-arrival")).json()
    await publish(client, added, key="peer-arrival")
    old = await client.get(PEER, headers=service(),
        params={"snapshot_id": page["snapshot_id"], "cursor": page["first_cursor"]})
    assert old.json()["total"] == 1 and added["collection_id"] not in json.dumps(old.json())


async def test_node_published_snapshot_stable_pages_total_and_limit(client, session, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", NODE)
    _, version, _, _ = await source(session)
    ids = []
    for i in range(2):
        created = (await create(client, version, key=f"peer-{i}")).json()
        assert (await publish(client, created, key=f"peer-{i}")).status_code == 200
        ids.append(created["collection_id"])
    first = (await client.get(PEER, headers=service(), params={"limit": 1})).json()
    assert first["total"] == 2 and first["complete"] is False and len(first["collections"]) == 1
    collected = [c["collection_id"] for c in first["collections"]]
    page = first
    while not page["complete"]:
        response = await client.get(PEER, headers=service(),
            params={"snapshot_id": first["snapshot_id"], "cursor": page["next_cursor"]})
        assert response.status_code == 200, response.text
        page = response.json()
        collected.extend(c["collection_id"] for c in page["collections"])
    assert sorted(collected) == sorted(ids)
    for field in ("created_at", "valid_until", "registry_revision", "first_cursor", "terminal_cursor", "total"):
        assert page[field] == first[field]
    changed = await client.get(PEER, headers=service(),
        params={"snapshot_id": first["snapshot_id"], "cursor": first["first_cursor"], "limit": 2})
    assert changed.status_code == 400


async def test_node_published_snapshot_withdrawal_is_not_completion(client, session, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", NODE)
    _, version, _, _ = await source(session)
    collection = (await create(client, version)).json()
    await publish(client, collection)
    page = (await client.get(PEER, headers=service())).json()
    withdrawn = await client.post(f"{BASE}/{collection['collection_id']}/withdraw",
        headers={**actor_headers(), "Idempotency-Key": "peer-withdraw"},
        json={"expected_revision": 2})
    assert withdrawn.status_code == 200, withdrawn.text
    revoked = await client.get(PEER, headers=service(),
        params={"snapshot_id": page["snapshot_id"], "cursor": page["terminal_cursor"]})
    assert revoked.status_code == 410
    assert revoked.json()["error"]["code"] == "catalog_snapshot_invalid"
    assert collection["collection_id"] in revoked.json()["revoked_collection_ids"]
    after = (await client.get(PEER, headers=service())).json()
    assert after["total"] == 0 and after["collections"] == []


async def test_node_published_snapshot_fails_closed_without_node_identity(client, monkeypatch):
    monkeypatch.setattr(settings, "bundle_node_id", "")
    response = await client.get(PEER, headers=service())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "node_identity_unavailable"
