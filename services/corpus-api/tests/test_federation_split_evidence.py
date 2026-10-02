"""T34: complementary evidence from real federation nodes, not duplicate attribution."""
import json

import pytest
import respx

from ddp_corpus.config import settings
from federation_two_node import NODE_A, NODE_B, TwoNodeFixture
from test_federation_answer import (
    _answer_config,
    gateway_channel,
    grounded_reply,
    mock_chat,
    mock_gateway,
)
from test_federation_two_node import (
    approve_task as two_node_approve,
    coverage_of as two_node_coverage,
    create_intent as two_node_create,
    entry_for as two_node_entry,
    exploration as two_node_exploration,
    member as two_node_member,
    plan_task as two_node_plan,
    publish_a as two_node_publish_a,
    scope_manifest as two_node_scope_manifest,
    submit_task as two_node_submit,
    task_spec as two_node_task_spec,
)


@pytest.fixture
async def two_node(tmp_path):
    fixture = await TwoNodeFixture.create(
        tmp_path, b_texts=("ESP32 has 34 programmable GPIOs",))
    try:
        yield fixture
    finally:
        await fixture.stop()


@pytest.mark.parametrize("b_unreachable", [False, True], ids=["both-origins", "b-unreachable"])
@respx.mock
async def test_distinct_two_node_facts_feed_answer_and_bindings(
        actor_client, session, two_node, monkeypatch, b_unreachable):
    """T34: a two-part answer needs distinct facts from A and real subprocess B."""
    from ddp_corpus import node_identity

    fact_a = "Raspberry Pi Pico exposes 26 multi-function GPIO pins"
    fact_b = "ESP32 has 34 programmable GPIOs"
    question = "How many GPIO pins do Raspberry Pi Pico and ESP32 each expose?"
    monkeypatch.setattr(settings, "bundle_node_id", NODE_A)
    node_identity.reset()
    node_identity.bind_static_for_tests(NODE_A)
    monkeypatch.setattr(settings, "federation_admissions_enabled", True)
    monkeypatch.setattr(settings, "federation_peers", two_node.peers_json())
    monkeypatch.setattr(settings, "federation_allow_loopback", True)
    two_node.install_counting_transport(monkeypatch)

    respx.route(url__startswith=two_node.b_endpoint).pass_through()
    mock_gateway(channels=[gateway_channel()])
    # The model must refuse absent excerpts, never invent the other node's fact.
    facts = (fact_a,) if b_unreachable else (fact_a, fact_b)
    chat = mock_chat(grounded_reply(
        *((fact, (ordinal,)) for ordinal, fact in enumerate(facts, 1)), originals=facts))
    _version_a, evidence_a, collection_a = await two_node_publish_a(
        actor_client, session, texts=(fact_a,), key="distinct-facts-a")
    b = two_node.b_seed
    manifest = two_node_scope_manifest([
        two_node_member(collection_a["collection_id"], NODE_A),
        two_node_member(b.collection_id, NODE_B),
    ])
    intent = await two_node_create(
        actor_client, spec=two_node_task_spec(query=question),
        consent=two_node_exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan = await two_node_plan(actor_client, root)
    await two_node_approve(actor_client, root, plan)
    if b_unreachable:
        # B was genuinely reachable during planning; kill only this fixture's
        # subprocess before retrieval to exercise real connection refusal.
        await two_node.stop()
    response = await two_node_submit(actor_client, root, plan["plan_digest"], "distinct-facts")
    assert response.status_code == 200, response.text
    status = response.json()
    result = status["result"]

    assert status["status"] == "succeeded"
    assert chat.call_count == 1, "the fused excerpts must reach the model boundary"
    prompt = json.loads(json.loads(chat.calls[0].request.content)["messages"][1]["content"])
    assert prompt["question"] == question
    prompt_texts = {item["text"] for item in prompt["evidence"]}
    assert prompt_texts == set(facts), "generation needs every retrieved distinct fact"
    assert result["validation_state"] == "passed", result
    assert result["answer_reason"] is None
    bindings = result["claim_evidence_bindings"]
    assert [binding["claim_text"] for binding in bindings] == list(facts)
    by_id = {item["evidence_id"]: item for item in result["evidence"]}
    expected_ids = {fact_a: evidence_a[0].id, fact_b: b.evidence_id}
    expected_origins = {fact_a: NODE_A, fact_b: NODE_B}
    for binding in bindings:
        fact = binding["claim_text"]
        assert binding["evidence_refs"] == [expected_ids[fact]]
        assert by_id[expected_ids[fact]]["origin_node_id"] == expected_origins[fact]
        assert fact in result["answer"]
    cited_origins = {
        by_id[evidence_id]["origin_node_id"]
        for binding in bindings for evidence_id in binding["evidence_refs"]
    }
    assert cited_origins == ({NODE_A} if b_unreachable else {NODE_A, NODE_B})

    coverage = await two_node_coverage(actor_client, root)
    completeness = "partial" if b_unreachable else "complete"
    assert status["retrieval_completeness"] == completeness
    assert result["retrieval_completeness"] == completeness
    assert coverage["retrieval_completeness"] == completeness
    assert coverage["counts"] == {
        "total_targets": 2, "applicable_targets": 2,
        "succeeded": 1 if b_unreachable else 2, "excluded": 0,
        "incomplete": 1 if b_unreachable else 0,
    }
    assert two_node_entry(coverage, NODE_A)["state"] == "succeeded"
    if b_unreachable:
        assert any(
            target["target_key"]["origin_node_id"] == NODE_B
            for target in result["unretrieved_targets"])
        assert two_node_entry(coverage, NODE_B)["state"] == "unreachable"
        assert fact_b not in result["answer"]
    else:
        assert two_node_entry(coverage, NODE_B)["state"] == "succeeded"
        assert result["unretrieved_targets"] == []
