"""Fixed reviewable plan templates for the desktop host.

The renderer never composes a TaskSpec, TaskPlan, node set, endpoint or payload
binding. It names a paired center (resolved by the host) and a query; this module
turns that typed request into exactly one scope shape that the consent ledger
validates like any other prepared scope. Nothing here grants, sends or trusts.
"""
from __future__ import annotations

import uuid

from ddp_core.application.plans import content_digest, task_plan_digest, task_spec_digest, utc_instant

CENTER_TRANSPORT_REF = "center"
QUERY_EDGE = "edge-query"
RETENTIONS = ("temporary", "task_pinned")


def center_query_scope(request, *, local_node_id, workspace_id, now):
    """`center_query`: one query sent to one paired center, local inputs pinned only.

    Inputs stay local: the template binds no `source_files` payload, so their
    digests are rechecked on every approval and dispatch but no original byte is
    released. The query is the only payload, once per phase, over one edge.
    """
    center = request["center"]
    recipient = center["recipient_node_id"]
    query = request["query"]
    payload = query.encode("utf-8")
    retention = request["retention"]
    valid_until = utc_instant(now + request["valid_seconds"])
    plan_id = "plan-" + uuid.uuid4().hex
    refs = [item["ref"] for item in request["inputs"]]
    spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "rag.answer.cited", "workspace_ref": workspace_id, "query": query,
        # The center searches what it publishes; local refs mean nothing to it.
        "resource_scope": {"kind": "site_public"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "center_only", "coordinator_ref": recipient},
        "consent_refs": {"exploration": None, "execution": None},
        "budget_ref": "budget-" + plan_id,
    }
    step = {"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": recipient,
            "depends_on": []}
    if refs:
        step["fixed_inputs"] = refs
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": plan_id, "revision": 1,
        "task_spec_digest": task_spec_digest(spec), "root_coordinator_node_id": local_node_id,
        "planning_state": "ready", "steps": [step],
        "data_edges": [{"edge_id": QUERY_EDGE, "from_node_id": local_node_id, "to_node_id": recipient,
                        "payload_kind": "query_text", "retention": retention,
                        "authorised_by": "local:" + local_node_id}],
        "budget": {"max_requests": 4, "max_bytes": 4 * len(payload), "max_generation_tokens": 0,
                   "max_hops": 1, "deadline": valid_until},
        "final_result_writer": local_node_id, "valid_until": valid_until,
        "execution_consent_ref": None,
    }
    plan["plan_digest"] = task_plan_digest(plan)
    binding = {"recipient_node_id": recipient, "payload_kind": "query_text",
               "digest": content_digest(payload), "size_bytes": len(payload),
               "transport_ref": CENTER_TRANSPORT_REF}
    return {
        "task_spec": spec, "plan": plan,
        "input_manifest": [dict(item) for item in request["inputs"]],
        "payload_bindings": [
            {"payload_id": "exploration-query", "phase": "exploration", **binding},
            {"payload_id": "execution-query", "phase": "execution", "edge_id": QUERY_EDGE, **binding},
        ],
        "output_locations": ["local:" + workspace_id], "retention": retention,
        "exploration": {"egress_mode": "listed_nodes", "allowed_payload": ["query_text"],
                        "allowed_recipients": [recipient],
                        "budget": {"max_probe_requests": 2, "max_egress_bytes": 2 * len(payload)},
                        "valid_until": valid_until},
        "transport_bindings": [{"transport_ref": CENTER_TRANSPORT_REF, **center}],
    }
