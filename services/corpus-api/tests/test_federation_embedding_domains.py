"""Real peer HTTP: embedding coordinates and raw scores stay node-local.

The seam is the approved task HTTP flow and B's evidence-set HTTP responses.
The opt-in index performs actual cosine retrieval over dimension-matched seeds;
no lexical term in the query occurs in the source excerpts.
"""
import json

from ddp_corpus import node_identity
from ddp_corpus.config import settings
from federation_two_node import (
    NODE_A, NODE_B, EmbeddingDomain, ModelStub, TwoNodeFixture, VectorDomainIndex,
)
from test_federation_two_node import (
    approve_task, member, plan_manifest, publish_a, submit_task,
)

QUERY = "semanticneedle"
A_TEXTS = ("amber lesser finding", "amber stronger finding")
B_TEXTS = ("violet lesser finding", "violet stronger finding")
A_DOMAIN = EmbeddingDomain("amber-encoder-3", 3, (0.6, 0.8), 1.0)
B_DOMAIN = EmbeddingDomain("violet-encoder-7", 7, (0.95, 0.999), 1000.0)


def assert_no_raw_scores(value):
    if isinstance(value, dict):
        assert not {"_score", "_similarity", "score", "similarity"}.intersection(value), value
        for child in value.values():
            assert_no_raw_scores(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_raw_scores(child)


async def test_each_node_encodes_locally_and_fusion_preserves_node_local_ranks(
        actor_client, session, app_state, tmp_path, monkeypatch):
    fixture = await TwoNodeFixture.create(
        tmp_path, b_texts=B_TEXTS, b_embedding_domain=B_DOMAIN)
    gateway_a = ModelStub((), embedding_domain=A_DOMAIN)
    endpoint_a = gateway_a.start()
    index_a = VectorDomainIndex(A_DOMAIN)
    try:
        monkeypatch.setattr(settings, "bundle_node_id", NODE_A)
        node_identity.reset()
        node_identity.bind_static_for_tests(NODE_A)
        monkeypatch.setattr(settings, "federation_admissions_enabled", True)
        monkeypatch.setattr(settings, "federation_peers", fixture.peers_json(include_c=False))
        monkeypatch.setattr(settings, "federation_allow_loopback", True)
        monkeypatch.setattr(settings, "embedding_model", A_DOMAIN.model)
        monkeypatch.setattr(settings, "embedding_url", endpoint_a + "/v1/embeddings")
        monkeypatch.setattr(settings, "service_url", endpoint_a)
        monkeypatch.setattr(app_state, "search_index", index_a)
        fixture.install_counting_transport(monkeypatch)
        _, evidence_a, collection_a = await publish_a(
            actor_client, session, texts=A_TEXTS, key="embedding-domain-a")
        root, plan = await plan_manifest(
            actor_client,
            members=[member(fixture.b_seed.collection_id, NODE_B),
                     member(collection_a["collection_id"], NODE_A)], query=QUERY)
        await approve_task(actor_client, root, plan)
        response = await submit_task(actor_client, root, plan["plan_digest"], "embedding-domains")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["status"] == "succeeded", status
        assert status["retrieval_completeness"] == "complete", status
        evidence = status["result"]["evidence"]
        assert [(item["origin_node_id"], item["locator"]["seq"]) for item in evidence] == [
            (NODE_A, 1), (NODE_A, 0), (NODE_B, 1), (NODE_B, 0)]
        assert [item["evidence_id"] for item in evidence[:2]] == [
            evidence_a[1].id, evidence_a[0].id]
        assert evidence[-1]["evidence_id"] == fixture.b_seed.evidence_id
        assert_no_raw_scores(status["result"])
        wire_sets = [call["body"] for call in fixture.outbound
                     if "/evidence-sets/" in call["path"]]
        assert wire_sets, "real B evidence-set HTTP responses were not observed"
        for body in wire_sets:
            assert_no_raw_scores(body)
            assert [item["locator"]["seq"] for item in body["items"]] == [1, 0]
        b_observations = fixture.vector_observations()
        for gateway, domain, observations in (
                (gateway_a, A_DOMAIN, index_a.observations),
                (fixture.model_stub, B_DOMAIN, b_observations)):
            requests = [call for call in gateway.requests if call["path"] == "/v1/embeddings"]
            assert requests, "node never called its local encoder over HTTP"
            assert all(call["payload"] == {"input": [QUERY], "model": domain.model}
                       for call in requests)
            assert observations, "vector index was not exercised"
            assert all(row["query_dimension"] == domain.dimension
                       and row["seed_dimensions"] == [domain.dimension, domain.dimension]
                       and row["ranked_seqs"] == [1, 0] for row in observations)
        # Both score and cosine scales put *every* B hit above *every* A hit.
        # A still precedes B; within a node the true vector rank is retained.
        assert min(b_observations[-1]["scores"]) > max(index_a.observations[-1]["scores"])
        assert min(b_observations[-1]["similarities"]) > max(index_a.observations[-1]["similarities"])
        artifact = {
            "scenario": "node_local_embedding_domains", "status": status["status"],
            "models": [{"model": domain.model, "dimension": domain.dimension}
                       for domain in (A_DOMAIN, B_DOMAIN)],
            "local_vector_observations": {"A": index_a.observations, "B": b_observations},
            "gateway_embedding_requests": {
                "A": [row for row in gateway_a.requests if row["path"] == "/v1/embeddings"],
                "B": [row for row in fixture.model_stub.requests if row["path"] == "/v1/embeddings"]},
            "task_evidence_order": [["A" if item["origin_node_id"] == NODE_A else "B",
                                     item["locator"]["seq"]] for item in evidence],
            "peer_requests": [{key: value for key, value in call.items()
                               if key in ("method", "path", "status")} for call in fixture.calls()],
            "http_evidence_sets_score_free": True, "task_result_score_free": True,
            "limits": "A uses ASGI; B is real uvicorn TCP; explicit cosine test index, not pgvector/GPU",
        }
        (tmp_path / "embedding-domains-evidence.json").write_text(json.dumps(artifact, indent=2))
    finally:
        gateway_a.stop()
        await fixture.stop()
