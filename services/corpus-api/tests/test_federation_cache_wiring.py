"""P6 缓存接入协调者（PR32 接入面）：探测复用、负面缓存、目录摘要排序。

这个文件量的**不是**缓存表本身的正确性（那是 `test_cache.py` 的范围），而是
协调者有没有按冻结 API 用它，以及用错时后果有没有被守住：

1. 复用必须绑定当前 query digest 与当前索引修订，且不跨调用者；
2. 负面缓存命中时零 HTTP、不续命；新修订/过期后必须重新接触；
3. 远端目录读是元数据外发：未授权/无预算时零请求；取到的描述符必须真的
   进入 `routing.candidates`，fast 按摘要排序选人（而不是身份顺序）；
4. 执行阶段按**计划**恢复目标集合 —— 摘要排序选出的计划不能在执行时错位。

所有"零请求"断言都直接数 stub 的调用日志：静默外发在这类测试里最容易被放过。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import ORG, actor_headers
from ddp_corpus import cache, federation_tasks
from ddp_corpus.cache import FederationCacheEntry
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.models import as_aware, utcnow
from test_federation_probes import NODE, configure_federation, indexed_source, publish_collection
from test_federation_tasks import (
    PEER_NODE,
    StubPeer,
    approve_task,
    calls_to,
    create_intent,
    exploration,
    install_peer,
    member,
    peer_descriptor,
    plan_task,
    scope_manifest,
    submit_task,
    task_spec,
)


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


# ------------------------------------------------------------------ 夹具构造

def peer_consent(*, payloads=("query_text", "collection_filters"), discovery=8,
                 probes=16):
    return exploration(
        egress="listed_nodes", recipients=(PEER_NODE,), payload=payloads,
        budget={"max_probe_requests": probes, "max_egress_bytes": 1 << 20,
                "max_discovery_requests": discovery})


def manifest_for(collections, *, revision=1):
    return scope_manifest([member(collection_id, PEER_NODE) for collection_id in collections],
                          revisions=[(NODE, 1), (PEER_NODE, revision)])


async def start_task(client, *, consent, manifest, key, mode="fast",
                     query="retrieval target", operation="corpus.retrieve"):
    intent = await create_intent(
        client, spec=task_spec(scope="federation_public", mode=mode, query=query,
                               scope_ref="scope-1", operation=operation),
        consent=consent, manifest=manifest, key=key)
    plan = await plan_task(client, intent["root_task_id"])
    return intent["root_task_id"], plan


async def plan_event(client, root, *, headers=None):
    response = await client.get(f"/api/v1/tasks/{root}/events", headers=headers)
    events = response.json()["events"]
    return next(event for event in events if event["type"] == "plan_ready")


async def evidence_probe_rows(session, *, collection="peer-collection-1",
                              node=PEER_NODE):
    return list(await session.scalars(select(FederationProbe).where(
        FederationProbe.organization_id == ORG,
        FederationProbe.target_node_id == node,
        FederationProbe.probe_kind == "evidence_retrieval",
        FederationProbe.collection_id == collection)))


async def negative_rows(session):
    return list(await session.scalars(select(FederationCacheEntry).where(
        FederationCacheEntry.kind == cache.NEGATIVE_KIND
    ).execution_options(populate_existing=True)))


# ------------------------------------------------- 目录摘要排序（评测发现 2）

async def test_fast_selection_uses_peer_catalog_descriptors(actor_client, session, monkeypatch):
    """描述符必须真的进 `routing.candidates`，且 fast 按摘要选人。

    9 个目标、上限 8：身份顺序会选 col-1..col-8；摘要（col-9 主题命中）必须
    把 col-9 排进来。执行阶段还要证明选出来的是**同一个集合** —— 计划是选择
    的权威，执行不再重排（否则 retrieve 步骤与目标会错位挂回执）。
    """
    collections = [f"col-{index}" for index in range(1, 10)]
    peer = StubPeer(collections=[
        peer_descriptor(collection_id,
                        topics=("power",) if collection_id == "col-9" else ())
        for collection_id in collections])
    install_peer(monkeypatch, peer)
    seen: list[list[str]] = []
    real_candidates = federation_tasks.routing.candidates

    def spy(targets, descriptors, **kwargs):
        seen.append([item.get("collection_id") for item in descriptors])
        return real_candidates(targets, descriptors, **kwargs)

    monkeypatch.setattr(federation_tasks.routing, "candidates", spy)
    root, plan = await start_task(
        actor_client, consent=peer_consent(), manifest=manifest_for(collections),
        key="descriptor-rank", query="power adapter")
    assert seen, "routing.candidates 必须收到从对等目录读回来的描述符"
    assert set(seen[0]) == set(collections)
    chosen = {step["fixed_inputs"][1].removeprefix("collection:")
              for step in plan["steps"] if step["operation"] == "retrieve"}
    assert chosen == {"col-9", *[f"col-{index}" for index in range(1, 8)]}, \
        "fast 必须按摘要把 col-9 排进候选并保留 8 个上限"
    assert calls_to(peer, "/probes") == 8, "排序只换人，不能多探目标"

    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"],
                                "descriptor-rank-exec")).json()
    assert status["status"] == "succeeded"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    states = {entry["target_key"]["collection_id"]: entry
              for entry in coverage["entries"]}
    assert states["col-8"]["state"] == "not_attempted"
    assert states["col-8"]["last_error"] == "search_mode_fast"
    assert states["col-9"]["state"] == "succeeded", \
        "执行必须跑计划选中的 col-9，而不是重排后的身份顺序"


# ------------------------------------------------------------- 探测复用

async def test_create_plan_reuses_a_valid_probe_without_touching_the_peer(
        actor_client, session, monkeypatch):
    """同 query/同索引修订/同调用者的回执直接复用：零探测请求、账本带缓存标记。"""
    peer = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent()
    first_root, first_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="reuse-1")
    probes_after_first = calls_to(peer, "/probes")
    assert probes_after_first == 1
    first_step = next(step for step in first_plan["steps"]
                      if step["operation"] == "retrieve")
    assert first_step["probe_refs"]

    second_root, second_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="reuse-2")
    assert calls_to(peer, "/probes") == probes_after_first, \
        "复用命中时一个探测请求都不许发"
    second_step = next(step for step in second_plan["steps"]
                       if step["operation"] == "retrieve")
    assert second_step["probe_refs"] == first_step["probe_refs"], \
        "计划必须引用被复用的那一行回执"
    assert len(await evidence_probe_rows(session)) == 1, \
        "复用不是新观测：不得再落一行冒充本次探测"
    ready = await plan_event(actor_client, second_root)
    assert ready["payload"]["reused_probes"], "事件流必须如实暴露复用"

    # 执行阶段：账本把这一行标成缓存回执，而不是把它读成新探测。
    await approve_task(actor_client, second_root, second_plan,
                       recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, second_root, second_plan["plan_digest"],
                                "reuse-2-exec")).json()
    assert status["status"] == "succeeded"
    coverage = (await actor_client.get(f"/api/v1/tasks/{second_root}/coverage")).json()
    entry = coverage["entries"][0]
    assert entry["state"] == "succeeded"
    assert entry["search_profile"] == federation_tasks.CACHED_PROBE_PROFILE
    assert entry["probe_receipts"], "成功条目仍然要有回执引用（复用的那一行）"


async def test_reuse_is_rejected_when_the_index_revision_moved(actor_client, session,
                                                               monkeypatch):
    """集合索引修订前进后，旧回执不许再被当成当前检索（变异确认的靶子）。"""
    peer = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent()
    _, first_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="rev-1")
    assert calls_to(peer, "/probes") == 1

    peer.index_revision = "peer-index-2"
    peer.collections = [peer_descriptor("peer-collection-1", index_revision="peer-index-2")]
    second_root, second_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="rev-2")
    assert calls_to(peer, "/probes") == 2, "新索引修订必须重新探测"
    ready = await plan_event(actor_client, second_root)
    assert ready["payload"]["reused_probes"] == {}
    second_step = next(step for step in second_plan["steps"]
                       if step["operation"] == "retrieve")
    first_step = next(step for step in first_plan["steps"]
                      if step["operation"] == "retrieve")
    assert second_step["probe_refs"] != first_step["probe_refs"]
    assert len(await evidence_probe_rows(session)) == 2


async def test_reuse_is_rejected_for_another_query_or_an_expired_receipt(
        actor_client, session, monkeypatch):
    """query digest 不同或回执过期：都退回真实探测，不拿旧回执充数。"""
    peer = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent()
    _, first_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="digest-1",
        query="retrieval target")
    assert calls_to(peer, "/probes") == 1

    await start_task(actor_client, consent=consent, manifest=manifest, key="digest-2",
                     query="a different question")
    assert calls_to(peer, "/probes") == 2, "问题不同不许复用"
    assert len(await evidence_probe_rows(session)) == 2

    for row in await evidence_probe_rows(session):
        row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    third_root, _ = await start_task(actor_client, consent=consent, manifest=manifest,
                                     key="digest-3", query="retrieval target")
    assert calls_to(peer, "/probes") == 3, "过期回执不许复用"
    ready = await plan_event(actor_client, third_root)
    assert ready["payload"]["reused_probes"] == {}


async def test_reuse_never_crosses_the_calling_actor(actor_client, session, monkeypatch):
    """同组织、不同调用者：回执不跨调用者复用（租户内也有可见性边界）。"""
    peer = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent()
    alice_root, _ = await start_task(
        actor_client, consent=consent, manifest=manifest, key="actor-1")
    assert calls_to(peer, "/probes") == 1

    bob = actor_headers("bob")
    body = {"task_spec": task_spec(scope="federation_public", mode="fast",
                                   query="retrieval target", scope_ref="scope-1",
                                   operation="corpus.retrieve"),
            "exploration_consent": consent, "scope_manifest": manifest}
    created = await actor_client.post("/api/v1/task-intents", json=body,
                                      headers={**bob, "Idempotency-Key": "actor-bob"})
    assert created.status_code == 201, created.text
    bob_root = created.json()["root_task_id"]
    planned = await actor_client.post("/api/v1/task-plans",
                                      json={"root_task_id": bob_root}, headers=bob)
    assert planned.status_code == 200, planned.text
    assert calls_to(peer, "/probes") == 2, "别人的回执不许端进 bob 的任务"
    assert (await plan_event(actor_client, bob_root,
                             headers=bob))["payload"]["reused_probes"] == {}
    assert len(await evidence_probe_rows(session)) == 2
    assert alice_root != bob_root


# ------------------------------------------------------------- 负面缓存

async def test_negative_hit_skips_the_peer_and_does_not_extend_its_life(
        actor_client, session, monkeypatch):
    """不可达观测：第二次规划零 HTTP 命中，且不续命、不占探测预算。"""
    peer = StubPeer(fail="probe")
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent(payloads=("query_text",))
    first_root, _ = await start_task(
        actor_client, consent=consent, manifest=manifest, key="negative-1")
    assert calls_to(peer, "/probes") == 1
    rows = await negative_rows(session)
    assert len(rows) == 1 and rows[0].kind == cache.NEGATIVE_KIND
    assert rows[0].value_json["reason"].startswith("unreachable:")
    expires_at = as_aware(rows[0].expires_at)
    hits = rows[0].hits

    second_root, _ = await start_task(
        actor_client, consent=consent, manifest=manifest, key="negative-2")
    assert calls_to(peer, "/probes") == 1, "负面命中不许再联系对端"
    assert calls_to(peer, "/published-collections") == 0, \
        "这个许可没有 collection_filters，目录也不许读"
    ready = await plan_event(actor_client, second_root)
    remote_key = next(key for key in ready["payload"]["outcomes"]
                      if key.startswith(PEER_NODE + "/"))
    assert ready["payload"]["outcomes"][remote_key] == "unreachable"

    rows = await negative_rows(session)
    assert len(rows) == 1
    assert as_aware(rows[0].expires_at) == expires_at, \
        "命中负面条目不许把它的 TTL 续长"
    assert rows[0].hits == hits + 1, "命中的使用计数照记（驱逐依据）"
    assert first_root != second_root


async def test_a_new_registry_revision_is_never_blocked_by_a_negative_entry(
        actor_client, session, monkeypatch):
    """节点修订前进（新上传/重新登记）产生新键：旧否定条目结构上挡不住新内容。"""
    peer = StubPeer(fail="probe")
    install_peer(monkeypatch, peer)
    consent = peer_consent(payloads=("query_text",))
    await start_task(actor_client, consent=consent,
                     manifest=manifest_for(["peer-collection-1"], revision=1),
                     key="revision-negative-1")
    assert calls_to(peer, "/probes") == 1

    # 同一个（仍然是坏的）节点，但目录修订前进到 2：新键，必须重新接触。
    await start_task(actor_client, consent=consent,
                     manifest=manifest_for(["peer-collection-1"], revision=2),
                     key="revision-negative-2")
    assert calls_to(peer, "/probes") == 2, "新修订不许被旧否定条目挡住"
    assert len(await negative_rows(session)) == 2

    # 短 TTL 过期后，即便修订没变也必须重新接触。
    rows = await negative_rows(session)
    for row in rows:
        row.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()
    await start_task(actor_client, consent=consent,
                     manifest=manifest_for(["peer-collection-1"], revision=2),
                     key="revision-negative-3")
    assert calls_to(peer, "/probes") == 3, "过期的负面条目不许继续挡路"


async def test_denied_probe_is_cached_as_denied_not_failed(actor_client, monkeypatch):
    """对端 403：负面条目记 denied；命中时目标是 denied，且零 HTTP。"""
    peer = StubPeer()
    peer.deny_probes = True
    install_peer(monkeypatch, peer)
    manifest = manifest_for(["peer-collection-1"])
    consent = peer_consent(payloads=("query_text",))
    await start_task(actor_client, consent=consent, manifest=manifest, key="denied-1")
    assert calls_to(peer, "/probes") == 1

    second_root, _ = await start_task(actor_client, consent=consent,
                                      manifest=manifest, key="denied-2")
    assert calls_to(peer, "/probes") == 1, "denied 观测命中后同样不发请求"
    ready = await plan_event(actor_client, second_root)
    remote_key = next(key for key in ready["payload"]["outcomes"]
                      if key.startswith(PEER_NODE + "/"))
    assert ready["payload"]["outcomes"][remote_key] == "denied"


# ------------------------------------------- 目录读的许可门与发现预算

async def test_remote_catalog_fetch_needs_the_collection_filters_payload(
        actor_client, monkeypatch):
    """元数据外发与 Probe 同级审查：载荷没批准就零目录请求。"""
    peer = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    root, plan = await start_task(
        actor_client, consent=peer_consent(payloads=("query_text",)),
        manifest=manifest_for(["peer-collection-1"]), key="no-filters")
    assert calls_to(peer, "/published-collections") == 0
    ready = await plan_event(actor_client, root)
    assert ready["payload"]["descriptor_sources"]["remote"][PEER_NODE] \
        == "payload_not_allowed"
    assert plan["steps"], "没有摘要只是排序退化，不许阻断规划"


async def test_remote_catalog_fetch_respects_the_discovery_budget(actor_client,
                                                                  monkeypatch):
    """发现预算为 0 零请求；为 1 时只允许第一页（排序仍可用第一页摘要）。"""
    zero = StubPeer(collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, zero)
    root_zero, _ = await start_task(
        actor_client, consent=peer_consent(discovery=0),
        manifest=manifest_for(["peer-collection-1"]), key="discovery-zero")
    assert calls_to(zero, "/published-collections") == 0
    ready_zero = await plan_event(actor_client, root_zero)
    assert ready_zero["payload"]["descriptor_sources"]["remote"][PEER_NODE] \
        == "budget_exhausted"

    collections = [f"col-{index}" for index in range(1, 10)]
    limited = StubPeer(collections=[
        peer_descriptor(collection_id,
                        topics=("power",) if collection_id == "col-9" else ())
        for collection_id in collections])
    install_peer(monkeypatch, limited)
    root_one, plan_one = await start_task(
        actor_client, consent=peer_consent(discovery=1),
        manifest=manifest_for(collections), key="discovery-one", query="power adapter")
    assert calls_to(limited, "/published-collections") == 1, \
        "发现预算 1 只允许一页目录请求"
    chosen = {step["fixed_inputs"][1].removeprefix("collection:")
              for step in plan_one["steps"] if step["operation"] == "retrieve"}
    assert "col-9" in chosen, "第一页拿到的摘要必须被用于排序（终止页证明不是前置条件）"


async def test_remote_catalog_failure_degrades_instead_of_blocking_planning(
        actor_client, monkeypatch):
    """对端目录读失败：该节点没有摘要，排序退化，规划照常完成。"""
    peer = StubPeer(fail="catalog", collections=[peer_descriptor("peer-collection-1")])
    install_peer(monkeypatch, peer)
    root, plan = await start_task(
        actor_client, consent=peer_consent(),
        manifest=manifest_for(["peer-collection-1"]), key="catalog-down")
    assert calls_to(peer, "/published-collections") == 1
    assert calls_to(peer, "/probes") == 1, "目录失败不影响探测"
    ready = await plan_event(actor_client, root)
    assert ready["payload"]["descriptor_sources"]["remote"][PEER_NODE] \
        == "peer_unavailable"


# ------------------------------------------------- 撤回后的复用边界（本地）

async def test_withdrawn_collection_is_never_reused(actor_client, session, monkeypatch):
    """本地集合撤回后没有描述符：旧探测回执不许再被复用，必须重新走授权探测。"""
    _, version, _, _, _ = await indexed_source(session)
    collection = await publish_collection(actor_client, version, key="reuse-withdraw")
    manifest = scope_manifest([member(collection["collection_id"])],
                              revisions=[(NODE, 1)])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    _, first_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="local-reuse-1")
    assert len(await evidence_probe_rows(session, collection=collection["collection_id"],
                                         node=NODE)) == 1
    second_root, second_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="local-reuse-2")
    assert (await plan_event(actor_client, second_root))["payload"]["reused_probes"], \
        "撤回前同一集合的回执应当可复用（否则这条用例量不到撤回的差别）"
    first_step = next(step for step in first_plan["steps"] if step["operation"] == "retrieve")
    second_step = next(step for step in second_plan["steps"] if step["operation"] == "retrieve")
    assert second_step["probe_refs"] == first_step["probe_refs"]

    withdrawn = await actor_client.post(
        f"/api/v1/collections/{collection['collection_id']}/withdraw",
        headers={**actor_headers(), "Idempotency-Key": "withdraw-reuse"},
        json={"expected_revision": collection["revision"]})
    assert withdrawn.status_code == 200, withdrawn.text

    third_root, third_plan = await start_task(
        actor_client, consent=consent, manifest=manifest, key="local-reuse-3")
    ready = await plan_event(actor_client, third_root)
    assert ready["payload"]["reused_probes"] == {}, \
        "撤回的集合不许命中任何缓存回执"
    target_key = next(iter(ready["payload"]["outcomes"]))
    assert ready["payload"]["outcomes"][target_key] == "failed"
    third_step = next(step for step in third_plan["steps"]
                      if step["operation"] == "retrieve")
    assert third_step["probe_refs"] == [], \
        "重新探测失败就不该有回执引用，更不许指向撤回前的旧回执"
    assert len(await evidence_probe_rows(session, collection=collection["collection_id"],
                                         node=NODE)) == 1, \
        "失败的重新探测不会伪造一行回执"
    assert first_plan["plan_digest"] != third_plan["plan_digest"]
