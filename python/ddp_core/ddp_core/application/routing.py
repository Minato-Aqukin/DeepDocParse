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


def _route_lengths(node_routes):
    if node_routes is None:
        return {}
    if not isinstance(node_routes, list):
        reject(message="node routes must be an array")
    lengths = {}
    for route in node_routes:
        _obj(route, ("node_id", "via_node_ids"), name="node route")
        _string(route["node_id"], node=True, name="route node")
        via = route["via_node_ids"]
        if not isinstance(via, list) or not via:
            reject(message="delegated route needs intermediate nodes")
        for node in via:
            _string(node, node=True, name="intermediate node")
        origin = route["node_id"]
        lengths[origin] = min(lengths.get(origin, 1 + len(via)), 1 + len(via))
    return lengths


def candidates(targets, descriptors, *, query, limit, ordering="local_first", local_node_id=None,
               node_routes=None, unreachable_node_ids=()) -> list[dict]:
    """只排序已满足硬约束的冻结目标：本地偏好、跳数、负面观测，再比摘要。"""
    if ordering not in ORDERINGS:
        reject(message="unknown ordering")
    _integer(limit, 1, name="candidate limit")
    if not isinstance(query, str):
        reject(message="query must be a string")
    if not isinstance(descriptors, list):
        reject(message="descriptors must be an array")
    if local_node_id is not None:
        _string(local_node_id, node=True, name="local node")
    route_lengths = _route_lengths(node_routes)
    unreachable = set(unreachable_node_ids)
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
        distance = 0 if local else route_lengths.get(target["origin_node_id"], 1)
        negative = target["origin_node_id"] in unreachable
        reasons.append(f"route_length:{distance}")
        if negative:
            reasons.append("negative_cache")
        preference = (not local,) if ordering == "local_first" else ()
        summary = (-recency, -score) if ordering == "freshness_first" else \
            (-score,) if ordering == "cost_first" else (-score, -recency)
        rank = (*preference, distance, negative, *summary, *identity)
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

    def check(self, kind: str, amount: int = 1) -> None:
        """Check a reservation without changing counters."""
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

    def reserve(self, kind: str, amount: int = 1) -> None:
        """预占额度；先查全部上限再一起记账，失败的预占不产生半截消耗。"""
        self.check(kind, amount)
        counter, sub = self._KINDS[kind]
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
                ("requests", "bytes", "generation_tokens", "hops", "discovery",
                 "probes", "egress_bytes")}


def _probe_fresh(probe, now, ttl_seconds=_PROBE_TTL_SECONDS):
    observed = probe.get("observed_at")
    if not isinstance(observed, str):
        return False
    try:
        return now - _epoch(observed, "observed_at") <= ttl_seconds
    except ApplicationError:
        return False


def _shortest_routes(node_routes) -> dict:
    """每个来源节点只留一条路由：最短，同长按字典序（确定、只执行一次）。"""
    routes = {}
    for route in node_routes or []:
        via = route["via_node_ids"]
        previous = routes.get(route["node_id"])
        if previous is None or (len(via), tuple(via)) < (len(previous), tuple(previous)):
            routes[route["node_id"]] = list(via)
    return routes


#: One execution's request allowance: admission, lookup, evidence read and up to
#: 64 status polls. Each direct target and each delegated leaf gets it.
_EXECUTION_REQUESTS = 68
#: Admission attempts a parent keeps per delegate (outside the delegate's share).
_ADMISSION_REQUESTS = 8


def _work_requests(executor, assigned) -> int:
    """`executor` 这一份要的请求数：每个自己检索的目标一次执行额度，每个下级委托
    受理额度加它那份。与 `plan_steps` 在 executor 上留给自己的固定额度一致。"""
    own = 0
    children: dict[str, list] = {}
    for origin, via in assigned:
        if via:
            children.setdefault(via[0], []).append((origin, via[1:]))
        else:
            own += 1
    return _EXECUTION_REQUESTS * own + sum(_ADMISSION_REQUESTS + _work_requests(child, items)
                                           for child, items in children.items())


def request_need(*, targets, coordinator_node_id, node_routes=None) -> int:
    """协调者的检索与委托需要的请求数（不含探测、发现与生成）。

    直连目标与经委托的叶目标一样各有一次执行额度；路由上每一级受理再加
    受理额度。根预算按它定额，`plan_steps` 先给各委托这份需要再分余数。
    """
    routes = _shortest_routes(node_routes)
    assigned = [(member["origin_node_id"], routes.get(member["origin_node_id"], []))
                for member in _dedup_sorted(targets)]
    return _work_requests(coordinator_node_id, assigned)


def _work_hops(executor, assigned, path_length) -> int:
    """`executor` 自己要花的跳数：每个直连远端检索 2，每个下级委托受理 2 加它的份额。

    `assigned` 是 `(来源节点, executor 之后的 via)`；`path_length` 是 executor
    被受理时带的委托路径长度（根为 0）。与 `plan_steps` 在 executor 上的切法一致。
    """
    own_remote = 0
    children: dict[str, list] = {}
    for origin, via in assigned:
        if via:
            children.setdefault(via[0], []).append((origin, via[1:]))
        elif origin != executor:
            own_remote += 1
    return 2 * own_remote + sum(2 + _share_hops(child, items, path_length + 1)
                                for child, items in children.items())


def _transmission_hops(executor, assigned) -> int:
    """`plans.validate_plan` 给 executor 这份计划记的传输跳数。

    每个远端叶目标两条边（问题去、证据回），每条按 1 + 中继数计；executor
    自己的本地目标不出边。按叶计，所以同一中继背后叶子越多需要越多。
    """
    return sum(2 * (1 + len(via)) for origin, via in assigned if via or origin != executor)


def _share_hops(executor, assigned, path_length) -> int:
    """委托份额至少要多少跳：够它自己的活，够它那份子计划的传输校验，
    也过得了受理深度闸（路径长度 < 份额）。"""
    return max(path_length + 1, _work_hops(executor, assigned, path_length),
               _transmission_hops(executor, assigned))


def hop_need(*, targets, coordinator_node_id, node_routes=None, delegation_depth=0) -> int:
    """协调者的检索与委托在整条路由上需要的跳数（不含生成）。

    两条下限取大：一是要花的——直连远端目标 2 跳（问题去、证据回），按第一跳
    聚合的每个委托 2 跳受理加一份够下游逐级走完、且每一级都过得了严格深度闸
    的份额；二是 `plans.validate_plan` 按叶记的全部传输（含中继）。根预算按它
    定额，`plan_steps` 也按同一规则给各委托分跳数，多级路由不会在下游被深度闸
    或计划校验拒掉。
    """
    routes = _shortest_routes(node_routes)
    assigned = [(member["origin_node_id"], routes.get(member["origin_node_id"], []))
                for member in _dedup_sorted(targets)]
    return max(_work_hops(coordinator_node_id, assigned, delegation_depth),
               _transmission_hops(coordinator_node_id, assigned))


def plan_steps(*, targets, probes, local_node_id, coordinator_node_id, query, now,
               node_routes=None, budget=None, delegation_depth=0) -> tuple[list[dict], list[dict]]:
    """生成最小步骤图：每个目标一个 retrieve，协调者上 fuse，能生成就再 answer。

    跨节点依赖必须带类型化数据边；同节点不产生边。返回的边都是单跳，
    调用方据此为 `max_hops` 留出至少 `len(data_edges)` 的额度。
    `delegation_depth` 是协调者自己被受理时的委托路径长度（根为 0），
    下级委托的深度闸按它往下算。
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
    routes = _shortest_routes(node_routes)
    groups = {}
    for index, member in enumerate(members, 1):
        origin = member["origin_node_id"]
        via = routes.get(origin, [])
        if via:
            executor = via[0]
            step = groups.get(executor)
            if step is None:
                step = {"step_id": f"delegate-{len(groups) + 1}", "operation": "delegate",
                        "executor_node_id": executor, "depends_on": [], "fixed_inputs": ["query"],
                        "delegated_targets": []}
                groups[executor] = step
                steps.append(step)
                retrieve_ids.append(step["step_id"])
            step["delegated_targets"].append({"target_key": member, "via_node_ids": via[1:]})
        else:
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
            if via:
                edges[-2]["relay_via"] = via
                edges[-1]["relay_via"] = list(reversed(via))
    if groups:
        if budget is None:
            reject("budget_exceeded", "recursive plans require a frozen parent budget")
        count = len(groups)
        used = budget.get("used_requests", 0) if isinstance(budget.get("used_requests"), int) else 0
        used_bytes = budget.get("used_bytes", 0) if isinstance(budget.get("used_bytes"), int) else 0
        used_probes = budget.get("used_probes", 0) if isinstance(budget.get("used_probes"), int) else 0
        used_hops = budget.get("used_hops", 0) if isinstance(budget.get("used_hops"), int) else 0
        # Shares come out of what the parent has NOT spent yet: planning probes,
        # discovery reads and payload bytes already went through this same ledger.
        # A share carved from raw caps would fail its reservation at execution.
        remaining_requests = max(0, budget["max_requests"] - used)
        remaining_bytes = max(0, budget["max_bytes"] - used_bytes)
        remaining_probes = max(0, budget.get("max_probe_requests", budget["max_requests"]) - used_probes)
        remaining_hops = max(0, budget["max_hops"] - used_hops)
        own_targets = sum(1 for step in steps if step["operation"] == "retrieve")
        own_remote = sum(1 for step in steps
                         if step["operation"] == "retrieve"
                         and step["executor_node_id"] != coordinator_node_id)
        # The coordinator's own work keeps its fixed costs: one admission
        # allowance per delegate, and per direct target the same execution
        # allowance a delegated leaf gets. Requests go to each delegate's need
        # first (its leaves' execution allowances plus every admission on its
        # routes) and the rest by delegated leaf count; a caller budget too
        # tight for those needs is split by delegated leaf count, best effort.
        # Bytes/probes split by leaf count over all leaves (floor), all from the
        # remaining allowance, not the raw caps. Hops follow route depth: each
        # delegate first gets what its route needs to reach every leaf through
        # strict depth gates, the rest is split evenly; a pool short of that is
        # refused here, at planning, rather than by a downstream relay.
        total_leaves = sum(len(step["delegated_targets"]) for step in groups.values()) + own_targets
        delegated_leaves = total_leaves - own_targets
        requests = max(0, remaining_requests - _ADMISSION_REQUESTS * count
                       - _EXECUTION_REQUESTS * own_targets)
        byte_cap = max(0, remaining_bytes - 32768 * count - 4096 * own_targets)
        hops = max(0, remaining_hops - 2 * count - 2 * own_remote)
        probe_cap = min(requests, remaining_probes)
        assigned = [[(item["target_key"]["origin_node_id"], item["via_node_ids"])
                     for item in step["delegated_targets"]] for step in groups.values()]
        hop_needs = [_share_hops(step["executor_node_id"], items, delegation_depth + 1)
                     for step, items in zip(groups.values(), assigned)]
        if hops < sum(hop_needs):
            reject("budget_exceeded", "hop shares cannot reach the delegated route depth")
        spare_hops = hops - sum(hop_needs)
        request_needs = [_work_requests(step["executor_node_id"], items)
                         for step, items in zip(groups.values(), assigned)]
        needs_fit = requests >= sum(request_needs)
        spare_requests = requests - sum(request_needs) if needs_fit else requests
        for position, step in enumerate(groups.values()):
            leaves = len(step["delegated_targets"])
            def portion(value, _leaves=leaves):
                # Leaf-proportional floor over every leaf: the coordinator's own
                # targets keep their proportion of bytes and probes.
                if not total_leaves:
                    return 0
                return max(0, (value * _leaves) // total_leaves)
            def request_portion(_position=position, _leaves=leaves):
                # Own targets already hold their allowance outside `requests`,
                # so only delegated leaves divide what is left; floors never
                # exceed the pool.
                share = (spare_requests * _leaves) // delegated_leaves
                return share + (request_needs[_position] if needs_fit else 0)
            def hop_portion(_position=position):
                return (hop_needs[_position] + spare_hops // count
                        + int(_position < spare_hops % count))
            request_share = request_portion()
            step["budget_share"] = {
                "max_requests": request_share, "max_bytes": portion(byte_cap),
                # Probes are requests: never above the share's own request cap.
                "max_hops": hop_portion(), "max_probes": min(portion(probe_cap), request_share),
                "deadline": budget["deadline"]}
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
