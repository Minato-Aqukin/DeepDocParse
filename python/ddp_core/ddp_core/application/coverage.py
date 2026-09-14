"""覆盖账本：分母、分子与 §7.4 的合取判定。

`complete` 是合取，缺一条都不许写：范围已封存、所有适用目标都有有效回执、
没有在途/未尝试/超时/拒绝/撤销/来源自报 partial，且每个被排除的目标都有可核验
依据。`unsupported` 行**不进成功数也不进缺口数** —— 它进排除数（§7.4 与
`coverage-ledger-exhaustive-complete` 夹具）。fast 模式永远返回 partial：
它只完成了自己选中的候选，不能因为候选全成功就宣布全范围查完（§7.2）。
"""
from __future__ import annotations

import copy

from ddp_contracts.enums import (
    COVERAGE_TARGET_STATE_VALUES,
    ENUMERATION_STATE_VALUES,
    RETRIEVAL_COMPLETENESS_VALUES,
    EVIDENCE_SUFFICIENCY_VALUES,
    SEARCH_MODE_VALUES,
)

from ddp_core.application import plans
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import reusable, validate_probe

_TARGET_FIELDS = ("origin_node_id", "collection_id", "operation")
#: 记账为一次尝试的状态；planned/not_attempted/revoked 不是尝试。
_ATTEMPTED = {"in_flight", "succeeded", "partial", "denied", "failed", "unreachable"}
#: 自报内部不完整的取值域。与 ddp-task-probe 的 retrieval.internal_limits 同一组。
_INTERNAL_LIMITS = ("shard_failed", "index_lagging", "subset_only", "truncated_by_limit")
#: 单条缺失要求与整份排除依据的持久化上限。超限显式拒绝，**绝不静默截断**：
#: 2026-09 复核发现内核可以拼出 64KiB 的依据，而列原本是 String(160) ——
#: PostgreSQL 上那是 DataError 500，任务停在 running。列已放宽到 Text，
#: 这里保留显式上限作为契约。
MAX_REQUIREMENT_CHARS = 1024
MAX_EXCLUSION_BASIS_CHARS = 4096


def reject(code="protocol_incompatible", message="invalid coverage object"):
    raise ApplicationError(code, message)


def _obj(value, required, optional=(), name="object"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        reject(message=f"invalid {name}: missing or unknown fields")


def _string(value, *, node=False, checksum=False, empty=False, name="field"):
    if not isinstance(value, str) or (not empty and not value) or len(value) > 65536:
        reject(message=f"invalid {name}")
    if node and not plans.NODE.fullmatch(value):
        reject(message="invalid node identity")
    if checksum and not plans.DIGEST.fullmatch(value):
        reject(message="invalid content digest")


def _enum(value, allowed, name):
    if value not in allowed:
        reject(message=f"unknown {name}")


def _integer(value, minimum=0, name="integer"):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        reject(message=f"invalid {name}")


def _epoch(value, name="instant"):
    try:
        return plans.instant(value)
    except ApplicationError:
        reject(message=f"{name} must be an RFC 3339 timestamp")


def _validate_target_key(value):
    _obj(value, _TARGET_FIELDS, name="target key")
    _string(value["origin_node_id"], node=True, name="origin node")
    _string(value["collection_id"], name="collection id")
    _string(value["operation"], name="operation")


def target_key(origin_node_id, collection_id, operation) -> dict:
    """`(origin_node_id, collection_id, operation)` 三元组；origin 而非 next_hop，
    同一来源经多条路径到达只计一次（§5.3）。"""
    value = {"origin_node_id": origin_node_id, "collection_id": collection_id, "operation": operation}
    _validate_target_key(value)
    return value


def new_entry(target_key, scope_ref, query_digest) -> dict:
    """新建一行覆盖记录，初始 state=planned，分子与尝试数都从零开始。"""
    entry = {
        "target_key": copy.deepcopy(target_key),
        "scope_ref": scope_ref,
        "query_or_subquery_digest": query_digest,
        "state": "planned",
        "probe_receipts": [],
        "actual_index_revision": None,
        "search_profile": None,
        "attempts": 0,
        "last_error": None,
        "evidence_refs": [],
        "used_budget": {"requests": 0, "bytes": 0},
        "exclusion_basis": None,
    }
    validate_entry(entry)
    return entry


def _derived_state(entry, probe):
    if probe is None:
        return entry.get("state", "planned")
    retrieval = probe.get("retrieval") or {}
    if retrieval:
        limits = retrieval.get("internal_limits") or []
        return "partial" if limits else retrieval["status"]
    if probe.get("missing_requirements"):
        return "unsupported"
    if probe["capability_check"]["readiness"] == "unhealthy":
        return "failed"
    # 能力探测只能回答"支不支持"；没有检索回执就不能声称检索成功。
    return entry.get("state", "planned")


def _exclusion_basis(probe: dict) -> str | None:
    """把 `missing_requirements` 拼成可核验依据；越界显式拒绝。

    个体要求与整份依据都有上限。旧实现直接 `"; ".join(...)`，既能被单个
    超长字符串撑爆列宽，也会把非字符串成员变成 TypeError —— 两种都不是
    契约里的"有依据"。
    """
    requirements = probe.get("missing_requirements") or []
    if not isinstance(requirements, list):
        reject(message="missing_requirements must be an array")
    parts: list[str] = []
    for item in requirements:
        if not isinstance(item, str) or not item or len(item) > MAX_REQUIREMENT_CHARS:
            reject("protocol_incompatible",
                   "missing requirement must be a nonempty bounded string")
        parts.append(item)
    basis = "; ".join(parts)
    if len(basis) > MAX_EXCLUSION_BASIS_CHARS:
        reject("protocol_incompatible", "exclusion basis exceeds the persistence limit")
    return basis or None


def record(entry: dict, probe: dict | None, *, state=None, error=None, now,
           limits=()) -> dict:
    """把一次 probe（或无 probe 的失败事实）写进一行覆盖记录，返回新 entry。

    不修改传入对象；`succeeded` 必须有回执、实际索引修订与未过期的探测，
    `unsupported` 必须有可核验依据（缺失时从 `missing_requirements` 派生）。

    `limits` 是**没有 probe 可依的执行结果**自报的内部限制（如执行回执里的
    `internal_limits`）。它与 probe 自报的限制行为完全一致：非空一律把
    这一行压成 `partial`。少了这个入口，一个 `partial + truncated_by_limit`
    的执行会在账本里被洗成 `succeeded`（T85）。
    """
    updated = copy.deepcopy(entry)
    if not isinstance(updated, dict):
        reject(message="coverage entry must be an object")
    limits = list(limits)
    for limit in limits:
        _enum(limit, _INTERNAL_LIMITS, "internal limit")
    if probe is not None:
        validate_probe(probe)
        retrieval = probe.get("retrieval") or {}
        limits = list(retrieval.get("internal_limits") or []) + limits
        receipts = list(updated.get("probe_receipts") or [])
        if probe["probe_id"] not in receipts:
            receipts.append(probe["probe_id"])
        updated["probe_receipts"] = receipts
        if retrieval.get("index_revision"):
            updated["actual_index_revision"] = retrieval["index_revision"]
        if retrieval.get("evidence_set_ref"):
            refs = list(updated.get("evidence_refs") or [])
            if retrieval["evidence_set_ref"] not in refs:
                refs.append(retrieval["evidence_set_ref"])
            updated["evidence_refs"] = refs
    resolved = state if state is not None else _derived_state(updated, probe)
    if limits and resolved in {"succeeded", "planned", "in_flight"}:
        # 对方自报内部不完整时不允许在本账本上把这一行洗成成功或"还在跑"。
        resolved = "partial"
    updated["state"] = resolved
    if probe is not None or resolved in _ATTEMPTED:
        updated["attempts"] = int(updated.get("attempts", 0)) + 1
    if error is not None:
        updated["last_error"] = error
    if resolved == "succeeded":
        if not updated.get("probe_receipts") or not updated.get("actual_index_revision"):
            reject("partial_retrieval", "succeeded target requires a probe receipt and an actual index revision")
        if probe is not None and not reusable(probe, now=now):
            # 过期探测只能重新探测，不能把旧证据洗成新证据。
            reject("partial_retrieval", "an expired probe cannot be recorded as a new retrieval success")
    if resolved == "unsupported" and not updated.get("exclusion_basis") and probe is not None:
        basis = _exclusion_basis(probe)
        if basis:
            updated["exclusion_basis"] = basis
    validate_entry(updated)
    return updated


def validate_entry(entry: dict) -> None:
    """校验 ddp-scope-coverage/1#CoverageEntry 的字段与两条 allOf 规则。"""
    _obj(entry, ("target_key", "scope_ref", "query_or_subquery_digest", "state", "attempts"),
         ("probe_receipts", "actual_index_revision", "search_profile", "last_error",
          "evidence_refs", "used_budget", "exclusion_basis"), name="coverage entry")
    _validate_target_key(entry["target_key"])
    _string(entry["scope_ref"], name="scope ref")
    _string(entry["query_or_subquery_digest"], checksum=True, name="query or subquery digest")
    _enum(entry["state"], COVERAGE_TARGET_STATE_VALUES, "coverage target state")
    _integer(entry["attempts"], name="attempts")
    for key in ("probe_receipts", "evidence_refs"):
        if key in entry:
            if not isinstance(entry[key], list):
                reject(message=f"{key} must be an array")
            for item in entry[key]:
                _string(item, empty=True, name=key)
    if entry.get("actual_index_revision") is not None:
        _string(entry["actual_index_revision"], empty=True, name="actual index revision")
    for key in ("search_profile", "last_error", "exclusion_basis"):
        if entry.get(key) is not None:
            _string(entry[key], empty=True, name=key)
    if entry.get("exclusion_basis") and len(entry["exclusion_basis"]) > MAX_EXCLUSION_BASIS_CHARS:
        # 显式拒绝，不静默截断：截断后看起来仍是一条"可核验依据"。
        reject("protocol_incompatible", "exclusion basis exceeds the persistence limit")
    if "used_budget" in entry:
        _obj(entry["used_budget"], ("requests", "bytes"), name="used budget")
        for key, value in entry["used_budget"].items():
            _integer(value, name=f"used budget {key}")
    if entry["state"] == "unsupported" and not entry.get("exclusion_basis"):
        reject("capability_unsupported", "unsupported target requires a verifiable exclusion basis")
    if entry["state"] == "succeeded":
        if not entry.get("probe_receipts") or not entry.get("actual_index_revision"):
            reject("partial_retrieval", "succeeded target requires a probe receipt and an actual index revision")


def validate_manifest(manifest: dict) -> None:
    """校验 ddp-scope-coverage/1#ScopeManifest；sealed 不得留未展开子域。"""
    _obj(manifest, ("schema", "scope_id", "caller_scope_hash", "created_at", "valid_until",
                    "registry_revision_vector", "expanded_members", "unexpanded_subtrees",
                    "enumeration_state", "manifest_digest"), ("child_manifests",), name="scope manifest")
    if manifest["schema"] != "ddp-scope-coverage/1#ScopeManifest":
        reject(message="unsupported scope manifest schema")
    _string(manifest["scope_id"], name="scope id")
    _string(manifest["caller_scope_hash"], checksum=True, name="caller scope hash")
    _epoch(manifest["created_at"], "created_at")
    _epoch(manifest["valid_until"], "valid_until")
    revisions = manifest["registry_revision_vector"]
    if not isinstance(revisions, list) or not revisions:
        reject(message="registry revision vector must not be empty")
    for revision in revisions:
        _obj(revision, ("node_id", "registry_revision", "fetched_at"),
             ("directory_ref", "snapshot_ref"), name="directory revision")
        _string(revision["node_id"], node=True, name="directory node")
        _integer(revision["registry_revision"], 1, name="registry revision")
        _epoch(revision["fetched_at"], "fetched_at")
        for key in ("directory_ref", "snapshot_ref"):
            if revision.get(key) is not None:
                _string(revision[key], name=key)
    if not isinstance(manifest["expanded_members"], list):
        reject(message="expanded_members must be an array")
    for member in manifest["expanded_members"]:
        _validate_target_key(member)
    if not isinstance(manifest["unexpanded_subtrees"], list):
        reject(message="unexpanded_subtrees must be an array")
    for subtree in manifest["unexpanded_subtrees"]:
        _obj(subtree, ("node_id", "reason"), name="unexpanded subtree")
        _string(subtree["node_id"], node=True, name="unexpanded node")
        _enum(subtree["reason"], ("timeout", "denied", "enumeration_unsupported", "budget_exhausted", "unknown"),
              "unexpanded reason")
    _enum(manifest["enumeration_state"], ENUMERATION_STATE_VALUES, "enumeration state")
    _string(manifest["manifest_digest"], checksum=True, name="manifest digest")
    for child in manifest.get("child_manifests", []):
        _obj(child, ("node_id", "scope_ref", "enumeration_state"), name="child manifest")
        _string(child["node_id"], node=True, name="child node")
        _string(child["scope_ref"], name="child scope ref")
        _enum(child["enumeration_state"], ENUMERATION_STATE_VALUES, "child enumeration state")
    if manifest["enumeration_state"] == "sealed" and manifest["unexpanded_subtrees"]:
        reject(message="sealed scope cannot keep unexpanded subtrees")


def _groups(entries):
    groups = {}
    for entry in entries:
        key = entry["target_key"]
        groups.setdefault((key["origin_node_id"], key["collection_id"], key["operation"]), []).append(entry)
    return groups


def _excluded(group):
    return all(entry.get("state") == "unsupported" and entry.get("exclusion_basis") for entry in group)


def _succeeded(group):
    return all(entry.get("state") == "succeeded" for entry in group)


def completeness(enumeration_state: str, search_mode: str, entries) -> str:
    """§7.4 的合取判定。多子查询目标必须逐项成功；一个子查询成功不算目标完成。"""
    _enum(search_mode, SEARCH_MODE_VALUES, "search mode")
    _enum(enumeration_state, ENUMERATION_STATE_VALUES, "enumeration state")
    if not isinstance(entries, list):
        reject(message="coverage entries must be an array")
    if search_mode == "fast":
        # fast 只完成选中的候选，结局最多是 partial（§7.2）。
        return "partial"
    if not entries:
        return "not_started"
    if enumeration_state != "sealed":
        return "partial"
    for group in _groups(entries).values():
        if _succeeded(group) or _excluded(group):
            continue
        return "partial"
    return "complete"


def sufficiency(entries, *, bindings=(), conflicting=False) -> str:
    """证据充分性只用规则判，不读 LLM 自报信心；unknown 只在还没评估时用。"""
    if type(conflicting) is not bool:
        reject(message="conflicting must be a boolean")
    if not isinstance(entries, list):
        reject(message="coverage entries must be an array")
    if not isinstance(bindings, (list, tuple)):
        reject(message="bindings must be a sequence")
    if conflicting:
        return "conflicting"
    if not entries:
        return "unknown"
    if not bindings:
        return "insufficient"
    return "sufficient_by_policy"


def _counts(entries):
    total = succeeded = excluded = incomplete = 0
    for group in _groups(entries).values():
        total += 1
        if _succeeded(group):
            succeeded += 1
        elif _excluded(group):
            excluded += 1
        else:
            incomplete += 1
    return {
        "total_targets": total,
        "applicable_targets": total - excluded,
        "succeeded": succeeded,
        "excluded": excluded,
        "incomplete": incomplete,
    }


def ledger(*, root_task_id, scope_ref, search_mode, enumeration_state, entries) -> dict:
    """从覆盖记录组装 CoverageLedger；counts 按去重后的目标口径统计。"""
    _string(root_task_id, name="root task id")
    _string(scope_ref, name="scope ref")
    _enum(search_mode, SEARCH_MODE_VALUES, "search mode")
    _enum(enumeration_state, ENUMERATION_STATE_VALUES, "enumeration state")
    if not isinstance(entries, list):
        reject(message="coverage entries must be an array")
    prepared = [copy.deepcopy(entry) for entry in entries]
    for entry in prepared:
        validate_entry(entry)
        if entry["scope_ref"] != scope_ref:
            reject(message="coverage entry belongs to another scope")
    retrieval_completeness = completeness(enumeration_state, search_mode, prepared)
    counts = _counts(prepared)
    if retrieval_completeness == "complete" and counts["incomplete"]:
        # 防御性：complete 与缺口计数不可能同时成立。
        retrieval_completeness = "partial"
    return {
        "schema": "ddp-scope-coverage/1#CoverageLedger",
        "root_task_id": root_task_id,
        "scope_ref": scope_ref,
        "search_mode": search_mode,
        "enumeration_state": enumeration_state,
        "retrieval_completeness": retrieval_completeness,
        "evidence_sufficiency": sufficiency(
            prepared, bindings=[ref for entry in prepared for ref in entry.get("evidence_refs", [])]),
        "entries": prepared,
        "counts": counts,
    }


def validate_ledger(value: dict) -> None:
    """校验 ddp-scope-coverage/1#CoverageLedger 及其三条 allOf（fast/complete/计数）。"""
    _obj(value, ("schema", "root_task_id", "scope_ref", "search_mode", "enumeration_state",
                 "retrieval_completeness", "evidence_sufficiency", "entries", "counts"), name="coverage ledger")
    if value["schema"] != "ddp-scope-coverage/1#CoverageLedger":
        reject(message="unsupported coverage ledger schema")
    _string(value["root_task_id"], name="root task id")
    _string(value["scope_ref"], name="scope ref")
    _enum(value["search_mode"], SEARCH_MODE_VALUES, "search mode")
    _enum(value["enumeration_state"], ENUMERATION_STATE_VALUES, "enumeration state")
    _enum(value["retrieval_completeness"], RETRIEVAL_COMPLETENESS_VALUES, "retrieval completeness")
    _enum(value["evidence_sufficiency"], EVIDENCE_SUFFICIENCY_VALUES, "evidence sufficiency")
    if not isinstance(value["entries"], list):
        reject(message="coverage entries must be an array")
    for entry in value["entries"]:
        validate_entry(entry)
    _obj(value["counts"], ("total_targets", "applicable_targets", "succeeded", "excluded", "incomplete"),
         name="coverage counts")
    for key, count in value["counts"].items():
        _integer(count, name=f"coverage count {key}")
    if value["search_mode"] == "fast" and value["retrieval_completeness"] == "complete":
        reject("partial_retrieval", "fast mode can never claim complete")
    if value["retrieval_completeness"] == "complete":
        if value["enumeration_state"] != "sealed":
            reject(message="complete retrieval requires a sealed enumeration")
        if value["counts"]["incomplete"] != 0:
            reject(message="complete retrieval requires zero incomplete targets")
