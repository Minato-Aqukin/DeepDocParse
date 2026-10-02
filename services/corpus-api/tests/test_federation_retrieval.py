"""Federation's shared retrieval seam must retain every requested facet."""
import pytest

from conftest import ACTOR, ORG
from ddp_core.search import MemoryIndex
from ddp_corpus import federation, upstream
from ddp_corpus.deps import Actor
from test_federation_probes import indexed_source
from test_search_ranking import add_document


@pytest.mark.parametrize("embedding_available", [False, True])
async def test_federated_retrieve_preserves_each_requested_facet(
        session, app_state, monkeypatch, embedding_available):
    _, version, job, document, evidence = await indexed_source(session, texts=(
        "supply voltage regulated supply voltage",
        "supply voltage low noise",
        "wireless protocol 802.11n",
        *("wireless background" for _ in range(8)),
        *("protocol background" for _ in range(8)),
    ))
    private = await add_document(session, {"private": "supply voltage wireless protocol"})

    async def embed(_http, texts):
        if not embedding_available:
            raise ConnectionError("embedding offline")
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(upstream, "embed_texts", embed)
    hits, degraded, truncated, contexts = await federation._retrieve(
        session, Actor(ACTOR, "user", ORG, "contributor"),
        query=("What supply voltage does the device use "
               "and which wireless protocol does it support?"),
        candidate_limit=2, version_ids=[version.id],
        http=app_state.http, index=MemoryIndex())

    returned = {hit["evidence_id"] for hit in hits}
    assert evidence[2].id in returned, "the wireless-protocol facet must survive the limit"
    assert returned & {evidence[0].id, evidence[1].id}
    assert len(hits) == 2
    assert truncated is True
    assert degraded == (None if embedding_available else "embedding_unavailable")
    assert set(contexts) == {job.id}
    assert all(hit["document_id"] == document.id and hit["document_id"] != private.id
               for hit in hits)


async def test_federated_overfetch_does_not_drop_a_facet_at_the_final_limit(session):
    question = "What is the supply voltage and what is the wireless protocol?"
    routes = {
        question: ["overview"],
        "is the supply voltage": ["power"],
        "is the wireless protocol": ["radio"],
    }

    class FacetIndex:
        async def search(self, _session, *, query, **_kwargs):
            return [{"chunk_id": cid, "score": 0.03, "similarity": 0.8}
                    for cid in routes[query]]

    hits, degraded, truncated, _ = await federation._retrieve(
        session, Actor(ACTOR, "user", ORG, "contributor"),
        query=question, candidate_limit=2, contexts={"job": []}, document_ids=["doc"],
        index=FacetIndex())

    assert [hit["chunk_id"] for hit in hits] == ["power", "radio"]
    assert truncated is True
    assert degraded == "embedding_unavailable"
