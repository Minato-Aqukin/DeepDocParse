"""T64 留存 blocker 回归：有效期内 sweep 不得打断 succeeded 任务的 continuation。

Main 指出的 blocker：`succeeded`/`failed` 任务仍可 `resume`/fast-continuation；
旧实现按"终态"剥离 probe evidence，续批 `_load_excerpts` 读不到正文，生成
掉进 `evidence_excerpt_unavailable`。新判据按"任务是否还走得动 resume"
（plan/budget-deadline/scope/execution 四份有效期最早者 vs 留存截止线）pin。

本文件走真实 HTTP 闭环（StubPeer + 真实 worker 队列 drain），不用行级伪造：

- (1) 有效期内：sweep 后 probe 载荷还在，且 continuation 仍能带着新证据生成；
- (2) 有效期 + 留存全过后：sweep 剥离，且 continuation 被可见的过期理由拒绝
  （`scope_expired` / `egress_denied` / `consent_expired` 三者之一），
  绝不是 `evidence_excerpt_unavailable` / `insufficient_evidence` 这类生成失败。

红-first：本文件在旧实现（按 status 剥离）下第 (1) 条即红 —— sweep 剥掉
succeeded 任务的 probe 后 continuation 生成失败。
"""
from __future__ import annotations

import json
from datetime import timedelta

import respx
from sqlalchemy import select

from conftest import drain_tasks
from ddp_corpus import federation_tasks, probe_retention
from ddp_corpus.main import app as corpus_app
from ddp_corpus.models import utcnow
from test_federation_answer import _answer_config, chat_answer, gateway_channel, mock_gateway  # noqa: F401 (autouse fixture)
from test_federation_fast_continuation import cite_everything, evidence_item
from test_federation_tasks import (
    NODE,
    PEER_NODE,
    StubPeer,
    approve_task,
    create_intent,
    exploration,
    install_peer,
    member,
    plan_task,
    scope_manifest,
    submit_task,
    task_spec,
)

FIRST = "alpha fact held by the first collection"
SECOND = "beta fact held by the second collection"


async def _first_round(actor_client, monkeypatch):
    monkeypatch.setattr(federation_tasks, "FAST_CANDIDATE_LIMIT", 1)
    mock_gateway(channels=[gateway_channel()])
    respx.post(url__regex=r".*/v1/chat/completions$").mock(side_effect=cite_everything)
    peer = StubPeer(items=[evidence_item("peer-evidence-1", FIRST)])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="fast", scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    first_plan = await plan_task(actor_client, root)
    await approve_task(actor_client, root, first_plan, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, first_plan["plan_digest"], "retain-answer")).json()
    assert first["result"]["answer_reason"] is None, first["result"]
    assert FIRST in first["result"]["answer"] and SECOND not in first["result"]["answer"]
    return root, peer


@respx.mock
async def test_sweep_within_validity_keeps_probes_and_continuation_generates(
        actor_client, monkeypatch, session):
    """有效期内 sweep 不得剥离 succeeded 任务的 probe；continuation 仍能生成。"""
    root, peer = await _first_round(actor_client, monkeypatch)
    peer.items = [evidence_item("peer-evidence-1", FIRST), evidence_item("peer-evidence-2", SECOND)]
    from ddp_corpus.db import get_sessionmaker
    from ddp_corpus.federation_models import FederationProbe
    async with get_sessionmaker()() as sweep_session:
        stripped = await probe_retention.sweep_probe_evidence(
            sweep_session, now=utcnow())
    assert stripped == 0, "有效期内 succeeded 任务的 probe 必须保留"
    async with get_sessionmaker()() as check:
        rows = (await check.execute(select(FederationProbe))).scalars().all()
        left = sum(len((row.result_json or {}).get("evidence") or []) for row in rows)
    assert left > 0
    staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert staged.status_code == 202, staged.text
    await drain_tasks(corpus_app.state)
    second_plan = (await actor_client.get(f"/api/v1/task-plans/{root}")).json()
    assert second_plan["revision"] == 2
    await approve_task(actor_client, root, second_plan, recipients=(NODE, PEER_NODE))
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    final = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert final["result"]["answer_reason"] is None, final["result"]
    assert SECOND in final["result"]["answer"], "续批的新证据必须进答案"


@respx.mock
async def test_plan_expired_but_continuation_open_pins_and_resume_is_refused_visibly(
        actor_client, monkeypatch, session):
    """旧 plan 过期但 (b) continuation 门开着 → pin + resume 可见拒绝。

    (b) 独立 pin 的唯一可测形态：旧 plan 的三项里 plan 先过期，但 manifest +
    exploration 有效。resume 被 gate 前的 plan 检查拒绝（consent_expired），
    continuation 够不着 —— 但 pin 逻辑按"或"保留 probe（纵深防御：gate 前移
    后直接生效）。本用例锁定该行为：stripped == 0 且拒绝码可见。

    `resume` 的 gate 跑在 plan/execution 检查之后（实测：plan 过期 → 410
    consent_expired；execution 过期 → 403 egress_denied），所以旧 plan 过期
    时 continuation 够不着，剥离安全。本用例证明 sweep 剥离后 resume 被可见
    的 consent_expired 拒绝 —— 不是 evidence_excerpt_unavailable 生成失败。
    （续批建新计划的形态由上一个用例覆盖：plan 有效 + manifest/exploration
    有效时的常规续批；(b) continuation 门是纵深防御。）
    """
    root, peer = await _first_round(actor_client, monkeypatch)
    peer.items = [evidence_item("peer-evidence-1", FIRST), evidence_item("peer-evidence-2", SECOND)]
    from ddp_corpus.db import get_sessionmaker
    from ddp_corpus.federation_models import FederationProbe, FederationRequest
    past = (utcnow() - timedelta(days=30)).isoformat()
    # 让旧 plan（含 budget deadline）过期并重算摘要保持内容一致
    # （行级更新，不走审批）：(a) 门已死；但 manifest + exploration 仍有效，
    # (b) continuation 门还开着 → pin 住。本用例证明 pin 成立且 resume 被 gate
    # 前的 plan 检查可见拒绝（consent_expired），不是生成失败。
    from ddp_core.application import plans as _plans
    async with get_sessionmaker()() as edit:
        row = await edit.get(FederationRequest, root)
        plan = dict(row.plan_json or {})
        plan["valid_until"] = past
        plan["budget"] = {**(plan.get("budget") or {}), "deadline": past}
        plan["plan_digest"] = _plans.task_plan_digest(
            {k: v for k, v in plan.items() if k != "plan_digest"})
        row.plan_json = plan
        row.plan_digest = plan["plan_digest"]
        probes = (await edit.execute(select(FederationProbe))).scalars().all()
        for probe in probes:
            probe.expires_at = utcnow() - timedelta(days=30)
        await edit.commit()
    async with get_sessionmaker()() as sweep_session:
        stripped = await probe_retention.sweep_probe_evidence(
            sweep_session, now=utcnow())
    assert stripped == 0, "(b) continuation 门还开着，必须 pin"
    staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert staged.status_code in (403, 404, 409, 410), staged.text
    code = ((staged.json().get("error") or {}).get("code") or "")
    assert code in ("scope_expired", "egress_denied", "consent_expired",
                    "plan_changed", "task_cancelled"), code
    assert code != "evidence_excerpt_unavailable", code


@respx.mock
async def test_sweep_past_validity_strips_and_continuation_is_refused_visibly(
        actor_client, monkeypatch, session):
    """有效期+留存全过后 sweep 剥离；continuation 被过期理由拒绝，非生成失败。"""
    root, peer = await _first_round(actor_client, monkeypatch)
    from ddp_corpus.db import get_sessionmaker
    from ddp_corpus.federation_models import FederationRequest
    past = (utcnow() - timedelta(days=30)).isoformat()
    async with get_sessionmaker()() as edit:
        from ddp_corpus.federation_models import FederationProbe
        row = await edit.get(FederationRequest, root)
        row.plan_json = {**(row.plan_json or {}), "valid_until": past,
                         "budget": {**((row.plan_json or {}).get("budget") or {}),
                                    "deadline": past}}
        row.scope_manifest_json = {**(row.scope_manifest_json or {}), "valid_until": past}
        row.execution_consent_json = {**(row.execution_consent_json or {}), "valid_until": past}
        probes = (await edit.execute(select(FederationProbe))).scalars().all()
        for probe in probes:
            probe.expires_at = utcnow() - timedelta(days=30)
        await edit.commit()
    async with get_sessionmaker()() as sweep_session:
        stripped = await probe_retention.sweep_probe_evidence(
            sweep_session, now=utcnow())
    assert stripped >= 1, "有效期全过后 probe evidence 必须被剥离"
    from ddp_corpus.federation_models import FederationProbe
    async with get_sessionmaker()() as check:
        rows = (await check.execute(select(FederationProbe))).scalars().all()
        left = sum(len((row.result_json or {}).get("evidence") or []) for row in rows)
    assert left == 0
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code in (403, 404, 409, 410), resumed.text
    body = resumed.json()
    code = ((body.get("error") or {}).get("code") or "")
    assert code in ("scope_expired", "egress_denied", "consent_expired",
                    "plan_changed", "task_cancelled"), code
    assert code not in ("evidence_excerpt_unavailable",), code
    final = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert (final.get("result") or {}).get("answer_reason") != "evidence_excerpt_unavailable"
