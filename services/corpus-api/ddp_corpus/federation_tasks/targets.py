"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ddp_corpus.federation_peers import PeerDirectory

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_peers import PeerUnavailable
from ddp_corpus import cache, catalog, federation
from ddp_core.application import routing

from ddp_corpus.federation_tasks.common import (
    FAST_CANDIDATE_LIMIT,
    GENERATION_OPERATIONS,
    LOCATE_OPERATION,
    _target_identity,
)

# ---------------------------------------------------------------------------
# 目标与候选
# ---------------------------------------------------------------------------


def _all_targets(task_spec: dict, manifest: dict | None, node: str) -> list[dict]:
    if manifest is not None:
        return routing.targets(manifest)
    refs: list[str] = []
    for ref in task_spec["resource_scope"].get("resource_refs") or []:
        if ref not in refs:
            refs.append(ref)
    return [{"origin_node_id": node, "collection_id": ref, "operation": LOCATE_OPERATION}
            for ref in refs]

def _ordered_targets(targets: list[dict]) -> list[dict]:
    """与 `routing._dedup_sorted` 同一顺序：retrieve-{i} 与这个列表逐位对应。"""
    keys = {}
    for target in targets:
        keys[(target["origin_node_id"], target["collection_id"], target["operation"])] = {
            "origin_node_id": target["origin_node_id"],
            "collection_id": target["collection_id"],
            "operation": target["operation"]}
    return [keys[key] for key in sorted(keys)]

def _fast_stop_reason(task_spec: dict, all_targets: list[dict], selected: list[dict],
                      outcome: dict, generation_ready: bool,
                      delegated: str | None) -> str:
    """fast 首轮的可审查停止原因（写入 plan_ready 事件与结果内部字段）。"""
    mode = task_spec["search_policy"]["mode"]
    fixed = task_spec["resource_scope"]["kind"] == "fixed_resources"
    if mode == "exhaustive_scope" or fixed or len(selected) >= len(all_targets):
        return "complete_scope_covered"
    if task_spec["operation"] in GENERATION_OPERATIONS and not generation_ready \
            and delegated is None:
        gate = any(isinstance(value, tuple) and str(value[1] or "").startswith(
            ("budget_exhausted", "egress_mode:", "recipient_not_allowed",
             "payload_not_allowed")) for value in outcome.values())
        return "budget_or_consent_gate" if gate else "no_generation_capacity"
    gate = any(isinstance(value, tuple) and str(value[1] or "").startswith(
        ("budget_exhausted", "egress_mode:", "recipient_not_allowed",
         "payload_not_allowed", "not_attempted")) for value in outcome.values())
    if gate:
        return "budget_or_consent_gate"
    return "candidate_limit_reached"

def _select_targets(targets: list[dict], task_spec: dict, node: str, *,
                    descriptors: list[dict] | None = None,
                    rank_all: bool = False,
                    manifest: dict | None = None,
                    unreachable_node_ids=()) -> list[dict]:
    """按集合目录摘要给候选定序并截到模式上限（排序实现只在路由内核里）。

    没有摘要（未取到/未授权/预算耗尽）时排序退化为确定性 local_first ——
    摘要只影响顺序与 fast 取谁，**永不删除成员**：穷查仍然枚举全部目标。
    """
    if not targets:
        raise APIError(409, "scope enumerates no retrieval targets", "invalid_request_error",
                       "discovery_incomplete")
    mode = task_spec["search_policy"]["mode"]
    fixed = task_spec["resource_scope"]["kind"] == "fixed_resources"
    limit = len(targets) if rank_all or mode == "exhaustive_scope" or fixed \
        else min(FAST_CANDIDATE_LIMIT, len(targets))
    try:
        ranked = routing.candidates(
            targets, list(descriptors or []), query=task_spec.get("query") or "", limit=limit,
            ordering=task_spec["search_policy"].get("ordering", "local_first"),
            local_node_id=node, node_routes=(manifest or {}).get("node_routes"),
            unreachable_node_ids=unreachable_node_ids)
    except ApplicationError as exc:
        raise federation.api_error(exc) from None
    return [item["target_key"] for item in ranked]

async def _ranking_unreachable_nodes(session: AsyncSession, actor: Actor,
                                     manifest: dict | None, *, now: datetime) -> dict[str, str]:
    """只采信当前组织/目录修订下仍存活的负面观测；没有观测不捏造健康。

    返回 {node_id: reason}：排序只看键，探测复用值里的理由，不再为同一
    修订读第二次（负面命中的使用计数只 +1，见 test_negative_hit_…）。
    """
    negative: dict[str, str] = {}
    scope_key = cache.organization_scope(actor.organization_id)
    for origin, revision in _registry_revisions(manifest).items():
        seen = await cache.get_negative(session, scope_key=scope_key, node_id=origin,
                                        node_revision=revision, now=now)
        if seen is not None:
            negative[origin] = str(seen.get("reason") or "unreachable")
    # 中间节点不可达时，该路径上的叶目标同样不能由此协调者直接到达。
    for route in (manifest or {}).get("node_routes") or []:
        for origin in route["via_node_ids"]:
            if origin in negative:
                negative.setdefault(route["node_id"], negative[origin])
                break
    return negative

def _fast_continuation_targets(ranked: list[dict], selected: list[dict]) -> list[dict]:
    """The next fast batch: up to `FAST_CANDIDATE_LIMIT` unselected targets in rank order.

    An explicit continuation may expand even a nonempty previous result: cited evidence
    is not proof that the question is fully answered. Like the first batch it is bounded,
    stays inside the frozen scope and needs a fresh approval (plan §7.2 "继续下一批").
    """
    selected_keys = {(item["origin_node_id"], item["collection_id"], item["operation"])
                     for item in selected}
    remaining = [target for target in ranked
                 if (target["origin_node_id"], target["collection_id"], target["operation"])
                 not in selected_keys]
    return remaining[:FAST_CANDIDATE_LIMIT]

def _plan_selected_targets(plan: dict, all_targets: list[dict]) -> list[dict]:
    """从已落定的计划恢复被选中的目标；执行阶段不再重排。

    `create_plan` 给每个 retrieve 步写了 `fixed_inputs=["query", "collection:<id>"]`
    （固定资源目标是裸资源 id），`executor_node_id` 是 origin —— 这两个字段把
    步骤映回枚举目标。**不能在这里重跑 `_select_targets`**：计划是按目录摘要
    选出来的，执行阶段没有（也不该重新外发）同一份摘要，重排会选出不同集合；
    而 retrieve 步骤与目标是按计划顺序 zip 的，错位会把一个目标的探测回执
    静默挂到另一个目标上。
    """
    index: dict[tuple[str, str], list[dict]] = {}
    for target in all_targets:
        reference = (target["collection_id"] if target["operation"] == LOCATE_OPERATION
                     else "collection:" + target["collection_id"])
        index.setdefault((target["origin_node_id"], reference), []).append(target)
    for matches in index.values():
        matches.sort(key=lambda item: (item["collection_id"], item["operation"]))
    selected: list[dict] = []
    consumed: dict[tuple[str, str], int] = {}
    for step in plan.get("steps") or []:
        if step.get("operation") == "delegate":
            for assigned in step["delegated_targets"]:
                target = assigned["target_key"]
                if target not in all_targets:
                    raise APIError(409, "delegated leaf lies outside frozen scope",
                                   "invalid_request_error", "plan_changed")
                selected.append(target)
            continue
        if step.get("operation") != "retrieve":
            continue
        inputs = step.get("fixed_inputs") or []
        if len(inputs) != 2:
            raise APIError(409, "stored plan has no target binding",
                           "invalid_request_error", "plan_changed")
        lookup = (step["executor_node_id"], inputs[1])
        matches = index.get(lookup)
        position = consumed.get(lookup, 0)
        if not matches or position >= len(matches):
            raise APIError(409, "stored plan references a target outside the scope",
                           "invalid_request_error", "plan_changed")
        selected.append(matches[position])
        consumed[lookup] = position + 1
    return selected

def _steps_by_target(plan: dict, candidates: list[dict]) -> dict:
    mapping = {}
    delegated = {}
    for step in plan["steps"]:
        if step["operation"] == "delegate":
            for item in step["delegated_targets"]:
                delegated[_target_identity(item["target_key"])] = step
    direct = [target for target in _ordered_targets(candidates)
              if _target_identity(target) not in delegated]
    retrieve = [step for step in plan["steps"] if step["operation"] == "retrieve"]
    for step, target in zip(retrieve, direct, strict=False):
        mapping[_target_identity(target)] = step
    return {**mapping, **delegated}

def _peer_probe_denial(consent: dict, node_id: str) -> str | None:
    """探索许可门：一个字节都不外发时的理由（None 表示允许发）。

    条件按 §6.2：模式必须是 listed_nodes、节点在固定接收方集合里、
    问题类别（query_text/subquery_text）在允许外发的载荷里。三者缺一不发。
    """
    if consent["egress_mode"] != "listed_nodes":
        return "egress_mode:" + str(consent["egress_mode"])
    if node_id not in consent["allowed_recipients"]:
        return "recipient_not_allowed"
    if not ({"query_text", "subquery_text"} & set(consent["allowed_payload"])):
        return "payload_not_allowed"
    return None

def _directory_denial(consent: dict, node_id: str) -> str | None:
    """集合目录读的探索许可门：读远端目录是**元数据外发**，与 Probe 同级审查。

    载荷类别是 `collection_filters`：它描述"我想按哪些集合过滤"，不是问题正文。
    未列入接收方或载荷不允许就不读 —— 排序退化为 local_first，不是错误。
    """
    if consent["egress_mode"] != "listed_nodes":
        return "egress_mode:" + str(consent["egress_mode"])
    if node_id not in consent["allowed_recipients"]:
        return "recipient_not_allowed"
    if "collection_filters" not in consent["allowed_payload"]:
        return "payload_not_allowed"
    return None

def _registry_revisions(manifest: dict | None) -> dict[str, str]:
    """ScopeManifest 的 registry_revision_vector -> {node_id: revision 文本}。

    负面缓存的键分量与探测复用的策略修订都取自这里；manifest 缺该节点的条目
    时**不编造修订号**（返回空映射），调用方据此跳过或保守处理。
    """
    if not manifest:
        return {}
    revisions: dict[str, str] = {}
    for item in manifest.get("registry_revision_vector") or []:
        if not isinstance(item, dict):
            continue
        node_id, revision = item.get("node_id"), item.get("registry_revision")
        if isinstance(node_id, str) and node_id and revision is not None:
            revisions.setdefault(node_id, str(revision))
    return revisions

def _probe_policy_revision(manifest: dict | None, node_id: str) -> str:
    """探测复用的策略修订口径：该节点的目录修订（登记/成员可见性变化的载体）。

    当前 P5 回执不记录 policy_revision（缓存文档已声明这是已知缺口），所以这个
    值只在有记录时才参与比对；无该节点修订时用显式 `unbound` 而不是编一个号。
    """
    revision = _registry_revisions(manifest).get(node_id)
    return f"registry:{node_id}:{revision}" if revision else "registry:unbound"

def _descriptor_index(descriptors: list[dict]) -> dict[tuple[str, str], dict]:
    """描述符列表 -> {(origin, collection): descriptor}；坏条目直接丢弃。

    排序只读这几个字段，坏描述符最多让排序退化，不能让规划失败（对端失约
    不是调用方的错误）。
    """
    index: dict[tuple[str, str], dict] = {}
    for item in descriptors:
        if not isinstance(item, dict):
            continue
        origin, collection = item.get("origin_node_id"), item.get("collection_id")
        if isinstance(origin, str) and origin and isinstance(collection, str) and collection:
            index.setdefault((origin, collection), item)
    return index

def _onward_policies(targets: list[dict],
                     descriptors: dict[tuple[str, str], dict]) -> list[tuple[str, frozenset]]:
    """选中目标里带转交策略（`onward_recipients`）的集合：[(来源节点, 允许的节点)]。

    策略取自来源自己的集合描述；描述没取到就不知道策略 —— 那时执行者受理会按整份
    计划复核并 egress_denied，规划这一层只是不去选来源明说禁止的生成节点。
    """
    policies = []
    for target in targets:
        descriptor = descriptors.get((target["origin_node_id"], target["collection_id"])) or {}
        allowed = descriptor.get("onward_recipients")
        if isinstance(allowed, list):
            policies.append((target["origin_node_id"],
                             frozenset(item for item in allowed if isinstance(item, str))))
    return policies

def _policy_forbids(policies: list[tuple[str, frozenset]], candidate: str) -> bool:
    """委托生成会把融合证据 A -> candidate 外发；任一来源不允许它就不能选。"""
    return any(candidate != origin and candidate not in allowed for origin, allowed in policies)

async def _gather_descriptors(session: AsyncSession, actor: Actor, *, node: str,
                              all_targets: list[dict], manifest: dict | None,
                              consent: dict, budget: routing.RootBudget,
                              peers: PeerDirectory,
                              valid_until: datetime,
                              spend=None) -> tuple[list[dict], dict, list[str]]:
    """计划期集合摘要：本地已发布集合 + 许可允许的远端自发布目录。

    - 本地摘要是本库读，不出网，不受探索许可约束；
    - 远端目录读先过 `_directory_denial`（接收方 + `collection_filters`），
      每页请求先占根预算的 `discovery` 额度（与请求预算共用，T87）；
    - 任一步失败（未授权/超时/对端错误/预算耗尽/目录过大）只意味着该节点没有
      摘要可用，规划继续，排序退化为确定性顺序 —— **绝不因此少枚举一个成员**。
    """
    descriptors: list[dict] = []
    notes: dict = {"local": 0, "remote": {}}
    if manifest is not None:
        try:
            local, _readiness = await catalog.visible_catalog(session, actor, node, valid_until)
        except APIError as exc:
            notes["local"] = exc.code or "catalog_unavailable"
        else:
            descriptors.extend(local)
            notes["local"] = len(local)

    exhausted = {"budget": False}

    resource_nodes = {target["origin_node_id"] for target in all_targets}
    routed = {node_id for route in (manifest or {}).get("node_routes", [])
              for node_id in [route["node_id"], *route["via_node_ids"]]}
    remote_nodes = sorted((resource_nodes | set(_registry_revisions(manifest))) - {node} - routed)
    # A frozen scope can include a compute-only node with no collection. It is
    # merely a candidate until its separately authorized capability probe passes.
    capability_only = [origin for origin in remote_nodes if origin not in resource_nodes
                       and _peer_probe_denial(consent, origin) is None]

    async def reserve_page():
        try:
            if spend is not None:
                await spend(kind="discovery", amount=1)
            else:
                budget.reserve("discovery")
        except ApplicationError:
            exhausted["budget"] = True
            return False
        return True
    for origin in remote_nodes:
        denial = _directory_denial(consent, origin)
        if denial is not None:
            notes["remote"][origin] = denial
            continue
        exhausted["budget"] = False
        try:
            fetched = await peers.collections(origin, reserve=reserve_page)
        except PeerUnavailable as exc:
            notes["remote"][origin] = exc.code or "peer_unavailable"
            continue
        # 只采信"这个节点说自己的集合"：对端描述符的 origin 必须就是它自己，
        # 否则一个坏对端可以用别人的 origin 抬高/压低别家目标的排序。
        usable = [item for item in fetched if item.get("origin_node_id") == origin]
        descriptors.extend(usable)
        if exhausted["budget"] and not usable:
            notes["remote"][origin] = "budget_exhausted"
        else:
            notes["remote"][origin] = len(usable)
        if not exhausted["budget"] and not usable and origin not in capability_only:
            capability_only.append(origin)
    return descriptors, notes, capability_only
