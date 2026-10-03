"""Copies preserve provenance, but cannot manufacture independent support."""
import json

import pytest
from ddp_core.application import plans
from federation_two_node import ModelClaim, NODE_A, NODE_B
from test_federation_two_node import (
    B_TEXT, _node_a_federation_config, approve_task, member, plan_manifest,
    publish_a, submit_task, two_node,
)


@pytest.mark.parametrize("two_node", [(ModelClaim("A supported fact", (B_TEXT,)),)], indirect=True)
async def test_duplicate_content_has_one_support_unit_and_one_generation_excerpt(
        actor_client, session, two_node):  # noqa: F811 — imported fixture
    version, evidence_a, collection = await publish_a(
        actor_client, session, texts=(B_TEXT,), key="support-copy-a")
    root, plan = await plan_manifest(
        actor_client, members=[member(collection["collection_id"], NODE_A),
                               member(two_node.b_seed.collection_id, NODE_B)],
        query="federation keyword")
    await approve_task(actor_client, root, plan)
    response = await submit_task(actor_client, root, plan["plan_digest"], "support-copies")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["support_counts"] == {"independent_sources": 1, "evidence_copies": 2}
    group, = result["support_groups"]
    expected_a = {"origin_node_id": NODE_A, "resource_id": version.resource_id,
                  "source_version_id": version.id, "evidence_id": evidence_a[0].id}
    expected_b = {"origin_node_id": NODE_B, "resource_id": two_node.b_seed.resource_id,
                  "source_version_id": two_node.b_seed.version_id,
                  "evidence_id": two_node.b_seed.evidence_id}
    assert group["copies"] == [expected_a, expected_b]
    assert group["representatives"] == [expected_a]
    assert [item["evidence_id"] for item in result["evidence"]] == [
        evidence_a[0].id, two_node.b_seed.evidence_id]
    binding, = result["claim_evidence_bindings"]
    assert binding["evidence_refs"] == [evidence_a[0].id]
    assert binding["support_refs"] == [group["support_id"]]
    chat, = [call for call in two_node.model_stub.requests
             if call["path"] == "/v1/chat/completions"]
    supplied = json.loads(chat["payload"]["messages"][1]["content"])
    assert supplied["evidence"] == [{"evidence_id": evidence_a[0].id, "text": B_TEXT}]
    # Delivery commits to both retained copies and the support accounting.
    document = {key: value for key, value in result.items() if key != "result_manifest_digest"}
    assert result["result_manifest_digest"] == plans.digest(document)
