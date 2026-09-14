import copy

import pytest

from ddp_core.application.plans import (canonical_bytes, digest, task_spec_digest, task_plan_digest, validate_scope)
from ddp_core.application.ports import ApplicationError
from plan_samples import NOW, EXPIRY, plan_scope, source_policies, redigest


def test_digests_bind_content_but_do_not_cycle_with_approval():
    scope = plan_scope()
    spec, plan = scope["task_spec"], scope["plan"]
    assert digest({"b": "原文", "a": 2}) == digest({"a": 2, "b": "原文"})
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    original = task_spec_digest(spec), task_plan_digest(plan)
    spec["consent_refs"]["execution"] = "explicit-consent"
    plan.update(planning_state="approved", execution_consent_ref="explicit-consent")
    assert original == (task_spec_digest(spec), task_plan_digest(plan))
    for field, value in (("query", "new private question"), ("budget_ref", "larger-budget")):
        changed = copy.deepcopy(spec)
        changed[field] = value
        assert task_spec_digest(changed) != original[0]
    for field, value in (("final_result_writer", "node-evil"), ("revision", 2)):
        changed = copy.deepcopy(plan)
        changed[field] = value
        assert task_plan_digest(changed) != original[1]


def test_registered_templates_and_actual_source_permissions():
    scope = plan_scope()
    assert "center-a" in validate_scope(scope, local_node_id="local-env", now=NOW, source_policies=source_policies(scope))
    with pytest.raises(ApplicationError, match="source policy"):
        validate_scope(scope, local_node_id="local-env", now=NOW, source_policies={})
    scope["plan"]["steps"][0]["operation"] = "shell"
    with pytest.raises(ApplicationError, match="unregistered"):
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=source_policies(scope))


@pytest.mark.parametrize("mutation", ["cycle", "missing", "reverse", "local_only", "unknown_field", "receiver"])
def test_fail_closed_invalid_graph_and_boundary(mutation):
    scope = plan_scope()
    if mutation == "cycle":
        scope["plan"]["steps"][0]["depends_on"] = ["retrieve-1"]
    elif mutation == "missing":
        scope["plan"]["steps"][0]["depends_on"] = ["missing"]
    elif mutation == "reverse":
        scope["plan"]["steps"].append({"step_id": "parse-2", "operation": "parse", "executor_node_id": "center-a", "depends_on": ["retrieve-1"]})
    elif mutation == "local_only":
        scope["task_spec"]["execution_policy"]["mode"] = "local_only"
    elif mutation == "unknown_field":
        scope["plan"]["steps"][0]["shell"] = "echo bypass"
    else:
        scope["payload_bindings"][0]["recipient_node_id"] = "node-new"
    with pytest.raises(ApplicationError):
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=source_policies(scope))


def test_b_data_c_generation_denied_even_through_local_reexport():
    scope = plan_scope()
    scope["task_spec"]["execution_policy"]["mode"] = "trusted_federation"
    scope["plan"]["data_edges"] = [
        {"edge_id": "from-b", "from_node_id": "node-b", "to_node_id": "local-env", "payload_kind": "evidence_excerpts", "retention": "temporary", "authorised_by": "b-grant"},
        {"edge_id": "to-c", "from_node_id": "local-env", "to_node_id": "node-c", "payload_kind": "evidence_excerpts", "retention": "temporary", "authorised_by": "local:local-env"}]
    scope["payload_bindings"] = []
    policies = {"b-grant": {"source_node_id": "node-b", "allowed_recipients": ["local-env"], "allowed_payload": ["evidence_excerpts"], "allowed_retention": ["temporary"], "valid_until": EXPIRY},
                "local:local-env": {"source_node_id": "local-env", "allowed_recipients": ["node-c"], "allowed_payload": ["evidence_excerpts"], "allowed_retention": ["temporary"], "valid_until": EXPIRY}}
    with pytest.raises(ApplicationError, match="source policy"):
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=policies)
    policies["b-grant"]["allowed_recipients"].append("node-c")
    validate_scope(scope, local_node_id="local-env", now=NOW, source_policies=policies)


def test_center_only_does_not_authorize_a_third_executor():
    scope = plan_scope()
    scope["plan"]["steps"][0]["executor_node_id"] = "third-party"
    with pytest.raises(ApplicationError) as exc:
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=source_policies(scope))
    assert exc.value.code == "policy_denied"


def test_root_hop_budget_counts_multiple_edges():
    scope = plan_scope()
    scope["plan"]["budget"]["max_hops"] = 1
    scope["plan"]["data_edges"].append({**scope["plan"]["data_edges"][0], "edge_id": "another-edge"})
    with pytest.raises(ApplicationError) as exc:
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=source_policies(scope))
    assert exc.value.code == "budget_exceeded"


def test_exploration_source_payload_requires_verified_origin():
    scope = plan_scope()
    scope["input_manifest"] = []
    scope["plan"]["steps"][0]["fixed_inputs"] = []
    scope["exploration"]["allowed_payload"] = ["source_files"]
    scope["payload_bindings"][0]["payload_kind"] = "source_files"
    with pytest.raises(ApplicationError) as exc:
        validate_scope(redigest(scope), local_node_id="local-env", now=NOW, source_policies=source_policies(scope))
    assert exc.value.code == "input_changed"
