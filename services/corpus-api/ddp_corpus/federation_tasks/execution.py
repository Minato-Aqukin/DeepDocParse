"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

import hashlib
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationExecution,
    FederationProbe,
    FederationRequest,
)
from ddp_corpus.federation_peers import Delegation, PeerDirectory, PeerUnavailable
from sqlalchemy.exc import IntegrityError
from ddp_corpus.models import as_aware, utcnow
from ddp_corpus import catalog, federation, federation_budget, queue
from ddp_core.application import coverage as coverage_kernel, plans
from sqlalchemy import delete, select, update
from ddp_corpus.usage import record_usage
from ddp_corpus.config import settings

from ddp_corpus.federation_tasks.common import (
    CACHED_PROBE_PROFILE,
    GENERATION_WITHHELD_FIELD,
    _EVENT_CANCELLED,
    _EVENT_COMPLETED,
    _EVENT_DELIVERY_PENDING,
    _EVENT_FAILED,
    _EVENT_RESUMED,
    _EVENT_STARTED,
    _RETRYABLE_STATES,
    _append_event,
    _commit,
    _concurrent_write,
    _coverage_key,
    _entry_from_row,
    _entry_key,
    _entry_row,
    _enumeration_state,
    _generation_excerpt,
    _load_request,
    _recorded_conflicts,
    _status_output,
    _target_digest,
    _target_key,
    _ts,
    _unavailable_answer,
)
from ddp_corpus.federation_tasks.consent import (
    _egress_denied,
    peer_directory,
    validate_execution_consent,
)
from ddp_corpus.federation_tasks.delivery import (_deliver_result)
from ddp_corpus.federation_tasks.intent import (_intent_budget, _subquery_digests)
from ddp_corpus.federation_tasks.plan import (create_plan)
from ddp_corpus.federation_tasks.relay import (
    _delegation_failed_entries,
    _delegation_failure_state,
    _run_delegate_step,
)
from ddp_corpus.federation_tasks.steps import (
    _lookup_local_receipt, _lookup_remote_receipt, _run_local_step, _run_remote_step)
from ddp_corpus.federation_tasks.synthesis import (_answer_result)
from ddp_corpus.federation_tasks.targets import (
    _all_targets,
    _fast_continuation_targets,
    _ordered_targets,
    _peer_probe_denial,
    _plan_selected_targets,
    _steps_by_target,
)

def _evidence_key(item: dict) -> tuple:
    return (item.get("origin_node_id"), item.get("resource_id"),
            item.get("source_version_id"), item.get("evidence_id"))

def _subquery_texts(task_spec: dict) -> list[str] | None:
    """Bound subquery texts, or None when the task has no query plan."""
    requirements = task_spec.get("requirements") or {}
    plan = requirements.get("query_plan") if isinstance(requirements, dict) else None
    subqueries = plan.get("subqueries") if isinstance(plan, dict) else None
    if not isinstance(subqueries, list) or not subqueries:
        return None
    return [str(subquery) for subquery in subqueries]

def _content_tokens(text: str) -> set[str]:
    """Tokenizer output minus the search plane's closed function-word set.

    Reuses `ddp_core.search._QUERY_FUNCTION_WORDS` — the same filter the
    keyword leg applies — so attribution and retrieval agree on what a
    content-bearing token is. Single-char CJK and punctuation-only texts
    yield no tokens here; such subqueries are rejected at plan validation
    (`plans.requirements_query_plan`) rather than matched loosely.
    """
    from ddp_core.search import _QUERY_FUNCTION_WORDS as _FUNCTION_WORDS
    from ddp_core.tokenize import tokens as _tokens
    return {token for token in _tokens(text) if token not in _FUNCTION_WORDS}

def _attribute_evidence_to_subqueries(items: list[dict], subqueries: list[str],
                                      *, local_source: bool) -> dict[int, list[str]]:
    """Map each returned evidence item to the subqueries its text can answer.

    One retrieval cannot vouch for every bound subquery: the executor runs a
    single query, so per-subquery completion must come from the evidence text
    itself. A subquery counts as answered only when at least one returned
    item's excerpt shares quorum content tokens with it; anything else stays
    unattempted for that subquery rather than inheriting another subquery's
    success. Matching uses the shared tokenizer plus the search plane's
    function-word filter so CJK queries behave the same here as in search.

    Quorum: every content token of the subquery must appear in one item's
    excerpt (single content token: that token). One shared function word —
    or one shared domain word among several — never marks a subquery done.
    """
    texts: list[tuple[str, set[str]]] = []
    for item in items:
        excerpt = _generation_excerpt(item, local_source=local_source)
        if not excerpt:
            continue
        texts.append((str(item.get("evidence_id") or ""), _content_tokens(excerpt)))
    attributed: dict[int, list[str]] = {}
    for index, subquery in enumerate(subqueries):
        wanted = _content_tokens(subquery)
        if not wanted:
            continue
        hits = sorted(evidence_id for evidence_id, words in texts if wanted <= words)
        if hits:
            attributed[index] = hits
    return attributed


def _slot_digest(target: dict, digest: str) -> str:
    """Row key for one (target, subquery_digest) slot.

    The `coverage_entries` primary key is `(root_task_id, target_digest)`,
    one column wide per target. A bound query plan needs one row per slot,
    so the digest folds the subquery digest into the target hash; the
    unsplit target digest is never persisted for a multi-subquery task and
    no reader looks rows up by the stored digest value.
    """
    return hashlib.sha256(
        _target_digest(target).encode("ascii") + b"\x00" + digest.encode("ascii")
    ).hexdigest()


async def _lookup_remote_receipt_for_cancel(peers: PeerDirectory, node_id: str,
                                            key: str) -> dict | None:
    """Best-effort remote receipt fetch for cancel reconciliation (404 -> None)."""
    try:
        client = peers.client(node_id)
    except PeerUnavailable:
        return None
    try:
        return await _lookup_remote_receipt(client, key)
    except PeerUnavailable:
        return None

async def _cancel_step_executions(session, actor, row: FederationRequest, *,
                                  plan: dict | None, now) -> dict:
    """Cancel the root's local step executions + dispatched peer executions.

    Local: enumerate `FederationExecution` rows owned by this root in a live
    state and apply `federation.cancel_execution` (generation+1 + dedupe
    cancel) for each. Remote: collect `executor_task_id`s from local receipts
    (business key `f"{root}:{step_id}"` via `_lookup_local_receipt`) and from
    remote receipts tracked per plan step (lookup by the same business key via
    a peer client for the step's executor node), then best-effort
    `PeerClient.cancel` per executor with `_unknown_admission`-style tolerance
    (transport failure never fails the cancel itself). Returns the
    per-executor reconciliation payload for the cancel event.
    """
    from ddp_corpus.federation_tasks.steps import _unknown_admission
    cancelled_local: list[str] = []
    already_terminal_local: list[str] = []
    executions = list(await session.scalars(select(FederationExecution).where(
        FederationExecution.root_task_id == row.root_task_id)))
    for execution in executions:
        if execution.state in ("queued", "running", "claimed"):
            try:
                await federation.cancel_execution(
                    session, actor, execution.executor_task_id, now=now)
                cancelled_local.append(execution.executor_task_id)
            except APIError as exc:
                if exc.status_code == 404:
                    already_terminal_local.append(execution.executor_task_id)
                else:
                    raise
        else:
            already_terminal_local.append(execution.executor_task_id)
    cancelled_remote: list[str] = []
    already_terminal_remote: list[str] = []
    unreachable_remote: list[str] = []
    if plan:
        peers = peer_directory(actor, Delegation(root_task_id=row.root_task_id,
                                                 task_spec_digest=row.task_spec_digest))
        try:
            seen: set[str] = set()
            local_node = federation.local_node_id()
            for step in plan.get("steps") or []:
                step_id = step.get("step_id")
                executor_node = step.get("executor_node_id")
                if not step_id or not executor_node or executor_node == local_node:
                    continue
                key = f"{row.root_task_id}:{step_id}"
                for receipt in (await _lookup_local_receipt(session, actor, key),
                                await _lookup_remote_receipt_for_cancel(
                                    peers, executor_node, key)):
                    executor_task_id = (receipt or {}).get("executor_task_id")
                    if not executor_task_id or executor_task_id in seen:
                        continue
                    seen.add(executor_task_id)
                    if executor_task_id in set(cancelled_local) | set(already_terminal_local):
                        continue
                    try:
                        status = await peers.client(executor_node).cancel(executor_task_id)
                    except PeerUnavailable as exc:
                        if _unknown_admission(exc):
                            unreachable_remote.append(executor_task_id)
                        else:
                            already_terminal_remote.append(executor_task_id)
                        continue
                    state = (status or {}).get("state")
                    if state in ("cancelled", "succeeded", "failed"):
                        (cancelled_remote if state == "cancelled"
                         else already_terminal_remote).append(executor_task_id)
                    else:
                        cancelled_remote.append(executor_task_id)
        finally:
            await peers.aclose()
    return {"local_cancelled": cancelled_local,
            "local_already_terminal": already_terminal_local,
            "remote_cancelled": cancelled_remote,
            "remote_already_terminal": already_terminal_remote,
            "remote_unreachable": unreachable_remote}


def _public_item(item: dict) -> dict:
    """HTTP 出口只出契约字段。

    `_excerpt`/`_score` 是审计用内部字段；`excerpt` 是证据集读取专供生成用的
    有界正文，也不进任务结果 —— 正文只在生成时进 prompt，不随状态响应扩散。
    """
    return {key: value for key, value in item.items()
            if not str(key).startswith("_") and key != "excerpt"}

async def _mark_failed(session: AsyncSession, root_task_id: str, *, now: datetime,
                       error: str) -> bool:
    """落协调任务失败；**终态守卫是条件 UPDATE，不是"先读后写"**。

    取消可能恰好发生在业务错误抛出的前后；先读后写时，读到的"running"在
    写入的那一刻可能已经是别人提交的 cancelled —— 一条迟到的 `_mark_failed`
    会把用户显式取消的任务改写回 failed（语义从"用户不要了"变成"系统做砸了"）。
    这里让数据库替我们仲裁：`UPDATE ... WHERE status='running'`，命中 0 行
    表示终态已经由别人落定，本次一律不写，连事件也不追加。

    返回 True = 本次真的把 running 落成了 failed；False = 别人拥有这个结局。
    """
    await session.rollback()
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == root_task_id,
            FederationRequest.status == "running").values(
            status="failed", error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        await session.rollback()
        return False
    await _append_event(session, root_task_id, _EVENT_FAILED, {"error": error}, now=now)
    await session.commit()
    return True

async def _load_probe(session: AsyncSession, actor: Actor, step: dict, *,
                      request_created_at: datetime | None = None
                      ) -> tuple[dict | None, bool]:
    """读回步骤绑定的探测回执，并如实区分"本次计划新探测的"与"复用的"。

    复用判据是持久行早于本任务受理（`create_plan` 的新探测都以计划时刻写入，
    晚于受理）。标记只影响 `search_profile` 的可读性，不改变覆盖计数。
    """
    for probe_id in step.get("probe_refs") or []:
        row = await session.get(FederationProbe, probe_id)
        if row is not None and row.organization_id == actor.organization_id:
            reused = (request_created_at is not None
                      and as_aware(row.created_at) < as_aware(request_created_at))
            return (row.result_json or {}).get("result"), reused
    return None, False

#: 协调者结果里的内部字段：上一轮可归属（自报来源 = 返回目标节点）的证据键，供
#: resume 时让复原条目参与版本分歧比较。以 `_` 开头，不进交付文档、不出 HTTP。
_ATTRIBUTED_FIELD = "_attributed_evidence"

def _first_error(entries: list[dict]) -> str | None:
    for entry in entries:
        if entry.get("last_error"):
            return str(entry["last_error"])
    return None

async def _no_longer_running(session: AsyncSession, root_task_id: str) -> bool:
    """协调者在下一次外发前读一次**已提交**的执行轴：不再是 running 就停手。

    **先结束读事务再读**：取消来自另一个会话；SQLite（以及 PG 的非 READ
    COMMITTED 快照）里不结束旧事务就看不到那次提交。协调者 session 在这里没有
    未提交的业务写入（执行/受理都在内部 commit 过，花费走独立会话），rollback
    是安全的；它也让本 session 不再握任何事务级咨询锁。
    """
    if session.in_transaction():
        await session.rollback()
    status = await session.scalar(select(FederationRequest.status).where(
        FederationRequest.root_task_id == root_task_id))
    return status != "running"

async def _execute_plan(session: AsyncSession, actor: Actor, row: FederationRequest, *,
                        now: datetime, http, index, retry_only: bool) -> dict:
    task_spec = row.task_spec_json
    plan = row.plan_json
    consent = row.execution_consent_json
    manifest = row.scope_manifest_json
    node = federation.local_node_id()
    query_digests = _subquery_digests(task_spec)
    query_digest = query_digests[0]
    subquery_texts = _subquery_texts(task_spec)
    all_targets = _all_targets(task_spec, manifest, node)
    # 计划是目标选择的权威：执行阶段没有目录摘要可重排，重排会错位挂回执。
    candidates = _plan_selected_targets(plan, all_targets)
    steps_by_target = _steps_by_target(plan, candidates)
    existing = await session.scalars(
        select(CoverageEntry).where(CoverageEntry.root_task_id == row.root_task_id))
    entries = {_entry_key(converted): converted
               for converted in (_entry_from_row(item, row.scope_id) for item in existing)}
    evidence: dict[tuple, dict] = {}
    previous = row.result_json or {}
    # §7.6 规则一路的输入：只收"自报来源 = 返回它的那个目标的节点"的条目。条目
    # 字段是对端自报的，不这样筛，坏对端就能伪造一条"本节点同资源同定位"的条目
    # 把本地证据标成矛盾。
    #
    # **归属要跨轮次保留**：resume 跳过上一轮已成功的目标，它们的条目只能从结果
    # 里复原。复原的条目若不参与分组，同一处的两个版本分两轮到达时就永远不会被
    # 比较，账本照报 sufficient_by_policy（第六次验收复现）。所以上一轮把可归属
    # 条目的键记进内部字段 `_attributed_evidence`（不进交付文档、不出 HTTP），
    # 这里据此把复原条目放回规则一路的输入。
    attributed_keys = {tuple(key) for key in previous.get(_ATTRIBUTED_FIELD) or []
                       if isinstance(key, list)}
    attributed: list[dict] = []
    for item in previous.get("evidence") or []:
        evidence[_evidence_key(item)] = item
        if _evidence_key(item) in attributed_keys:
            attributed.append(item)
    # 没有 `_attributed_evidence` 的旧结果（本字段上线前完成的任务）：复原的条目
    # 没有归属信息，至少把它记下的规则矛盾原样带回；生成标注的矛盾不复原（本轮
    # 会重新生成）。
    restored_divergences = [item for item in _recorded_conflicts(row)
                            if item["basis"] == "version_divergence"]
    # 本轮真正看到的正文片段（公开结果只出契约字段，`_excerpt` 不进 HTTP 出口）。
    live_excerpts: dict[str, str] = {}
    generation = int(row.delegation_generation or 0)
    root_task_id = row.root_task_id
    # resume 前移过代次：上一轮可能已受理了某个目标却在落账前死掉（没有
    # coverage 行）。不先对账就按新代次重新受理，同一个业务键的请求摘要变了，
    # 执行者只能 409 idempotency_conflict —— 所以补做一律先对账。
    # 循环里不能再读 `row.*`：本地目标等待终态时 `_local_execution_outcome`
    # 会 rollback 结束读事务，行对象随之过期；之后在同步上下文里摸属性就是
    # MissingGreenlet。需要的列在这里一次取完。
    scope_id = row.scope_id
    exploration_consent = row.exploration_consent_json
    request_created_at = as_aware(row.created_at)
    from ddp_corpus.db import get_sessionmaker
    async with get_sessionmaker()() as ledger_session:
        root_ledger = await federation_budget.ensure_ledger(
            ledger_session, root_task_id=root_task_id, organization_id=actor.organization_id,
            caller_budget=None,
            server_caps={**_intent_budget(task_spec, manifest, exploration_consent, now=now),
                         **plan["budget"]},
            legacy_result=previous, now=now)
        await ledger_session.commit()
    try:
        exec_budget = federation_budget.rebuild_from_ledger(
            root_ledger, plan["budget"], now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    peers = peer_directory(actor, Delegation(root_task_id=root_task_id,
                                             task_spec_digest=row.task_spec_digest))

    async def _spend(kind: str, amount: int = 1, step_id: str | None = None) -> None:
        await federation_budget.spend(
            root_task_id=root_task_id, organization_id=actor.organization_id,
            kind=kind, amount=amount, budget=exec_budget, now=utcnow(), step_id=step_id)
    delegated_done = set()
    try:
        for target in candidates:
            # 每次派出之前确认任务还在 running：取消可能在上一个目标检索期间
            # 落了库，之后的目标与生成都不许再外发（ABC 演练 F10：取消之后协调者
            # 仍把问题与证据摘录发给了生成节点）。
            if await _no_longer_running(session, root_task_id):
                break
            key = (target["origin_node_id"], target["collection_id"], target["operation"])
            currents = [entries.get(_coverage_key(target, digest)) for digest in query_digests]
            if all(current is not None and current["state"] not in _RETRYABLE_STATES
                   for current in currents):
                continue
            current = next((current for current in currents if current is not None), None)
            step = steps_by_target.get(key)
            if step is None:
                continue
            if step["operation"] == "delegate":
                if step["step_id"] in delegated_done:
                    continue
                delegated_done.add(step["step_id"])
                try:
                    recipients = {step["executor_node_id"]}
                    for assigned in step["delegated_targets"]:
                        recipients.update([assigned["target_key"]["origin_node_id"],
                                           *assigned["via_node_ids"]])
                    if any(_peer_probe_denial(exploration_consent, recipient) for recipient in recipients):
                        raise _egress_denied("exploration consent omits a relay or leaf")
                    report = await _run_delegate_step(
                        peers, actor, root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                        consent=consent, step=step, budget=exec_budget, spend=_spend,
                        scope_ref=scope_id, query_digest=query_digest,
                        query_digests=query_digests,
                        reconcile=retry_only or current is not None)
                    delegated_entries = report["entries"]
                    for item in report["evidence"]:
                        item_key = _evidence_key(item)
                        if item_key not in evidence:
                            evidence[item_key] = _public_item(item)
                            attributed.append(_public_item(item))
                        excerpt = _generation_excerpt(item, local_source=False)
                        if excerpt is not None:
                            live_excerpts[item["evidence_id"]] = excerpt
                except (APIError, ApplicationError, PeerUnavailable) as exc:
                    error = getattr(exc, "code", None) or "peer_unavailable"
                    state = _delegation_failure_state(exc)
                    delegated_entries = _delegation_failed_entries(
                        step, scope_ref=scope_id, query_digests=query_digests, state=state, error=error)
                for delegated_entry in delegated_entries:
                    entries[_entry_key(delegated_entry)] = delegated_entry
                continue
            entry = coverage_kernel.new_entry(_target_key(target), scope_id, query_digest)
            probe, probe_reused = await _load_probe(
                session, actor, step, request_created_at=request_created_at)
            probe_limits: list[str] = []
            if probe is not None:
                # 规划期探测在这里只提供**回执绑定**（probe_receipts + 实际索引
                # 修订），成败由下面的执行决定，所以按 in_flight 记。让内核从
                # 探测派生 succeeded 会触发"过期探测不许记成新成功"：计划有效
                # 900s、探测只有 300s，用户审批慢一点，成功执行就被降成
                # partial/probe_receipt_missing 并丢掉证据。
                try:
                    entry = coverage_kernel.record(entry, probe, state="in_flight",
                                                   now=_ts(now))
                    probe_limits = list(
                        (probe.get("retrieval") or {}).get("internal_limits") or [])
                except ApplicationError:
                    entry = coverage_kernel.new_entry(_target_key(target), scope_id,
                                                      query_digest)
            if probe_reused:
                # 这一行的探测回执来自更早的任务：账本照实带缓存标记，
                # 不把它读成"这一轮真的重新探测过"。
                entry["search_profile"] = CACHED_PROBE_PROFILE
            before_cost = exec_budget.used()
            entry["used_budget"] = {"requests": 0, "bytes": 0}
            if target["origin_node_id"] != node:
                # 探索许可门在执行阶段仍然生效：探都不许探的目标，admission 更
                # 不许发。local_only / 未列入接收方 / 载荷不许的目标保持 denied。
                denial = _peer_probe_denial(exploration_consent,
                                            target["origin_node_id"])
                if denial is not None:
                    for digest in query_digests:
                        denied = coverage_kernel.new_entry(_target_key(target), scope_id, digest)
                        entries[_coverage_key(target, digest)] = coverage_kernel.record(
                            denied, None, state="denied", error=denial, now=_ts(now))
                    continue
            internal_limits: list[str] = []
            try:
                if target["origin_node_id"] == node:
                    state, error, items, revision, internal_limits = await _run_local_step(
                        session, actor, root_task_id=root_task_id, plan=plan,
                        task_spec=task_spec,
                        consent=consent, step=step, target=target, generation=generation,
                        now=now, http=http, index=index,
                        reconcile=retry_only or current is not None,
                        budget=exec_budget, spend=_spend)
                else:
                    state, error, items, revision, internal_limits = await _run_remote_step(
                        peers, root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                        consent=consent,
                        step=step, target=target, generation=generation,
                        reconcile=retry_only or current is not None,
                        budget=exec_budget, spend=_spend)
            except (APIError, ApplicationError) as exc:
                state, error, items, revision, internal_limits = "failed", exc.code, [], None, []
            after_cost = exec_budget.used()
            entry["used_budget"] = {key: after_cost[key] - before_cost[key]
                                    for key in ("requests", "bytes")}
            # A probe's set reference is not evidence: the set may be empty,
            # or execution may fail after a successful planning probe.
            entry["evidence_refs"] = []
            if state == "succeeded" and revision:
                # 执行报了自己检索的索引修订就以它为准：规划期探测的修订最多可能
                # 是计划有效期（900s）之前的，覆盖账本要记"实际检索的是哪一版"。
                entry["actual_index_revision"] = revision
            if state == "succeeded":
                if not entry.get("probe_receipts") or not entry.get("actual_index_revision"):
                    # §7.4：没有回执与实际索引修订的"成功"不许进成功数。
                    state, error = "partial", "probe_receipt_missing"
                else:
                    local_source = target["origin_node_id"] == node
                    for item in items:
                        key = _evidence_key(item)
                        # A relay's self-reported envelope cannot replace evidence
                        # already obtained from its source, including after resume.
                        if item.get("origin_node_id") != target["origin_node_id"] \
                                and key in evidence:
                            continue
                        evidence[key] = _public_item(item)
                        entry["evidence_refs"].append(str(item["evidence_id"]))
                        if item.get("origin_node_id") == target["origin_node_id"]:
                            attributed.append(_public_item(item))
                        excerpt = _generation_excerpt(item, local_source=local_source)
                        if excerpt is not None:
                            live_excerpts[str(item.get("evidence_id") or "")] = excerpt
            # 执行/probe 自报的内部限制一并进账本：非空必须落 partial，
            # 绝不允许由一条 truncated_by_limit 的执行推出 complete（T85）。
            recorded = coverage_kernel.record(
                entry, None, state=state, error=error, now=_ts(now),
                limits=probe_limits + list(internal_limits))
            answered = _attribute_evidence_to_subqueries(
                items, subquery_texts,
                local_source=target["origin_node_id"] == node) \
                if subquery_texts is not None and state == "succeeded" else {}
            for position, digest in enumerate(query_digests):
                if subquery_texts is None or state != "succeeded" or position in answered:
                    entries[_coverage_key(target, digest)] = {
                        **recorded, "query_or_subquery_digest": digest}
                    continue
                # This subquery's text matches none of the returned evidence:
                # one retrieval answering another subquery must not mark this
                # one complete. The target was attempted (attempts/receipts
                # preserved) but this subquery stays visibly unanswered.
                pending = coverage_kernel.new_entry(
                    _target_key(target), scope_id, digest)
                pending["probe_receipts"] = list(recorded.get("probe_receipts") or [])
                pending["actual_index_revision"] = recorded.get("actual_index_revision")
                pending["search_profile"] = recorded.get("search_profile")
                pending["attempts"] = int(recorded.get("attempts") or 0)
                pending["used_budget"] = dict(recorded.get("used_budget") or {})
                entries[_coverage_key(target, digest)] = coverage_kernel.record(
                    pending, None, state="not_attempted",
                    error="subquery_evidence_missing", now=_ts(now))
        # 分母补齐在 resume 上同样要做：上一轮若在落账前死掉，库里没有任何
        # coverage 行，fast 模式里没被选中的目标也就没有 entry，下面按全量
        # 目标取 entries 会 KeyError（既不是 APIError 也不是 ApplicationError，
        # 队列里反复重试，行一直停在 running）。已有的行原样保留。
        for target in all_targets:
            for digest in query_digests:
                slot = _coverage_key(target, digest)
                if slot in entries:
                    continue
                fill = coverage_kernel.new_entry(_target_key(target), scope_id, digest)
                if target not in candidates:
                    fill = coverage_kernel.record(fill, None, state="not_attempted",
                                                 error="search_mode_fast", now=_ts(now))
                entries[slot] = fill
    finally:
        await peers.aclose()
    # 本地执行失败时 `federation.execute` 会 rollback 整个 session（那是对的：
    # 失败要落库），副作用是协调者行被 expire。生成与做账之前先确认终态，再按
    # 主键重新加载一次，不让"某个目标失败"把后面所有属性读都变成 MissingGreenlet。
    if await _no_longer_running(session, root_task_id):
        # 取消（cancelled）、回收清扫（failed）或别的终态在本次执行期间落了库：
        # 迟到的成功/失败结果一律不许覆盖，覆盖账本也不许重写 —— cancel 已经把
        # 未完成目标记成 not_attempted，这里再写一遍会把那份账目改掉。
        return await _status_output(session, await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    row = await session.get(FederationRequest, root_task_id, populate_existing=True)
    ordered = [entries[_coverage_key(target, digest)]
               for target in _ordered_targets(all_targets) for digest in query_digests]
    fused = list(evidence.values())
    support_groups, representatives = coverage_kernel.support_groups(fused)
    # §7.6 规则一路：同一来源不同版本在同一定位上的正文分歧，生成之前就能算。
    # 它只会把"充分"压成"矛盾"：没有绑定时账本仍是 insufficient，下面的生成闸
    # 照样拦住（内核 `sufficiency` 的优先级钉着这件事）。
    divergences = coverage_kernel.merge_conflicts(
        restored_divergences, coverage_kernel.version_conflicts(attributed))
    ledger = coverage_kernel.ledger(
        root_task_id=row.root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
        enumeration_state=_enumeration_state(row), entries=ordered, conflicts=divergences)
    coverage_kernel.validate_ledger(ledger)
    succeeded = ledger["counts"]["succeeded"]
    # 状态轴必须与证据轴一致（N1）：有真实证据就不是失败 —— 即使所有目标都
    # 因内部限额只落 partial（counts.succeeded 可以是 0）。覆盖账本照实带
    # partial/incomplete，`unretrieved_targets` 列出没查全的目标；failed 只
    # 留给"没有任何目标产出证据"（全部 denied/unreachable/failed/unsupported）。
    status = "succeeded" if succeeded > 0 or fused \
        or ledger["retrieval_completeness"] == "complete" else "failed"
    error = None if status == "succeeded" else (_first_error(ordered) or "no_retrievable_target")
    unretrieved = [{"target_key": entry["target_key"], "state": entry["state"],
                    "last_error": entry.get("last_error")}
                   for entry in ordered if entry["state"] != "succeeded"]
    try:
        answer = await _answer_result(
            session, actor, row, plan=plan, fused=representatives, live_excerpts=live_excerpts,
            sufficiency=ledger["evidence_sufficiency"], http=http,
            budget=exec_budget, spend=_spend)
    except ApplicationError as exc:
        if exc.code != "budget_exhausted":
            raise
        # 根预算在生成这一步用完（通常在出站前扣账时，生成请求根本没发出）。
        # `budget_exceeded` 是"模型输出超出 token 预算、答案作废"，不能拿来描述这里。
        answer = _unavailable_answer("root_budget_exhausted")
    if answer.get("conflicts"):
        # 生成一路标出的矛盾（已按本次证据编号域校验）并入账本：只会把充分性压成
        # conflicting，不会把 insufficient 抬高 —— 没有证据就根本不会走到生成。
        ledger = coverage_kernel.ledger(
            root_task_id=row.root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
            enumeration_state=_enumeration_state(row), entries=ordered,
            conflicts=coverage_kernel.merge_conflicts(divergences, answer["conflicts"]))
        coverage_kernel.validate_ledger(ledger)
    # 结果文档 = 交付字节的规范原文（**不含摘要字段本身**）。摘要是对这份文档的
    # content_digest，客户端下载后重算它才允许 ack；文档有界且不含正文摘录。
    support_by_evidence: dict[str, set[str]] = {}
    for group in support_groups:
        for copy_ref in group["copies"]:
            support_by_evidence.setdefault(copy_ref["evidence_id"], set()).add(group["support_id"])
    for binding in answer.get("claim_evidence_bindings") or []:
        binding["support_refs"] = sorted({support_id for ref in binding["evidence_refs"]
                                         for support_id in support_by_evidence.get(ref, ())})
    document = {
        **answer,
        "operation": task_spec.get("operation"),
        "search_mode": row.search_mode,
        "retrieval_completeness": ledger["retrieval_completeness"],
        "evidence_sufficiency": ledger["evidence_sufficiency"],
        "counts": ledger["counts"], "coverage_ref": row.root_task_id,
        "conflicts": ledger.get("conflicts", []),
        "evidence": fused, "unretrieved_targets": unretrieved,
        "support_groups": support_groups,
        "support_counts": {"independent_sources": len(support_groups),
                           "evidence_copies": len(fused)},
    }
    result = {**document, "result_manifest_digest": plans.digest(document),
              _ATTRIBUTED_FIELD: sorted({_evidence_key(item) for item in attributed},
                                        key=lambda key: tuple(str(part) for part in key))}
    for key in (federation_budget.CANDIDATE_GRAPH_FIELD, federation_budget.FAST_STOP_FIELD,
                GENERATION_WITHHELD_FIELD):
        if key in previous:
            result[key] = previous[key]
    await session.execute(delete(CoverageEntry).where(
        CoverageEntry.root_task_id == row.root_task_id))
    ledger_row = await session.get(CoverageLedger, row.root_task_id)
    if ledger_row is None:
        ledger_row = CoverageLedger(root_task_id=row.root_task_id, created_at=now)
        session.add(ledger_row)
    ledger_row.scope_ref = row.scope_id
    ledger_row.search_mode = row.search_mode
    ledger_row.enumeration_state = ledger["enumeration_state"]
    ledger_row.retrieval_completeness = ledger["retrieval_completeness"]
    ledger_row.evidence_sufficiency = ledger["evidence_sufficiency"]
    ledger_row.counts_json = ledger["counts"]
    ledger_row.manifest_digest = row.scope_digest or plans.digest(row.scope_id)
    ledger_row.updated_at = now
    await session.flush()   # entries 有指向 ledger 的外键，父行先落
    rows = []
    for entry in ordered:
        stored = _entry_row(row.root_task_id, entry)
        stored.target_digest = _slot_digest(
            entry["target_key"], entry["query_or_subquery_digest"])
        rows.append(stored)
    session.add_all(rows)
    # **终态围栏放在这里，而且是条件 UPDATE**：`_answer_result` 可能跑了很久
    # （远端生成），取消/清扫可能在这期间落库；只按内存里的 `row` 写终态就是
    # 用旧快照覆盖别人的终态。条件 UPDATE 命中 0 行 = 有人在执行期间改了终态，
    # 整笔（覆盖账本、交付、事件）rollback 丢弃，返回那个真实终态。
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == row.root_task_id,
            FederationRequest.status == "running").values(
            coverage_ref=row.root_task_id, status=status,
            retrieval_completeness=ledger["retrieval_completeness"],
            evidence_sufficiency=ledger["evidence_sufficiency"],
            result_json=result, error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        await session.rollback()
        return await _status_output(session, await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    await session.refresh(row)
    if status == "succeeded":
        await _deliver_result(session, row, document=document,
                              digest=result["result_manifest_digest"], now=now)
        # 协调者侧计量：与终态同一个事务、只在赢下终态围栏时记，按根任务恰好一次 ——
        # 补做后再次成功、交付读取与确认都不是第二笔（T81／T58）。
        await record_usage(session, actor_id=federation.acting_actor(actor),
                           organization_id=actor.organization_id, api_key_id=actor.api_key_id,
                           kind="federated_delivery", requests=1,
                           business_key=f"federation-delivery:{row.root_task_id}")
    await _append_event(session, row.root_task_id,
                        _EVENT_COMPLETED if status == "succeeded" else _EVENT_FAILED, {
                            "retrieval_completeness": ledger["retrieval_completeness"],
                            "evidence_sufficiency": ledger["evidence_sufficiency"],
                            "counts": ledger["counts"], "delivery_id": row.delivery_id,
                            "error": error}, now=now)
    if row.delivery_id:
        await _append_event(session, row.root_task_id, _EVENT_DELIVERY_PENDING, {
            "delivery_id": row.delivery_id,
            "result_manifest_digest": result["result_manifest_digest"]}, now=now)
    # Terminal write goes through _commit like every other coordinator write path:
    # a concurrent event append on the same root colliding on (root_task_id, seq)
    # maps to retryable 409 idempotency_conflict, not a bare 500.
    await _commit(session)
    return await _status_output(session, row)

async def execute_task(session: AsyncSession, actor: Actor, root_task_id: str, *,
                       plan_digest: str, idempotency_key: str, now: datetime,
                       http, index) -> tuple[dict, bool]:
    """以幂等键受理已批准计划；同键重放不再发任何请求。

    返回 `(status, created)`。**队列模式（默认）**：created=True 时受理与
    `federation_plan` 队列任务在同一个事务里提交，HTTP 端点据此回 202，
    真正的执行由 corpus-worker 推进 —— 受理进程重启不会留下永远 running
    的任务（企业边界 7）。created=False 是同键重放，回 200 + 权威状态。
    已取消的任务 409 `task_cancelled`（终态不许被新幂等键复活）。

    `FEDERATION_EXECUTION_INLINE=true` 时保持旧的请求内执行行为（回 200）。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status == "cancelled":
        # 取消是终态：一个还没提交过的已批准计划也不许凭新的幂等键"复活"。
        # 与 resume 同一判据 —— 重跑要另起一条新任务。
        raise APIError(409, "task was cancelled; create a new task instead of submitting",
                       "invalid_request_error", "task_cancelled")
    if row.plan_digest != plan_digest:
        raise APIError(409, "submitted digest does not match the stored plan revision",
                       "invalid_request_error", "plan_changed")
    if row.planning_state != "approved" or not row.execution_consent_json:
        raise _egress_denied("task has no approved execution consent")
    if row.idempotency_key:
        if row.idempotency_key != idempotency_key:
            raise APIError(409, "root task already accepted under another idempotency key",
                           "invalid_request_error", "idempotency_conflict")
        return await _status_output(session, row), False
    validate_execution_consent(row.execution_consent_json, now=now)
    try:
        plans.validate_plan(row.plan_json, row.task_spec_json,
                            local_node_id=federation.local_node_id(), now=_ts(now))
    except ApplicationError as exc:
        if exc.code == "consent_expired":
            raise _egress_denied("plan or budget has expired; re-plan instead") from None
        raise federation.api_error(exc) from None
    # The submit key lives in the root owner's domain (`row.actor_id`), also when an
    # administrator submits someone else's root; the conflict lookup must use the same one.
    owner_id = row.actor_id
    row.idempotency_key = idempotency_key
    row.delegation_generation = int(row.delegation_generation or 0) + 1
    row.status = "running"
    row.updated_at = now
    try:
        # 事件里那次 `SELECT max(seq)` 会触发 autoflush；必须放在 try 内，
        # 否则同键异任务时唯一约束在事件追加处就炸成 500，409 分支永远到不了。
        await _append_event(session, row.root_task_id, _EVENT_STARTED, {
            "plan_digest": plan_digest, "generation": row.delegation_generation}, now=now)
        if not settings.federation_execution_inline:
            # **状态行与队列任务同一个事务**：两个半截状态（running 没任务、
            # 任务没 running）都不可能出现。dedupe 键取 root —— 同一 root
            # 的重复受理会先在上面按 idempotency_key 幂等返回。
            await queue.enqueue(
                session, kind="federation_plan",
                payload={"root_task_id": root_task_id, "retry_only": False,
                         "actor": federation.actor_binding(actor)},
                organization_id=actor.organization_id,
                dedupe_key=f"federation-request:{root_task_id}")
        await session.commit()
    except IntegrityError:
        # 同一个幂等键已经用去受理了另一个 root task；唯一约束替我们仲裁。
        # 兜底：未知约束冲突（并发写输了竞态，如事件 `(root_task_id, seq)` 撞
        # 序号）同样是"客户端可原样重试"的 409，而不是 500 —— 与 `_commit`
        # 的 concurrent-write 同码（`idempotency_conflict` 已在 POST /tasks
        # 的 409 契约里）。回滚先发生：行不留在 running，键不被占住。
        # 只接 `IntegrityError`：连接丢失、超时这类 `DBAPIError` 是基础设施
        # 故障，必须继续以 5xx 可见（不变式 2），不许被翻译成客户端冲突。
        await session.rollback()
        existing = await session.scalar(select(FederationRequest).where(
            FederationRequest.organization_id == actor.organization_id,
            FederationRequest.actor_id == owner_id,
            FederationRequest.idempotency_key == idempotency_key))
        if existing is not None and existing.root_task_id != root_task_id:
            raise APIError(409, "idempotency key already accepted for another task",
                           "invalid_request_error", "idempotency_conflict") from None
        raise APIError(409, "concurrent write for the same task; retry the same key",
                       "invalid_request_error", "idempotency_conflict") from None
    if not settings.federation_execution_inline:
        return await _status_output(session, row), True
    try:
        result = await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                     retry_only=False)
        return result, True
    except APIError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        raise
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        raise federation.api_error(exc) from None

async def run_queued(session: AsyncSession, actor: Actor, root_task_id: str, *,
                     retry_only: bool, now: datetime, http, index) -> dict:
    """worker 入口：推进一个已受理的协调任务（kind=`federation_plan`）。

    执行权仍来自持久行：状态不是 running 就直接返回当前状态，绝不把
    已取消/已终态的任务再跑一遍。已知业务错误（APIError/ApplicationError）
    落成 `failed` 并返回；未知异常交给 runner 重试（那才是队列的用武之地）。
    唯一的例外是并发写 409 `idempotency_conflict`：终态竞输只是"同一任务有
    并发写"，行还留在 running，落成 failed 等于把一次可重试的竞态变成终态 ——
    这里直接重抛，让 runner 按正常失败路径退避重试，重跑时只补未完成目标。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status != "running":
        return await _status_output(session, row)
    # 锁只仲裁"由谁开跑"，不罩住整段执行：内联受理与 resume 都在执行前提交过，
    # 只有这里一直握着它。取消要拿同一把锁，于是排在整段远端检索之后才落库，
    # 而协调者放锁时读到的仍是 running，照样派出了生成（ABC 演练 F10）。执行期间
    # 的并发由 generation 围栏、业务键幂等与最后那次条件 UPDATE 仲裁。
    await session.commit()
    try:
        return await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                   retry_only=retry_only)
    except APIError as exc:
        if exc.status_code == 409 and exc.code == "idempotency_conflict":
            await session.rollback()
            raise
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        return await _status_output(session, await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        return await _status_output(session, await session.get(FederationRequest, root_task_id,
                                                populate_existing=True))

async def _resume_continuation_gate(session: AsyncSession, actor: Actor,
                                    row: FederationRequest, *,
                                    now: datetime, http, index) -> bool:
    """Stage the next fast batch behind a fresh approval, retaining the root ledger."""
    task_spec = row.task_spec_json
    if (row.status not in ("succeeded", "failed")
            or task_spec["search_policy"]["mode"] != "fast"
            or task_spec["resource_scope"]["kind"] == "fixed_resources"):
        return False
    graph = (row.result_json or {}).get(federation_budget.CANDIDATE_GRAPH_FIELD)
    if not graph:
        return False
    selected = _plan_selected_targets(row.plan_json, graph)
    entries = list(await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == row.root_task_id)))
    outcomes = {tuple(entry.target_key_json[field] for field in
                      ("origin_node_id", "collection_id", "operation")):
                (entry.state, entry.last_error) for entry in entries}
    # Reconcile unknown receipts/crashed work before changing its graph binding.
    if any(outcomes.get(tuple(target[field] for field in
                             ("origin_node_id", "collection_id", "operation")),
                        ("planned", None))[0] in ("planned", "in_flight", "unreachable")
           for target in selected):
        return False
    additions = _fast_continuation_targets(graph, selected)
    if not additions:
        return False
    row.result_json = {**row.result_json, "_continuation_targets": selected + additions}
    row.planning_state = "draft"
    row.execution_consent_json = None
    row.execution_consent_ref = None
    # Gate strands without this: the staged revision needs a mandatory fresh
    # approve-then-submit, but the consumed submit key is still on the row, so
    # execute_task replays the old status on the same key and 409s any new key.
    # Rotating the key here releases the old submit binding (nullable column, no
    # unique-key conflict on NULL) while the row stays queued/ready for approval;
    # nothing is auto-enqueued before the fresh approval.
    row.idempotency_key = None
    row.status = "queued"
    # Previous delivery remains immutable and addressable, but is not this revision's result.
    row.delivery_id = None
    row.delivery_state = "not_requested"
    row.error = None
    await create_plan(session, actor, row.root_task_id, now=now, http=http, index=index)
    return True

async def resume(session: AsyncSession, actor: Actor, root_task_id: str, *, now: datetime,
                 http, index) -> dict:
    """重新判权后补做未完成目标。旧授权过期就 403/410，不隐式扩权。

    **队列模式（默认）**：状态改 running + 排 `federation_plan`（retry_only=True）
    后返回，端点回 202；执行由 worker 推进。同 root 已有的未完成任务靠 dedupe
    键挡住第二次排队 —— 那次运行会读到最新的行状态并只补未完成目标。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status == "cancelled":
        # 取消是显式终态：resume 不得把它"复活"回 running。任何重跑都必须
        # 走一条新的任务（新授权、新覆盖分母），而不是拿旧计划接着跑。
        raise APIError(409, "task was cancelled; create a new task instead of resuming",
                       "invalid_request_error", "task_cancelled")
    if row.planning_state != "approved" or not row.plan_json:
        raise _egress_denied("task has no approved plan to resume")
    manifest = row.scope_manifest_json
    if manifest is not None and plans.instant(manifest["valid_until"]) <= _ts(now):
        raise APIError(410, "scope has expired; re-enumerate instead of resuming",
                       "invalid_request_error", "scope_expired")
    validate_execution_consent(row.execution_consent_json, now=now)
    try:
        plans.validate_plan(row.plan_json, row.task_spec_json,
                            local_node_id=federation.local_node_id(), now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    if await _resume_continuation_gate(session, actor, row, now=now, http=http, index=index):
        return await _status_output(session, row)
    row.delegation_generation = int(row.delegation_generation or 0) + 1
    row.status = "running"
    row.updated_at = now
    try:
        # 事件序号的并发冲突在 enqueue 刷出挂起写入时就会出现，比 commit 早：
        # 同样回滚并回 409，任务不留在 running。
        await _append_event(session, row.root_task_id, _EVENT_RESUMED, {
            "generation": row.delegation_generation}, now=now)
        if not settings.federation_execution_inline:
            await queue.enqueue(
                session, kind="federation_plan",
                payload={"root_task_id": root_task_id, "retry_only": True,
                         "actor": federation.actor_binding(actor)},
                organization_id=actor.organization_id,
                dedupe_key=f"federation-request:{root_task_id}")
    except IntegrityError:
        await session.rollback()
        raise _concurrent_write() from None
    await _commit(session)
    if not settings.federation_execution_inline:
        return await _status_output(session, row)
    try:
        return await _execute_plan(session, actor, row, now=now, http=http, index=index,
                                   retry_only=True)
    except APIError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code or "task_failed")
        raise
    except ApplicationError as exc:
        await _mark_failed(session, root_task_id, now=now, error=exc.code)
        raise federation.api_error(exc) from None

async def cancel(session: AsyncSession, actor: Actor, root_task_id: str, *,
                 now: datetime) -> dict:
    """显式、幂等取消：终态不被改写；未完成目标留在分母里记 not_attempted。

    落 `status="cancelled"`（契约 task_status 的终态）。**同时取消队列里的
    `federation_plan` 任务**：不然 worker 领取后只会看到终态空转，而且它
    占着并发位；已开跑的那次会因 generation 前移而写不进结果。

    - `succeeded` / `failed` / `cancelled` 都是已落定的结局，原样返回。
      把 failed 改成 cancelled 会把"系统做砸了"改写成"用户不要了"（契约要求
      两者分开），还会抹掉逐目标的 denied/unreachable 原因、顺手封死 resume。
    - 只改**可重做**的目标（`_RETRYABLE_STATES`）；succeeded / unsupported /
      denied / revoked 是本任务无法靠重做改变的结论，照实保留。
    - 覆盖账本行跟着逐目标记录重算，不留一份过期的 counts。
    """
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.status in ("succeeded", "failed", "cancelled"):
        return await _status_output(session, row)
    entries = list(await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == root_task_id)))
    if not entries:
        # 还没跑过：先把计划分母落成 entries，再逐条标 not_attempted。
        # coverage_entries 有指向 ledger 的外键，父行必须先落。
        node = federation.local_node_id()
        manifest = row.scope_manifest_json
        query_digests = _subquery_digests(row.task_spec_json)
        ledger_row = CoverageLedger(
            root_task_id=root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
            enumeration_state=_enumeration_state(row), retrieval_completeness="partial",
            evidence_sufficiency="insufficient", counts_json={}, manifest_digest="",
            created_at=now, updated_at=now)
        session.add(ledger_row)
        await session.flush()
        for target in _all_targets(row.task_spec_json, manifest, node):
            for digest in query_digests:
                seeded = _entry_row(root_task_id, coverage_kernel.new_entry(
                    _target_key(target), row.scope_id, digest))
                seeded.target_digest = _slot_digest(_target_key(target), digest)
                session.add(seeded)
        await session.flush()
        entries = list(await session.scalars(select(CoverageEntry).where(
            CoverageEntry.root_task_id == root_task_id)))
    marked = 0
    for entry in entries:
        if entry.state not in _RETRYABLE_STATES:
            continue
        entry.state = "not_attempted"
        entry.last_error = "cancelled"
        marked += 1
    ledger = coverage_kernel.ledger(
        root_task_id=root_task_id, scope_ref=row.scope_id, search_mode=row.search_mode,
        enumeration_state=_enumeration_state(row),
        entries=[_entry_from_row(entry, row.scope_id) for entry in entries],
        conflicts=_recorded_conflicts(row))
    ledger_row = await session.get(CoverageLedger, root_task_id)
    if ledger_row is not None:
        ledger_row.retrieval_completeness = ledger["retrieval_completeness"]
        ledger_row.evidence_sufficiency = ledger["evidence_sufficiency"]
        ledger_row.counts_json = ledger["counts"]
        ledger_row.updated_at = now
    row.coverage_ref = root_task_id   # 账本已落（上面建或更新），状态要能指过去
    row.retrieval_completeness = ledger["retrieval_completeness"]
    row.evidence_sufficiency = ledger["evidence_sufficiency"]
    row.status = "cancelled"
    row.error = "cancelled"
    row.updated_at = now
    # 本地步骤执行 + 已派出的远端执行一起停：本地走 `federation.cancel_execution`
    #（generation+1 + dedupe cancel），远端按业务键找 executor_task_id 后
    # best-effort `PeerClient.cancel`（传输失败不炸掉取消本身）。 reconciliation
    # 进 cancel 事件 payload；本地无执行时保持原行为（空 reconciliation）。
    reconciliation = await _cancel_step_executions(
        session, actor, row, plan=row.plan_json, now=now)
    await _append_event(session, root_task_id, _EVENT_CANCELLED,
                        {"marked": marked, "executions": reconciliation}, now=now)
    await _commit(session)
    if not settings.federation_execution_inline:
        await queue.cancel_by_dedupe(session, kind="federation_plan",
                                     dedupe_key=f"federation-request:{root_task_id}")
    return await _status_output(session, row)

async def mark_stalled(session: AsyncSession, root_task_id: str, *, now: datetime,
                       error: str = "coordinator_stalled") -> bool:
    """回收清扫用：把卡在 running 且超过 deadline 的协调任务落成显式失败。

    **状态守卫必须落在 UPDATE 的 WHERE 里**：候选查询与函数各持一道闸，但
    写入本身也必须是条件 UPDATE。SQLite 单连接、PG 的 READ COMMITTED 快照
    都会让"先读后写"拿着旧快照覆盖并发提交的终态 —— 取消发生在读与写之间时，
    一次超时清扫会把用户显式取消的任务改写回 failed。命中 0 行 = 别人已落终态，
    本行不写、事件不追加。

    覆盖账本原样保留（分母不因清扫消失）；真的赢了才追加可见原因的事件。
    """
    changed = await session.execute(
        update(FederationRequest).where(
            FederationRequest.root_task_id == root_task_id,
            FederationRequest.status == "running").values(
            status="failed", error=error, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        return False
    await _append_event(session, root_task_id, _EVENT_FAILED, {"error": error}, now=now)
    return True

async def heartbeat_request(session: AsyncSession, root_task_id: str) -> bool:
    """续协调任务的活性戳（`updated_at`）。返回 False = 已不在 running。

    清扫按 `updated_at` 判"卡死"，而一次 exhaustive 计划可能合法地跑十几分钟；
    `federation_plan` handler 在 `run_queued` 旁边按 `task_heartbeat_seconds`
    调这里，避免把正在推进的任务误杀。
    """
    done = await session.execute(update(FederationRequest).where(
        FederationRequest.root_task_id == root_task_id,
        FederationRequest.status == "running").values(updated_at=utcnow()))
    await session.commit()
    return done.rowcount == 1
