"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus import catalog, federation
from ddp_core.application import plans

from ddp_corpus.federation_tasks.common import (
    _EVENT_APPROVED,
    _append_event,
    _commit,
    _load_request,
    _ts,
)
from ddp_corpus.federation_tasks.consent import (
    _egress_denied,
    validate_execution_consent,
)

# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------


async def approve(session: AsyncSession, actor: Actor, root_task_id: str, *,
                  plan_digest: str, execution_consent: dict, now: datetime) -> dict:
    """批准精确的计划修订与外发边界；摘要不符 409 plan_changed，边界不覆盖 403。"""
    # 同上：同 root 的审批与规划串行化，重放不产生第二次副作用。
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    plan = row.plan_json
    if not plan or row.planning_state not in ("ready", "approved"):
        raise APIError(409, "task has no plan revision to approve", "invalid_request_error",
                       "plan_changed")
    if (row.planning_state == "approved"
            and row.execution_consent_ref == (execution_consent or {}).get("consent_id")
            and row.execution_consent_json == execution_consent):
        return plan
    if plan_digest != row.plan_digest \
            or (execution_consent or {}).get("plan_digest") != plan_digest:
        raise APIError(409, "submitted revision does not match the stored plan revision",
                       "invalid_request_error", "plan_changed")
    consent = validate_execution_consent(execution_consent, now=now)
    recipients = set(consent["allowed_recipients"])
    endpoints = {step["executor_node_id"] for step in plan["steps"]}
    for edge in plan["data_edges"]:
        endpoints.update(edge.get("relay_via") or [])
        endpoints.add(edge["from_node_id"])
        endpoints.add(edge["to_node_id"])
    if endpoints - recipients:
        raise _egress_denied("execution consent does not cover every executor and data edge")
    planned_edges = {edge["edge_id"] for edge in plan["data_edges"]}
    if not planned_edges <= set(consent["allowed_edges"]):
        raise _egress_denied("execution consent does not approve every data edge")
    if any(edge["retention"] != consent["retention"] for edge in plan["data_edges"]):
        raise _egress_denied("execution consent retention does not match the plan edges")
    # 批准时把 execution 引用写回 TaskSpec。两个摘要都不覆盖 consent_refs 与
    # planning_state/execution_consent_ref，所以这是对同一修订的补充，不是新修订。
    task_spec = dict(row.task_spec_json)
    task_spec["consent_refs"] = {**task_spec["consent_refs"],
                                 "execution": consent["consent_id"]}
    try:
        plans.validate_spec(task_spec)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    if plans.task_spec_digest(task_spec) != row.task_spec_digest:
        raise APIError(409, "task spec changed during approval", "invalid_request_error",
                       "plan_changed")
    approved = dict(plan, planning_state="approved",
                    execution_consent_ref=consent["consent_id"])
    if plans.task_plan_digest(approved) != row.plan_digest:
        raise APIError(409, "plan digest changed during approval", "invalid_request_error",
                       "plan_changed")
    try:
        plans.validate_plan(approved, task_spec, local_node_id=federation.local_node_id(),
                            now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    row.task_spec_json = task_spec
    row.execution_consent_json = consent
    row.execution_consent_ref = consent["consent_id"]
    row.plan_json = approved
    row.planning_state = "approved"
    row.updated_at = now
    await _append_event(session, row.root_task_id, _EVENT_APPROVED, {
        "plan_digest": row.plan_digest, "execution_consent_ref": consent["consent_id"],
    }, now=now)
    await _commit(session)
    return approved
