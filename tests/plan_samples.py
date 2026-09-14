"""Reusable concrete plan fixtures; no network or model behavior is simulated."""
from ddp_core.application.plans import content_digest, task_spec_digest, task_plan_digest

NOW = 1_800_000_000
EXPIRY = "2030-01-01T00:00:00Z"
IDENTITY = {"environment_id": "local-env", "workspace_id": "workspace-a", "subject": "alice"}


def plan_scope(local="local-env", workspace="workspace-a"):
    query = "Which page contains the evidence?"
    payload = query.encode()
    spec = {"schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1", "operation": "rag.answer.cited", "workspace_ref": workspace, "query": query,
            "resource_scope": {"kind": "fixed_resources", "resource_refs": ["input-1"]}, "search_policy": {"mode": "fast", "ordering": "local_first"},
            "execution_policy": {"mode": "center_only", "coordinator_ref": "center-a"}, "consent_refs": {"exploration": None, "execution": None}, "budget_ref": "root-budget-1"}
    plan = {"schema": "ddp-plan-admission/1#TaskPlan", "plan_id": "plan-1", "revision": 1, "task_spec_digest": task_spec_digest(spec),
            "root_coordinator_node_id": local, "planning_state": "ready", "steps": [{"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": "center-a", "depends_on": [], "fixed_inputs": ["input-1"]}],
            "data_edges": [{"edge_id": "edge-query", "from_node_id": local, "to_node_id": "center-a", "payload_kind": "query_text", "retention": "temporary", "authorised_by": "local:" + local}],
            "budget": {"max_requests": 4, "max_bytes": 4096, "max_generation_tokens": 100, "max_hops": 2, "deadline": EXPIRY},
            "final_result_writer": local, "valid_until": EXPIRY, "execution_consent_ref": None}
    plan["plan_digest"] = task_plan_digest(plan)
    return {"task_spec": spec, "plan": plan,
            "input_manifest": [{"ref": "input-1", "digest": content_digest(b"approved file"), "size_bytes": len(b"approved file")}],
            "payload_bindings": [{"payload_id": phase + "-query", "phase": phase, "recipient_node_id": "center-a", "payload_kind": "query_text", "digest": content_digest(payload), "size_bytes": len(payload), **({"edge_id": "edge-query"} if phase == "execution" else {})} for phase in ("exploration", "execution")],
            "output_locations": ["local:workspace-a"], "retention": "temporary",
            "exploration": {"egress_mode": "listed_nodes", "allowed_payload": ["query_text"], "allowed_recipients": ["center-a"], "budget": {"max_probe_requests": 1, "max_egress_bytes": 4096}, "valid_until": EXPIRY}}


def source_policies(scope):
    return {"local:local-env": {"source_node_id": "local-env", "allowed_recipients": ["center-a"], "allowed_payload": ["query_text"], "allowed_retention": ["temporary"], "valid_until": EXPIRY}}


def redigest(scope):
    scope["plan"]["task_spec_digest"] = task_spec_digest(scope["task_spec"])
    scope["plan"]["plan_digest"] = task_plan_digest(scope["plan"])
    return scope
