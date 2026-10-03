"""F14: a fast continuation regenerates the answer within the root's generation allowance.

A continuation stages a new plan revision whose step ids are renamed (`r2-…`) and whose
retrieve steps are re-indexed. Plan-step allowances (hops, generation tokens) are reserved
once per logical step; keyed by the renamed ids, the continuation's answer reserved the
root's whole token cap a second time and always ended as `root_budget_exhausted`, so a
continuation could fetch the missing evidence but never answer with it.
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
