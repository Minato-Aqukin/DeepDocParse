"""T80: HTTP idempotency domains never cross principals, organizations or issuers."""

import pytest

from conftest import ACTOR, ORG, actor_headers, drain_tasks
from ddp_bundle_fixture import sample_bundle
from ddp_corpus.config import settings
from ddp_corpus.main import app
from ddp_corpus.models import new_id
from node_credentials_fixture import (
    OTHER_KEY, OTHER_NODE_ID, PEER_NODE_ID, caller, install, key_for, node_id_for,
    public_key_b64, trust_record,
)
from test_federation_pg import pg_stack  # noqa: F401 — opt-in real-PG HTTP fixture
from test_federation_admissions import admission_body, exec_constraints, post_admission
from test_federation_probes import NODE, configure_federation, indexed_source
from test_federation_tasks import execution_consent, exploration, member, scope_manifest, task_spec


@pytest.fixture(autouse=True)
def federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")


async def intent(client, actor, org, key, query):
    body = {"task_spec": task_spec(query=query), "exploration_consent": exploration()}
    return await client.post(
        "/api/v1/task-intents", json=body,
        headers={**actor_headers(actor, organization_id=org), "Idempotency-Key": key})


async def approve(client, actor, org, root):
    headers = actor_headers(actor, organization_id=org)
    response = await client.post("/api/v1/task-plans", json={"root_task_id": root},
                                 headers=headers)
    assert response.status_code == 200, response.text
    plan = response.json()
    response = await client.post(
        f"/api/v1/task-plans/{root}/approve", headers=headers,
        json={"plan_digest": plan["plan_digest"],
              "execution_consent": execution_consent(plan["plan_digest"], recipients=(NODE,))})
    assert response.status_code == 200, response.text
    return plan["plan_digest"]


async def submit(client, actor, org, root, digest, key):
    return await client.post(
        "/api/v1/tasks", json={"root_task_id": root, "plan_digest": digest},
        headers={**actor_headers(actor, organization_id=org), "Idempotency-Key": key})


async def test_intents_and_submits_scope_keys_to_principal_and_organization(client, app_state, session):
    for org in (ORG, "org-other"):
        resource, version, _, document, _ = await indexed_source(session)
        resource.organization_id = document.organization_id = org
        await session.commit()
        headers = {**actor_headers(organization_id=org), "Idempotency-Key": "source"}
        created = await client.post("/api/v1/collections", headers=headers, json={
            "name": "Domain source", "licence": "CC-BY-4.0", "version_ids": [version.id]})
        assert created.status_code == 201, created.text
        collection = created.json()
        published = await client.post(
            f"/api/v1/collections/{collection['collection_id']}/publish", headers=headers,
            json={"expected_revision": collection["revision"]})
        assert published.status_code == 200, published.text
    domains = [(ACTOR, ORG, "retrieval target alice"), ("actor-bob", ORG, "retrieval target bob"),
               (ACTOR, "org-other", "retrieval target other organization")]
    roots = []
    for actor, org, query in domains:
        created = await intent(client, actor, org, "shared-intent", query)
        assert created.status_code == 201, created.text
        root = created.json()["root_task_id"]
        assert root not in roots
        roots.append(root)
        replay = await intent(client, actor, org, "shared-intent", query)
        assert replay.status_code == 201 and replay.json() == created.json()
        key_headers = {**actor_headers("owned-api-key", kind="api_key", organization_id=org),
                       "X-DDP-User": actor, "Idempotency-Key": "shared-intent"}
        key_replay = await client.post(
            "/api/v1/task-intents", headers=key_headers,
            json={"task_spec": task_spec(query=query), "exploration_consent": exploration()})
        assert key_replay.status_code == 201 and key_replay.json() == created.json()
        conflict = await intent(client, actor, org, "shared-intent", query + " changed")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        digest = await approve(client, actor, org, root)
        accepted = await submit(client, actor, org, root, digest, "shared-submit")
        assert accepted.status_code == 202, accepted.text
        replay = await submit(client, actor, org, root, digest, "shared-submit")
        assert replay.status_code == 200
        assert replay.json()["root_task_id"] == root
        key_replay = await client.post(
            "/api/v1/tasks", headers={**key_headers, "Idempotency-Key": "shared-submit"},
            json={"root_task_id": root, "plan_digest": digest})
        assert key_replay.status_code == 200 and key_replay.json()["root_task_id"] == root
        changed = await submit(client, actor, org, root, "sha256:" + "0" * 64, "shared-submit")
        assert changed.status_code == 409
        second = await intent(client, actor, org, "second-intent", query + " second")
        second_root = second.json()["root_task_id"]
        second_digest = await approve(client, actor, org, second_root)
        conflict = await submit(client, actor, org, second_root, second_digest, "shared-submit")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
    await drain_tasks(app_state)
    for (actor, org, _), root in zip(domains, roots, strict=True):
        status = await client.get(f"/api/v1/tasks/{root}",
                                  headers=actor_headers(actor, organization_id=org))
        assert status.status_code == 200
        assert status.json()["root_task_id"] == root
        assert status.json()["status"] == "succeeded", status.text
        delivery_id = status.json()["delivery_id"]
        manifest = status.json()["result"]["result_manifest_digest"]
        headers = {**actor_headers(actor, organization_id=org), "Idempotency-Key": "shared-ack"}
        ack_path = f"/api/v1/deliveries/{delivery_id}/ack"
        ack = await client.post(ack_path, headers=headers,
                                json={"result_manifest_digest": manifest})
        assert ack.status_code == 200 and ack.json()["state"] == "confirmed"
        assert ack.json()["root_task_id"] == root
        replay = await client.post(ack_path, headers=headers,
                                   json={"result_manifest_digest": manifest})
        assert replay.status_code == 200 and replay.json() == ack.json()
        changed = await client.post(ack_path, headers=headers,
                                    json={"result_manifest_digest": "sha256:" + "0" * 64})
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "idempotency_conflict"
        for other_actor, other_org, _ in domains:
            if (other_actor, other_org) != (actor, org):
                denied = await client.post(
                    ack_path, json={"result_manifest_digest": manifest},
                    headers={**actor_headers(other_actor, organization_id=other_org),
                             "Idempotency-Key": "shared-ack"})
                assert denied.status_code == 404


async def test_admissions_scope_keys_to_issuer_and_organization(client, app_state, monkeypatch):
    trust = install(monkeypatch, app, node_id=NODE, organization_id=ORG)
    third_key = key_for("ddp-test-domain-other-org")
    third_node = node_id_for("ddp-test-domain-other-org")
    trust.records[third_node] = trust_record(
        third_node, public_key_b64(third_key), organization_id="org-other",
        authority_node_id=NODE)
    domains = [
        (caller(client, audience_node_id=NODE, organization_id=ORG), PEER_NODE_ID),
        (caller(client, audience_node_id=NODE, organization_id=ORG,
                issuer_node_id=OTHER_NODE_ID, key=OTHER_KEY), OTHER_NODE_ID),
        (caller(client, audience_node_id=NODE, organization_id="org-other",
                issuer_node_id=third_node, key=third_key), third_node),
    ]
    receipts = []
    for index, (peer, issuer) in enumerate(domains):
        body = admission_body(key="root-1:retrieve-1", root_task_id="root-1",
                              coordinator=issuer, query=f"retrieval target {index}")
        accepted = await post_admission(peer, body)
        assert accepted.status_code == 201, accepted.text
        receipt = accepted.json()
        assert receipt["issuer_node_id"] == issuer
        assert receipt["idempotency_key"] == "root-1:retrieve-1"
        assert receipt["admission_id"] not in [item["admission_id"] for item in receipts]
        assert receipt["executor_task_id"] not in [item["executor_task_id"] for item in receipts]
        receipts.append(receipt)
        replay = await post_admission(peer, body)
        assert replay.status_code == 200 and replay.json() == receipt
        lookup = await peer.post(
            "/api/v1/federation/admissions/lookup",
            json_body={"idempotency_key": body["idempotency_key"]},
            constraints=exec_constraints(body))
        assert lookup.status_code == 200 and lookup.json() == receipt
        changed = admission_body(key=body["idempotency_key"], root_task_id="root-1",
                                 coordinator=issuer, query="changed body")
        conflict = await post_admission(peer, changed)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
    # The issuer owns the business key, not the remote user subject.
    other_subject = caller(client, audience_node_id=NODE, organization_id=ORG,
                           subject="another-user-on-same-issuer")
    body = admission_body(key="root-1:retrieve-1", root_task_id="root-1",
                          coordinator=PEER_NODE_ID, query="retrieval target 0")
    replay = await post_admission(other_subject, body)
    assert replay.status_code == 200 and replay.json() == receipts[0]


async def test_bundle_import_keys_scope_to_principal_and_organization(client):
    domains = [(ACTOR, ORG, "alice.pdf"), ("actor-bob", ORG, "bob.pdf"),
               (ACTOR, "org-other", "other.pdf")]
    resources = []
    for actor, org, filename in domains:
        headers = {**actor_headers(actor, organization_id=org),
                   "Idempotency-Key": "same-import-key", "Content-Type": "application/zip"}
        payload = sample_bundle(filename=filename)
        imported = await client.post("/api/bundles/import", headers=headers, content=payload)
        assert imported.status_code == 201, imported.text
        result = imported.json()
        assert result["resource_id"] not in resources
        resources.append(result["resource_id"])
        assert result["source"]["filename"] == filename
        replay = await client.post("/api/bundles/import", headers=headers, content=payload)
        assert replay.status_code == 200 and replay.json() == result
        conflict = await client.post("/api/bundles/import", headers=headers,
                                     content=sample_bundle(filename="changed.pdf"))
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        evidence_path = (f"/api/resources/{result['resource_id']}/versions/"
                         f"{result['source_version_id']}/bundle/evidence")
        assert (await client.get(evidence_path, headers=headers)).status_code == 200
        for other_actor, other_org, _ in domains:
            if (other_actor, other_org) != (actor, org):
                denied = await client.get(evidence_path, headers=actor_headers(
                    other_actor, organization_id=other_org))
                assert denied.status_code == 404
    # User-owned API keys share the principal's import domain.
    headers = {**actor_headers("key-alice", kind="api_key"),
               "X-DDP-User": ACTOR, "Idempotency-Key": "same-import-key",
               "Content-Type": "application/zip"}
    replay = await client.post("/api/bundles/import", headers=headers,
                               content=sample_bundle(filename="alice.pdf"))
    assert replay.status_code == 200 and replay.json()["resource_id"] == resources[0]


async def test_catalog_keys_scope_to_actor_binding_and_organization(client, session):
    versions = {}
    for org in (ORG, "org-other"):
        resource, version, _, document, _ = await indexed_source(session)
        resource.organization_id = document.organization_id = org
        await session.commit()
        versions[org] = version.id
    domains = [(actor_headers(ACTOR), "Alice"),
               (actor_headers("actor-bob"), "Bob"),
               (actor_headers(ACTOR, organization_id="org-other"), "Other organization"),
               ({**actor_headers("key-alice", kind="api_key"), "X-DDP-User": ACTOR}, "API key")]
    collections = []
    for headers, name in domains:
        headers = {**headers, "Idempotency-Key": "same-create-key"}
        body = {"name": name, "licence": "CC-BY-4.0",
                "version_ids": [versions[headers["X-DDP-Organization"]]]}
        created = await client.post("/api/v1/collections", headers=headers, json=body)
        assert created.status_code == 201, created.text
        collection = created.json()
        assert collection["collection_id"] not in collections
        collections.append(collection["collection_id"])
        replay = await client.post("/api/v1/collections", headers=headers, json=body)
        assert replay.status_code == 201
        assert replay.json()["collection_id"] == collection["collection_id"]
        assert replay.json()["name"] == name
        assert replay.json()["revision"] == collection["revision"] == 1
        conflict = await client.post("/api/v1/collections", headers=headers,
                                     json={**body, "name": "Changed"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_pg_submit_domains_follow_migrated_constraints(pg_stack):  # noqa: F811 — imported fixture
    client, factory, state = pg_stack
    prefix = new_id()
    async with factory() as session:
        _, version, _, _, _ = await indexed_source(session)
        headers = {**actor_headers(), "Idempotency-Key": f"pg-source-{prefix}"}
        created = await client.post("/api/v1/collections", headers=headers, json={
            "name": "PG domain source", "licence": "CC-BY-4.0", "version_ids": [version.id]})
        assert created.status_code == 201, created.text
        collection = created.json()
        published = await client.post(
            f"/api/v1/collections/{collection['collection_id']}/publish", headers=headers,
            json={"expected_revision": collection["revision"]})
        assert published.status_code == 200, published.text
    accepted_roots = []
    for actor in (ACTOR, "actor-bob"):
        created = await client.post(
            "/api/v1/task-intents", headers={
                **actor_headers(actor), "Idempotency-Key": f"pg-intent-{actor}-{prefix}"},
            json={"task_spec": task_spec(scope="federation_public", query="retrieval target"),
                  "exploration_consent": exploration(),
                  "scope_manifest": scope_manifest([member(collection["collection_id"])])})
        assert created.status_code == 201, created.text
        root = created.json()["root_task_id"]
        digest = await approve(client, actor, ORG, root)
        accepted = await submit(client, actor, ORG, root, digest, f"pg-shared-submit-{prefix}")
        assert accepted.status_code == 202, accepted.text
        accepted_roots.append(root)
        replay = await submit(client, actor, ORG, root, digest, f"pg-shared-submit-{prefix}")
        assert replay.status_code == 200 and replay.json()["root_task_id"] == root
    assert accepted_roots[0] != accepted_roots[1]
    await drain_tasks(state)
    for actor, root in zip((ACTOR, "actor-bob"), accepted_roots, strict=True):
        status = await client.get(f"/api/v1/tasks/{root}", headers=actor_headers(actor))
        assert status.status_code == 200 and status.json()["status"] == "succeeded", status.text


async def test_admin_submit_uses_the_root_owners_key_domain(client, app_state, session):
    """An administrator submitting another user's root writes the key into the owner's
    domain, so a key the owner already used for a different root is a 409 conflict."""
    _, version, _, _, _ = await indexed_source(session)
    headers = {**actor_headers(organization_id=ORG), "Idempotency-Key": "admin-domain-source"}
    created = await client.post("/api/v1/collections", headers=headers, json={
        "name": "Admin domain source", "licence": "CC-BY-4.0", "version_ids": [version.id]})
    assert created.status_code == 201, created.text
    published = await client.post(
        f"/api/v1/collections/{created.json()['collection_id']}/publish", headers=headers,
        json={"expected_revision": created.json()["revision"]})
    assert published.status_code == 200, published.text
    first = (await intent(client, ACTOR, ORG, "owner-intent-1", "retrieval target one")).json()
    second = (await intent(client, ACTOR, ORG, "owner-intent-2", "retrieval target two")).json()
    first_digest = await approve(client, ACTOR, ORG, first["root_task_id"])
    second_digest = await approve(client, ACTOR, ORG, second["root_task_id"])
    accepted = await submit(client, ACTOR, ORG, first["root_task_id"], first_digest, "owner-key")
    assert accepted.status_code == 202, accepted.text

    admin = {**actor_headers("actor-admin", role="admin", organization_id=ORG),
             "Idempotency-Key": "owner-key"}
    conflict = await client.post(
        "/api/v1/tasks", headers=admin,
        json={"root_task_id": second["root_task_id"], "plan_digest": second_digest})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    fresh = await client.post(
        "/api/v1/tasks", headers={**admin, "Idempotency-Key": "admin-key"},
        json={"root_task_id": second["root_task_id"], "plan_digest": second_digest})
    assert fresh.status_code == 202, fresh.text
    replay = await submit(client, ACTOR, ORG, second["root_task_id"], second_digest, "admin-key")
    assert replay.status_code == 200, "the owner replays the key the administrator used"
    await drain_tasks(app_state)
