"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ddp_corpus.federation_models import FederationRequest

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import routing
from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_contracts.enums import FEDERATED_ANSWER_REASON_VALUES
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus import federation
from ddp_corpus.config import settings
from ddp_corpus.models import utcnow

from ddp_corpus.federation_tasks.common import (
    ANSWER_OPERATION,
    GENERATION_WITHHELD_FIELD,
    SOURCE_POLICY_DENIED,
    WIKI_OPERATION,
    _excerpt_reason,
    _generation_excerpt,
    _reservation_step,
    _unavailable_answer,
)
from ddp_corpus.federation_tasks.steps import (
    _delegated_answer,
    _delegated_wiki_draft,
    _wiki_failure,
)

async def _load_excerpts(session: AsyncSession, actor: Actor,
                         plan: dict) -> dict[str, str]:
    """从已落库的探测回执里取回每条融合证据的原文片段。

    执行时新检索到的证据在手上就带 `_excerpt`；这里补的是 resume 路径上被
    跳过的、上一轮已成功的目标 —— 结果里存的是公开信封（没有正文），而生成
    必须真的看到正文，否则 [n] 只是一串空编号。
    """
    excerpts: dict[str, str] = {}
    node = federation.local_node_id()
    for step in plan["steps"]:
        if step["operation"] != "retrieve":
            continue
        for probe_id in step.get("probe_refs") or []:
            probe = await session.get(FederationProbe, probe_id)
            if probe is None or probe.organization_id != actor.organization_id:
                continue
            # 来源按**协调者自己生成的计划**判：retrieve 步的执行者就是目标 origin。
            # 不用探测行的 `target_node_id` —— 远端行的这一列取自对端回执，坏对端
            # 报成本节点就能让自己的超长 `_excerpt` 被当成本地正文静默截断，
            # 绕过 N6 的显式拒绝。条目自报的 origin 同样不信。
            local_source = step["executor_node_id"] == node
            for item in (probe.result_json or {}).get("evidence") or []:
                evidence_id = str(item.get("evidence_id") or "")
                excerpt = _generation_excerpt(item, local_source=local_source)
                if evidence_id and excerpt is not None:
                    excerpts.setdefault(evidence_id, excerpt)
    return excerpts

async def _grounded_answer(http, *, query: str, fused: list[dict],
                           excerpts: dict[str, str], max_generation_tokens: int) -> dict:
    """本地带引用生成（委托 `federation.grounded_answer`，与远端执行者同一实现）。

    本地与远端只有在 provider 归属上不同：本地生成的 `provider.location` 是
    `local`。引用结构验收、越界/空白正文拒绝、超预算拒绝与绑定形状全部由共享
    实现决定，这里不再复制一份。
    """
    return await federation.grounded_answer(
        http, query=query,
        evidence_ids=[str(item.get("evidence_id") or "") for item in fused],
        excerpts=excerpts, max_generation_tokens=max_generation_tokens,
        provider_model=settings.chat_model or "unknown",
        provider_endpoint=settings.chat_endpoint, location="local")

def _wiki_error_reason(code: str | None) -> str:
    """federated_wiki helper 的错误码 -> 契约 `federated_answer_reason`。"""
    if code in FEDERATED_ANSWER_REASON_VALUES:
        return code
    mapping = {
        "wiki_source_unavailable": "insufficient_evidence",
        "wiki_source_permission": "insufficient_evidence",
        "wiki_budget_exceeded": "budget_exceeded",
        "wiki_budget_invalid": "budget_exceeded",
        "wiki_generation_invalid": "unsupported_generation",
        "unsupported_generation": "unsupported_generation",
        "wiki_relation_unsupported": "unsupported_generation",
        "wiki_title_invalid": "unsupported_generation",
    }
    return mapping.get(code or "", "upstream_error")

def _validate_remote_wiki_draft(document, *, evidence_ids: list[str]) -> dict | None:
    """校验 C 回传的原始页面草稿；返回可用草稿或失败答案字段。

    - 引用绑定的 evidence id 必须是本次发送证据 id 的子集（与 answer 同口径）；
    - 只复制已知字段，绝不把对端的任意 JSON 透传进任务结果；
    - `semantic_review` 一律重写成 `needs_review`。
    返回 None 表示校验通过（调用方用 document 本体继续）；否则返回失败字段。
    """
    if not isinstance(document, dict):
        return _wiki_failure("delegated_answer_missing")
    pages = document.get("pages")
    if not isinstance(pages, list) or not pages:
        return _wiki_failure("delegated_bindings_missing")
    allowed = set(evidence_ids)
    for page in pages:
        if not isinstance(page, dict):
            return _wiki_failure("delegated_binding_out_of_scope")
        for section in page.get("generated_sections") or []:
            if not isinstance(section, dict):
                return _wiki_failure("delegated_binding_out_of_scope")
            for claim in section.get("sentences") or []:
                if not isinstance(claim, dict):
                    return _wiki_failure("delegated_binding_out_of_scope")
                refs = claim.get("evidence_ids")
                if not isinstance(refs, list) or not refs \
                        or not set(map(str, refs)) <= allowed:
                    return _wiki_failure("delegated_binding_out_of_scope")
                claim["semantic_review"] = "needs_review"
    return None

def _fused_evidence_items(fused: list[dict], excerpts: dict[str, str],
                          *, node: str) -> tuple[list[dict], str | None]:
    """融合证据 -> federated_wiki 证据条目（envelope + 有界 excerpt）。

    本地来源取内部 `_excerpt`（同一把 2000 字符尺子），远端用证据集 `excerpt`
    原样；缺失/越界返回 (None, reason)，调用方显式失败，不静默截断。
    外来 relay 自报不能覆盖权威来源同键证据（`_evidence_key` 归属防伪已在
    `_execute_plan` 落定，这里只收调用方传进来的已归属 fused）。
    """
    from ddp_corpus import federated_wiki as federated_wiki_plane
    _ = federated_wiki_plane
    items: list[dict] = []
    for item in fused:
        evidence_id = str(item.get("evidence_id") or "")
        text = excerpts.get(evidence_id)
        reason = _excerpt_reason(text)
        if reason is not None or not evidence_id:
            return [], reason or "evidence_excerpt_unavailable"
        envelope = {key: value for key, value in item.items()
                    if not str(key).startswith("_") and key != "excerpt"}
        items.append({**envelope, "excerpt": text})
    _ = node
    return items, None

async def _live_recheck_evidence(session: AsyncSession, actor: Actor, *,
                                 fused: list[dict], now: datetime) -> tuple[list[dict], str | None]:
    """生成前实时复查：逐条 `resolve_evidence` 重判本地来源授权。"""
    node = federation.local_node_id()
    live: list[dict] = []
    for item in fused:
        if item.get("origin_node_id") != node:
            live.append(item)
            continue
        try:
            await federation.resolve_evidence(
                session, actor, evidence_ref=str(item.get("evidence_id") or ""),
                now=now)
        except APIError as exc:
            if exc.code == "source_revoked":
                return [], "source_revoked"
            return [], "input_not_verified"
    return live, None

def _no_generator_reason(row: FederationRequest) -> str:
    """计划里没有生成步骤的原因：来源策略排除了生成节点（T83），否则就是没有模型。"""
    if (row.result_json or {}).get(GENERATION_WITHHELD_FIELD) == SOURCE_POLICY_DENIED:
        return "source_policy_denied"
    return "local_model_missing"

async def _wiki_result(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                       plan: dict, fused: list[dict], live_excerpts: dict[str, str],
                       sufficiency: str, http,
                       budget: routing.RootBudget | None = None,
                       spend=None) -> dict:
    """wiki.pages 执行决定：证据复查 -> 生成（本地/C 委托）-> A 本地提交。"""
    from ddp_corpus import federated_wiki as federated_wiki_plane
    node = federation.local_node_id()
    wiki_step = next((step for step in plan["steps"]
                      if step["operation"] == "wiki_pages"), None)
    if wiki_step is None:
        return _wiki_failure(_no_generator_reason(row))
    if sufficiency == "insufficient" or not fused:
        return _wiki_failure("insufficient_evidence")
    excerpts = await _load_excerpts(session, actor, plan)
    excerpts.update({key: value for key, value in live_excerpts.items()
                     if isinstance(value, str) and value.strip()})
    live, failure = await _live_recheck_evidence(
        session, actor, fused=fused, now=utcnow())
    if failure is not None:
        # `source_revoked`/`input_not_verified` 不是 answer 原因闭集的成员：
        # 撤销即证据不可用，走 `insufficient_evidence`，明细留给 error 轴。
        return _wiki_failure("insufficient_evidence")
    fused = live
    items, reason = _fused_evidence_items(fused, excerpts, node=node)
    if reason is not None:
        return _wiki_failure(reason)
    if len(items) > federation.ADMISSION_EVIDENCE_LIMIT:
        return _wiki_failure("evidence_delegation_over_limit")
    cap = int((plan.get("budget") or {}).get("max_generation_tokens") or 0)
    if cap <= 0:
        return _wiki_failure("budget_exceeded")
    task_spec = row.task_spec_json
    wiki_req = (task_spec.get("requirements") or {}).get("wiki") or {}
    title = wiki_req.get("title")
    if not isinstance(title, str) or not title.strip():
        # 新 Wiki 必须带 title（plans.requirements_wiki 已校验）；这里不截断、
        # 不编造——缺 title 即显式失败。
        return _wiki_failure("unsupported_generation")
    max_pages = wiki_req.get("max_pages", 4)
    if type(max_pages) is not int or not 1 <= max_pages <= 12:
        return _wiki_failure("budget_exceeded")
    body = {"title": title.strip(), "max_pages": max_pages}
    if wiki_req.get("wiki_id") is not None:
        body["wiki_id"] = wiki_req["wiki_id"]
    if wiki_req.get("base_revision_id") is not None:
        body["base_revision_id"] = wiki_req["base_revision_id"]
    generator = wiki_step["executor_node_id"]
    # Main 冻结：payload evidence_id = source_ref(真实信封)；校验域取 refs，
    # source_envelope.evidence_id 的原始 ID 由 helper 绑定校验。
    from ddp_corpus import federated_wiki as _fw2
    evidence_ids = [_fw2.source_ref(
        {key: value for key, value in item.items() if key != "excerpt"})
        for item in items]
    if generator == node:
        if spend is not None:
            await spend(kind="generation_tokens", amount=cap, step_id=_reservation_step(wiki_step))
            await spend(kind="request", amount=1)
        try:
            draft = await federated_wiki_plane.generate_federated(
                http, body=body, evidence=items, max_tokens=cap)
        except (APIError, ApplicationError) as exc:
            return _wiki_failure(_wiki_error_reason(getattr(exc, "code", None)))
    else:
        draft = await _delegated_wiki_draft(
            row, plan=plan, step=wiki_step, items=items, actor=actor,
            budget=budget, spend=spend)
        if not isinstance(draft, dict) or draft.get("validation_state") != "passed":
            return draft if isinstance(draft, dict) else _wiki_failure("delegated_answer_missing")
    failure = _validate_remote_wiki_draft(draft, evidence_ids=evidence_ids) \
        if generator != node else None
    if failure is not None:
        return failure
    try:
        revision_out = await federated_wiki_plane.commit_federated_revision(
            session, actor, root_task_id=row.root_task_id,
            task_spec=task_spec, result=draft, evidence=items)
    except (APIError, ApplicationError) as exc:
        return _wiki_failure(_wiki_error_reason(getattr(exc, "code", None)))
    wiki_id = (revision_out.get("wiki") or {}).get("id")
    revision_id = (revision_out.get("revision") or {}).get("id")
    if not wiki_id or not revision_id:
        return _wiki_failure("delegated_answer_missing")
    return {**federation.answer_skeleton(),
            "answer": None, "answer_reason": None,
            "validation_state": "passed",
            "provider": draft.get("provider"),
            "disclosure": {"remote": generator != node,
                           "payload": ["question", "selected_evidence"]},
            "wiki": {"wiki_id": wiki_id, "revision_id": revision_id}}

async def _answer_result(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                         plan: dict, fused: list[dict], live_excerpts: dict[str, str],
                         sufficiency: str, http,
                         budget: routing.RootBudget | None = None,
                         spend=None) -> dict:
    """执行阶段的答案/Wiki 决定：不要生成 / 没有模型 / 证据不足 / 本地生成 /
    委托生成，都可见。Wiki 走 `_wiki_result`（生成位置可远端、提交权留 A）。"""
    if row.task_spec_json["operation"] == WIKI_OPERATION:
        return await _wiki_result(session, actor, row, plan=plan, fused=fused,
                                  live_excerpts=live_excerpts,
                                  sufficiency=sufficiency, http=http,
                                  budget=budget, spend=spend)
    node = federation.local_node_id()
    if row.task_spec_json["operation"] != ANSWER_OPERATION:
        # 只取证据：没有答案也**没有原因** —— "没模型"和"你没要答案"是两件事，
        # 界面据此说"这个任务只取证据，不生成回答"。
        return federation.answer_skeleton()
    answer_step = next((step for step in plan["steps"]
                        if step["operation"] == "answer"), None)
    if answer_step is None:
        # 规划时本地与远端都没有可用的生成能力：明说没有模型，不伪造答案。
        return _unavailable_answer(_no_generator_reason(row))
    if sufficiency == "insufficient" or not fused:
        # 没有可引用的证据就不给模型留"凭常识补一句"的机会；契约也要求
        # insufficient 时绑定必须为空（ddp-evidence/v1 FederatedAnswer 的 allOf）。
        # 远端委托同理：没有证据就不发数据边。
        return _unavailable_answer("insufficient_evidence")
    cap = int((plan.get("budget") or {}).get("max_generation_tokens") or 0)
    if cap <= 0:
        return _unavailable_answer("budget_exceeded")
    excerpts = await _load_excerpts(session, actor, plan)
    excerpts.update({key: value for key, value in live_excerpts.items()
                     if isinstance(value, str) and value.strip()})
    if answer_step["executor_node_id"] != node:
        return await _delegated_answer(row, plan=plan, step=answer_step, fused=fused,
                                       excerpts=excerpts, actor=actor,
                                       budget=budget, spend=spend)
    if spend is not None:
        await spend(kind="generation_tokens", amount=cap, step_id=_reservation_step(answer_step))
        await spend(kind="request", amount=1)
    return await _grounded_answer(http, query=row.task_spec_json.get("query") or "",
                                  fused=fused, excerpts=excerpts,
                                  max_generation_tokens=cap)
