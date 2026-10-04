"""B1: delegate admission endpoint coverage + P-own-leaf onward policy (red-first)."""
import pytest

from ddp_corpus.main import app  # noqa: F401 — keeps the root app importable in this module
from federation_recursive_node import NODE_A, NODE_P, NODE_R, NODE_S, RecursiveFixture
from test_federation_two_node import (
    approve_task, coverage_of, entry_for, execution_consent, execution_recipients,
    plan_task, submit_task,
)
from test_federation_recursive import assert_adjacent_traffic, recursive_nodes, recursive_plan


@pytest.mark.parametrize("missing", [NODE_R, NODE_S], ids=["relay", "leaf"])
async def test_narrowed_chain_consent_denies_delegate_leaf_before_any_byte(
        actor_client, tmp_path, monkeypatch, app_state, missing):
    """Root plan covers everything, but the consent P actually receives drops a chain node.

    Red-first for B1(a): P's delegate admission must 403 `egress_denied`
    before persisting any admission row on P and before P sends any byte
    downstream (R/S see zero calls, P sends zero outbound).
    """
    from ddp_corpus import federation_tasks

    fixture = await RecursiveFixture.create(tmp_path)
    try:
        fixture.install_root(monkeypatch, app)
        root, plan = await recursive_plan(actor_client, fixture)
        narrowed = execution_consent(
            plan["plan_digest"],
            recipients=[node for node in execution_recipients(plan) if node != missing],
            edges=[edge["edge_id"] for edge in plan["data_edges"]])
        real_directory = federation_tasks.peer_directory

        def _narrowing_directory(actor, delegation):
            directory = real_directory(actor, delegation)
            real_client = directory.client

            def _client(node_id):
                client = real_client(node_id)
                real_admit = client.admit

                async def _admit(body, **kwargs):
                    if node_id == NODE_P:
                        body = {**body, "execution_consent": narrowed}
                    return await real_admit(body, **kwargs)

                client.admit = _admit
                return client

            directory.client = _client
            return directory

        monkeypatch.setattr(federation_tasks, "peer_directory", _narrowing_directory)
        await approve_task(actor_client, root, plan)
        response = await submit_task(actor_client, root, plan["plan_digest"],
                                     "recursive-narrow-delegate")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["retrieval_completeness"] == "partial"
        assert status["result"]["evidence"] == []
        entry = entry_for(await coverage_of(actor_client, root), NODE_S)
        assert entry["last_error"] == "egress_denied", entry
        assert entry["reported_by"] == NODE_P, entry
        calls = fixture.nodes[NODE_P].calls("/admissions")
        assert len(calls) == 1 and calls[0]["status"] == 403, calls
        assert fixture.nodes[NODE_P].outbound() == [], fixture.nodes[NODE_P].outbound()
        assert fixture.nodes[NODE_R].calls() == []
        assert fixture.nodes[NODE_S].calls() == []
        assert_adjacent_traffic(fixture)
    finally:
        await fixture.stop()


async def _run_p_gen_case(actor_client, tmp_path, monkeypatch, app_state, policy):
    """One B1(b) case: inject a P-origin leaf plus an A->GEN evidence edge.

    Returns (fixture, root, plan). The P delegate admission sees the P leaf
    served under A's downstream walk; GEN is in that walk with A exempt.
    """
    from ddp_core.application import plans as _plans
    from ddp_corpus import federation_tasks

    from test_federation_recursive import recursive_plan as _recursive_plan
    from test_federation_two_node import approve_task as _approve

    GEN = "node-" + "g" * 48
    fixture = await RecursiveFixture.create(tmp_path, policies={NODE_P: policy})
    fixture.install_root(monkeypatch, app)
    import test_federation_recursive as _rec_mod
    _orig_recursive_plan = _rec_mod.recursive_plan

    async def _big_budget_plan(client, fix, **kwargs):
        import copy
        from ddp_core.application import plans as _plans2
        from test_federation_two_node import (
            exploration as _exploration, plan_task as _plan_task,
            task_spec as _task_spec,
        )
        from federation_recursive_node import NODE_A as _A, NODE_P as _P, NODE_R as _R, NODE_S as _S
        from test_federation_recursive import recursive_manifest as _manifest
        spec = _task_spec(coordinator=_A)
        spec["operation"] = "corpus.retrieve"
        spec["resource_scope"]["scope_ref"] = "scope-recursive"
        consent = _exploration(recipients=(_P, _R, _S),
                              budget={"max_probe_requests": 64, "max_egress_bytes": 64 << 20})
        body = {"task_spec": spec, "exploration_consent": consent,
                "scope_manifest": _manifest(fix, _S),
                "budget": {"max_requests": 512, "max_bytes": 64 << 20,
                           "max_hops": 16, "deadline": "2030-01-01T00:00:00Z"}}
        response = await client.post("/api/v1/task-intents", json=body,
                                     headers={"Idempotency-Key": _plans2.digest(body)})
        assert response.status_code == 201, response.text
        _root = response.json()["root_task_id"]
        return _root, await _plan_task(client, _root)

    import sys as _sys
    _this = _sys.modules[__name__]
    monkeypatch.setattr(_rec_mod, "recursive_plan", _big_budget_plan)
    monkeypatch.setattr(_this, "recursive_plan", _big_budget_plan)
    p_collection = fixture.nodes[NODE_P].seed.collection_id
    _root_directory = federation_tasks.peer_directory

    def _injecting_directory(actor, delegation):
        directory = _root_directory(actor, delegation)
        real_client = directory.client.__get__(directory, type(directory))

        def _client(node_id):
            client = real_client(node_id)
            real_admit = client.admit.__get__(client, type(client))

            async def _admit(body, idempotency_key, **kwargs):
                step = next((s for s in body["plan"]["steps"]
                             if s["step_id"] == body["step_id"]), None)
                if step is not None and step["operation"] == "delegate" \
                        and step["executor_node_id"] == NODE_P:
                    have = {(t["target_key"]["origin_node_id"],
                             t["target_key"]["collection_id"])
                            for t in step["delegated_targets"]}
                    if (NODE_P, p_collection) not in have:
                        first = step["delegated_targets"][0]
                        step["delegated_targets"] = [
                            *step["delegated_targets"],
                            {"target_key": {"origin_node_id": NODE_P,
                                            "collection_id": p_collection,
                                            "operation": first["target_key"]["operation"]},
                             "via_node_ids": []}]
                        gen_edge = {
                            "edge_id": "edge-evidence-p-gen",
                            "from_node_id": NODE_P,
                            "to_node_id": GEN,
                            "payload_kind": "evidence_excerpts",
                            "retention": body["execution_consent"].get(
                                "retention", "temporary"),
                            "authorised_by": "relay:" + NODE_A}
                        body["plan"]["data_edges"] = [
                            *body["plan"]["data_edges"], gen_edge]
                        body["plan"]["plan_digest"] = _plans.task_plan_digest(
                            body["plan"])
                        consent = body["execution_consent"]
                        body["execution_consent"] = {
                            **consent,
                            "plan_digest": body["plan"]["plan_digest"],
                            "allowed_recipients": sorted(
                                {*consent.get("allowed_recipients", []), GEN}),
                            "allowed_edges": [
                                *(consent.get("allowed_edges") or []),
                                "edge-evidence-p-gen"]}
                return await real_admit(body, idempotency_key=idempotency_key,
                                        **kwargs)

            client.admit = _admit
            return client

        directory.client = _client
        return directory

    monkeypatch.setattr(federation_tasks, "peer_directory", _injecting_directory)
    root, plan = await _big_budget_plan(actor_client, fixture)
    delegates = [step for step in plan["steps"] if step["operation"] == "delegate"]
    assert len(delegates) == 1 and delegates[0]["executor_node_id"] == NODE_P, plan
    await _approve(actor_client, root, plan)
    return fixture, root, plan, GEN


async def test_p_own_leaf_source_policy_denies_forbidden_generator_before_any_byte(
        actor_client, tmp_path, monkeypatch, app_state):
    """P's collection forbids the generator: delegate admission 403s, zero bytes.

    Same rule as single-hop retrieve: the plan-wide downstream walk from P
    (coordinator exempt) contains GEN, which is outside P's `onward_recipients`.
    """
    GEN = "node-" + "g" * 48
    fixture, root, plan, GEN = await _run_p_gen_case(
        actor_client, tmp_path, monkeypatch, app_state, [NODE_R, NODE_S])
    try:
        response = await submit_task(actor_client, root, plan["plan_digest"],
                                     "recursive-p-gen-denied")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["retrieval_completeness"] == "partial"
        assert status["result"]["evidence"] == []
        entry = entry_for(await coverage_of(actor_client, root), NODE_S)
        assert entry["state"] == "denied", entry
        assert entry["last_error"] == "egress_denied", entry
        assert GEN not in str(status["result"]["evidence"])
        calls = fixture.nodes[NODE_P].calls("/admissions")
        assert len(calls) == 1 and calls[0]["status"] == 403, calls
        assert fixture.nodes[NODE_P].outbound() == [], fixture.nodes[NODE_P].outbound()
        assert fixture.nodes[NODE_R].calls() == []
        assert fixture.nodes[NODE_S].calls() == []
        assert_adjacent_traffic(fixture)
    finally:
        await fixture.stop()


async def test_p_own_leaf_source_policy_allows_listed_generator(
        actor_client, tmp_path, monkeypatch, app_state):
    """Positive twin: policy lists the generator, so the admission succeeds.

    Same shape as the deny case; only the policy differs. P admits (201),
    serves both leaves, and the S evidence still carries its leaf origin.
    """
    from federation_recursive_node import NODE_S as _LEAF
    from test_federation_two_node import coverage_of as _coverage_of, entry_for as _entry_for

    GEN = "node-" + "g" * 48
    fixture, root, plan, _ = await _run_p_gen_case(
        actor_client, tmp_path, monkeypatch, app_state, [GEN])
    try:
        response = await submit_task(actor_client, root, plan["plan_digest"],
                                     "recursive-p-gen-allowed")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["status"] == "succeeded", status
        evidence = status["result"]["evidence"]
        assert len(evidence) == 1, status
        assert evidence[0]["origin_node_id"] == _LEAF, evidence
        calls = fixture.nodes[NODE_P].calls("/admissions")
        assert len(calls) == 1 and calls[0]["status"] == 201, calls
        s_entry = _entry_for(await _coverage_of(actor_client, root), _LEAF)
        assert s_entry["state"] == "succeeded", s_entry
        assert s_entry["reported_by"] == NODE_P, s_entry
        assert_adjacent_traffic(fixture)
    finally:
        await fixture.stop()

