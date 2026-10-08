"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ddp_corpus.federation_peers import PeerDirectory

from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_models import CoverageEntry
from ddp_corpus.federation_peers import Delegation, PeerUnavailable
from ddp_corpus import capabilities, catalog, federation, federation_budget
from ddp_corpus.models import utcnow
from datetime import datetime, timezone
import hashlib
from ddp_core.application import plans, routing
from sqlalchemy import select

from ddp_corpus.federation_tasks.common import (
    ANSWER_OPERATION,
    GENERATION_OPERATIONS,
    GENERATION_WITHHELD_FIELD,
    LOCATE_OPERATION,
    MAX_ANSWER_CANDIDATES,
    SCOPE_TTL_SECONDS,
    SOURCE_POLICY_DENIED,
    WIKI_OPERATION,
    _EVENT_PLAN_READY,
    _RETRYABLE_STATES,
    _append_event,
    _commit,
    _load_request,
    _target_identity,
    _ts,
    _validate_manifest,
)
from ddp_corpus.federation_tasks.consent import (
    peer_directory,
    validate_exploration_consent,
)
from ddp_corpus.federation_tasks.intent import (_intent_budget)
from ddp_corpus.federation_tasks.probes import (_probe_targets)
from ddp_corpus.federation_tasks.targets import (
    _all_targets,
    _descriptor_index,
    _fast_stop_reason,
    _gather_descriptors,
    _onward_policies,
    _ordered_targets,
    _peer_probe_denial,
    _policy_forbids,
    _ranking_unreachable_nodes,
    _select_targets,
    _steps_by_target,
)

def _drop_answer_steps(steps: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """丢掉 `plan_steps` 因远端 `can_generate` 生成的 answer 步（连同它的边）。

    `plan_steps` 的生成节点判据是**证据探测回执自称的 can_generate**，而这条
    路径没有经过授权的能力探测。协调者的 answer 步只有两种来源：
    `_append_local_answer_step`（本地生成就绪）与 `_append_delegated_answer_step`
    （`rag.answer.cited` 能力探测确认就绪）。两者都在计划落定时单独补上，
    所以 `plan_steps` 的答案步一律丢掉，避免绕过能力探测。
    """
    removed = {step["step_id"] for step in steps if step["operation"] == "answer"}
    if not removed:
        return steps, edges
    steps = [step for step in steps if step["step_id"] not in removed]
    edges = [edge for edge in edges if not edge["edge_id"].startswith("edge-answer")]
    return steps, edges

def _answer_probe_key(root_task_id: str, node_id: str, operation: str = "rag.answer.cited",
                       revision: int = 1) -> str:
    parts = [root_task_id, node_id, operation]
    if revision > 1:
        parts.append(revision)
    return "answer-probe:" + hashlib.sha256(plans.canonical_bytes(parts)).hexdigest()

def _append_delegated_answer_step(steps: list[dict], edges: list[dict], *,
                                  coordinator: str, executor: str) -> tuple[list[dict], list[dict]]:
    """在远端生成节点上补一个消费融合证据的 answer 步骤与类型化数据边。

    依赖 `fuse-1`（协调者）：跨节点依赖必须有一条 `evidence_excerpts` 边，
    这条边在**审批之前**就进计划，因此 ExecutionConsent.allowed_edges 覆盖不到
    它时审批会 `egress_denied`，证据一个字节都发不出去。
    """
    steps.append({"step_id": "answer-1", "operation": "answer",
                  "executor_node_id": executor, "depends_on": ["fuse-1"],
                  "fixed_inputs": ["query"]})
    edges.append({"edge_id": "edge-answer-1", "from_node_id": coordinator,
                  "to_node_id": executor, "payload_kind": "evidence_excerpts",
                  "retention": "temporary", "authorised_by": f"relay:{coordinator}"})
    return steps, edges

async def _probe_answer_candidates(*, root_task_id: str, task_spec_digest: str,
                                   consent: dict, scope_ref: str, targets: list[dict],
                                   peers: PeerDirectory,
                                   budget: routing.RootBudget,
                                   extra_nodes: list[str] | None = None,
                                   operation: str = "rag.answer.cited",
                                   revision: int = 1,
                                   spend=None,
                                   policies: list[tuple[str, frozenset]] = (),
                                   manifest: dict | None = None,
                                   unreachable_node_ids=(),
                                   ) -> tuple[str | None, dict]:
    chosen: str | None = None
    outcomes: dict[str, str] = {}
    node = federation.local_node_id()
    origins = {target["origin_node_id"] for target in targets}
    origins.update(extra_nodes or [])
    origins.discard(node)
    # 先排全部节点，再截探测上限：身份顺序不能在距离排序前挤掉最近节点。
    ranked = routing.candidates(
        [{"origin_node_id": origin, "collection_id": "generation", "operation": operation}
         for origin in origins], [], query="", limit=MAX_ANSWER_CANDIDATES,
        local_node_id=node, node_routes=(manifest or {}).get("node_routes"),
        unreachable_node_ids=unreachable_node_ids)
    for item in ranked:
        candidate = item["target_key"]["origin_node_id"]
        if _policy_forbids(policies, candidate):
            # 来源不允许把证据转给它：连能力探测都不发（T83）。
            outcomes[candidate] = SOURCE_POLICY_DENIED
            continue
        denial = _peer_probe_denial(consent, candidate)
        if denial is not None:
            outcomes[candidate] = denial
            continue
        try:
            # 单预扣（spend 内独立提交 + 内存 reserve），调用点禁 double-reserve。
            await spend(kind="probe", amount=1)
        except ApplicationError as exc:
            outcomes[candidate] = exc.code
            break
        request = {
            "schema": "ddp-task-probe/1#ProbeRequest",
            "task_spec_digest": task_spec_digest,
            "consent_ref": consent["consent_id"], "probe_kind": "capability_input",
            "target_node_id": candidate, "scope_ref": scope_ref,
            "operation": operation,
        }
        try:
            await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(request)))
            probe = await peers.client(candidate).probe(
                request, idempotency_key=_answer_probe_key(root_task_id, candidate, operation, revision))
        except ApplicationError as exc:
            outcomes[candidate] = exc.code
            break
        except PeerUnavailable as exc:
            outcomes[candidate] = exc.code or "peer_unavailable"
            continue
        check = probe.get("capability_check")
        readiness = check.get("readiness") if isinstance(check, dict) else None
        if operation == "wiki.pages":
            # wiki 探测看 `readiness` 本身：执行器 `can_generate` 冻结为
            # answer-only（旧契约），不能拿它卡 wiki。
            ready = readiness == "ready"
        else:
            ready = probe.get("can_generate") is True and readiness == "ready"
        if ready:
            chosen = candidate
            outcomes[candidate] = "ready"
            break
        # unknown (never observed) is retryable-needs-precheck, never excludable;
        # only proven-not-ready may exclude. capability_unknown ≠ not_ready.
        outcomes[candidate] = federation.capability_outcome(readiness, probe=probe)
    return chosen, outcomes

async def _generation_available(http, *, now: datetime) -> bool:
    """协调者本地现在真的能做 `rag.answer.cited` 吗。

    **只认本层给 control / peer 的那份能力清单**（`capabilities.answer_generation_ready`
    就是同一个观测，远端执行者接单前用它复核）：模型名、`settings.chat_url`
    配过、远端探测里的 `can_generate`，单独哪一个都不是可用性证据。取不到
    观测、清单里没有这条 operation、readiness 不是 `ready`，一律 False ——
    读不出来就按不可用处理，绝不猜。
    """
    return await capabilities.answer_generation_ready(http, now=now)

def _append_local_answer_step(steps: list[dict], *, coordinator: str) -> list[dict]:
    """在协调者上补一个消费全部检索结果的 answer 步骤。

    执行权威在合成证据之后（`_execute_plan` 的 `_grounded_answer`），所以依赖
    直接写 retrieve 步骤；跨节点来源的证据回传边由 `routing.plan_steps` 生成，
    本地来源同节点不出边（`plans.validate_plan` 的同节点豁免）。
    """
    retrieve_ids = [step["step_id"] for step in steps if step["operation"] == "retrieve"]
    steps.append({"step_id": "answer-1", "operation": "answer",
                  "executor_node_id": coordinator, "depends_on": retrieve_ids,
                  "fixed_inputs": ["query"]})
    return steps

def _append_wiki_steps(steps: list[dict], edges: list[dict], *, coordinator: str,
                       generator: str) -> tuple[list[dict], list[dict]]:
    """补 `source_manifest(A) -> wiki_pages(G) -> validate(A)` 三步。

    生成位置可远端（无模型 A 委托已就绪的 C 只做生成），提交权永远在 A：
    `source_manifest` 与 `validate` 固定本地，`wiki_pages` 落在 `generator`
   （本地就绪时就是协调者自己）。跨节点时 `wiki_draft` 回传边在审批前落图，
    许可覆盖不到就 egress_denied，证据与草稿一个字节都不发。
    """
    retrieve_ids = [step["step_id"] for step in steps if step["operation"] == "retrieve"]
    steps.append({"step_id": "source-manifest-1", "operation": "source_manifest",
                  "executor_node_id": coordinator, "depends_on": retrieve_ids,
                  "fixed_inputs": ["query"]})
    steps.append({"step_id": "wiki-pages-1", "operation": "wiki_pages",
                  "executor_node_id": generator, "depends_on": ["source-manifest-1"],
                  "fixed_inputs": ["query"]})
    steps.append({"step_id": "validate-1", "operation": "validate",
                  "executor_node_id": coordinator, "depends_on": ["wiki-pages-1"],
                  "fixed_inputs": ["query"]})
    if generator != coordinator:
        edges.append({"edge_id": "edge-wiki-evidence-1", "from_node_id": coordinator,
                      "to_node_id": generator, "payload_kind": "evidence_excerpts",
                      "retention": "temporary", "authorised_by": f"relay:{coordinator}"})
        edges.append({"edge_id": "edge-wiki-draft-1", "from_node_id": generator,
                      "to_node_id": coordinator, "payload_kind": "wiki_draft",
                      "retention": "temporary", "authorised_by": f"source:{generator}"})
    return steps, edges

async def read_plan(session: AsyncSession, actor: Actor, root_task_id: str) -> dict:
    row = await _load_request(session, actor, root_task_id)
    if not row.plan_json:
        raise APIError(409, "task has no generated plan", "invalid_request_error", "plan_changed")
    return row.plan_json

async def _settled_targets(session: AsyncSession, root_task_id: str
                           ) -> dict[tuple[str, str, str], tuple[str, str | None, list[str]]]:
    """Targets whose coverage entry is final for this root: (state, last error, receipts)."""
    entries = await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == root_task_id))
    return {_target_identity(entry.target_key_json):
            (entry.state, entry.last_error, list(entry.probe_refs_json or []))
            for entry in entries if entry.state not in _RETRYABLE_STATES}

async def create_plan(session: AsyncSession, actor: Actor, root_task_id: str, *,
                      now: datetime, http, index) -> dict:
    """按持久化的 scope 与探索许可做 Probe、生成并持久化 TaskPlan。"""
    # 每 root 串行化：并发重放不能各做一轮 Probe（外发是有副作用的事实）。
    # 锁内先读现状，计划落定后的重放直接返回已存修订，不再探测。
    await catalog.lock_key(session, "federation-task:" + root_task_id)
    row = await _load_request(session, actor, root_task_id)
    if row.plan_json and row.planning_state in ("ready", "approved"):
        # 重放：计划修订已经定了，不隐式重规划（要新范围就新建任务）。
        return row.plan_json
    if row.planning_state != "draft":
        raise APIError(409, "plan is not open for planning", "invalid_request_error",
                       "plan_changed")
    # 探索许可可能已经过期 —— 这一轮再验一次，过期就不发任何探测。
    consent = validate_exploration_consent(row.exploration_consent_json,
                                           row.task_spec_json, now=now)
    manifest = row.scope_manifest_json
    if manifest is not None:
        _validate_manifest(manifest, now=now)
    node = federation.local_node_id()
    task_spec = row.task_spec_json
    all_targets = _all_targets(task_spec, manifest, node)
    routed_nodes = {route["node_id"] for route in (manifest or {}).get("node_routes", [])}
    valid_until = min(plans.instant(manifest["valid_until"]) if manifest is not None
                      else plans.instant(consent["valid_until"]),
                      plans.instant(consent["valid_until"]), _ts(now) + SCOPE_TTL_SECONDS)
    deadline = plans.utc_instant(valid_until)
    # 本地生成就绪与否决定计划里有没有生成步与 token 额度。判据只来自
    # 能力清单，不来自模型名（"注册即就绪"是这个项目反复吃亏的地方）。
    wants_answer = task_spec["operation"] == ANSWER_OPERATION
    wants_wiki = task_spec["operation"] == WIKI_OPERATION
    wants_generation = task_spec["operation"] in GENERATION_OPERATIONS
    generation_ready = False
    # Pre-0037 intents acquire their ledger in a separate committed transaction.
    # Never update/lock a live ledger in the coordinator's business transaction.
    # Intent-time operation-specific generation limits are frozen before capability
    # readiness is known. Planning may omit generation, but never raises that root cap.
    from ddp_corpus.db import get_sessionmaker
    async with get_sessionmaker()() as ledger_session:
        ledger = await federation_budget.ensure_ledger(
            ledger_session, root_task_id=root_task_id, organization_id=actor.organization_id,
            caller_budget=None, server_caps=_intent_budget(task_spec, manifest, consent, now=now),
            legacy_result=row.result_json, now=now)
        await ledger_session.commit()
    try:
        root_budget = federation_budget.rebuild_from_ledger(
            ledger, None, now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    # 规划阶段的出站全部属于这一个根任务与这一份需求修订：凭证的范围约束从这里取，
    # 不从将要发出的请求体里抄 —— 抄的话，请求体被换掉时凭证会跟着换，对端就核对不出来。
    peers = peer_directory(actor, Delegation(root_task_id=root_task_id,
                                             task_spec_digest=row.task_spec_digest))
    delegated: str | None = None
    answer_outcomes: dict[str, str] = {}
    descriptor_notes: dict = {}
    # 探测可能回滚会话；先在普通值上固定住能力探测需要的全部输入
    # （root_task_id 是函数入参，本来就是普通值）。
    task_spec_digest = row.task_spec_digest
    scope_ref = row.scope_id

    async def _plan_spend(kind: str, amount: int = 1) -> None:
        # 独立原子预扣：先提交后发送；单次内存 reserve（spend 内部做），
        # 调用点不再另做 reserve。父业务回滚/崩溃不退款。
        await federation_budget.spend(
            root_task_id=root_task_id, organization_id=actor.organization_id,
            kind=kind, amount=amount, budget=root_budget, now=utcnow())
    try:
        if wants_generation and http is not None:
            await _plan_spend(kind="request", amount=1)
            generation_ready = await _generation_available(http, now=now)
        # 摘要 -> 选目标 -> 探测：顺序不能反。摘要没取到就退化为确定性
        # local_first 排序，成员一个不少（穷查仍然全量）。
        descriptors, descriptor_notes, capability_only = await _gather_descriptors(
            session, actor, node=node, all_targets=all_targets, manifest=manifest,
            consent=consent, budget=root_budget, peers=peers,
            valid_until=datetime.fromtimestamp(valid_until, timezone.utc),
            spend=_plan_spend)
        unreachable_nodes = await _ranking_unreachable_nodes(session, actor, manifest, now=now)
        candidate_graph = (row.result_json or {}).get(
            federation_budget.CANDIDATE_GRAPH_FIELD) or _select_targets(
                all_targets, task_spec, node, descriptors=descriptors, rank_all=True,
                manifest=manifest, unreachable_node_ids=unreachable_nodes)
        pending = (row.result_json or {}).get("_continuation_targets")
        selected = pending if pending is not None else _select_targets(
            all_targets, task_spec, node, descriptors=descriptors,
            manifest=manifest, unreachable_node_ids=unreachable_nodes)
        # A continuation re-runs only targets whose coverage entry is still retryable; a
        # settled target keeps its recorded receipts. Re-probing it would spend the
        # exploration budget on work that never executes again and starve the new batch.
        settled = await _settled_targets(session, root_task_id) if pending is not None else {}
        budget = federation_budget.limits(ledger)
        probes, outcome, extra = await _probe_targets(
            session, actor, row,
            targets=[target for target in selected if _target_identity(target) not in settled
                     and target["origin_node_id"] not in routed_nodes],
            now=now, http=http, index=index,
            peers=peers, budget=root_budget, manifest=manifest,
            descriptors=_descriptor_index(descriptors), spend=_plan_spend,
            unreachable_nodes=unreachable_nodes)
        if wants_generation and not generation_ready:
            # 本地没有生成能力：在探索许可与根预算之内问候选执行节点
            # "你能不能生成"。answer 问 `rag.answer.cited`，wiki 问 `wiki.pages`
            # —— 真能力就绪才给 token 预算；没有任何 ready 节点就保持诚实的
            # 无生成结果（不伪造答案/Wiki，也不再多发一个字节）。
            delegated, answer_outcomes = await _probe_answer_candidates(
                root_task_id=root_task_id, task_spec_digest=task_spec_digest,
                consent=consent, scope_ref=scope_ref, targets=selected,
                peers=peers, budget=root_budget, extra_nodes=capability_only,
                operation="wiki.pages" if wants_wiki else "rag.answer.cited",
                revision=int(row.plan_revision or 0) + 1, spend=_plan_spend,
                policies=_onward_policies(selected, _descriptor_index(descriptors)),
                manifest=manifest, unreachable_node_ids=set(unreachable_nodes) | {
                    key[0] for key, value in outcome.items() if value[0] == "unreachable"})
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    finally:
        await peers.aclose()
    # Probe 路径在碰撞时可能回滚过 SAVEPOINT，行会被 expire；按主键重读，后面
    # 的计划构造不能触发 async 懒加载（N8；与 `_execute_plan` 的同款防御一致）。
    row = await _load_request(session, actor, root_task_id)
    try:
        steps, edges = routing.plan_steps(
            targets=selected, probes=probes, local_node_id=node,
            coordinator_node_id=node, query=task_spec.get("query") or "",
            now=_ts(now), node_routes=(manifest or {}).get("node_routes", []),
            budget={**budget,
                    # Room for the coordinator's own generation request and hops; plan_steps
                    # carves shares from the REMAINING ledger (cap − used below), because
                    # planning probes/discovery/bytes were already spent above.
                    # The delegated generation admission spends its hops on this
                    # ledger too: 1 for an answer edge (evidence A→generator only;
                    # the answer returns inside the status poll), 2 for a wiki
                    # round trip (evidence out, draft back). Reserving 2 for an
                    # answer over-charges the child shares by one hop: with a
                    # direct remote sibling plus a two-deep delegated leaf the
                    # share comes out 4 instead of the 5 the relay's depth gate
                    # needs (strict: len(path) < max_hops).
                    "max_requests": max(0, budget["max_requests"]
                                        - int(wants_generation and (generation_ready or delegated is not None))),
                    "max_hops": budget["max_hops"] - (2 if wants_wiki else 1)
                    * int(wants_generation and delegated is not None),
                    "used_requests": root_budget.used()["requests"],
                    "used_bytes": root_budget.used()["bytes"],
                    "used_probes": root_budget.used()["probes"],
                    "used_hops": root_budget.used()["hops"]})
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    steps, edges = _drop_answer_steps(steps, edges)
    # 只取证据的任务不会走到这里的任何一支：`generation_ready` 与 `delegated`
    # 都只在要生成（answer/wiki）时才可能为真 —— 既不白跑一次生成，也不留生成预算。
    generates = False
    if wants_wiki:
        # 生成位置可远端、提交权留 A：本地就绪则本地生成；否则委托真实 probe
        # 就绪的 C 只做生成（原始页面草稿），A 收到后校验绑定再本地提交。
        # No ready generator means no generation step; execution reports that absence.
        generator = node if generation_ready else delegated
        if generator is not None:
            steps, edges = _append_wiki_steps(
                steps, edges, coordinator=node, generator=generator)
            generates = True
    elif wants_answer and generation_ready:
        steps = _append_local_answer_step(steps, coordinator=node)
        generates = True
    elif wants_answer and delegated is not None:
        steps, edges = _append_delegated_answer_step(
            steps, edges, coordinator=node, executor=delegated)
        generates = True
    if not generates:
        # The ledger freezes a generation reservation at intent time, before readiness is
        # known; the plan is what gets approved, so it must not carry tokens it never spends.
        budget["max_generation_tokens"] = 0
    steps_by_target = _steps_by_target({"steps": steps}, selected)
    for target in _ordered_targets(selected):
        key = (target["origin_node_id"], target["collection_id"], target["operation"])
        step = steps_by_target.get(key)
        if step is None or step["operation"] == "delegate":
            continue
        fixed_inputs = ["query"]
        if target["operation"] == LOCATE_OPERATION:
            fixed_inputs.append(target["collection_id"])
        else:
            fixed_inputs.append(f"collection:{target['collection_id']}")
        step["fixed_inputs"] = fixed_inputs
        probe_id = extra["probe_ids"].get(key)
        _, _, carried = settled.get(key, (None, None, []))
        step["probe_refs"] = [probe_id] if probe_id else list(carried)
    revision = int(row.plan_revision or 0) + 1
    if revision > 1:
        # A new approved graph must never collide with an old admission key.
        step_ids = {step["step_id"]: f"r{revision}-{step['step_id']}" for step in steps}
        for step in steps:
            step["step_id"] = step_ids[step["step_id"]]
            step["depends_on"] = [step_ids[dependency] for dependency in step["depends_on"]]
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan",
        "plan_id": "plan-" + row.root_task_id,
        "revision": revision,
        "task_spec_digest": row.task_spec_digest,
        "root_coordinator_node_id": node,
        "planning_state": "ready",
        "steps": steps,
        "data_edges": edges,
        "budget": {"max_requests": budget["max_requests"], "max_bytes": budget["max_bytes"],
                   "max_hops": budget["max_hops"], "deadline": budget["deadline"],
                   "max_generation_tokens": budget["max_generation_tokens"]},
        "final_result_writer": node,
        "valid_until": deadline,
    }
    plan["plan_digest"] = plans.task_plan_digest(plan)
    try:
        plans.validate_plan(plan, task_spec, local_node_id=node, now=_ts(now))
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    row.plan_json = plan
    row.plan_revision = plan["revision"]
    row.plan_digest = plan["plan_digest"]
    row.planning_state = "ready"
    row.scope_id = manifest["scope_id"] if manifest is not None else row.scope_id
    row.updated_at = now
    # Settled targets were not probed this round; their recorded outcome (e.g. a consent
    # denial) still explains why fast stopped where it did.
    fast_stop = _fast_stop_reason(
        task_spec, all_targets, selected,
        {**{key: (state, error) for key, (state, error, _) in settled.items()}, **outcome},
        generation_ready, delegated)
    await _append_event(session, row.root_task_id, _EVENT_PLAN_READY, {
        "plan_digest": plan["plan_digest"], "revision": plan["revision"],
        "targets": len(selected),
        "outcomes": {"/".join(key): value[0] for key, value in outcome.items()},
        # 复用与排序依据都要能在事件流里复核：复用的目标不占探测额度，
        # 也不该被读成"这一轮真的探测过"（search_profile 在账本上标记）。
        "reused_probes": {"/".join(key): probe_id
                          for key, probe_id in extra.get("reused", {}).items()},
        "reuse_skipped": {"/".join(key): reason
                          for key, reason in extra.get("reuse_skipped", {}).items()},
        "descriptor_sources": descriptor_notes,
        "answer_executor": delegated,
        "answer_probes": answer_outcomes,
        "generation_ready": generation_ready,
        # fast 可审查的停止原因与预声明候选图：首轮只选有界候选，完整排序
        # 与 capability-only 候选持久化在结果内部字段，供 resume 逐批继续；
        # 扩展超原批准图必须 plan_changed/新审批，不能静默增边。
        "fast_stop": fast_stop,
        "capability_only_nodes": sorted(capability_only),
    }, now=now)
    row.result_json = {**row.result_json,
                       federation_budget.CANDIDATE_GRAPH_FIELD: candidate_graph,
                       federation_budget.FAST_STOP_FIELD: fast_stop}
    row.result_json.pop("_continuation_targets", None)
    row.result_json.pop(GENERATION_WITHHELD_FIELD, None)
    if delegated is None and SOURCE_POLICY_DENIED in answer_outcomes.values():
        # 没有可用生成节点的原因是来源策略，不是"没有模型"（T83）。
        row.result_json[GENERATION_WITHHELD_FIELD] = SOURCE_POLICY_DENIED
    await _commit(session)
    return plan
