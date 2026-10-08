"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_peers import Delegation, PeerUnavailable
from ddp_core.application import coverage as coverage_kernel, plans, routing
from ddp_corpus import federation, federation_budget
from sqlalchemy import select
from ddp_corpus.models import utcnow

from ddp_corpus.federation_tasks.common import (
    _reservation_step,
    _target_identity,
    _ts,
)
from ddp_corpus.federation_tasks.consent import (peer_directory)
from ddp_corpus.federation_tasks.steps import (
    _admission_body,
    _lookup_local_receipt,
    _lookup_remote_receipt,
    _poll_execution,
    _receipt_binding_error,
    _run_local_step,
    _run_remote_step,
    _step_inputs,
    _unknown_admission,
)
from ddp_corpus.federation_tasks.targets import (_steps_by_target)

def validate_delegation_report(report, step, *, scope_ref, query_digest=None,
                               query_digests=None):
    """A relay report is untrusted: reject the entire report on any boundary breach."""
    if not isinstance(report, dict) or set(report) != {"entries", "consumption", "evidence"}:
        raise ApplicationError("protocol_incompatible", "invalid delegation report")
    consumption = report["consumption"]
    if not isinstance(consumption, dict) or set(consumption) != {"requests", "bytes", "hops", "probes"}:
        raise ApplicationError("protocol_incompatible", "invalid reported consumption")
    for counter, value in consumption.items():
        cap = step["budget_share"]["max_" + counter]
        if type(value) is not int or value < 0 or value > cap:
            raise ApplicationError("budget_exceeded", "reported consumption exceeds delegated share")
    assigned = {_target_identity(item["target_key"]) for item in step["delegated_targets"]}
    entries = report["entries"]
    if not isinstance(entries, list) or not entries:
        raise ApplicationError("protocol_incompatible", "delegated report omits or adds leaf targets")
    digests = list(query_digests) if query_digests is not None else [query_digest]
    allowed = set(digests)
    seen, merged = set(), []
    for entry in entries:
        coverage_kernel.validate_entry(entry)
        key = _target_identity(entry["target_key"])
        # A child running the same bound subqueries reports one entry per
        # (target, digest); an older single-query child reports one per target
        # with a foreign digest the parent rebinds to digests[0] (boundary test
        # pins this). Duplicates are per (target, digest), not per target.
        digest = entry.get("query_or_subquery_digest")
        if digest not in allowed:
            digest = digests[0]
        if key not in assigned or (key, digest) in seen:
            raise ApplicationError("protocol_incompatible", "delegated leaf lies outside its allocation")
        seen.add((key, digest))
        merged.append({**entry, "scope_ref": scope_ref, "query_or_subquery_digest": digest,
                       "reported_by": step["executor_node_id"]})
    if {key for key, _ in seen} != assigned:
        raise ApplicationError("protocol_incompatible", "delegated report omits or adds leaf targets")
    items = report["evidence"]
    if not isinstance(items, list) or len(items) > 1000:
        raise ApplicationError("protocol_incompatible", "unbounded delegated evidence")
    origins = {key[0] for key in assigned}
    routes = {}
    for item in step["delegated_targets"]:
        routes[item["target_key"]["origin_node_id"]] = list(item["via_node_ids"])
    references = {(entry["target_key"]["origin_node_id"], ref)
                  for entry in merged for ref in entry.get("evidence_refs", [])}
    provided = {(item.get("origin_node_id"), item.get("evidence_id"))
                for item in items if isinstance(item, dict)}
    if not references <= provided:
        raise ApplicationError("protocol_incompatible",
                               "delegated report references evidence it does not carry")
    for item in items:
        if not isinstance(item, dict) or item.get("origin_node_id") not in origins \
                or (item.get("origin_node_id"), item.get("evidence_id")) not in references:
            raise ApplicationError("protocol_incompatible", "delegated evidence lies outside its leaf allocation")
        for field in ("evidence_id", "resource_id", "source_version_id", "parse_revision", "policy_revision"):
            plans.string(item.get(field))
        plans.string(item.get("authority_node_id"), node=True)
        plans.string(item.get("source_digest"), checksum=True)
        excerpt = item.get("excerpt")
        if federation.excerpt_reason(excerpt) is not None \
                or plans.content_digest(excerpt.encode("utf-8")) != item.get("excerpt_digest"):
            raise ApplicationError("input_not_verified", "delegated excerpt digest differs from concrete content")
        relay = item.get("relay_via", [])
        plans.strings(relay, node=True)
        expected = [*reversed(routes[item["origin_node_id"]]), step["executor_node_id"]]
        if list(relay) != expected and list(relay) != expected[1:]:
            raise ApplicationError("protocol_incompatible", "delegated evidence relay does not match its route")
    return {"entries": merged, "consumption": consumption, "evidence": items}

async def _run_delegate_step(peers, actor, *, root_task_id, plan, task_spec, consent,
                             step, budget, spend, scope_ref, query_digest=None,
                             query_digests=None, delegation_path=None, reconcile=False):
    digests = list(query_digests) if query_digests is not None else [query_digest]
    await federation_budget.reserve_share(
        root_task_id=root_task_id, organization_id=actor.organization_id,
        step_id=_reservation_step(step), share=step["budget_share"], budget=budget, now=utcnow())
    client = peers.client(step["executor_node_id"])
    body = _admission_body(root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                           consent=consent, step=step, inputs=_step_inputs(task_spec.get("query") or ""),
                           generation=0)
    body["delegation_path"] = delegation_path or [plan["root_coordinator_node_id"]]
    key = body["idempotency_key"]
    receipt = None
    if reconcile:
        await spend(kind="request")
        receipt = await _lookup_remote_receipt(client, key)
    if receipt is None:
        await spend(kind="hops", amount=2, step_id=_reservation_step(step))
        await spend(kind="request")
        await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(body)))
        try:
            receipt = await client.admit(body, idempotency_key=key)
        except PeerUnavailable as exc:
            if not _unknown_admission(exc):
                raise
            await spend(kind="request")
            receipt = await _lookup_remote_receipt(client, key)
            if receipt is None:
                raise
    mismatch = _receipt_binding_error(receipt, key=key, root_task_id=root_task_id,
                                      step_id=step["step_id"], plan_digest=plan["plan_digest"],
                                      executor_node_id=step["executor_node_id"])
    if mismatch or receipt.get("state") != "accepted":
        raise ApplicationError("protocol_incompatible", mismatch or "delegation not accepted")
    status = await _poll_execution(client, receipt["executor_task_id"], spend=spend,
                                   budget=budget, deadline_ts=plans.instant(step["budget_share"]["deadline"]))
    if status.get("state") != "succeeded":
        raise ApplicationError("peer_unavailable" if status.get("state") == "unreachable"
                               else status.get("error") or "protocol_incompatible", "delegation did not succeed")
    report = status.get("delegation_report")
    await spend(kind="bytes", amount=len(plans.canonical_bytes(report)))
    await federation_budget.record_share_report(
        root_task_id=root_task_id, organization_id=actor.organization_id,
        step_id=_reservation_step(step), reported=report.get("consumption") if isinstance(report, dict) else {},
        now=utcnow())
    return _fan_out_report(report, step, scope_ref=scope_ref, digests=digests)


def _fan_out_report(report, step, *, scope_ref, digests):
    """Validate (target, digest) entries the child already fanned out (identity if 1)."""
    return validate_delegation_report(report, step, scope_ref=scope_ref,
                                      query_digests=list(digests))


def _delegation_failed_entries(step, *, scope_ref, query_digest=None, query_digests=None,
                               state, error):
    digests = list(query_digests) if query_digests is not None else [query_digest]
    entries = []
    for assigned in step["delegated_targets"]:
        for digest in digests:
            entry = coverage_kernel.new_entry(assigned["target_key"], scope_ref, digest)
            entry = coverage_kernel.record(entry, None, state=state, error=error,
                                           now=_ts(utcnow()))
            entry["reported_by"] = step["executor_node_id"]
            entries.append(entry)
    return entries

async def execute_delegation(session, actor, spec, *, execution_id, now, http, index):
    """Coordinate only allocated leaves using a durable ledger owned by this execution."""
    from ddp_corpus.db import get_sessionmaker
    request, delegated_step = spec["request"], spec["step"]
    node = federation.local_node_id()
    share = delegated_step["budget_share"]
    budget_caps = {
        "max_requests": share["max_requests"], "max_bytes": share["max_bytes"],
        "max_hops": share["max_hops"], "deadline": share["deadline"],
        "max_probe_requests": share["max_probes"], "max_egress_bytes": share["max_bytes"],
        "max_discovery_requests": 0, "max_generation_tokens": 0}
    async with get_sessionmaker()() as ledger_session:
        ledger = await federation_budget.ensure_ledger(
            ledger_session, root_task_id=execution_id, organization_id=actor.organization_id,
            caller_budget=None, server_caps=budget_caps, now=now)
        await ledger_session.commit()
    budget = federation_budget.rebuild_from_ledger(ledger, None, now=_ts(now))
    async def spend(kind, amount=1, step_id=None):
        await federation_budget.spend(root_task_id=execution_id, organization_id=actor.organization_id,
                                      kind=kind, amount=amount, step_id=step_id, budget=budget, now=utcnow())
    targets = [item["target_key"] for item in delegated_step["delegated_targets"]]
    routes = []
    for item in delegated_step["delegated_targets"]:
        rest = [hop for hop in item["via_node_ids"] if hop != node]
        if rest:
            routes.append({"node_id": item["target_key"]["origin_node_id"], "via_node_ids": rest})
    steps, edges = routing.plan_steps(targets=targets, probes=[], local_node_id=node,
                                     coordinator_node_id=node, query=request["task_spec"].get("query") or "",
                                     now=_ts(now), node_routes=routes,
                                     # Sub-delegates are admitted one level deeper than this node.
                                     delegation_depth=len(request["delegation_path"]), budget={
                                         **budget_caps,
                                         # The delegate already spent part of its
                                         # share (lookups, local steps) before
                                         # carving sub-shares: carve from rest.
                                         "used_requests": budget.used()["requests"],
                                         "used_bytes": budget.used()["bytes"],
                                         "used_probes": budget.used()["probes"],
                                         "used_hops": budget.used()["hops"]})
    mapping = _steps_by_target({"steps": steps}, targets)
    for target in targets:
        step = mapping[_target_identity(target)]
        if step["operation"] == "retrieve":
            step["fixed_inputs"] = ["query", "collection:" + target["collection_id"]]
    path = request["delegation_path"]
    final_node = request["plan"]["final_result_writer"]
    edges.append({"edge_id": "edge-final-recipient", "from_node_id": node, "to_node_id": final_node,
                  "relay_via": list(reversed(path[1:])), "payload_kind": "evidence_excerpts",
                  "retention": request["execution_consent"]["retention"], "authorised_by": f"relay:{node}"})
    # Downstream generation is onward transfer too: retain the approved root's
    # evidence-bearing edges leaving its final recipient, without expanding recipients.
    for edge in request["plan"]["data_edges"]:
        if edge["from_node_id"] == final_node and edge["payload_kind"] != "query_text":
            edges.append({**edge, "edge_id": "upstream-" + edge["edge_id"]})
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": "child-" + execution_id, "revision": 1,
        "task_spec_digest": plans.task_spec_digest(request["task_spec"]),
        "root_coordinator_node_id": node, "final_result_writer": final_node,
        "planning_state": "approved", "execution_consent_ref": request["execution_consent"]["consent_id"],
        "steps": steps, "data_edges": edges,
        "budget": {key: budget_caps[key] for key in ("max_requests", "max_bytes", "max_hops", "deadline")},
        "valid_until": share["deadline"]}
    plan["plan_digest"] = plans.task_plan_digest(plan)
    consent = {**request["execution_consent"], "plan_digest": plan["plan_digest"],
               "allowed_edges": [edge["edge_id"] for edge in edges]}
    plans.validate_plan(plan, request["task_spec"], local_node_id=node, now=_ts(now))
    peers = peer_directory(actor, Delegation(root_task_id=execution_id, task_spec_digest=plan["task_spec_digest"]))
    from ddp_corpus.federation_tasks.intent import _subquery_digests
    query_digests = _subquery_digests(request["task_spec"])
    from ddp_corpus.federation_tasks.execution import _subquery_texts as _bound_texts
    subquery_texts = _bound_texts(request["task_spec"])
    entries, evidence, executed = [], [], set()
    try:
        for target in targets:
            step = mapping[_target_identity(target)]
            if step["step_id"] in executed:
                continue
            executed.add(step["step_id"])
            try:
                if step["operation"] == "delegate":
                    report = await _run_delegate_step(
                        peers, actor, root_task_id=execution_id, plan=plan, task_spec=request["task_spec"],
                        consent=consent, step=step, budget=budget, spend=spend,
                        scope_ref=execution_id, query_digests=query_digests,
                        delegation_path=path + [node], reconcile=True)
                    entries.extend(report["entries"])
                    evidence.extend(report["evidence"])
                    continue
                receipt_refs = []
                if target["origin_node_id"] == node:
                    state, error, items, revision, limits = await _run_local_step(
                        session, actor, root_task_id=execution_id, plan=plan,
                        task_spec=request["task_spec"], consent=consent, step=step, target=target,
                        generation=0, now=now, http=http, index=index, reconcile=True,
                        budget=budget, spend=spend)
                    local_receipt = await _lookup_local_receipt(session, actor, f"{execution_id}:{step['step_id']}")
                    if local_receipt:
                        receipt_refs.append("admission:" + local_receipt["admission_id"])
                else:
                    state, error, items, revision, limits = await _run_remote_step(
                        peers, root_task_id=execution_id, plan=plan, task_spec=request["task_spec"],
                        consent=consent, step=step, target=target, generation=0, reconcile=True,
                        budget=budget, spend=spend, receipt_refs=receipt_refs)
                from ddp_corpus.federation_tasks.execution import (
                    _attribute_evidence_to_subqueries as _attribute_items)
                answered = _attribute_items(
                    items, subquery_texts,
                    local_source=target["origin_node_id"] == node) \
                    if subquery_texts is not None and state == "succeeded" else {}
                for position, digest in enumerate(query_digests):
                    leaf = coverage_kernel.new_entry(target, execution_id, digest)
                    # The executed receipt, not a planning probe, is explicitly self-reported.
                    leaf_state, leaf_error = state, error
                    if state == "succeeded":
                        leaf["probe_receipts"] = receipt_refs
                        leaf["actual_index_revision"] = revision
                        if leaf["actual_index_revision"] is None:
                            leaf_state, leaf_error = "partial", "probe_receipt_missing"
                        elif (subquery_texts is not None
                                and position not in answered):
                            leaf_state, leaf_error = "not_attempted", "subquery_evidence_missing"
                    if leaf_state == "succeeded":
                        leaf["evidence_refs"] = [item["evidence_id"] for item in items]
                    else:
                        # Coordinator shape for unmatched slots: no evidence
                        # claimed. record() never increments attempts for
                        # not_attempted, so preset the one attempt this leaf
                        # actually made (receipts/revision preserved above).
                        leaf["evidence_refs"] = []
                        if leaf_state == "not_attempted":
                            leaf["attempts"] = 1
                    entries.append(coverage_kernel.record(leaf, None, state=leaf_state,
                                                         error=leaf_error, now=_ts(utcnow()),
                                                         limits=limits))
                evidence.extend(items)
            except (ApplicationError, APIError, PeerUnavailable) as exc:
                code = getattr(exc, "code", None) or "peer_unavailable"
                state = _delegation_failure_state(exc)
                assigned_step = step if step["operation"] == "delegate" else {
                    "executor_node_id": node, "delegated_targets": [{"target_key": target}]}
                entries.extend(_delegation_failed_entries(
                    assigned_step, scope_ref=execution_id, query_digests=query_digests,
                    state=state, error=code))
    finally:
        await peers.aclose()
    for item in evidence:
        via = list(item.get("relay_via", []))
        via = [entry for entry in via if entry != node]
        via.append(node)
        item["relay_via"] = via
    used = await federation_budget.ledger_used(
        session, root_task_id=execution_id, organization_id=actor.organization_id)
    from ddp_corpus.federation_models import FederationDelegationConsumption
    # The root ledger includes prepaid sub-shares. A successful sub-report replaces
    # that conservative reservation in the actual report only, never in the ledger.
    children = await session.scalars(select(FederationDelegationConsumption).where(
        FederationDelegationConsumption.root_task_id == execution_id))
    for child in children:
        reported = child.reported_json
        if not isinstance(reported, dict) or set(reported) != {"requests", "bytes", "hops", "probes"}:
            continue
        if any(type(reported[key]) is not int or not 0 <= reported[key] <= child.reserved_json["max_" + key]
               for key in reported):
            continue
        for key, value in reported.items():
            used[key] += value - child.reserved_json["max_" + key]
    return {"entries": entries, "evidence": evidence,
            "consumption": {key: used[key] for key in ("requests", "bytes", "hops", "probes")}}

def _delegation_failure_state(exc):
    if getattr(exc, "code", None) == "egress_denied" or getattr(exc, "status_code", None) == 403 \
            or getattr(exc, "status", None) == 403:
        return "denied"
    if isinstance(exc, PeerUnavailable) and exc.status is None \
            or getattr(exc, "code", None) == "peer_unavailable":
        return "unreachable"
    return "failed"
