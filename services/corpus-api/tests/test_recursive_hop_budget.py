"""Hop budget for a remote direct sibling plus a two-deep delegated leaf (red-first).

Shape under test (the live P6 drill shape, one level deeper on the delegated
branch): root A, direct remote retrieval target B, delegated leaf D reached
via P then R (A -> P -> R -> D), generation delegated to the remote generator
B. The stub relay P emulates production's sub-delegation faithfully: it
carves R's sub-share with the REAL ``routing.plan_steps`` and enforces the
REAL ``admission.validate_delegation_path`` depth gate, so a too-small share
fails exactly where a real R would refuse it (``budget_exceeded``).

Derived need (hop ledger): B admission 2 + P share reservation S + P
admission 2 + B answer admission 1 = 5 + S on the root ledger, with S = 5
(P admission 2 + R sub-share 3; R needs sub-share 3 because its admission
depth check is strict: len([A, P]) = 2 < sub-share). Root need = 10, which
the intent budget already provides (2*remote + 2*generation + 2*via =
4 + 2 + 4). The bug is only the carve-time reservation for the delegated
generation edge: ``create_plan`` subtracts 2, but an answer admission spends
1 hop, so the carved share is 4 instead of 5 and R's depth check refuses it.
"""
import json

import httpx
import respx
from ddp_core.application import admission as admission_kernel
from ddp_core.application import coverage as coverage_kernel
from ddp_core.application import plans, routing
from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import digest as bundle_digest
from ddp_corpus import federation_tasks
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.federation_peers import PeerDirectory, parse_peers
from node_credentials_fixture import LocalControlSigner
from test_federation_answer_delegation import mock_gateway_not_ready
from test_federation_probes import NODE, configure_federation
from test_federation_tasks import (
    EXPIRY,
    PEER_NODE,
    StubPeer,
    approve_task,
    bound_peer_receipt,
    create_intent,
    exploration,
    member,
    peer_evidence,
    peer_status,
    plan_task,
    scope_manifest,
    submit_task,
    task_spec,
)

LEAF_NODE = "node-" + "d" * 48
RELAY_NODE = "node-" + "r" * 48
FIRST_HOP_NODE = "node-" + "p" * 48

D_TEXT = "deep delegated leaf federation keyword fact"
B_TEXT = "direct sibling federation keyword fact"


def _peer(host: str) -> dict:
    return {"endpoint": f"https://{host}.example"}


class RelayStubPeer(StubPeer):
    """Emulates the first-hop relay P with production's own gates.

    Retrieve/admission behaviour for non-delegate steps is inherited. A
    delegate admission carves R's sub-share with the REAL
    ``routing.plan_steps`` and enforces the REAL
    ``admission.validate_delegation_path`` depth gate for R, exactly as a
    production P sub-delegating to a real R would. Rejection yields a failed
    delegation report (``budget_exceeded``), acceptance a successful one.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delegate_bodies: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        inner = super().transport()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/admissions"):
                parsed = json.loads(request.content or b"{}")
                if (parsed.get("step_id") or "").startswith("delegate"):
                    return self._delegate_response(request, parsed)
            if "/tasks/" in request.url.path and not request.url.path.endswith("/cancel"):
                executor_task_id = request.url.path.rsplit("/", 1)[1]
                if executor_task_id in self.reports:
                    _, status = self.reports[executor_task_id]
                    return httpx.Response(200, json={
                        **peer_status(), "executor_task_id": executor_task_id,
                        "step_id": self.executions.get(executor_task_id, "delegate-1"),
                        "operation": "delegate", "state": "succeeded",
                        "delegation_report": status["delegation_report"]})
            return inner.handle_request(request)

        return httpx.MockTransport(handler)

    def _delegate_response(self, request, body) -> httpx.Response:

        self.calls.append((request.method, request.url.path,
                           request.headers.get("Idempotency-Key")))
        self.admissions.append(body)
        self.delegate_bodies.append(body)
        plan = body["plan"]
        step = next(item for item in plan["steps"]
                    if item.get("step_id") == body["step_id"])
        share = step["budget_share"]
        key = body["idempotency_key"]
        previous = self.accepted.get(key)
        if previous is not None:
            if previous[0] != body:
                return httpx.Response(409, json={"error": {
                    "code": "idempotency_conflict",
                    "message": "same idempotency key with a different request"}})
            return httpx.Response(200, json=previous[1])
        leaf = step["delegated_targets"][0]["target_key"]
        rest = step["delegated_targets"][0]["via_node_ids"]
        item, status = self._sub_delegate(step, share, leaf, rest,
                                          body.get("delegation_path") or [])
        receipt = bound_peer_receipt(len(self.calls), body=body, key=key)
        receipt["executor_node_id"] = FIRST_HOP_NODE
        executor_task_id = f"relay-exec-{len(self.calls)}"
        receipt["executor_task_id"] = executor_task_id
        self.executions[executor_task_id] = str(body.get("step_id") or "")
        self.accepted[key] = (body, receipt)
        self.reports[executor_task_id] = (item, status)
        return httpx.Response(201, json=receipt)
    reports: dict = {}

    def _sub_delegate(self, step, share, leaf, rest, path):
        caps = {"max_requests": share["max_requests"], "max_bytes": share["max_bytes"],
                "max_hops": share["max_hops"], "max_probe_requests": share["max_probes"],
                "deadline": share["deadline"]}
        sub_steps, _ = routing.plan_steps(
            targets=[leaf], probes=[], local_node_id=FIRST_HOP_NODE,
            coordinator_node_id=FIRST_HOP_NODE,
            query="federation keyword", now=0, node_routes=[{
                "node_id": leaf["origin_node_id"], "via_node_ids": rest}],
            budget={**caps, "used_requests": 0, "used_bytes": 0,
                    "used_probes": 0, "used_hops": 0})
        sub_share = next(item["budget_share"] for item in sub_steps
                         if item.get("operation") == "delegate")["max_hops"]
        try:
            admission_kernel.validate_delegation_path(
                [*path, FIRST_HOP_NODE], receiver_node_id=rest[0],
                issuer_node_id=FIRST_HOP_NODE, max_hops=sub_share)
        except ApplicationError as exc:
            entry = coverage_kernel.new_entry(
                coverage_kernel.target_key(leaf["origin_node_id"],
                                           leaf["collection_id"], leaf["operation"]),
                "scope-relay", "sha256:" + "c" * 64)
            entry = coverage_kernel.record(entry, None, state="failed",
                                           error=exc.code, now=0)
            return None, {"state": "succeeded", "executor_task_id": "",
                          "delegation_report": {
                              "entries": [entry], "evidence": [],
                              "consumption": {"requests": 1, "bytes": 512,
                                              "hops": 2, "probes": 0}}}
        evidence = peer_evidence(evidence_id="leaf-d-evidence-1",
                                 resource_id="leaf-d-resource-1")
        evidence.update(origin_node_id=leaf["origin_node_id"],
                        authority_node_id=leaf["origin_node_id"],
                        excerpt=D_TEXT, excerpt_digest=bundle_digest(D_TEXT.encode()),
                        relay_via=[RELAY_NODE, FIRST_HOP_NODE])
        entry = coverage_kernel.new_entry(
            coverage_kernel.target_key(leaf["origin_node_id"],
                                       leaf["collection_id"], leaf["operation"]),
            "scope-relay", "sha256:" + "c" * 64)
        entry.update(state="succeeded", attempts=1, probe_receipts=["receipt-relay-r"],
                     actual_index_revision="index-leaf-d",
                     evidence_refs=[evidence["evidence_id"]])
        return evidence, {"state": "succeeded", "executor_task_id": "",
                          "delegation_report": {
                              "entries": [entry], "evidence": [evidence],
                              "consumption": {"requests": 2, "bytes": 2048,
                                              "hops": 4, "probes": 0}}}


def install_chain(monkeypatch, data_peer: StubPeer, relay_peer: RelayStubPeer) -> None:
    peers = parse_peers(json.dumps({
        PEER_NODE: _peer("data"), FIRST_HOP_NODE: _peer("relay")}))
    signer = LocalControlSigner(issuer_node_id=NODE)

    def handler(request):
        if request.url.host == "relay.example":
            return relay_peer.transport().handle_request(request)
        return data_peer.transport().handle_request(request)

    def factory(actor: Actor, delegation=None) -> PeerDirectory:
        return PeerDirectory(peers, actor=actor, transport=httpx.MockTransport(handler),
                             signer=signer, delegation=delegation)

    monkeypatch.setattr(federation_tasks, "peer_directory", factory)


def _task_spec():
    spec = task_spec(scope="federation_public", mode="exhaustive_scope",
                     scope_ref="scope-hop-budget", query="federation keyword")
    return spec


def test_carve_leaves_five_hops_for_sibling_plus_two_deep_leaf():
    """Routing-seam characterization for the same shape (not the red guard).

    Pins what `plan_steps` carves from each input: 8 gives the relay 4 hops,
    9 gives the 5 its strict depth gate needs (the literal from the module
    docstring). 9 is the fixed allowance: ledger 10 minus the 1 hop the
    delegated answer admission spends. `plan_steps` itself is unchanged, so
    this passes before and after the fix; the HTTP test is the red guard.
    """
    targets = [
        {"origin_node_id": PEER_NODE, "collection_id": "peer-collection-b",
         "operation": "corpus.retrieve"},
        {"origin_node_id": LEAF_NODE, "collection_id": "leaf-collection-d",
         "operation": "corpus.retrieve"},
    ]
    routes = [{"node_id": LEAF_NODE,
               "via_node_ids": [FIRST_HOP_NODE, RELAY_NODE]}]
    for carve_input, expected in ((8, 4), (9, 5)):
        steps, _ = routing.plan_steps(
            targets=targets, probes=[], local_node_id=NODE,
            coordinator_node_id=NODE, query="federation keyword", now=0,
            node_routes=routes,
            budget={"max_requests": 304, "max_bytes": 1 << 20,
                    "max_hops": carve_input, "max_probe_requests": 16,
                    "deadline": EXPIRY})
        share = next(step["budget_share"]["max_hops"] for step in steps
                     if step.get("operation") == "delegate")
        assert share == expected, (carve_input, share)


@respx.mock
async def test_remote_sibling_plus_two_deep_leaf_gets_a_five_hop_share(
        actor_client, session, monkeypatch):
    """The delegate share for [P, R]-routed D next to direct B must be 5 hops.

    Literal 5 from the derivation in the module docstring, not recomputed
    from the carve formula: the relay needs 2 (its admission) + 3 (R's
    sub-share, strict depth 2 < 3).
    """
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_allow_loopback", False)
    mock_gateway_not_ready()
    data_peer = StubPeer(
        items=[{**peer_evidence(evidence_id="peer-evidence-b",
                                resource_id="peer-resource-b"),
                "excerpt": B_TEXT}],
        can_generate=True,
        answer_document={
            "answer": "direct sibling cited answer [1]",
            "answer_reason": None,
            "claim_evidence_bindings": [{
                "claim_id": "claim-1", "claim_text": "direct sibling cited answer",
                "evidence_refs": ["peer-evidence-b", "leaf-d-evidence-1"],
                "structural_validation": "passed",
                "semantic_review": "needs_review"}],
            "provider": {"model": "peer-instruct",
                         "endpoint": "https://data.example",
                         "location": "local"},
            "disclosure": {"remote": False,
                           "payload": ["question", "selected_evidence"]},
            "validation_state": "passed",
        })
    relay_peer = RelayStubPeer()
    RelayStubPeer.reports = {}
    install_chain(monkeypatch, data_peer, relay_peer)

    manifest = scope_manifest(
        [member("peer-collection-b", PEER_NODE),
         {"origin_node_id": LEAF_NODE, "collection_id": "leaf-collection-d",
          "operation": "corpus.retrieve"}],
        revisions=[(NODE, 1), (PEER_NODE, 1), (FIRST_HOP_NODE, 1),
                   (RELAY_NODE, 1), (LEAF_NODE, 1)])
    manifest["scope_id"] = "scope-hop-budget"
    manifest["node_routes"] = [{"node_id": LEAF_NODE,
                                "via_node_ids": [FIRST_HOP_NODE, RELAY_NODE]}]
    manifest["manifest_digest"] = plans.digest({
        key: value for key, value in manifest.items() if key != "manifest_digest"})
    consent = exploration(recipients=(PEER_NODE, FIRST_HOP_NODE, RELAY_NODE, LEAF_NODE))
    intent = await create_intent(actor_client, spec=_task_spec(), consent=consent,
                                 manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)
    delegates = [step for step in plan["steps"] if step["operation"] == "delegate"]
    assert len(delegates) == 1, plan
    assert delegates[0]["executor_node_id"] == FIRST_HOP_NODE, plan
    assert delegates[0]["budget_share"]["max_hops"] == 5, plan

    await approve_task(actor_client, root, plan,
                       recipients=(NODE, PEER_NODE, FIRST_HOP_NODE, RELAY_NODE, LEAF_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"],
                                "hop-budget-five")).json()
    assert status["status"] == "succeeded", status
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    by_origin = {entry["target_key"]["origin_node_id"]: entry
                 for entry in coverage["entries"]}
    assert by_origin[LEAF_NODE]["state"] == "succeeded", by_origin[LEAF_NODE]
    assert by_origin[LEAF_NODE]["reported_by"] == FIRST_HOP_NODE
    origins = {item["origin_node_id"] for item in status["result"]["evidence"]}
    assert {PEER_NODE, LEAF_NODE} <= origins, status
