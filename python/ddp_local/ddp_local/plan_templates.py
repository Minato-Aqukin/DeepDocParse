"""Fixed reviewable plan templates for the desktop host.

The renderer never composes a TaskSpec, TaskPlan, node set, endpoint or payload
binding. It names a paired center (resolved by the host) and a query; this module
turns that typed request into exactly one scope shape that the consent ledger
validates like any other prepared scope. Nothing here grants, sends or trusts.
"""
from __future__ import annotations

import uuid

from ddp_core.application.plans import content_digest, reject, task_plan_digest, task_spec_digest, utc_instant

CENTER_TRANSPORT_REF = "center"
STORAGE_TRANSPORT_REF = "center-storage"
QUERY_EDGE = "edge-query"
RETENTIONS = ("temporary", "task_pinned")
FILE_EDGE = "edge-file"
FILE_PARSE_OPERATION = "corpus.parse"
CONTROL_REQUEST_BUDGET = 32
CONTROL_METADATA_BYTES = 65536
CENTER_REQUEST_BUDGET = 96
CENTER_BYTE_BUDGET = 4 * 1024 * 1024
CENTER_GENERATION_BUDGET = 2048
CENTER_HOP_BUDGET = 16
PURPOSES = ("answer", "wiki")
ANSWER_OPERATION = "rag.answer.cited"
WIKI_OPERATION = "wiki.pages"


def _wiki_request(request, purpose):
    from ddp_core.application.plans import requirements_wiki
    wiki = request.get("wiki")
    if purpose == "wiki":
        if not isinstance(wiki, dict):
            reject("invalid_plan", "wiki purpose requires a typed wiki request")
        requirements_wiki(wiki, operation=WIKI_OPERATION)
        if wiki.get("wiki_id") is not None or wiki.get("base_revision_id") is not None:
            reject("invalid_plan", "desktop wiki plans always create a new Wiki")
        return {"title": wiki["title"].strip(), "max_pages": int(wiki.get("max_pages", 4))}
    if wiki is not None:
        reject("invalid_plan", "requirements.wiki is only valid for wiki.pages")
    return None


def _purpose(request):
    # Default only when the field is omitted: an explicit "" or null is not
    # silently treated as answer; it fails the membership check below.
    purpose = request.get("purpose", "answer")
    if purpose not in PURPOSES:
        reject("invalid_plan", "purpose must be answer or wiki")
    return purpose


def _control_transport(center):
    # The upload origin is a separate reviewed transport, never a field on
    # the control binding (including when Pydantic supplies its None default).
    return {key: value for key, value in center.items() if key != "upload_endpoint"}


def center_query_scope(request, *, local_node_id, workspace_id, now):
    """`center_query`: one query sent to one paired center, local inputs pinned only.

    Inputs stay local: the template binds no `source_files` payload, so their
    digests are rechecked on every approval and dispatch but no original byte is
    released. The query is the only payload, once per phase, over one edge.
    """
    center = _control_transport(request["center"])
    recipient = center["recipient_node_id"]
    _frozen_recipients(request, recipient)
    purpose = _purpose(request)
    wiki = _wiki_request(request, purpose)
    query = request["query"]
    payload = query.encode("utf-8")
    retention = request["retention"]
    valid_until = utc_instant(now + request["valid_seconds"])
    plan_id = "plan-" + uuid.uuid4().hex
    refs = [item["ref"] for item in request["inputs"]]
    spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": WIKI_OPERATION if purpose == "wiki" else ANSWER_OPERATION, "workspace_ref": workspace_id, "query": query,
        # The center searches what it publishes; local refs mean nothing to it.
        "resource_scope": {"kind": "site_public"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "center_only", "coordinator_ref": recipient},
        "consent_refs": {"exploration": None, "execution": None},
        "budget_ref": "budget-" + plan_id,
    }
    if wiki is not None:
        spec["requirements"] = {"wiki": wiki}
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
        "budget": {"max_requests": CONTROL_REQUEST_BUDGET + CENTER_REQUEST_BUDGET,
                   "max_bytes": CONTROL_METADATA_BYTES + CENTER_BYTE_BUDGET + 8 * len(payload),
                   "max_generation_tokens": CENTER_GENERATION_BUDGET,
                   "max_hops": 1 + CENTER_HOP_BUDGET, "deadline": valid_until},
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
                        "budget": {"max_probe_requests": CONTROL_REQUEST_BUDGET,
                                   "max_egress_bytes": CONTROL_METADATA_BYTES + 2 * len(payload)},
                        "valid_until": valid_until},
        "transport_bindings": [{"transport_ref": CENTER_TRANSPORT_REF, **center}],
    }
def center_file_scope(request, *, local_node_id, workspace_id, now):
    """`center_file_parse`: one approved local file sent once to one paired center.

    Unlike `center_query` (query text only, inputs stay local), this template
    binds exactly one `source_files` payload in the execution phase to the
    pinned input snapshot. The exploration phase still carries only a bounded
    descriptor (filename + digest + size, never the bytes), so the center can
    persist a waiting-input record before the bytes move. The execution bytes
    must equal the pinned manifest on every approval and dispatch; anything
    else fails closed as `input_changed`. Retention stays temporary: a remote
    parse never becomes a permanent center asset by default.
    """
    center = _control_transport(request["center"])
    recipient = center["recipient_node_id"]
    pinned = request["inputs"][0] if len(request["inputs"]) == 1 else None
    if pinned is None:
        reject("invalid_plan", "file parse binds exactly one pinned input")
    retention = request["retention"]
    valid_until = utc_instant(now + request["valid_seconds"])
    plan_id = "plan-" + uuid.uuid4().hex
    descriptor = ("%s\n%s\n%d\n" % (request["filename"], pinned["digest"], pinned["size_bytes"])).encode("utf-8")
    spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": FILE_PARSE_OPERATION, "workspace_ref": workspace_id,
        "query": descriptor.decode("utf-8"),
        "resource_scope": {"kind": "site_public"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "center_only", "coordinator_ref": recipient},
        "consent_refs": {"exploration": None, "execution": None},
        "budget_ref": "budget-" + plan_id,
    }
    step = {"step_id": "parse-1", "operation": "parse", "executor_node_id": recipient,
            "depends_on": [], "fixed_inputs": [pinned["ref"]]}
    # All real control sends and multipart attempts share the displayed root
    # cap. Payload permission checks do not charge a second time. Reserve
    # bounded protocol metadata plus one full-file retransmission allowance,
    # plus the bounded output fetch below (one Range per 1MiB chunk plus the
    # status read, each a 0-byte control reservation).
    part_size_floor = 5 * 1024 * 1024
    max_parts = (pinned["size_bytes"] + part_size_floor - 1) // part_size_floor
    fetch_requests = 2
    max_requests = CONTROL_REQUEST_BUDGET + 2 * max_parts + fetch_requests
    max_bytes = CONTROL_METADATA_BYTES + 2 * pinned["size_bytes"]
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": plan_id, "revision": 1,
        "task_spec_digest": task_spec_digest(spec), "root_coordinator_node_id": local_node_id,
        "planning_state": "ready", "steps": [step],
        "data_edges": [
            {"edge_id": QUERY_EDGE, "from_node_id": local_node_id, "to_node_id": recipient,
             "payload_kind": "query_text", "retention": retention,
             "authorised_by": "local:" + local_node_id},
            {"edge_id": FILE_EDGE, "from_node_id": local_node_id, "to_node_id": recipient,
             "payload_kind": "source_files", "retention": retention,
             "authorised_by": "local:" + local_node_id},
        ],
        "budget": {"max_requests": max_requests, "max_bytes": max_bytes, "max_generation_tokens": 0,
                   "max_hops": 2, "deadline": valid_until},
        "final_result_writer": local_node_id, "valid_until": valid_until,
        "execution_consent_ref": None,
    }
    plan["plan_digest"] = task_plan_digest(plan)
    probe = {"recipient_node_id": recipient, "payload_kind": "query_text",
             "digest": content_digest(descriptor), "size_bytes": len(descriptor),
             "transport_ref": CENTER_TRANSPORT_REF}
    # The original bytes never go to the control endpoint: execution-file is
    # bound to the paired object-upload origin (`center-storage`), fixed by
    # host pairing metadata (default = center origin). `transport_ref=center`
    # stays for describe/control; storage bytes use `center-storage`.
    upload = dict(center)
    upload["endpoint"] = request["center"].get("upload_endpoint") or center["endpoint"]
    source = {"recipient_node_id": recipient, "payload_kind": "source_files",
              "digest": pinned["digest"], "size_bytes": pinned["size_bytes"],
              "transport_ref": STORAGE_TRANSPORT_REF}
    return {
        "task_spec": spec, "plan": plan,
        "input_manifest": [dict(pinned)],
        "payload_bindings": [
            {"payload_id": "exploration-descriptor", "phase": "exploration", **probe},
            {"payload_id": "execution-file", "phase": "execution", "edge_id": FILE_EDGE, **source},
        ],
        "output_locations": ["local:" + workspace_id], "retention": retention,
        "exploration": {"egress_mode": "listed_nodes", "allowed_payload": ["query_text"],
                        "allowed_recipients": [recipient],
                        "budget": {"max_probe_requests": CONTROL_REQUEST_BUDGET,
                                   "max_egress_bytes": CONTROL_METADATA_BYTES + 2 * len(descriptor)},
                        "valid_until": valid_until},
        "transport_bindings": [{"transport_ref": CENTER_TRANSPORT_REF, **center},
                               {"transport_ref": STORAGE_TRANSPORT_REF, "recipient_node_id": recipient,
                                "environment_id": center["environment_id"], "workspace_id": center["workspace_id"],
                                "profile_id": center["profile_id"], "issuer": center["issuer"],
                                "subject": center["subject"], "endpoint": upload["endpoint"]}],
    }

def _frozen_recipients(request, recipient):
    """Resolve the frozen recipient set for a trusted template.

    `template=center_only` (default): exactly [recipient]. An explicit
    `recipients` must equal that singleton; a scope_manifest is refused.
    `template=trusted_federation`: explicit `recipients` of 1..100 node ids,
    always containing the center recipient. Never expanded from a peer list.
    """
    from ddp_core.application.plans import NODE, reject
    template = request.get("template") or "center_only"
    if template not in {"center_only", "trusted_federation"}:
        reject("invalid_plan", "template must be center_only or trusted_federation")
    given = request.get("recipients")
    if template == "center_only":
        if request.get("scope_manifest") is not None:
            reject("policy_denied", "center_only carries no frozen scope manifest")
        if given is None:
            return [recipient], template
        if list(given) != [recipient]:
            reject("policy_denied", "center_only cannot expand beyond the paired center")
        return [recipient], template
    if not isinstance(given, list) or not 1 <= len(given) <= 100 or len(set(given)) != len(given):
        reject("invalid_plan", "trusted_federation requires 1..100 unique recipients")
    for node in given:
        if not isinstance(node, str) or not NODE.fullmatch(node):
            reject("invalid_plan", "trusted_federation recipient is not a node identity")
    if recipient not in given:
        reject("policy_denied", "trusted_federation recipients must contain the paired center")
    return list(given), template


def center_trusted_query_scope(request, *, local_node_id, workspace_id, now):
    """Trusted multi-recipient query parent: federation_public/fast over a frozen set.

    resource_scope is federation_public with optional scope_ref (sealed scope id
    created by POST /api/v1/federation/scopes with allowed_node_ids=recipients).
    Without a scope_ref, exploration dispatch creates it inside the approved
    exploration budget (never a renderer-supplied manifest). The scope_manifest
    request field, when present, must be the center-returned persisted mirror
    (validated by digest on use, never trusted as authority).
    """
    center = _control_transport(request["center"])
    recipient = center["recipient_node_id"]
    recipients, _ = _frozen_recipients(request, recipient)
    query = request["query"]
    payload = query.encode("utf-8")
    retention = request["retention"]
    valid_until = utc_instant(now + request["valid_seconds"])
    plan_id = "plan-" + uuid.uuid4().hex
    refs = [item["ref"] for item in request["inputs"]]
    purpose = _purpose(request)
    wiki = _wiki_request(request, purpose)
    spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": WIKI_OPERATION if purpose == "wiki" else ANSWER_OPERATION, "workspace_ref": workspace_id, "query": query,
        "resource_scope": {"kind": "federation_public"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "trusted_federation", "coordinator_ref": recipient},
        "consent_refs": {"exploration": None, "execution": None},
        "budget_ref": "budget-" + plan_id,
    }
    if wiki is not None:
        spec["requirements"] = {"wiki": wiki}
    manifest = request.get("scope_manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("scope_id"), str):
        spec["resource_scope"]["scope_ref"] = manifest["scope_id"]
    step = {"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": recipient,
            "depends_on": []}
    if refs:
        step["fixed_inputs"] = refs
    # The displayed root covers device control and one immutable center slice.
    # Scope discovery/control sends consume the device share, never the
    # independently reserved center share. Child reviews cannot mint more.
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": plan_id, "revision": 1,
        "task_spec_digest": task_spec_digest(spec), "root_coordinator_node_id": local_node_id,
        "planning_state": "ready", "steps": [step],
        "data_edges": [{"edge_id": QUERY_EDGE, "from_node_id": local_node_id, "to_node_id": recipient,
                        "payload_kind": "query_text", "retention": retention,
                        "authorised_by": "local:" + local_node_id}],
        "budget": {"max_requests": CONTROL_REQUEST_BUDGET + CENTER_REQUEST_BUDGET,
                   "max_bytes": CONTROL_METADATA_BYTES + CENTER_BYTE_BUDGET + 8 * len(payload),
                   "max_generation_tokens": CENTER_GENERATION_BUDGET,
                   "max_hops": 1 + CENTER_HOP_BUDGET, "deadline": valid_until},
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
                        "allowed_recipients": recipients,
                        "budget": {"max_probe_requests": CONTROL_REQUEST_BUDGET,
                                   "max_discovery_requests": 8,
                                   "max_egress_bytes": CONTROL_METADATA_BYTES + 4 * len(payload)},
                        "valid_until": valid_until},
        "transport_bindings": [{"transport_ref": CENTER_TRANSPORT_REF, **center}],
    }
