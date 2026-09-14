"""评测执行器：在冻结夹具上跑 fast 与 exhaustive_scope，并守覆盖诚实性。

**真实与模拟的边界（必须写明）**：本模块不调用 corpus-api 的 HTTP/DB 应用，
也不跑任何真实索引或模型。它替换的只有"检索执行"（`federation.run_probe`
到真实索引之间的 I/O，由 `executor.FixtureExecutor` 这个夹具预言机承担）。
其余全部走真实实现：

- 目标枚举：`ddp_core.application.routing.targets`（含 manifest 校验）；
- 候选选择：协调者 `ddp_corpus.federation_tasks._select_targets`
  （fast 的 `FAST_CANDIDATE_LIMIT`、`local_first` 排序、exhaustive 全量）；
- 根预算：协调者 `_root_budget` + `routing.RootBudget`（probe 预占、超限抛错）；
- 外发许可门：协调者 `_peer_probe_denial`（未授权目标零调用）；
- Probe 构造：`ddp_core.application.probe.build_probe`；
- 覆盖记录与合取：`ddp_core.application.coverage.{new_entry,record,ledger}`；
- 步骤图：`routing.plan_steps` + `plans.validate_plan`。

诚实性不是"跑完再算"：`run_question` 在任何一条不变式被破坏时**当场抛
`CoverageHonestyError`**，而不是把违规记进报告继续。被守护的包括：fast 不得
宣称 complete、complete 必须每个适用目标 succeeded、返回证据必须属于被探集合
且不是私有诱饵、deny/未选中目标零调用、账本充足性与实际证据存在性一致。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from ddp_core.application import coverage as coverage_kernel
from ddp_core.application import plans, probe as probe_kernel, routing
from ddp_core.application.ports import ApplicationError
from ddp_corpus import federation_tasks as coordinator

from .dataset import FROZEN_AT, PROBE_CANDIDATE_LIMIT
from .executor import FixtureExecutor


class CoverageHonestyError(AssertionError):
    """覆盖账本说了与事实不符的话（或评测器自己破坏了不变式）。"""


DEFAULT_CONSENT_ID = "explore-routing-eval"
DEFAULT_OPERATION = "corpus.retrieve"

#: 评测自己声称守着的不变式（报告逐条列出；任何一条红都不许出报告）。
INVARIANTS = (
    "fast must never report retrieval_completeness=complete",
    "complete requires sealed enumeration and every applicable target succeeded",
    "ledger entries cover exactly the enumerated targets and the denominator matches",
    "evidence_sufficiency=sufficient_by_policy iff at least one evidence was retrieved",
    "retrieved evidence must belong to the probed collection and be in scope",
    "private collections must never be probed or returned",
    "denied or unselected targets must have zero executor calls",
    "the assembled plan must pass plans.validate_plan",
    "the ledger must pass coverage.validate_ledger",
)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _pair(target: dict) -> tuple[str, str, str]:
    return (target["origin_node_id"], target["collection_id"], target["operation"])


def _label(target: dict) -> str:
    return f"{target['origin_node_id']}/{target['collection_id']}"


def default_consent(dataset: dict, *, consent_id: str = DEFAULT_CONSENT_ID) -> dict:
    """默认探索许可：listed_nodes、明确接收方、查询文本载荷、有界预算。"""
    recipients = sorted({node["node_id"] for node in dataset["nodes"]
                         if node["node_id"] != dataset["local_node_id"]})
    return {
        "schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": consent_id,
        "granted_by": "user-routing-eval", "granted_at": _iso(FROZEN_AT),
        "valid_until": _iso(FROZEN_AT + timedelta(seconds=3600)),
        "egress_mode": "listed_nodes", "allowed_payload": ["query_text"],
        "allowed_recipients": recipients,
        "budget": {"max_probe_requests": 32, "max_egress_bytes": 1 << 20},
    }


def local_only_consent(*, consent_id: str = "explore-routing-eval-local-only") -> dict:
    """零外发许可：`local_only` 模式按契约禁止载荷、接收方与远端预算。"""
    return {
        "schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": consent_id,
        "granted_by": "user-routing-eval", "granted_at": _iso(FROZEN_AT),
        "valid_until": _iso(FROZEN_AT + timedelta(seconds=3600)),
        "egress_mode": "local_only", "allowed_payload": [],
        "allowed_recipients": [],
        "budget": {"max_probe_requests": 0, "max_egress_bytes": 0},
    }


def _task_spec(question: dict, mode: str, consent_id: str, manifest: dict,
               local_node_id: str) -> dict:
    spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "rag.answer.cited", "workspace_ref": "workspace-routing-eval",
        "query": question["query"],
        "resource_scope": {"kind": "site_public", "scope_ref": manifest["scope_id"]},
        "search_policy": {"mode": mode, "ordering": "local_first"},
        "execution_policy": {"mode": "trusted_federation",
                             "coordinator_ref": local_node_id},
        "consent_refs": {"exploration": consent_id, "execution": None},
        "budget_ref": "budget-routing-eval",
    }
    plans.validate_spec(spec)
    return spec


def _check_retrieval(dataset: dict, target: dict, result) -> None:
    """返回的证据必须属于被探集合，且不得来自私有集合。"""
    collection_id = target["collection_id"]
    collection = dataset["collections"].get(collection_id)
    if result.collection_id != collection_id:
        raise CoverageHonestyError(
            f"executor returned evidence for {result.collection_id!r} while "
            f"{collection_id!r} was probed")
    if collection is None or collection["publication"] != "published":
        raise CoverageHonestyError(f"target {collection_id!r} is not a published collection")
    for item in result.items:
        meta = dataset["evidence"].get(item.get("evidence_id"))
        if meta is None:
            raise CoverageHonestyError(
                f"retrieved unknown evidence {item.get('evidence_id')!r}")
        if meta["collection_id"] != collection_id \
                or meta["origin_node_id"] != target["origin_node_id"]:
            raise CoverageHonestyError(
                f"retrieved out-of-scope evidence {meta['evidence_id']!r} "
                f"({meta['origin_node_id']}/{meta['collection_id']}) for target "
                f"{_label(target)}")
        if dataset["collections"][meta["collection_id"]]["publication"] != "published":
            raise CoverageHonestyError(
                f"retrieved evidence from private collection {meta['collection_id']!r}")


def _check_ledger_honesty(ledger: dict, *, all_targets: list[dict], manifest: dict,
                          entries: list[dict], retrieved_ids: list[str],
                          mode: str) -> None:
    """独立于内核复算一遍覆盖合取；内核与这里任何一方说谎都要红。"""
    problems: list[str] = []
    digest = {coordinator._target_digest(target): target for target in all_targets}
    by_digest: dict[str, dict] = {}
    for entry in entries:
        key = entry["target_key"]
        target = {"origin_node_id": key["origin_node_id"],
                  "collection_id": key["collection_id"], "operation": key["operation"]}
        by_digest[coordinator._target_digest(target)] = entry
    if set(by_digest) != set(digest):
        problems.append("ledger entries do not cover exactly the enumerated targets")
    if mode == "fast" and ledger["retrieval_completeness"] == "complete":
        problems.append("fast mode claimed retrieval_completeness=complete")
    if ledger["retrieval_completeness"] == "complete":
        if manifest["enumeration_state"] != "sealed":
            problems.append("complete retrieval with an unsealed enumeration")
        for target_digest, target in digest.items():
            entry = by_digest.get(target_digest)
            if entry is None or entry["state"] != "succeeded":
                state = entry["state"] if entry else "missing"
                problems.append(f"complete retrieval but {_label(target)} is {state}")
        if ledger["counts"]["incomplete"] != 0:
            problems.append("complete retrieval with incomplete targets > 0")
    if ledger["counts"]["total_targets"] != len(all_targets):
        problems.append("total_targets denominator does not match the manifest")
    sufficient = ledger["evidence_sufficiency"] == "sufficient_by_policy"
    if sufficient != bool(retrieved_ids):
        problems.append(
            "evidence_sufficiency disagrees with the actual evidence set "
            f"(sufficiency={ledger['evidence_sufficiency']}, "
            f"retrieved={len(retrieved_ids)})")
    if problems:
        raise CoverageHonestyError("; ".join(problems))


def _check_audit(executor, *, outcomes: dict, all_targets: list[dict],
                 selected: list[dict]) -> None:
    """执行者调用日志必须与"允许尝试且成功"的目标逐个对齐。"""
    calls = getattr(executor, "calls", None)
    if calls is None:
        raise CoverageHonestyError("executor must expose an auditable `calls` log")
    attempted = {_pair(target) for target in selected
                 if outcomes[_pair(target)]["state"] == "succeeded"}
    allowed = {_pair(target) for target in all_targets}
    called = [tuple((call["origin_node_id"], call["collection_id"], call["operation"]))
              for call in calls]
    if len(called) != len(attempted) or set(called) != attempted:
        raise CoverageHonestyError(
            f"executor call log disagrees with planned probes: called={sorted(set(called))} "
            f"attempted={sorted(attempted)}")
    out_of_scope = set(called) - allowed
    if out_of_scope:
        raise CoverageHonestyError(f"executor was called out of scope: {sorted(out_of_scope)}")


def _kernel_rank_probe(dataset: dict, question: dict, all_targets: list[dict],
                       local_node_id: str) -> dict:
    """内核级摘要排序探针（**不是**协调者 fast 的实际选择）。

    P5 协调者给 `routing.candidates` 传的 descriptors 是空列表（`_select_targets`），
    所以真实 fast 是身份顺序 + 本地优先。这里额外用夹具的集合摘要跑一遍
    `routing.candidates`，把"摘要排序会把必需集合排到第几"如实记下来，
    这正是计划 T84 关注的摘要漏证据情形。
    """
    ranked = routing.candidates(
        all_targets, copy.deepcopy(dataset["descriptors"]), query=question["query"],
        limit=len(all_targets), ordering="local_first", local_node_id=local_node_id)
    required_collections = []
    missing_required = []
    for evidence_id in question["required_evidence"]:
        meta = dataset["evidence"].get(evidence_id)
        if meta is None:
            # 变异/坏夹具：标注指向不存在的证据。召回照实按标注算，这里如实报缺。
            missing_required.append(evidence_id)
            continue
        required_collections.append(meta["collection_id"])
    required_collections = sorted(set(required_collections))
    ranks = []
    for collection_id in required_collections:
        best = None
        for position, item in enumerate(ranked, start=1):
            if item["target_key"]["collection_id"] == collection_id:
                best = position
                break
        ranks.append({"collection_id": collection_id, "rank": best})
    best_rank = min((row["rank"] for row in ranks if row["rank"] is not None),
                    default=None)
    return {
        "note": "kernel summary ranking over fixture descriptors; not the coordinator fast path",
        "ranks": ranks,
        "missing_required_evidence": missing_required,
        "best_rank": best_rank,
        "candidate_limit": coordinator.FAST_CANDIDATE_LIMIT,
        "within_fast_candidate_limit": (
            best_rank is not None and best_rank <= coordinator.FAST_CANDIDATE_LIMIT
            if question["required_evidence"] else None),
    }


def run_question(dataset: dict, question: dict, mode: str, *, executor=None,
                 ledger_fn=None, consent: dict | None = None,
                 now: datetime = FROZEN_AT) -> dict:
    """在夹具上执行一个问题的一种搜索模式，返回逐题记录（JSON 可序列化）。"""
    if mode not in ("fast", "exhaustive_scope"):
        raise ValueError(f"unknown search mode {mode!r}")
    local_node_id = dataset["local_node_id"]
    manifest = copy.deepcopy(dataset["scopes"][question["scope_ref"]])
    try:
        coordinator._validate_manifest(manifest, now=now)
    except Exception as exc:  # APIError：摘要不符/过期都拒绝继续
        raise CoverageHonestyError(f"frozen scope manifest is invalid: {exc}") from exc
    all_targets = routing.targets(manifest)
    consent = copy.deepcopy(consent) if consent is not None else default_consent(dataset)
    task_spec = _task_spec(question, mode, consent["consent_id"], manifest, local_node_id)
    consent = coordinator.validate_exploration_consent(consent, task_spec, now=now)
    selected = coordinator._select_targets(all_targets, task_spec, local_node_id)
    remote_count = sum(1 for target in selected
                       if target["origin_node_id"] != local_node_id)
    valid_until = min(plans.instant(manifest["valid_until"]),
                      plans.instant(consent["valid_until"]),
                      now.timestamp() + coordinator.SCOPE_TTL_SECONDS)
    deadline = plans.utc_instant(valid_until)
    budget_body = coordinator._root_budget(
        consent, target_count=len(selected), remote_count=remote_count,
        deadline=deadline, generation_ready=False)
    budget = routing.RootBudget(budget_body, now=now.timestamp())
    task_spec_digest = plans.task_spec_digest(task_spec)

    executor = executor if executor is not None else FixtureExecutor(dataset)
    outcomes: dict[tuple, dict] = {}
    probes: dict[tuple, dict] = {}
    fused: dict[str, dict] = {}
    for index, target in enumerate(selected, start=1):
        key = _pair(target)
        origin = target["origin_node_id"]
        if origin != local_node_id:
            # 探索许可门在执行阶段仍然生效：deny 的目标一个字节都不发。
            denial = coordinator._peer_probe_denial(consent, origin)
            if denial is not None:
                outcomes[key] = {"state": "denied", "error": denial, "items": []}
                continue
            try:
                budget.reserve("probe")  # 与协调者一样：失败的预占不退款
            except ApplicationError as exc:
                outcomes[key] = {"state": "not_attempted", "error": exc.code, "items": []}
                continue
        result = executor.retrieve(target=target, query=question["query"],
                                   candidate_limit=PROBE_CANDIDATE_LIMIT)
        _check_retrieval(dataset, target, result)
        items = result.items
        probe_id = f"probe-{question['question_id']}-{index:02d}"
        evidence_set_ref = f"federation-probe:{probe_id}" if items else None
        probe = probe_kernel.build_probe(
            probe_id=probe_id, target_node_id=origin, task_spec_digest=task_spec_digest,
            consent_ref=consent["consent_id"], probe_kind="evidence_retrieval",
            capability_check={"operation": DEFAULT_OPERATION, "readiness": "ready",
                              "input_validation": "content_verified"},
            retrieval={"status": "succeeded",
                       "collection_ref": target["collection_id"],
                       "index_revision": dataset["collections"][target["collection_id"]]
                       ["index_revision"],
                       "candidate_limit": PROBE_CANDIDATE_LIMIT,
                       "continuation_ref": None, "evidence_set_ref": evidence_set_ref,
                       "internal_limits": []},
            can_generate=False, observed_at=_iso(now))
        probe["scope_ref"] = manifest["scope_id"]
        probe_kernel.validate_probe(probe)
        probes[key] = probe
        outcomes[key] = {"state": "succeeded", "error": None, "items": items}
        for item in items:
            fused[item["evidence_id"]] = item

    # 步骤图：与协调者 `create_plan` 同序，再补 fixed_inputs/probe_refs。
    steps, edges = routing.plan_steps(
        targets=selected, probes=list(probes.values()), local_node_id=local_node_id,
        coordinator_node_id=local_node_id, query=question["query"], now=now.timestamp())
    steps_by_target = coordinator._steps_by_target({"steps": steps}, selected)
    for target in coordinator._ordered_targets(selected):
        step = steps_by_target.get(_pair(target))
        if step is None:
            continue
        step["fixed_inputs"] = ["query", f"collection:{target['collection_id']}"]
        probe = probes.get(_pair(target))
        step["probe_refs"] = [probe["probe_id"]] if probe else []
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan",
        "plan_id": f"plan-{question['question_id']}",
        "revision": 1,
        "task_spec_digest": task_spec_digest,
        "root_coordinator_node_id": local_node_id,
        "planning_state": "ready",
        "steps": steps,
        "data_edges": edges,
        "budget": {key: budget_body[key] for key in
                   ("max_requests", "max_bytes", "max_hops", "deadline",
                    "max_generation_tokens")},
        "final_result_writer": local_node_id,
        "valid_until": deadline,
    }
    plan["plan_digest"] = plans.task_plan_digest(plan)
    try:
        plans.validate_plan(plan, task_spec, local_node_id=local_node_id,
                            now=now.timestamp())
    except ApplicationError as exc:
        raise CoverageHonestyError(f"assembled plan violates plans.validate_plan: {exc}") from exc

    # 覆盖记录：与协调者 `_execute_plan` 相同两步（先 probe、后执行结局）。
    query_digest = plans.content_digest(question["query"].encode("utf-8"))
    entries = []
    for target in all_targets:
        key = _pair(target)
        entry = coverage_kernel.new_entry(coordinator._target_key(target),
                                          manifest["scope_id"], query_digest)
        if key in probes:
            try:
                entry = coverage_kernel.record(entry, probes[key], now=now.timestamp())
            except ApplicationError:
                entry = coverage_kernel.new_entry(coordinator._target_key(target),
                                                  manifest["scope_id"], query_digest)
        if key in outcomes:
            outcome = outcomes[key]
            entry = coverage_kernel.record(entry, None, state=outcome["state"],
                                           error=outcome["error"], now=now.timestamp())
        else:
            entry = coverage_kernel.record(entry, None, state="not_attempted",
                                           error="search_mode_fast", now=now.timestamp())
        entries.append(entry)

    assemble = ledger_fn if ledger_fn is not None else coverage_kernel.ledger
    ledger = assemble(
        root_task_id=f"task-{question['question_id']}", scope_ref=manifest["scope_id"],
        search_mode=mode, enumeration_state=manifest["enumeration_state"], entries=entries)
    try:
        coverage_kernel.validate_ledger(ledger)
    except ApplicationError as exc:
        raise CoverageHonestyError(
            f"kernel ledger violates its own contract: {exc}") from exc
    retrieved_ids = sorted(fused)
    _check_ledger_honesty(ledger, all_targets=all_targets, manifest=manifest,
                          entries=entries, retrieved_ids=retrieved_ids, mode=mode)
    _check_audit(executor, outcomes=outcomes, all_targets=all_targets, selected=selected)

    required = sorted(question["required_evidence"])
    hit = sorted(set(required) & set(retrieved_ids))
    missing = sorted(set(required) - set(retrieved_ids))
    prober = {
        "targets_total": len(all_targets),
        "targets_planned": [_label(target) for target in selected],
        "targets_probed": [_label(target) for target in selected
                           if outcomes[_pair(target)]["state"] == "succeeded"],
        "targets_not_attempted": [{"target": _label(target),
                                   "state": outcomes[_pair(target)]["state"],
                                   "error": outcomes[_pair(target)]["error"]}
                                  for target in all_targets if _pair(target) in outcomes
                                  and outcomes[_pair(target)]["state"] != "succeeded"],
        "remote_targets_probed": sum(
            1 for target in selected
            if target["origin_node_id"] != local_node_id
            and outcomes[_pair(target)]["state"] == "succeeded"),
        "probe_requests": budget.used()["requests"],
        "budget": {
            "used": budget.used(),
            "caps": {
                "max_requests": budget_body["max_requests"],
                "max_probe_requests": budget_body["max_probe_requests"],
                "max_egress_bytes": budget_body["max_egress_bytes"],
                "max_hops": budget_body["max_hops"],
                "max_generation_tokens": budget_body["max_generation_tokens"],
            },
        },
        "plan": {
            "steps": len(plan["steps"]),
            "retrieve_steps": sum(1 for step in plan["steps"]
                                  if step["operation"] == "retrieve"),
            "data_edges": len(plan["data_edges"]),
            "plan_digest": plan["plan_digest"],
        },
    }
    record = {
        "question_id": question["question_id"], "class": question["class"],
        "mode": mode, "query": question["query"], "notes": question["notes"],
        "scope_ref": manifest["scope_id"],
        "required_evidence": required,
        "retrieved_evidence": retrieved_ids,
        "required_retrieved": hit, "required_missing": missing,
        "absolute_recall": {"hits": len(hit), "required": len(required),
                            "value": (len(hit) / len(required)) if required else None},
        "decoy_evidence_retrieved": sorted(
            set(question["decoy_evidence"]) & set(retrieved_ids)),
        "private_decoy_evidence_retrieved": sorted(
            set(question["private_decoy_evidence"]) & set(retrieved_ids)),
        "targets": prober,
        "target_outcomes": {
            _label(target): outcomes[_pair(target)]["state"]
            for target in all_targets if _pair(target) in outcomes},
        "retrieval_completeness": ledger["retrieval_completeness"],
        "evidence_sufficiency": ledger["evidence_sufficiency"],
        "counts": ledger["counts"],
        "unretrieved_targets": [
            {"target_key": entry["target_key"], "state": entry["state"],
             "last_error": entry.get("last_error")}
            for entry in entries if entry["state"] != "succeeded"],
        "conflict": {
            "annotated": bool(question["conflict"]),
            "observed": bool(question["conflict"]) and set(required) <= set(retrieved_ids),
            "kernel_conflicting_axis": (
                coverage_kernel.sufficiency(entries, bindings=retrieved_ids,
                                            conflicting=True)
                if question["conflict"] else None),
        },
        "summary_hidden": bool(question["summary_hidden"]),
        "kernel_rank_probe": _kernel_rank_probe(dataset, question, all_targets,
                                                local_node_id),
        "executor_calls": list(getattr(executor, "calls", [])),
        "honesty_violations": [],
    }
    return record
