"""Explicit fast continuations: the next bounded batch, answered within the root allowance.

A continuation stages a new plan revision whose step ids are renamed (`r2-…`) and whose
retrieve steps are re-indexed. Plan-step allowances (hops, generation tokens) are reserved
once per logical step; keyed by the renamed ids, the continuation's answer reserved the
root's whole token cap a second time and always ended as `root_budget_exhausted`, so a
continuation could fetch the missing evidence but never answer with it (F14).

Each approved continuation adds the next batch of up to `FAST_CANDIDATE_LIMIT` unselected
targets in ranked order (plan §7.2 "继续下一批"), not a single target: with local-first
ordering a one-target step spent every round on the remaining local look-alikes before
reaching a remote fact holder.
"""
import json

import respx

from ddp_corpus import federation_tasks
from ddp_core.application.plans import content_digest
from ddp_corpus.main import app as corpus_app
from conftest import drain_tasks
from test_federation_answer import _answer_config, chat_answer, gateway_channel, mock_gateway  # noqa: F401 (autouse fixture)
from test_federation_tasks import (
    NODE,
    PEER_NODE,
    StubPeer,
    approve_task,
    create_intent,
    exploration,
    install_peer,
    member,
    peer_evidence,
    plan_task,
    scope_manifest,
    submit_task,
    task_spec,
)

FIRST = "alpha fact held by the first collection"
SECOND = "beta fact held by the second collection"


def cite_everything(request):
    """A grounded model: one claim per excerpt it was actually given."""
    evidence = json.loads(json.loads(request.content)["messages"][1]["content"])["evidence"]
    if not evidence:
        return chat_answer(json.dumps({"status": "insufficient_evidence"}))
    return chat_answer(json.dumps({"status": "answered", "claims": [
        {"text": item["text"], "evidence_ids": [item["evidence_id"]]} for item in evidence]}))


def evidence_item(evidence_id, excerpt):
    """A distinct passage: its excerpt digest matches its own text."""
    return {**peer_evidence(), "evidence_id": evidence_id, "excerpt": excerpt,
            "excerpt_digest": content_digest(excerpt.encode())}


@respx.mock
async def test_fast_continuation_answers_with_the_new_evidence(actor_client, monkeypatch):
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
    first = (await submit_task(actor_client, root, first_plan["plan_digest"], "fast-answer")).json()
    assert first["result"]["answer_reason"] is None, first["result"]
    assert FIRST in first["result"]["answer"] and SECOND not in first["result"]["answer"]

    peer.items = [evidence_item("peer-evidence-1", FIRST), evidence_item("peer-evidence-2", SECOND)]
    staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert staged.status_code == 202, staged.text
    await drain_tasks(corpus_app.state)
    second_plan = (await actor_client.get(f"/api/v1/task-plans/{root}")).json()
    assert second_plan["revision"] == 2
    assert any(step["step_id"].startswith("r2-") and step["operation"] == "answer"
               for step in second_plan["steps"])
    await approve_task(actor_client, root, second_plan, recipients=(NODE, PEER_NODE))
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    final = (await actor_client.get(f"/api/v1/tasks/{root}")).json()

    assert final["result"]["answer_reason"] is None, final["result"]
    assert SECOND in final["result"]["answer"], "the continuation's evidence reaches the answer"
    assert final["used_budget"]["generation_tokens"] == first["used_budget"]["generation_tokens"], \
        "the logical answer step keeps its single reservation across revisions"
    assert final["used_budget"]["hops"] <= second_plan["budget"]["max_hops"]
    assert final["used_budget"]["requests"] > first["used_budget"]["requests"], \
        "physical requests of the continuation are still paid"


async def test_fast_continuation_adds_the_next_bounded_batch(actor_client, monkeypatch):
    monkeypatch.setattr(federation_tasks, "FAST_CANDIDATE_LIMIT", 2)
    peer = StubPeer(items=[peer_evidence()])
    install_peer(monkeypatch, peer)
    collections = [f"peer-collection-{index}" for index in range(1, 6)]
    manifest = scope_manifest([member(collection, PEER_NODE) for collection in collections])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="fast", scope_ref="scope-1",
                                     operation="corpus.retrieve"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]

    def retrieved(plan):
        return [step["fixed_inputs"][1] for step in plan["steps"] if step["operation"] == "retrieve"]

    plan = await plan_task(actor_client, root)
    first_batch = retrieved(plan)
    assert len(first_batch) == 2
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "fast-batch")).json()
    assert status["result"]["counts"]["succeeded"] == 2

    batches = [first_batch]
    probes = status["used_budget"]["probes"]
    for revision, expected in ((2, 4), (3, 5)):
        staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
        assert staged.status_code == 202, staged.text
        await drain_tasks(corpus_app.state)
        plan = (await actor_client.get(f"/api/v1/task-plans/{root}")).json()
        assert plan["revision"] == revision
        targets = retrieved(plan)
        assert len(targets) == expected, "a continuation adds the next batch, capped by the scope"
        assert targets[:len(batches[-1])] == batches[-1], "earlier targets keep their order"
        batches.append(targets)
        await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
        resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
        assert resumed.status_code == 202, resumed.text
        await drain_tasks(corpus_app.state)
        status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
        assert status["result"]["counts"]["succeeded"] == expected, status["result"]["counts"]
        assert status["used_budget"]["probes"] - probes == expected - len(batches[-2]), \
            "settled targets are not probed again; the exploration budget goes to the new batch"
        probes = status["used_budget"]["probes"]
    assert sorted(batches[-1]) == sorted("collection:" + item for item in collections)
    assert status["retrieval_completeness"] == "partial", "fast never claims exhaustive coverage"


async def test_continuation_stop_reason_keeps_settled_consent_denials(actor_client, monkeypatch):
    """Settled targets are not probed again, but a consent denial they recorded still
    explains why fast stopped: the next revision must not read as a plain candidate cut."""
    monkeypatch.setattr(federation_tasks, "FAST_CANDIDATE_LIMIT", 1)
    install_peer(monkeypatch, StubPeer(items=[peer_evidence()]))
    unlisted = "node-" + "a" * 48
    manifest = scope_manifest([member("unlisted-collection", unlisted)]
                              + [member(f"peer-collection-{index}", PEER_NODE)
                                 for index in range(1, 4)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="fast", scope_ref="scope-1",
                                     operation="corpus.retrieve"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE, unlisted))
    await submit_task(actor_client, root, plan["plan_digest"], "fast-denied")
    staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert staged.status_code == 202, staged.text
    await drain_tasks(corpus_app.state)

    events = (await actor_client.get(f"/api/v1/tasks/{root}/events?after=0")).json()["events"]
    ready = [event["payload"] for event in events if event["type"] == "plan_ready"]
    assert [payload["revision"] for payload in ready] == [1, 2]
    assert ready[0]["fast_stop"] == "budget_or_consent_gate"
    assert ready[1]["fast_stop"] == "budget_or_consent_gate", ready[1]
    assert "/".join((unlisted, "unlisted-collection", "corpus.retrieve")) not in ready[1]["outcomes"], \
        "the settled denial is not probed again"


async def test_continuation_gate_rotates_submit_key_for_fresh_approval(actor_client, monkeypatch,
                                                                      session):
    """分段续跑接受新一轮 approve-then-submit key：上一批的已消费 key 不得残留。

    Fast task finishes batch N with continuation open: the gate stages revision
    N+1 (status queued, execution consent cleared, new plan) and must rotate
    `row.idempotency_key` to None, so the mandatory fresh approve-then-submit
    accepts a new key. Before the fix, the consumed submit key stayed on the
    row: execute_task replayed the old status on the same key and 409'd any
    new key, stranding the task with no driver.
    """
    from ddp_corpus.federation_models import FederationRequest
    monkeypatch.setattr(federation_tasks, "FAST_CANDIDATE_LIMIT", 1)
    install_peer(monkeypatch, StubPeer(items=[peer_evidence()]))
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="fast", scope_ref="scope-1",
                                     operation="corpus.retrieve"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, plan["plan_digest"], "gate-first-key")).json()
    assert first["status"] == "succeeded", first

    staged = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert staged.status_code == 202, staged.text
    await drain_tasks(corpus_app.state)
    session.expunge_all()
    row = await session.get(FederationRequest, root, populate_existing=True)
    assert row.status == "queued", "staged revision waits for fresh approval, not auto-enqueued"
    assert row.idempotency_key is None, "gate must rotate the consumed submit key"
    assert row.execution_consent_json is None
    second_plan = (await actor_client.get(f"/api/v1/task-plans/{root}")).json()
    assert second_plan["revision"] == 2
    await approve_task(actor_client, root, second_plan, recipients=(NODE, PEER_NODE))
    resumed = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "gate-fresh-key"},
        json={"root_task_id": root, "plan_digest": second_plan["plan_digest"]})
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    final = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert final["status"] == "succeeded", final
    assert final["result"]["counts"]["succeeded"] == 2, final["result"]["counts"]
