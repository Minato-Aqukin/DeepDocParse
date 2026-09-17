"""Deterministic typed plans and consent boundaries, independent of transport/storage.

Digests use UTF-8 JSON with sorted object keys, no whitespace or NaN, preserving
array order. Plan state and consent references are excluded to avoid a circular
signature; all authority-bearing content is also bound by the local scope digest.
This module never grants permission from a model suggestion or a remote claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ddp_core.application.ports import ApplicationError

NODE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
OPERATIONS = {"parse", "index", "retrieve", "fuse", "rerank", "answer", "source_manifest", "wiki_pages", "validate", "deliver"}
PAYLOADS = {"query_text", "evidence_excerpts", "source_files", "parsed_layout", "embeddings", "answer_text", "wiki_draft"}
PROBE_PAYLOADS = {"query_text", "subquery_text", "entity_names", "resource_names", "collection_filters", "evidence_excerpts", "source_files"}
RETENTION = {"temporary", "task_pinned", "persistent"}
# Registered transition families; shell, arbitrary code and reverse execution
# dependencies cannot be introduced by a generated plan.
PREDECESSORS = {
    "parse": set(), "index": {"parse"}, "retrieve": {"index"},
    "fuse": {"retrieve"}, "rerank": {"retrieve", "fuse"},
    "answer": {"retrieve", "fuse", "rerank"},
    "source_manifest": {"retrieve", "fuse", "rerank"},
    "wiki_pages": {"source_manifest"}, "validate": {"answer", "wiki_pages"},
    "deliver": {"index", "validate"},
}


def reject(code="invalid_plan", message="invalid typed plan"):
    raise ApplicationError(code, message)


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        reject("invalid_plan", "plan must contain finite JSON values")


def content_digest(content: bytes) -> str:
    if not isinstance(content, bytes):
        reject("input_changed", "authorization requires concrete immutable bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def digest(value) -> str:
    return content_digest(canonical_bytes(value))


def task_spec_digest(spec: dict) -> str:
    return digest({k: v for k, v in spec.items() if k != "consent_refs"})


def task_plan_digest(plan: dict) -> str:
    return digest({k: v for k, v in plan.items() if k not in {"plan_digest", "planning_state", "execution_consent_ref"}})


def obj(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        reject(message="typed object has missing or unknown fields")


def string(value, *, node=False, checksum=False, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value) > 65536:
        reject(message="invalid string field")
    if node and not NODE.fullmatch(value):
        reject(message="invalid node identity")
    if checksum and not DIGEST.fullmatch(value):
        reject(message="invalid content digest")


def strings(value, *, node=False):
    if not isinstance(value, list) or len(value) > 1000:
        reject(message="invalid list field")
    for item in value:
        string(item, node=node)
    if len(set(value)) != len(value):
        reject(message="duplicate list value")


def integer(value, minimum=0):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        reject(message="invalid nonnegative budget")


def instant(value):
    string(value)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value):
        reject(message="expiry must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except ValueError:
        reject(message="a timezone-aware expiry is required")


def utc_instant(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def validate_spec(spec):
    obj(spec, ("schema", "protocol", "operation", "workspace_ref", "resource_scope", "search_policy", "execution_policy", "consent_refs", "budget_ref"), ("query", "requirements"))
    if spec["schema"] != "ddp-task-probe/1#TaskSpec" or spec["protocol"] != "ddp-task/1":
        reject(message="unsupported task schema")
    for key in ("operation", "workspace_ref", "budget_ref"):
        string(spec[key])
    if "query" in spec:
        string(spec["query"], empty=True)
    scope = spec["resource_scope"]
    obj(scope, ("kind",), ("scope_ref", "resource_refs"))
    if scope["kind"] not in {"local_only", "site_public", "federation_public", "fixed_resources"}:
        reject(message="unsupported resource scope")
    if scope.get("scope_ref") is not None:
        string(scope["scope_ref"])
    if "resource_refs" in scope:
        strings(scope["resource_refs"])
    search = spec["search_policy"]
    obj(search, ("mode",), ("ordering",))
    if search["mode"] not in {"fast", "exhaustive_scope"} or search.get("ordering", "local_first") not in {"local_first", "cost_first", "freshness_first"}:
        reject(message="unsupported search policy")
    if search["mode"] == "exhaustive_scope" and not scope.get("scope_ref"):
        reject(message="exhaustive scope requires a sealed scope reference")
    execution = spec["execution_policy"]
    obj(execution, ("mode",), ("coordinator_ref",))
    if execution["mode"] not in {"local_only", "trusted_federation", "center_only"}:
        reject(message="unsupported execution policy")
    if "coordinator_ref" in execution:
        string(execution["coordinator_ref"], node=True)
    if execution["mode"] == "local_only" and scope["kind"] == "federation_public":
        reject("local_only", "local execution forbids federation scope")
    obj(spec["consent_refs"], ("exploration", "execution"))
    for ref in spec["consent_refs"].values():
        if ref is not None:
            string(ref)
    if "requirements" in spec:
        obj(spec["requirements"], (), ("citations", "output_schema"))
        if spec["requirements"].get("citations", "required") not in {"required", "preferred", "not_required"}:
            reject(message="invalid citation requirement")
        if "output_schema" in spec["requirements"]:
            string(spec["requirements"]["output_schema"])


def validate_plan(plan, spec, *, local_node_id, now):
    validate_spec(spec)
    obj(plan, ("schema", "plan_id", "revision", "plan_digest", "task_spec_digest", "root_coordinator_node_id", "planning_state", "steps", "data_edges", "budget", "final_result_writer", "valid_until"), ("execution_consent_ref",))
    if plan["schema"] != "ddp-plan-admission/1#TaskPlan":
        reject(message="unsupported plan schema")
    string(plan["plan_id"])
    integer(plan["revision"], 1)
    if plan["task_spec_digest"] != task_spec_digest(spec) or plan["plan_digest"] != task_plan_digest(plan):
        reject("plan_changed", "task or plan digest differs from approved content")
    if plan["planning_state"] not in {"draft", "exploring", "ready", "awaiting_approval", "approved", "invalidated"}:
        reject(message="unknown planning state")
    if plan["planning_state"] == "invalidated":
        reject("plan_changed", "plan has been invalidated")
    if plan["planning_state"] == "approved" and not plan.get("execution_consent_ref"):
        reject("consent_required", "approved plan needs an execution consent")
    for key in ("root_coordinator_node_id", "final_result_writer"):
        string(plan[key], node=True)
    budget = plan["budget"]
    obj(budget, ("max_requests", "max_bytes", "max_hops", "deadline"), ("max_generation_tokens",))
    for key in ("max_requests", "max_bytes", "max_generation_tokens"):
        integer(budget.get(key, 0))
    integer(budget["max_hops"], 1)
    if min(instant(plan["valid_until"]), instant(budget["deadline"])) <= now:
        reject("consent_expired", "plan or root budget has expired")
    if not isinstance(plan["steps"], list) or not 1 <= len(plan["steps"]) <= 1000:
        reject(message="plan needs bounded registered steps")
    steps = {}
    for step in plan["steps"]:
        obj(step, ("step_id", "operation", "executor_node_id", "depends_on"), ("fixed_inputs", "probe_refs"))
        string(step["step_id"])
        string(step["executor_node_id"], node=True)
        if step["operation"] not in OPERATIONS or step["step_id"] in steps:
            reject(message="unregistered operation or duplicate step")
        for field in ("depends_on", "fixed_inputs", "probe_refs"):
            strings(step.get(field, []))
        steps[step["step_id"]] = step
    visiting, done = set(), set()

    def visit(identifier):
        if identifier in visiting or identifier not in steps:
            reject(message="cyclic or dangling dependency")
        if identifier in done:
            return
        visiting.add(identifier)
        step = steps[identifier]
        for parent in step["depends_on"]:
            visit(parent)
            if steps[parent]["operation"] not in PREDECESSORS[step["operation"]]:
                reject(message="dependency is outside registered workflow templates")
        if step["operation"] not in {"parse", "retrieve"} and not step["depends_on"]:
            reject(message="registered workflow step requires typed prerequisites")
        visiting.remove(identifier)
        done.add(identifier)

    for identifier in steps:
        visit(identifier)
    if not isinstance(plan["data_edges"], list) or len(plan["data_edges"]) > 1000:
        reject(message="invalid data edge list")
    edges, nodes = set(), {plan["root_coordinator_node_id"], plan["final_result_writer"]}
    nodes.update(s["executor_node_id"] for s in steps.values())
    for edge in plan["data_edges"]:
        obj(edge, ("edge_id", "from_node_id", "to_node_id", "payload_kind", "retention", "authorised_by"), ("relay_via",))
        for key in ("edge_id", "authorised_by"):
            string(edge[key])
        for key in ("from_node_id", "to_node_id"):
            string(edge[key], node=True)
            nodes.add(edge[key])
        strings(edge.get("relay_via", []), node=True)
        nodes.update(edge.get("relay_via", []))
        if edge["edge_id"] in edges or edge["payload_kind"] not in PAYLOADS or edge["retention"] not in RETENTION:
            reject(message="invalid typed data edge")
        if len(edge.get("relay_via", [])) + 1 > budget["max_hops"]:
            reject("budget_exceeded", "edge exceeds root hop budget")
        edges.add(edge["edge_id"])
    # Conservatively reserve every planned transmission, including relay hops,
    # against the root cap. Parallel branches do not acquire free hop budgets.
    if sum(1 + len(edge.get("relay_via", [])) for edge in plan["data_edges"]) > budget["max_hops"]:
        reject("budget_exceeded", "total planned transmissions exceed the root hop budget")
    for step in steps.values():
        for parent_id in step["depends_on"]:
            parent = steps[parent_id]
            if parent["executor_node_id"] == step["executor_node_id"]:
                continue
            compatible = {
                "parse": {"parsed_layout"}, "index": {"embeddings", "parsed_layout"},
                "retrieve": {"evidence_excerpts"}, "fuse": {"evidence_excerpts"},
                "rerank": {"evidence_excerpts"}, "source_manifest": {"evidence_excerpts"},
                "answer": {"answer_text"}, "wiki_pages": {"wiki_draft"},
                "validate": {"answer_text", "wiki_draft"},
            }.get(parent["operation"], set())
            if not any(edge["from_node_id"] == parent["executor_node_id"]
                       and edge["to_node_id"] == step["executor_node_id"]
                       and edge["payload_kind"] in compatible for edge in plan["data_edges"]):
                reject("policy_denied", "cross-node dependency has no typed data transfer edge")
    execution = spec["execution_policy"]
    if execution["mode"] == "center_only":
        if not execution.get("coordinator_ref") or nodes - {local_node_id, execution["coordinator_ref"]}:
            reject("policy_denied", "center-only policy cannot expand to third-party nodes")
    if spec["execution_policy"]["mode"] == "local_only" and nodes - {local_node_id}:
        reject("local_only", "local-only policy forbids remote execution and relay nodes")
    return nodes


def validate_scope(scope, *, local_node_id, now, source_policies):
    """source_policies is a trusted adapter snapshot, never a model/request claim."""
    # transport_bindings is optional. It used to be missing here, so every scope that
    # carried one failed this shape check and the reviewed-transport rules below were
    # unreachable; ConsentStore.prepare already admitted the field.
    obj(scope, ("task_spec", "plan", "input_manifest", "payload_bindings", "output_locations", "retention", "exploration"), ("transport_bindings",))
    spec, plan = scope["task_spec"], scope["plan"]
    nodes = validate_plan(plan, spec, local_node_id=local_node_id, now=now)
    strings(scope["output_locations"])
    if not scope["output_locations"] or scope["retention"] not in RETENTION:
        reject(message="reviewable output location and retention are required")
    inputs = {}
    if not isinstance(scope["input_manifest"], list) or len(scope["input_manifest"]) > 1000:
        reject(message="invalid input manifest")
    for item in scope["input_manifest"]:
        obj(item, ("ref", "digest", "size_bytes"))
        string(item["ref"])
        string(item["digest"], checksum=True)
        integer(item["size_bytes"])
        if item["ref"] in inputs:
            reject(message="duplicate fixed input")
        inputs[item["ref"]] = item
    if any(ref not in inputs for step in plan["steps"] for ref in step.get("fixed_inputs", [])):
        reject("input_changed", "every fixed input requires a content manifest")
    exploration = scope["exploration"]
    obj(exploration, ("egress_mode", "allowed_payload", "allowed_recipients", "budget", "valid_until"))
    strings(exploration["allowed_payload"])
    strings(exploration["allowed_recipients"], node=True)
    if exploration["egress_mode"] not in {"local_only", "listed_nodes"}:
        reject("policy_denied", "trust domains must first resolve to a fixed recipient set")
    if set(exploration["allowed_payload"]) - PROBE_PAYLOADS:
        reject(message="unknown exploration payload category")
    obj(exploration["budget"], ("max_probe_requests", "max_egress_bytes"), ("max_discovery_requests",))
    for value in exploration["budget"].values():
        integer(value)
    if instant(exploration["valid_until"]) <= now:
        reject("consent_expired", "exploration scope has expired")
    local_only = spec["execution_policy"]["mode"] == "local_only"
    if local_only and exploration["egress_mode"] != "local_only":
        reject("local_only", "local-only execution requires local-only exploration")
    if exploration["egress_mode"] == "local_only" or local_only:
        if exploration["allowed_payload"] or exploration["allowed_recipients"] or any(exploration["budget"].values()):
            reject("local_only", "local-only exploration forbids payloads, recipients and remote budget")
    elif not exploration["allowed_recipients"]:
        reject(message="listed-node exploration requires fixed recipients")
    edges = {e["edge_id"]: e for e in plan["data_edges"]}
    for edge in edges.values():
        policy = source_policies.get(edge["authorised_by"])
        recipients = {edge["to_node_id"], *edge.get("relay_via", [])}
        # Follow all downstream data edges: re-export via the coordinator cannot
        # turn a source's B→C denial into B→A→C permission, including derivatives.
        while True:
            expanded = recipients | {n for child in edges.values() if child["from_node_id"] in recipients for n in [child["to_node_id"], *child.get("relay_via", [])]}
            if expanded == recipients:
                break
            recipients = expanded
        recipients.discard(edge["from_node_id"])
        if (edge["retention"] != scope["retention"] or not policy or policy.get("source_node_id") != edge["from_node_id"]
                or recipients - set(policy.get("allowed_recipients", []))
                or edge["payload_kind"] not in policy.get("allowed_payload", [])
                or edge["retention"] not in policy.get("allowed_retention", [])
                or not policy.get("valid_until") or instant(policy["valid_until"]) <= now):
            reject("policy_denied", "source policy does not authorize this edge and every relay")
    transports = {}
    for transport in scope.get("transport_bindings", []):
        validate_transport(transport)
        if transport["transport_ref"] in transports:
            reject(message="duplicate transport binding")
        transports[transport["transport_ref"]] = transport
    seen = set()
    if not isinstance(scope["payload_bindings"], list) or len(scope["payload_bindings"]) > 1000:
        reject(message="invalid payload bindings")
    for payload in scope["payload_bindings"]:
        obj(payload, ("payload_id", "phase", "recipient_node_id", "payload_kind", "digest", "size_bytes"), ("edge_id", "generation_tokens", "transport_ref"))
        string(payload["payload_id"])
        string(payload["recipient_node_id"], node=True)
        string(payload["digest"], checksum=True)
        integer(payload["size_bytes"])
        if payload["payload_id"] in seen:
            reject(message="duplicate payload binding")
        seen.add(payload["payload_id"])
        recipient = payload["recipient_node_id"]
        if "transport_ref" in payload or transports:
            transport = transports.get(payload.get("transport_ref"))
            if not transport or transport["recipient_node_id"] != recipient:
                reject("policy_denied", "payload requires its exact reviewed transport")
        integer(payload.get("generation_tokens", 0))
        generates = payload["phase"] == "execution" and any(step["operation"] in {"answer", "wiki_pages"} and step["executor_node_id"] == recipient for step in plan["steps"])
        if generates and payload.get("generation_tokens", 0) < 1:
            reject("budget_exceeded", "payloads to generation steps require a fixed positive token reservation")
        if payload.get("generation_tokens", 0) > plan["budget"].get("max_generation_tokens", 0):
            reject("budget_exceeded", "payload token reservation exceeds the root generation budget")
        if payload["payload_kind"] in {"source_files", "parsed_layout", "embeddings", "evidence_excerpts"} and not inputs:
            reject("input_changed", "source and derived payloads need a verified input manifest")
        if payload["payload_kind"] == "source_files" and not any(item["digest"] == payload["digest"] and item["size_bytes"] == payload["size_bytes"] for item in inputs.values()):
            reject("input_changed", "source-file payload must match a verified snapshot")
        if local_only and recipient != local_node_id:
            reject("local_only", "local-only policy forbids every remote payload")
        if payload["phase"] == "exploration":
            if recipient not in exploration["allowed_recipients"] or payload["payload_kind"] not in exploration["allowed_payload"]:
                reject("policy_denied", "probe payload exceeds exploration boundary")
        elif payload["phase"] == "execution":
            edge = edges.get(payload.get("edge_id"))
            if not edge or recipient not in {edge["to_node_id"], *edge.get("relay_via", [])} or payload["payload_kind"] != edge["payload_kind"]:
                reject("policy_denied", "execution payload needs its exact approved data edge")
        else:
            reject(message="unknown payload phase")
    return nodes


def validate_transport(value):
    """Public recipient binding; it never contains or grants a credential."""
    obj(value, ("transport_ref", "recipient_node_id", "environment_id", "workspace_id",
                "profile_id", "issuer", "subject", "endpoint"))
    for key, item in value.items():
        string(item, node=key == "recipient_node_id")
    if value["environment_id"] != value["recipient_node_id"] or value["issuer"] != value["recipient_node_id"]:
        reject("policy_denied", "transport must bind the paired authority")
    try:
        url = urlsplit(value["endpoint"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or value["endpoint"].endswith("/")
                or any(c.isspace() or ord(c) < 32 for c in value["endpoint"])):
            raise ValueError()
        _ = url.port
    except ValueError:
        reject("policy_denied", "transport requires the exact paired HTTPS endpoint")
