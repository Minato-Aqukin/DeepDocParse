"""T83: a source collection can forbid its evidence from reaching a generation-only node.

Executor side: the source refuses admission (403 egress_denied, nothing persisted) when
any node downstream of its outbound edges in the approved plan, other than the root
coordinator, is not in the collection's `onward_recipients`. Coordinator side: planning
never selects a delegated generator a selected source forbids, sends that node no probe,
and reports `answer_reason=source_policy_denied` instead of "no model".
"""
import pytest
import respx
from sqlalchemy import func, select

from conftest import ORG
from ddp_core.application.plans import task_plan_digest
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationAdmission, FederationExecution
from ddp_corpus.main import app
from node_credentials_fixture import install
from test_federation_admissions import admission_body, peer, post_admission
from test_federation_answer_delegation import (
    answer_step,
    install_peer,
    mock_gateway_not_ready,
    peer_with_excerpt,
    ready_document,
)
from test_federation_probes import NODE, configure_federation, indexed_source, publish_collection
from test_federation_tasks import (
    PEER_NODE,
    approve_task,
    calls_to,
    create_intent,
    exploration,
    member,
    plan_task,
    probe_keys,
    scope_manifest,
    submit_task,
    task_spec,
)

C_NODE = "node-" + "c" * 48
RELAY = "node-" + "d" * 48
OTHER_SOURCE = "node-" + "e" * 48


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)
    monkeypatch.setattr(settings, "chat_url", "")
    monkeypatch.setattr(settings, "chat_model", "")


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def b_to_c_plan(collection_id, *, key, relay=(), other_source=False, reexport=False):
    """Approved plan: this node retrieves, the coordinator fuses, C generates from the fusion."""
    body = admission_body(key=key, collection_id=collection_id,
                          execution_mode="trusted_federation")
    plan = body["plan"]
    coordinator = plan["root_coordinator_node_id"]
    plan["steps"] += [
        {"step_id": "fuse-1", "operation": "fuse", "executor_node_id": coordinator,
         "depends_on": ["retrieve-1"], "fixed_inputs": ["query"]},
        {"step_id": "answer-1", "operation": "answer", "executor_node_id": C_NODE,
         "depends_on": ["fuse-1"], "fixed_inputs": ["query"]},
    ]
    evidence = {"edge_id": "edge-evidence", "from_node_id": NODE, "to_node_id": coordinator,
                "payload_kind": "evidence_excerpts", "retention": "temporary",
                "authorised_by": f"source:{NODE}"}
    if relay:
        evidence["relay_via"] = list(relay)
    edges = [
        {"edge_id": "edge-query", "from_node_id": coordinator, "to_node_id": NODE,
         "payload_kind": "query_text", "retention": "temporary",
         "authorised_by": f"source:{coordinator}"},
        evidence,
        {"edge_id": "edge-answer-1", "from_node_id": coordinator, "to_node_id": C_NODE,
         "payload_kind": "evidence_excerpts", "retention": "temporary",
         "authorised_by": f"relay:{coordinator}"},
    ]
    if other_source:
        # The question also goes to a second source; a query is not this source's content.
        edges.append({"edge_id": "edge-query-2", "from_node_id": coordinator,
                      "to_node_id": OTHER_SOURCE, "payload_kind": "query_text",
                      "retention": "temporary", "authorised_by": f"source:{coordinator}"})
    if reexport:
        # C forwards its generated answer (derived from this source) to another node.
        edges.append({"edge_id": "edge-reexport", "from_node_id": C_NODE,
                      "to_node_id": OTHER_SOURCE, "payload_kind": "answer_text",
                      "retention": "temporary", "authorised_by": f"source:{C_NODE}"})
    plan["data_edges"] = edges
    plan["budget"]["max_hops"] = 8
    plan["plan_digest"] = task_plan_digest(plan)
    consent = body["execution_consent"]
    consent["plan_digest"] = plan["plan_digest"]
    consent["allowed_recipients"] = sorted(
        {NODE, coordinator, C_NODE, *relay, *([OTHER_SOURCE] if other_source or reexport else [])})
    consent["allowed_edges"] = [edge["edge_id"] for edge in edges]
    return body


async def persisted(session):
    admissions = await session.scalar(select(func.count()).select_from(FederationAdmission))
    executions = await session.scalar(select(func.count()).select_from(FederationExecution))
    return admissions, executions


@pytest.mark.parametrize(("policy", "plan_shape", "denied"), [
    (None, {}, None),
    ([C_NODE], {}, None),
    ([], {}, C_NODE),
    ([C_NODE], {"relay": (RELAY,)}, RELAY),
    ([C_NODE], {"other_source": True}, None),
    ([C_NODE], {"reexport": True}, OTHER_SOURCE),
], ids=["no-policy", "c-allowed", "coordinator-only", "relay-not-allowed", "query-to-other-source",
        "derivative-reexport"])
async def test_source_refuses_plans_that_carry_its_evidence_past_its_policy(
        client, actor_client, session, _peer_auth, policy, plan_shape, denied):
    _, version, _, _, _ = await indexed_source(session)
    collection = await publish_collection(actor_client, version, onward_recipients=policy)
    response = await post_admission(peer(client), b_to_c_plan(
        collection["collection_id"], key="onward-" + str(denied), **plan_shape))
    if denied is None:
        assert response.status_code == 201, response.text
        assert response.json()["state"] == "accepted"
        return
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "egress_denied"
    assert denied in error["message"]
    assert await persisted(session) == (0, 0), "a refused admission persists and runs nothing"


async def test_untargeted_retrieval_is_checked_against_every_restricted_collection(
        client, actor_client, session, _peer_auth):
    """Without a collection target the search spans visible published content (fail closed)."""
    _, version, _, _, _ = await indexed_source(session)
    await publish_collection(actor_client, version, onward_recipients=[])
    response = await post_admission(peer(client), b_to_c_plan(None, key="onward-untargeted"))
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "egress_denied"
    assert await persisted(session) == (0, 0)


async def plan_with_local_source(actor_client, session, *, policy):
    """A-local collection (with `policy`) plus a remote source that can also generate."""
    _, version, _, _, _ = await indexed_source(session)
    local = await publish_collection(actor_client, version, onward_recipients=policy)
    manifest = scope_manifest([member(local["collection_id"], NODE),
                               member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    return root, await plan_task(actor_client, root)


@respx.mock
async def test_coordinator_never_delegates_generation_a_source_forbids(
        actor_client, session, monkeypatch):
    mock_gateway_not_ready()
    remote = peer_with_excerpt()
    remote.can_generate = True
    remote.answer_document = ready_document()
    install_peer(monkeypatch, remote)
    root, plan = await plan_with_local_source(actor_client, session, policy=[])

    assert answer_step(plan) is None
    assert plan["budget"]["max_generation_tokens"] == 0
    assert not any(edge["to_node_id"] == PEER_NODE and edge["payload_kind"] == "evidence_excerpts"
                   for edge in plan["data_edges"]), "the local source's evidence never goes out"
    assert not [key for key in probe_keys(remote) if key.startswith("answer-probe:")], \
        "a forbidden generator is not even asked whether it can generate"
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    result = (await submit_task(actor_client, root, plan["plan_digest"], "onward-denied")
              ).json()["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "source_policy_denied"
    assert result["evidence"], "retrieval still returns evidence; only generation is withheld"
    assert calls_to(remote, "/admissions") == 1, "only the remote retrieval is admitted"


@respx.mock
async def test_coordinator_delegates_when_the_source_lists_the_generator(
        actor_client, session, monkeypatch):
    mock_gateway_not_ready()
    remote = peer_with_excerpt()
    remote.can_generate = True
    remote.answer_document = ready_document()
    install_peer(monkeypatch, remote)
    _, plan = await plan_with_local_source(actor_client, session, policy=[PEER_NODE])
    step = answer_step(plan)
    assert step is not None and step["executor_node_id"] == PEER_NODE
