"""联邦路由的纯函数族：去重目标、候选排序、根预算与最小步骤图。

排序只读集合摘要（语言/主题/时间范围）与本地性，不读内容、不调模型；摘要只影响
顺序与 fast 的取数上限，**不删成员** —— 唯一证据不在主题摘要里时穷查仍要实际
探测（计划 T84）。`RootBudget` 是全任务唯一账本：发现、probe、检索、字节、
生成 token、hop 共用一份额度，任何子任务都不允许重新获得完整额度（§7.5）。
"""
from __future__ import annotations

import re

from ddp_contracts.enums import SEARCH_MODE_VALUES

from ddp_core.application import plans
from ddp_core.application.coverage import validate_manifest
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import reusable, validate_probe

ORDERINGS = ("local_first", "cost_first", "freshness_first")
_PROBE_TTL_SECONDS = 300
_LOCAL_BOOST = 2
_TOKEN = re.compile(r"[0-9a-z\u4e00-\u9fff]+")


def reject(code="protocol_incompatible", message="invalid routing input"):
    raise ApplicationError(code, message)


def _obj(value, required, optional=(), name="object"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        reject(message=f"invalid {name}: missing or unknown fields")


def _string(value, *, node=False, empty=False, name="field"):
    if not isinstance(value, str) or (not empty and not value) or len(value) > 65536:
        reject(message=f"invalid {name}")
    if node and not plans.NODE.fullmatch(value):
        reject(message="invalid node identity")


def _integer(value, minimum=0, name="integer"):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        reject(message=f"invalid {name}")


def _epoch(value, name="instant"):
    try:
        return plans.instant(value)
    except ApplicationError:
        reject(message=f"{name} must be an RFC 3339 timestamp")


def _validate_target(value):
    _obj(value, ("origin_node_id", "collection_id", "operation"), name="target key")
    _string(value["origin_node_id"], node=True, name="origin node")
    _string(value["collection_id"], name="collection id")
    _string(value["operation"], name="operation")


def _dedup_sorted(members) -> list[dict]:
    # 与 Go 侧 FinalizeScope 同一排序：origin, collection, operation 字典序后去重。
    if not isinstance(members, list):
        reject(message="targets must be an array")
    keys = {}
    for member in members:
        _validate_target(member)
        keys[(member["origin_node_id"], member["collection_id"], member["operation"])] = {
            "origin_node_id": member["origin_node_id"],
            "collection_id": member["collection_id"],
            "operation": member["operation"],
        }
    return [keys[key] for key in sorted(keys)]


def targets(manifest: dict) -> list[dict]:
    """从 ScopeManifest 取下去重后的 TargetKey；manifest 本身不合法就拒绝。"""
    validate_manifest(manifest)
    return _dedup_sorted(manifest["expanded_members"])


def _tokens(query):
    return sorted({token for token in _TOKEN.findall(query.casefold()) if len(token) > 1 or token.isascii() is False})


def _query_language(tokens):
    text = "".join(tokens)
    if any("\u4e00" <= character <= "\u9fff" for character in text):
        return "zh"
    if text and text.isascii():
        return "en"
    return None


def _recency(descriptor):
    if not isinstance(descriptor, dict):
        return 0.0
    time_range = descriptor.get("time_range")
    if not isinstance(time_range, dict) or time_range.get("to") is None:
        return 0.0
    try:
        return _epoch(time_range["to"], "time_range.to")
    except ApplicationError:
        return 0.0


def _score(descriptor, tokens, local):
    score, reasons = 0, []
    if descriptor is None:
        # 没有摘要只能排在后面，不能因此把成员从穷查里删掉（§7.3）。
        reasons.append("descriptor_missing")
    else:
        topics = [str(topic).casefold() for topic in descriptor.get("topics", []) if isinstance(topic, str)]
        hits = sum(1 for topic in topics if any(token in topic for token in tokens))
        if hits:
            score += 2 * hits
            reasons.append(f"topic_match:{hits}")
        languages = {str(language).casefold() for language in descriptor.get("languages", []) if isinstance(language, str)}
        language = _query_language(tokens)
        if language and language in languages:
            score += 1
            reasons.append(f"language:{language}")
    if local:
        score += _LOCAL_BOOST
        reasons.append("local")
    return score, reasons


def candidates(targets, descriptors, *, query, limit, ordering="local_first", local_node_id=None) -> list[dict]:
    """按摘要与排序偏好给目标定序；local_first 本地优先但不独占，相同输入定序恒定。"""
    if ordering not in ORDERINGS:
        reject(message="unknown ordering")
    _integer(limit, 1, name="candidate limit")
    if not isinstance(query, str):
        reject(message="query must be a string")
    if not isinstance(descriptors, list):
        reject(message="descriptors must be an array")
    if local_node_id is not None:
        _string(local_node_id, node=True, name="local node")
    catalogue = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            reject(message="descriptor must be an object")
        key = (descriptor.get("origin_node_id"), descriptor.get("collection_id"))
        catalogue.setdefault(key, descriptor)
    tokens = _tokens(query)
    ranked = []
    for target in _dedup_sorted(targets):
        descriptor = catalogue.get((target["origin_node_id"], target["collection_id"]))
        local = local_node_id is not None and target["origin_node_id"] == local_node_id
        score, reasons = _score(descriptor, tokens, local)
        recency = _recency(descriptor)
        identity = (target["origin_node_id"], target["collection_id"], target["operation"])
        if ordering == "freshness_first":
            # 时效优先：时间范围缺失或解析不了的下沉，其余按分数与稳定键。
            rank = (-recency, -score, *identity)
        elif ordering == "cost_first":
            # CollectionDescriptor 没有费用字段；少一跳的本地目标就是最低成本项。
            rank = (-score, *identity)
        else:
            rank = (-score, -recency, *identity)
        ranked.append((rank, score, reasons, target))
    ranked.sort(key=lambda row: row[0])
    return [
        {"target_key": row[3], "score": row[1], "reason": ";".join(row[2]) or "stable_order"}
        for row in ranked[:limit]
    ]


class RootBudget:
    """根预算的唯一账本；消耗只增不减，超限一律抛 `budget_exhausted`。"""

    _KINDS = {
        "request": ("requests", None),
        "requests": ("requests", None),
        "retrieve": ("requests", None),
        "probe": ("requests", "probe"),
        "discovery": ("requests", "discovery"),
        "bytes": ("bytes", None),
        "egress_bytes": ("bytes", "egress"),
        "hop": ("hops", None),
        "hops": ("hops", None),
        "generation": ("generation_tokens", None),
        "generation_tokens": ("generation_tokens", None),
        "tokens": ("generation_tokens", None),
    }
    _REQUIRED = ("max_requests", "max_bytes", "deadline")
    _OPTIONAL = ("max_generation_tokens", "max_hops", "max_probe_requests",
                 "max_egress_bytes", "max_discovery_requests")

    def __init__(self, budget: dict, *, now):
        _obj(budget, self._REQUIRED, self._OPTIONAL, name="root budget")
        for key in ("max_requests", "max_bytes"):
            _integer(budget[key], name=key)
        _integer(budget.get("max_generation_tokens", 0), name="max_generation_tokens")
        if "max_hops" in budget:
            _integer(budget["max_hops"], 1, name="max_hops")
        for key in ("max_probe_requests", "max_egress_bytes", "max_discovery_requests"):
            if key in budget:
                _integer(budget[key], name=key)
        if type(now) not in (int, float) or isinstance(now, bool):
            reject(message="now must be a timestamp")
        self._deadline = _epoch(budget["deadline"], "deadline")
        self._now = now
        self._used = {"requests": 0, "bytes": 0, "generation_tokens": 0, "hops": 0, "discovery": 0}
        self._used["probes"] = 0
        self._used["egress_bytes"] = 0
        self._caps = {
            "requests": budget["max_requests"],
            "bytes": budget["max_bytes"],
            "generation_tokens": budget.get("max_generation_tokens", 0),
            "hops": budget.get("max_hops", 1),
        }
        self._sub_caps = {
            "probe": budget.get("max_probe_requests", budget["max_requests"]),
            "discovery": budget.get("max_discovery_requests", budget["max_requests"]),
            "egress": budget.get("max_egress_bytes", budget["max_bytes"]),
        }

    def reserve(self, kind: str, amount: int = 1) -> None:
        """预占额度；先查全部上限再一起记账，失败的预占不产生半截消耗。"""
        _integer(amount, name="reservation amount")
        spec = self._KINDS.get(kind)
        if spec is None:
            reject(message=f"unknown budget kind {kind!r}")
        if self._now >= self._deadline:
            raise ApplicationError("budget_exhausted", "root budget deadline has passed")
        counter, sub = spec
        if self._used[counter] + amount > self._caps[counter]:
            raise ApplicationError("budget_exhausted", f"{counter} budget exhausted")
        if sub == "probe" and self._used["probes"] + amount > self._sub_caps["probe"]:
            raise ApplicationError("budget_exhausted", "probe budget exhausted")
        if sub == "egress" and self._used["egress_bytes"] + amount > self._sub_caps["egress"]:
            raise ApplicationError("budget_exhausted", "egress budget exhausted")
        if sub == "discovery" and self._used["discovery"] + amount > self._sub_caps["discovery"]:
            raise ApplicationError("budget_exhausted", "discovery budget exhausted")
        self._used[counter] += amount
        if sub == "probe":
            self._used["probes"] += amount
        elif sub == "egress":
            self._used["egress_bytes"] += amount
        elif sub == "discovery":
            # 发现与目录展开本身也占预算，且与请求额度共用一份（T87）。
            self._used["discovery"] += amount

    def used(self) -> dict:
        return {key: self._used[key] for key in
                ("requests", "bytes", "generation_tokens", "hops", "discovery")}


def _probe_fresh(probe, now, ttl_seconds=_PROBE_TTL_SECONDS):
    observed = probe.get("observed_at")
    if not isinstance(observed, str):
        return False
    try:
        return now - _epoch(observed, "observed_at") <= ttl_seconds
    except ApplicationError:
        return False


def plan_steps(*, targets, probes, local_node_id, coordinator_node_id, query, now) -> tuple[list[dict], list[dict]]:
    """生成最小步骤图：每个目标一个 retrieve，协调者上 fuse，能生成就再 answer。

    跨节点依赖必须带类型化数据边；同节点不产生边。返回的边都是单跳，
    调用方据此为 `max_hops` 留出至少 `len(data_edges)` 的额度。
    """
    _string(local_node_id, node=True, name="local node")
    _string(coordinator_node_id, node=True, name="coordinator node")
    if not isinstance(query, str):
        reject(message="query must be a string")
    if not isinstance(probes, list):
        reject(message="probes must be an array")
    for probe in probes:
        validate_probe(probe)
    members = _dedup_sorted(targets)
    if not members:
        reject(message="a task plan needs at least one retrieval target")
    steps, edges = [], []
    retrieve_ids = []
    for index, member in enumerate(members, 1):
        origin = member["origin_node_id"]
        step = {"step_id": f"retrieve-{index}", "operation": "retrieve", "executor_node_id": origin, "depends_on": []}
        refs = sorted({probe["probe_id"] for probe in probes
                       if probe["target_node_id"] == origin and reusable(probe, now=now)})
        if refs:
            step["probe_refs"] = refs
        steps.append(step)
        retrieve_ids.append(step["step_id"])
        if origin != coordinator_node_id:
            edges.append({"edge_id": f"edge-query-{index}", "from_node_id": coordinator_node_id,
                          "to_node_id": origin, "payload_kind": "query_text", "retention": "temporary",
                          "authorised_by": f"local:{coordinator_node_id}"})
            # 证据回传边：来源节点的授权引用，接收方是协调者。
            edges.append({"edge_id": f"edge-evidence-{index}", "from_node_id": origin,
                          "to_node_id": coordinator_node_id, "payload_kind": "evidence_excerpts",
                          "retention": "temporary",
                          "authorised_by": f"source:{origin}:{member['collection_id']}"})
    steps.append({"step_id": "fuse-1", "operation": "fuse", "executor_node_id": coordinator_node_id,
                  "depends_on": retrieve_ids})
    capable = sorted({probe["target_node_id"] for probe in probes
                      if probe.get("can_generate") and _probe_fresh(probe, now)})
    generation = next((node for node in (local_node_id, coordinator_node_id) if node in capable), None)
    if generation is None and capable:
        generation = capable[0]
    if generation is not None:
        steps.append({"step_id": "answer-1", "operation": "answer", "executor_node_id": generation,
                      "depends_on": ["fuse-1"]})
        if generation != coordinator_node_id:
            edges.append({"edge_id": "edge-answer-1", "from_node_id": coordinator_node_id,
                          "to_node_id": generation, "payload_kind": "evidence_excerpts",
                          "retention": "temporary", "authorised_by": f"relay:{coordinator_node_id}"})
    return steps, edges
