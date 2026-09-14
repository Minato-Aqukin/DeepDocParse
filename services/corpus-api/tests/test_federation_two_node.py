"""GENUINE two-node federation acceptance for the P5 plane.

Node A (coordinator) is the repo's standard in-process pytest app; node B
(executor) is a **real uvicorn subprocess with its own SQLite file database**
(see `federation_two_node.py`). Every A -> B call in every scenario below goes
through the production `PeerClient`/`PeerDirectory` code path over real
loopback TCP, with the real service bearer, peer token, actor headers and
target-node header. The peer protocol is never skipped: B's request log is the
ground truth for "what did the peer actually receive".

Scenario map (task brief -> test):

1. B-only evidence                          test_b_only_evidence_over_real_peer_http
2. Split evidence + duplicate content       test_split_evidence_keeps_origins_and_identity
3. Partial coverage (registered C, down)    test_unreachable_registered_peer_stays_in_denominator
4. Exploration consent gate (zero traffic)  test_local_only_exploration_gate_sends_zero_traffic
5. Idempotency replay / conflict            test_submit_replay_is_one_logical_task,
                                            test_idempotency_key_reuse_across_tasks_conflicts,
                                            test_same_task_tampered_plan_digest_is_rejected
6. Peer auth failure                        test_wrong_peer_token_fails_closed_without_fabrication
7. Fast vs exhaustive completeness          test_fast_mode_never_claims_complete,
                                            test_exhaustive_complete_requires_sealed_enumeration
8. Cancel / resume                          test_cancel_after_success_and_resume_after_peer_failure
"""
import json

import pytest
from conftest import ORG, drain_tasks
from ddp_core.application import plans
from ddp_core.bundle import digest as bundle_digest
from ddp_corpus import upstream
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationAdmission, FederationProbe
from ddp_corpus.main import app as corpus_app
from federation_two_node import (
    NODE_A,
    NODE_B,
    NODE_C,
    PEER_TOKEN_A,
    TwoNodeFixture,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_federation_probes import indexed_source, publish_collection

EXPIRY = "2030-01-01T00:00:00Z"
A_TEXT = "alpha federation keyword fact"
B_TEXT = "beta federation keyword fact"


@pytest.fixture
async def two_node(tmp_path, request):
    # 默认 B 没有模型运行时（诚实的不就绪形态）。`parametrize(..., indirect=True)`
    # 传入一个字符串时，B 会挂上真实的 loopback 模型桩（能力探测与生成都走 HTTP）。
    answer = getattr(request, "param", None)
    fixture = await TwoNodeFixture.create(tmp_path, b_texts=(B_TEXT,),
                                          b_generate_answer=answer)
    try:
        yield fixture
    finally:
        await fixture.stop()


@pytest.fixture(autouse=True)
def _node_a_federation_config(two_node, monkeypatch):
    """Point node A at the real node B endpoint; A is the only in-process node."""
    monkeypatch.setattr(settings, "bundle_node_id", NODE_A)
    monkeypatch.setattr(settings, "federation_peer_token", PEER_TOKEN_A)
    monkeypatch.setattr(settings, "federation_admissions_enabled", True)
    monkeypatch.setattr(settings, "federation_peers", two_node.peers_json())
    monkeypatch.setattr(settings, "federation_allow_loopback", True)

    async def _embed(_http, _text):
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(upstream, "embed_one", _embed)


# --------------------------------------------------------------------------- fixtures


def member(collection_id, node):
    return {"origin_node_id": node, "collection_id": collection_id,
            "operation": "corpus.retrieve"}


def scope_manifest(members, *, enumeration="sealed", unexpanded=()):
    body = {
        "schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": "scope-two-node",
        "caller_scope_hash": "sha256:" + "1" * 64,
        "created_at": "2026-01-01T00:00:00Z", "valid_until": EXPIRY,
        "registry_revision_vector": [
            {"node_id": node, "registry_revision": 1,
             "fetched_at": "2026-01-01T00:00:00Z"}
            for node in (NODE_A, NODE_B, NODE_C)],
        "expanded_members": list(members), "unexpanded_subtrees": list(unexpanded),
        "enumeration_state": enumeration,
    }
    body["manifest_digest"] = plans.digest(
        {key: value for key, value in body.items() if key != "manifest_digest"})
    return body


def exploration(*, recipients=(NODE_B,), egress="listed_nodes", payload=("query_text",),
                budget=None, consent_id="explore-1"):
    return {
        "schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": consent_id,
        "granted_by": "user-1", "granted_at": "2026-01-01T00:00:00Z",
        "valid_until": EXPIRY, "egress_mode": egress,
        "allowed_payload": list(payload), "allowed_recipients": list(recipients),
        "budget": budget or {"max_probe_requests": 8, "max_egress_bytes": 1 << 20},
    }


def execution_consent(plan_digest, *, recipients, edges, consent_id="execute-1"):
    return {
        "schema": "ddp-plan-admission/1#ExecutionConsent", "consent_id": consent_id,
        "plan_digest": plan_digest, "granted_by": "user-1",
        "granted_at": "2026-01-01T00:00:00Z", "valid_until": EXPIRY,
        "allowed_recipients": list(recipients), "allowed_edges": list(edges),
        "output_locations": ["local:workspace-a"], "retention": "temporary",
    }


def task_spec(*, mode="exhaustive_scope", query="federation keyword",
              scope="federation_public", coordinator=NODE_A):
    return {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "rag.answer.cited", "workspace_ref": "workspace-a", "query": query,
        "resource_scope": {"kind": scope, "scope_ref": "scope-two-node"},
        "search_policy": {"mode": mode, "ordering": "local_first"},
        "execution_policy": {"mode": "trusted_federation", "coordinator_ref": coordinator},
        "consent_refs": {"exploration": "explore-1", "execution": None},
        "budget_ref": "budget-1",
    }


# --------------------------------------------------------------------------- HTTP flow


async def create_intent(client, *, spec, consent, manifest=None):
    body = {"task_spec": spec, "exploration_consent": consent}
    if manifest is not None:
        body["scope_manifest"] = manifest
    response = await client.post("/api/v1/task-intents", json=body,
                                 headers={"Idempotency-Key": plans.digest(body)})
    assert response.status_code == 201, response.text
    return response.json()


async def plan_task(client, root):
    response = await client.post("/api/v1/task-plans", json={"root_task_id": root})
    assert response.status_code == 200, response.text
    return response.json()


def execution_recipients(plan):
    nodes = {NODE_A}
    for step in plan["steps"]:
        nodes.add(step["executor_node_id"])
    for edge in plan["data_edges"]:
        nodes.add(edge["from_node_id"])
        nodes.add(edge["to_node_id"])
        nodes.update(edge.get("relay_via") or [])
    return sorted(nodes)


async def approve_task(client, root, plan):
    consent = execution_consent(plan["plan_digest"],
                                recipients=execution_recipients(plan),
                                edges=[edge["edge_id"] for edge in plan["data_edges"]])
    response = await client.post(f"/api/v1/task-plans/{root}/approve",
                                 json={"plan_digest": plan["plan_digest"],
                                       "execution_consent": consent})
    assert response.status_code == 200, response.text
    return response.json()


async def submit_task(client, root, plan_digest, key):
    """POST /tasks：202 = 新受理。跑一轮 A 的持久队列后返回权威状态响应。

    节点 B 没有 worker（真实子进程夹具里 `FEDERATION_EXECUTION_INLINE=true`），
    它的执行仍是请求内完成；A 自己的协调/本地执行走生产队列。
    """
    response = await client.post("/api/v1/tasks", headers={"Idempotency-Key": key},
                                 json={"root_task_id": root, "plan_digest": plan_digest})
    if response.status_code == 202:
        await drain_tasks(corpus_app.state)
        response = await client.get(f"/api/v1/tasks/{root}")
    return response


async def coverage_of(client, root):
    response = await client.get(f"/api/v1/tasks/{root}/coverage")
    assert response.status_code == 200, response.text
    return response.json()


def entry_for(coverage, node, collection_id=None):
    for entry in coverage["entries"]:
        key = entry["target_key"]
        if key["origin_node_id"] == node and (
                collection_id is None or key["collection_id"] == collection_id):
            return entry
    raise AssertionError(f"no coverage entry for {node} in {coverage['entries']}")


def probe_keys(two_node):
    """B 收到的探测请求的业务键：`plan:` = 证据探测，`answer-probe:` = 生成能力探测。"""
    return sorted(str(call.get("idempotency_key") or "")
                  for call in two_node.calls_to("/api/v1/federation/probes"))


def count_probe_kinds(two_node):
    keys = probe_keys(two_node)
    return (sum(1 for key in keys if key.startswith("plan:")),
            sum(1 for key in keys if key.startswith("answer-probe:")))


async def start_b_only(actor_client, two_node, *, query="federation keyword",
                       mode="exhaustive_scope", consent=None):
    root, plan = await plan_manifest(
        actor_client, members=[member(two_node.b_seed.collection_id, NODE_B)],
        query=query, mode=mode, consent=consent)
    return root, plan


async def plan_manifest(actor_client, *, members, query, mode="exhaustive_scope",
                        consent=None, enumeration="sealed", unexpanded=()):
    manifest = scope_manifest(members, enumeration=enumeration, unexpanded=unexpanded)
    intent = await create_intent(
        actor_client,
        spec=task_spec(mode=mode, query=query),
        consent=consent or exploration(recipients=(NODE_B,)), manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)
    return root, plan


async def publish_a(actor_client, session, *, texts, key="two-node-a"):
    _resource, version, _job, _document, evidence_rows = await indexed_source(
        session, texts=texts)
    collection = await publish_collection(actor_client, version, key=key)
    return version, evidence_rows, collection


# =========================================================================== 1. B-only


async def test_b_only_evidence_over_real_peer_http(actor_client, session, two_node):
    """A has no matching local doc; the manifest's only target lives on real node B."""
    b = two_node.b_seed
    root, plan = await start_b_only(actor_client, two_node)
    # Planning already crossed the network: an evidence probe plus the
    # generation-readiness probe (local generation is not configured in this
    # harness, so delegation is considered), then one evidence-set read.
    assert [call["status"] for call in two_node.calls_to("/api/v1/federation/probes")] \
        == [201, 201]
    assert count_probe_kinds(two_node) == (1, 1)
    assert two_node.calls_containing("/evidence-sets/") != []
    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "b-only")
    assert response.status_code == 200, response.text
    status = response.json()

    assert status["status"] == "succeeded"
    assert status["retrieval_completeness"] == "complete"
    result = status["result"]
    # No generation capability locally: evidence without a fabricated answer.
    assert result["answer"] is None
    assert result["answer_reason"] == "local_model_missing"

    evidence = result["evidence"]
    assert len(evidence) == 1
    item = evidence[0]
    assert item["origin_node_id"] == NODE_B
    assert item["authority_node_id"] == NODE_B
    assert item["evidence_id"] == b.evidence_id
    assert item["resource_id"] == b.resource_id
    assert item["source_version_id"] == b.version_id
    # The excerpt is not copied across the wire; its digest commits to the real
    # text seeded on B, and the locator is B's real parsed locator.
    assert item["excerpt_digest"] == bundle_digest(b.text.encode("utf-8"))
    assert item["locator"] == {"kind": "page_block", "physical_page_index": 0,
                               "seq": 0, "bbox": [10, 20, 300, 60],
                               "page_size": {"width": 612, "height": 792}}

    coverage = await coverage_of(actor_client, root)
    entry = entry_for(coverage, NODE_B)
    assert entry["state"] == "succeeded"
    assert entry["actual_index_revision"] == b.index_revision

    # Peer ground truth: exactly two probes (evidence + generation readiness),
    # one admission, two evidence-set reads (probe receipt + execution receipt).
    # No retry storm, no hidden extra calls.
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2
    assert count_probe_kinds(two_node) == (1, 1)
    assert len(two_node.calls_to("/api/v1/federation/admissions")) == 1
    assert len(two_node.calls_containing("/evidence-sets/")) == 2

    # A persisted the remote receipt, not a fake local probe row.
    probes = list(await session.scalars(select(FederationProbe).where(
        FederationProbe.organization_id == ORG)))
    assert len(probes) == 1
    assert probes[0].target_node_id == NODE_B
    assert probes[0].result_json["result"]["retrieval"]["index_revision"] \
        == b.index_revision


# ===================================================================== 2. split evidence


async def test_split_evidence_keeps_origins_and_identity(actor_client, session, two_node):
    """Same bytes on A and B: both provenance records survive, none is double-counted."""
    b = two_node.b_seed
    version_a, evidence_a, collection_a = await publish_a(
        actor_client, session, texts=(b.text,), key="two-node-a-split")
    root, plan = await plan_manifest(
        actor_client,
        members=[member(collection_a["collection_id"], NODE_A),
                 member(b.collection_id, NODE_B)],
        query="federation keyword")
    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "split")).json()

    assert status["status"] == "succeeded"
    assert status["retrieval_completeness"] == "complete"
    evidence = status["result"]["evidence"]
    assert len(evidence) == 2
    by_origin = {item["origin_node_id"]: item for item in evidence}
    assert set(by_origin) == {NODE_A, NODE_B}
    assert by_origin[NODE_A]["evidence_id"] == evidence_a[0].id
    assert by_origin[NODE_B]["evidence_id"] == b.evidence_id
    assert by_origin[NODE_A]["resource_id"] == version_a.resource_id
    assert by_origin[NODE_B]["resource_id"] == b.resource_id
    # Identical bytes: same excerpt digest, but ownership identity is never merged.
    assert by_origin[NODE_A]["excerpt_digest"] == by_origin[NODE_B]["excerpt_digest"]
    identities = {(item["origin_node_id"], item["resource_id"],
                   item["source_version_id"], item["evidence_id"]) for item in evidence}
    assert len(identities) == len(evidence)

    coverage = await coverage_of(actor_client, root)
    assert coverage["counts"] == {"total_targets": 2, "applicable_targets": 2,
                                  "succeeded": 2, "excluded": 0, "incomplete": 0}
    assert entry_for(coverage, NODE_A)["actual_index_revision"] \
        == collection_a["index_revision"]
    assert entry_for(coverage, NODE_B)["actual_index_revision"] == b.index_revision


# ==================================================================== 3. partial coverage


async def test_unreachable_registered_peer_stays_in_denominator(
        actor_client, session, two_node):
    """C is registered but nothing listens; the gap is visible, the task still returns B."""
    b = two_node.b_seed
    root, plan = await plan_manifest(
        actor_client,
        members=[member(b.collection_id, NODE_B),
                 member("node-c-collection", NODE_C)],
        query="federation keyword",
        consent=exploration(recipients=(NODE_B, NODE_C),
                            budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20}))
    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "partial")).json()

    assert status["status"] == "succeeded", "one successful target still returns evidence"
    assert status["retrieval_completeness"] == "partial"
    assert [item["origin_node_id"] for item in status["result"]["evidence"]] == [NODE_B]
    assert any(unretrieved["target_key"]["origin_node_id"] == NODE_C
               for unretrieved in status["result"]["unretrieved_targets"])

    coverage = await coverage_of(actor_client, root)
    assert coverage["counts"] == {"total_targets": 2, "applicable_targets": 2,
                                  "succeeded": 1, "excluded": 0, "incomplete": 1}
    assert coverage["retrieval_completeness"] == "partial"
    c_entry = entry_for(coverage, NODE_C)
    assert c_entry["state"] == "unreachable"
    assert c_entry["last_error"] == "peer_unavailable"

    # B saw its evidence probe plus the generation-readiness probe; C never
    # reached B's log (it is a separate, unlistening endpoint).
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2
    assert count_probe_kinds(two_node) == (1, 1)
    assert len(two_node.calls_to("/api/v1/federation/admissions")) == 1


# ================================================================== 4. exploration gate


async def test_local_only_exploration_gate_sends_zero_traffic(
        actor_client, session, two_node, monkeypatch):
    """local_only denies the remote target: plan carries the denial, no byte leaves A."""
    two_node.install_counting_transport(monkeypatch)
    b = two_node.b_seed
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    root, plan = await plan_manifest(
        actor_client, members=[member(b.collection_id, NODE_B)],
        query="federation keyword", consent=consent)
    # Implemented contract: planning succeeds and records the denial visibly
    # (routers/tasks.py has no 403 branch here); what must never happen is traffic.
    assert two_node.outbound == []
    assert two_node.calls() == []
    events = (await actor_client.get(f"/api/v1/tasks/{root}/events")).json()
    plan_ready = next(event for event in events["events"] if event["type"] == "plan_ready")
    assert set(plan_ready["payload"]["outcomes"].values()) == {"denied"}

    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "gate")).json()
    assert two_node.outbound == []
    assert two_node.calls() == []
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    coverage = await coverage_of(actor_client, root)
    entry = entry_for(coverage, NODE_B)
    assert entry["state"] == "denied"
    assert entry["last_error"] == "egress_mode:local_only"


# ======================================================================= 5. idempotency


async def test_submit_replay_is_one_logical_task(actor_client, two_node):
    """Same key + same plan digest replays the result without a second peer burst."""
    root, plan = await start_b_only(actor_client, two_node)
    await approve_task(actor_client, root, plan)
    first = await submit_task(actor_client, root, plan["plan_digest"], "replay-key")
    assert first.status_code == 200, first.text
    calls_after_first = two_node.calls()

    replay = await submit_task(actor_client, root, plan["plan_digest"], "replay-key")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert two_node.calls() == calls_after_first

    # Re-planning an already-approved revision does not re-probe either.
    replanned = await actor_client.post("/api/v1/task-plans",
                                        json={"root_task_id": root})
    assert replanned.status_code == 200
    assert two_node.calls() == calls_after_first


async def test_idempotency_key_reuse_across_tasks_conflicts(actor_client, two_node):
    """FINDING F1 (expected to FAIL until fixed): the cross-task conflict is a 500.

    `execute_task` assigns `row.idempotency_key` and then calls `_append_event`,
    whose `SELECT max(seq)` triggers SQLAlchemy autoflush *before* the
    `try: await session.commit() / except IntegrityError` handler can map the
    unique-constraint violation to `idempotency_conflict`
    (federation_tasks.py:1244-1252). The same key on a second root task must be
    a 409; instead the exception escapes as an unhandled DB error.
    """
    # 两个真正不同的 intent（同 body 会被 intent 幂等键折叠成同一个 root）。
    first_root, first_plan = await start_b_only(actor_client, two_node,
                                                query="federation keyword")
    await approve_task(actor_client, first_root, first_plan)
    first = await submit_task(actor_client, first_root, first_plan["plan_digest"],
                              "shared-key")
    assert first.status_code == 200, first.text

    second_root, second_plan = await start_b_only(actor_client, two_node,
                                                  query="beta federation keyword")
    assert second_root != first_root
    await approve_task(actor_client, second_root, second_plan)
    try:
        conflict = await submit_task(actor_client, second_root, second_plan["plan_digest"],
                                     "shared-key")
    except IntegrityError as exc:
        pytest.fail(
            "same Idempotency-Key on a second root task must answer 409 "
            "idempotency_conflict (federation-tasks-v1.yaml POST /tasks 409); instead "
            f"the request blew up with {type(exc).__name__} during autoflush before "
            "federation_tasks.execute_task's commit handler could map it "
            f"(federation_tasks.py:1244-1252). Underlying error: {exc}")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_same_task_tampered_plan_digest_is_rejected(actor_client, two_node):
    """A different revision under the same key is a 409, not a silent re-execution.

    The P5 brief phrased this as `idempotency_conflict`; the implementation
    verifies the submitted revision against the stored plan first and answers
    `plan_changed` (federation_tasks.py:1226-1228). The contract for POST
    /tasks allows either code for 409 (federation-tasks-v1.yaml:184), and the
    cross-task test above exercises the true `idempotency_conflict` path. This
    test pins the actual behavior so the ordering cannot drift silently.
    """
    root, plan = await start_b_only(actor_client, two_node)
    await approve_task(actor_client, root, plan)
    first = await submit_task(actor_client, root, plan["plan_digest"], "tamper-key")
    assert first.status_code == 200, first.text
    calls_after_first = two_node.calls()

    tampered = await submit_task(actor_client, root, "sha256:" + "0" * 64, "tamper-key")
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "plan_changed"
    assert two_node.calls() == calls_after_first


# ================================================================ 6. peer auth failure


async def test_wrong_peer_token_fails_closed_without_fabrication(
        actor_client, session, two_node, monkeypatch):
    monkeypatch.setattr(settings, "federation_peers",
                        two_node.peers_json(peer_token_b="definitely-not-the-token"))
    root, plan = await start_b_only(actor_client, two_node)
    # Planning crossed the network and got a real 401 from B (evidence probe
    # and generation-readiness probe both rejected).
    assert [call["status"] for call in two_node.calls_to("/api/v1/federation/probes")] \
        == [401, 401]
    assert count_probe_kinds(two_node) == (1, 1)

    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "bad-token")).json()
    assert status["status"] == "failed"
    assert status["result"]["evidence"] == [], "an auth failure must never fabricate evidence"
    assert status["error"] == "peer_unauthenticated"
    coverage = await coverage_of(actor_client, root)
    entry = entry_for(coverage, NODE_B)
    assert entry["state"] == "failed"
    assert entry["last_error"] == "peer_unauthenticated"

    # Exactly two rejected probes and one admission attempt: no retry loop.
    assert [call["status"] for call in two_node.calls_to("/api/v1/federation/admissions")] \
        == [401]
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2
    assert two_node.calls_containing("/evidence-sets/") == []
    count = await session.scalar(select(func.count()).select_from(FederationProbe))
    assert count == 0


# ========================================================== 7. fast vs exhaustive


async def test_fast_mode_never_claims_complete(actor_client, session, two_node):
    b = two_node.b_seed
    _version_a, _evidence_a, collection_a = await publish_a(
        actor_client, session, texts=(A_TEXT,), key="two-node-a-fast")
    root, plan = await plan_manifest(
        actor_client,
        members=[member(collection_a["collection_id"], NODE_A),
                 member(b.collection_id, NODE_B)],
        query="federation keyword", mode="fast")
    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "fast")).json()

    assert status["status"] == "succeeded"
    assert status["search_mode"] == "fast"
    assert status["result"]["counts"]["succeeded"] == 2
    assert status["result"]["counts"]["incomplete"] == 0
    assert status["result"]["unretrieved_targets"] == []
    # Fast is bounded to its candidates and can never claim the whole scope.
    assert status["retrieval_completeness"] == "partial"


async def test_exhaustive_complete_requires_sealed_enumeration(actor_client, session,
                                                               two_node):
    b = two_node.b_seed
    _version_a, _evidence_a, collection_a = await publish_a(
        actor_client, session, texts=(A_TEXT,), key="two-node-a-exhaustive")
    members = [member(collection_a["collection_id"], NODE_A),
               member(b.collection_id, NODE_B)]

    sealed_root, sealed_plan = await plan_manifest(
        actor_client, members=members, query="federation keyword", enumeration="sealed")
    await approve_task(actor_client, sealed_root, sealed_plan)
    sealed = (await submit_task(actor_client, sealed_root, sealed_plan["plan_digest"],
                                "sealed")).json()
    assert sealed["retrieval_completeness"] == "complete"
    assert sealed["result"]["counts"] == {"total_targets": 2, "applicable_targets": 2,
                                          "succeeded": 2, "excluded": 0, "incomplete": 0}

    # Same targets, same success, but the enumeration is not sealed: never complete.
    partial_root, partial_plan = await plan_manifest(
        actor_client, members=members, query="federation keyword",
        enumeration="partial",
        unexpanded=[{"node_id": NODE_C, "reason": "denied"}])
    await approve_task(actor_client, partial_root, partial_plan)
    partial = (await submit_task(actor_client, partial_root, partial_plan["plan_digest"],
                                 "unsealed")).json()
    assert partial["result"]["counts"]["succeeded"] == 2
    assert partial["result"]["counts"]["incomplete"] == 0
    assert partial["retrieval_completeness"] == "partial"


# ================================================================== 8. cancel / resume


async def test_cancel_after_success_and_resume_after_peer_failure(
        actor_client, session, two_node):
    b = two_node.b_seed
    _version_a, _evidence_a, collection_a = await publish_a(
        actor_client, session, texts=(A_TEXT,), key="two-node-a-resume")
    root, plan = await plan_manifest(
        actor_client,
        members=[member(collection_a["collection_id"], NODE_A),
                 member(b.collection_id, NODE_B)],
        query="federation keyword")
    # Planning probed B successfully (evidence + generation readiness), so the
    # probe receipt is already stored.
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2
    assert count_probe_kinds(two_node) == (1, 1)
    await approve_task(actor_client, root, plan)

    two_node.set_fault(True)
    failed_response = await submit_task(actor_client, root, plan["plan_digest"], "resume-key")
    assert failed_response.status_code == 200, failed_response.text
    failed = failed_response.json()
    assert failed["status"] == "succeeded", "A's local target still succeeded"
    assert failed["retrieval_completeness"] == "partial"
    coverage = await coverage_of(actor_client, root)
    assert entry_for(coverage, NODE_A)["state"] == "succeeded"
    b_entry = entry_for(coverage, NODE_B)
    assert b_entry["state"] == "failed"
    assert b_entry["last_error"] == "fault_injected"
    assert len(two_node.calls_to("/api/v1/federation/admissions")) == 1
    local_admissions = await session.scalar(
        select(func.count()).select_from(FederationAdmission))
    assert local_admissions == 1

    # Clear the fault; resume must re-run the incomplete B target only.
    two_node.set_fault(False)
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert status["status"] == "succeeded"
    assert status["retrieval_completeness"] == "complete"
    assert {item["origin_node_id"] for item in status["result"]["evidence"]} \
        == {NODE_A, NODE_B}
    assert len(two_node.calls_to("/api/v1/federation/admissions")) == 2, \
        "resume must re-run the failed target exactly once"
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2, \
        "resume must reuse the stored probes instead of re-probing"
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1, \
        "the already-succeeded local target must not be re-admitted"

    # Cancel after completion is a no-op: a succeeded task is never rewritten.
    before = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    calls_before_cancel = two_node.calls()
    cancelled = await actor_client.post(f"/api/v1/tasks/{root}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "succeeded"
    assert cancelled.json() == before
    assert two_node.calls() == calls_before_cancel


# ===================================================== 9. remote answer delegation

B_ANSWER = "beta federation keyword fact [1]"


@pytest.mark.parametrize("two_node", [B_ANSWER], indirect=True)
async def test_remote_answer_delegation_over_real_http(actor_client, two_node):
    """A 没有生成能力：B 经真实 HTTP 接单，用跨节点送去的正文生成带引用答案。

    B 的生成走真实 loopback HTTP（`ModelStub`），不是 mock B 的进程；A -> B 的
    probe/admission/poll 仍然是生产 `PeerClient` 在真实 socket 上跑的协议。
    """
    b = two_node.b_seed
    manifest = scope_manifest([member(b.collection_id, NODE_B)])
    intent = await create_intent(
        actor_client,
        spec=task_spec(mode="exhaustive_scope", query="federation keyword"),
        consent=exploration(recipients=(NODE_B,)), manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)

    # 本地生成未知 -> 规划先探测 B 的生成能力，再把 answer 步落在 B 上。
    assert count_probe_kinds(two_node) == (1, 1)
    step = next(item for item in plan["steps"] if item["operation"] == "answer")
    assert step["executor_node_id"] == NODE_B
    assert step["depends_on"] == ["fuse-1"]
    edge = next(item for item in plan["data_edges"]
                if item["edge_id"] == "edge-answer-1")
    assert edge["payload_kind"] == "evidence_excerpts"
    assert edge["from_node_id"] == NODE_A and edge["to_node_id"] == NODE_B
    assert plan["budget"]["max_generation_tokens"] > 0

    await approve_task(actor_client, root, plan)
    status = (await submit_task(actor_client, root, plan["plan_digest"], "delegate")
              ).json()
    assert status["status"] == "succeeded", status
    result = status["result"]
    assert result["answer"] == B_ANSWER, result
    assert result["answer_reason"] is None
    assert result["validation_state"] == "passed"
    bindings = result["claim_evidence_bindings"]
    assert [ref for binding in bindings for ref in binding["evidence_refs"]] \
        == [b.evidence_id], "绑定的 evidence id 必须是真实跨节点证据"
    assert result["disclosure"]["remote"] is True
    assert result["provider"]["location"] == "remote"

    # B 的模型真的看到了跨节点送来的正文（数据边的载荷），不是占位符。
    prompts = [json.dumps(item["payload"], ensure_ascii=False)
               for item in two_node.model_stub.requests
               if item["path"] == "/v1/chat/completions"]
    assert prompts and B_TEXT in prompts[0], prompts

    # B 端 ground truth：2 探测（证据 + 能力）、2 受理（取数 + 答案），
    # 证据集读 2 次（规划一份、取数执行一份）；没有重试风暴。
    assert len(two_node.calls_to("/api/v1/federation/probes")) == 2
    assert count_probe_kinds(two_node) == (1, 1)
    assert len(two_node.calls_to("/api/v1/federation/admissions")) == 2
    assert len(two_node.calls_containing("/evidence-sets/")) == 2
