import json

import httpx
import respx
from sqlalchemy import delete, select

from app.models import (
    Chunk, Citation, ExtractionItem, ExtractionRun, GraphEdge, KnowledgeEntity,
    KnowledgeReview, WikiEntry, WikiSection, WikiSentence,
)
from tests.conftest import CHAT
from tests.test_qa import _ask, _chat_sse, _conversation, _ready_document


async def _seed_knowledge(auth_client, session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "qa_verify_parse", False)
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("系统使用模型。", cited=True))
    events = dict(await _ask(auth_client, cid))
    evidence_id = events["assertions"]["assertions"][0]["evidence_ids"][0]

    source = KnowledgeEntity(
        canonical_name="DeepDocParse", normalized_name="deepdocparse", entity_type="system",
        aliases=["DDP"], merged_by="model", merge_confidence=0.7,
        entity_merge_uncertain=True, provider={"model": "fixture"})
    target = KnowledgeEntity(
        canonical_name="Qwen3-VL", normalized_name="qwen3vl", entity_type="model",
        provider={"model": "fixture"})
    session.add_all([source, target])
    await session.flush()
    edge = GraphEdge(subject_id=source.id, predicate="uses", object_id=target.id,
                     confidence=0.9, unsupported=False,
                     provider={"model": "fixture", "source_subject": "DDP"})
    entry = WikiEntry(entity_id=source.id, title="DeepDocParse",
                      outline=["架构"], provider={"model": "fixture"})
    session.add_all([edge, entry])
    await session.flush()
    section = WikiSection(entry_id=entry.id, position=0, heading="架构")
    session.add(section)
    await session.flush()
    sentence = WikiSentence(section_id=section.id, position=0, text="系统使用 Qwen3-VL。",
                            unsupported=False, provider={"model": "fixture"})
    session.add(sentence)
    await session.flush()
    from app.models import Evidence
    evidence = await session.get(Evidence, evidence_id)
    session.add_all([
        Citation(evidence_id=evidence_id, source_kind="graph_edge", source_id=edge.id,
                 role="primary", snippet=evidence.content,
                 content_digest=evidence.content_digest, rank=0),
        Citation(evidence_id=evidence_id, source_kind="wiki_sentence", source_id=sentence.id,
                 role="primary", snippet=evidence.content,
                 content_digest=evidence.content_digest, rank=0),
    ])
    await session.commit()
    return document, source, target, edge, entry, sentence, evidence_id


@respx.mock
async def test_graph_wiki_and_backlinks_share_the_same_evidence_truth(
        auth_client, session, monkeypatch):
    document, source, target, edge, entry, sentence, evidence_id = await _seed_knowledge(
        auth_client, session, monkeypatch)

    graph = (await auth_client.get(
        f"/api/knowledge/graph?entity={source.id}&depth=1")).json()
    assert graph["graph_version"] == "ddp-graph/1"
    assert {row["id"] for row in graph["entities"]} == {source.id, target.id}
    assert graph["edges"][0]["unsupported"] is False
    assert graph["edges"][0]["evidence_ids"] == [evidence_id]
    assert graph["edges"][0]["citations"][0]["bbox"] is not None

    wiki = (await auth_client.get(f"/api/wiki/{entry.id}")).json()
    payload = wiki["sections"][0]["sentences"][0]
    assert payload["unsupported"] is False and payload["evidence_ids"] == [evidence_id]
    backlinks = (await auth_client.get(f"/api/evidence/{evidence_id}/backlinks")).json()
    assert {row["source_kind"] for row in backlinks["backlinks"]} >= {
        "assertion", "graph_edge", "wiki_sentence"}

    # 同一知识产物的证据失效后，审计 Citation 还在，但不能继续支持边/wiki 句。
    await session.execute(delete(Chunk).where(Chunk.document_id == document["id"]))
    await session.commit()
    stale_graph = (await auth_client.get(
        f"/api/knowledge/graph?entity={source.id}&depth=1")).json()["edges"][0]
    assert stale_graph["unsupported"] is True and stale_graph["evidence_ids"] == []
    assert stale_graph["citations"][0]["resolved"] is False
    stale_wiki = (await auth_client.get(f"/api/wiki/{entry.id}")).json()
    assert stale_wiki["sections"][0]["sentences"][0]["unsupported"] is True


@respx.mock
async def test_review_queue_is_annotation_only_and_uncertain_merge_can_split(
        auth_client, session, monkeypatch):
    document, source, _, edge, _, sentence, _ = await _seed_knowledge(
        auth_client, session, monkeypatch)
    from app.models import User
    user = (await session.execute(select(User))).scalars().first()
    run = ExtractionRun(user_id=user.id, name="review fixture", schema_json={},
                        status="succeeded")
    session.add(run)
    await session.flush()
    extracted = ExtractionItem(
        run_id=run.id, document_id=document["id"], fields={
            "version": {"status": "found", "value": "1.0", "review_state": "unreviewed"}})
    session.add(extracted)
    await session.commit()

    queue = (await auth_client.get("/api/reviews")).json()["items"]
    assert {row["target_kind"] for row in queue} == {
        "graph_edge", "wiki_sentence", "entity_merge", "extract_field"}
    limited = (await auth_client.get("/api/reviews?limit=2")).json()
    assert len(limited["items"]) == 2 and limited["truncated"] is True
    assert limited["limit"] == 2

    response = await auth_client.post(f"/api/reviews/graph_edge/{edge.id}", json={
        "action": "reject", "reason_code": "relation_wrong", "reason_text": "原文不支持"})
    assert response.status_code == 201 and response.json()["review_state"] == "rejected"
    await session.refresh(edge)
    assert edge.review_state == "rejected"
    review = (await session.execute(select(KnowledgeReview).where(
        KnowledgeReview.target_id == edge.id))).scalars().one()
    assert review.action == "reject" and review.reason_code == "relation_wrong"

    response = await auth_client.post(
        f"/api/reviews/extract_field/{extracted.id}:version", json={"action": "pass"})
    assert response.status_code == 201
    await session.refresh(extracted)
    assert extracted.fields["version"]["review_state"] == "passed"

    response = await auth_client.post(f"/api/knowledge/entities/{source.id}/split",
                                      json={"alias": "DDP"})
    assert response.status_code == 201, response.text
    separated = response.json()
    assert separated["canonical_name"] == "DDP" and separated["split_from_id"] == source.id
    await session.refresh(source)
    assert "DDP" not in source.aliases and source.entity_merge_uncertain is False
    await session.refresh(edge)
    assert edge.subject_id == separated["id"]
    assert separated["rewired_edges"] == 1


@respx.mock
async def test_build_runs_relation_then_storm_outline_and_sentence_with_citations(
        auth_client, session):
    document = await _ready_document(auth_client)
    from app.models import Evidence
    evidence = (await session.execute(select(Evidence).where(
        Evidence.document_id == document["id"], Evidence.content != ""
    ).order_by(Evidence.seq))).scalars().first()
    assert evidence is not None

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "关系抽取" in system:
            value = {"entities": [
                {"name": "DeepDocParse", "type": "system", "aliases": ["DDP"]},
                {"name": "Qwen3-VL", "type": "model", "aliases": []},
            ], "relations": [{"subject": "DeepDocParse", "predicate": "uses",
                               "object": "Qwen3-VL", "confidence": 0.9,
                               "evidence_ids": [evidence.id, "invented-id"]}]}
        elif "阶段一" in system:
            value = {"entries": [{"entity": "DeepDocParse", "sections": ["架构"]}]}
        else:
            value = {"sections": [{"heading": "架构", "sentences": [{
                "text": "系统使用 Qwen3-VL。", "evidence_ids": [evidence.id, "invented-id"],
                "conflict_group": None}]}]}
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(value, ensure_ascii=False)}}]})

    route = respx.post(CHAT).mock(side_effect=handler)
    response = await auth_client.post("/api/knowledge/build",
                                      json={"evidence_ids": [evidence.id]})
    assert response.status_code == 201, response.text
    assert response.json() == {"status": "ok", "entities": 2, "edges": 1,
                               "relation_status": "ok",
                               "wiki_entries": 1}
    assert route.call_count == 3
    second = await auth_client.post("/api/knowledge/build",
                                    json={"evidence_ids": [evidence.id]})
    assert second.status_code == 201, second.text
    assert route.call_count == 6
    assert len((await session.execute(select(GraphEdge))).scalars().all()) == 1
    assert len((await session.execute(select(WikiSection))).scalars().all()) == 1
    assert len((await session.execute(select(WikiSentence))).scalars().all()) == 1
    graph = (await auth_client.get("/api/knowledge/graph?entity=DeepDocParse")).json()
    assert graph["edges"][0]["evidence_ids"] == [evidence.id]
    wiki = (await auth_client.get("/api/wiki/DeepDocParse")).json()
    assert wiki["sections"][0]["sentences"][0]["evidence_ids"] == [evidence.id]


@respx.mock
async def test_build_negative_sample_returns_not_found_without_inventing_edge(
        auth_client, session):
    document = await _ready_document(auth_client)
    from app.models import Evidence
    evidence = (await session.execute(select(Evidence).where(
        Evidence.document_id == document["id"], Evidence.content != ""
    ).order_by(Evidence.seq))).scalars().first()
    assert evidence is not None

    def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)["messages"][0]["content"]
        value = ({"entities": [], "relations": []}
                 if "关系抽取" in system else {"entries": []})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(value)}}]})

    route = respx.post(CHAT).mock(side_effect=handler)
    response = await auth_client.post("/api/knowledge/build",
                                      json={"evidence_ids": [evidence.id]})
    assert response.status_code == 201, response.text
    assert response.json() == {"status": "ok", "entities": 0, "edges": 0,
                               "relation_status": "not_found", "wiki_entries": 0}
    assert route.call_count == 2
    assert (await session.execute(select(GraphEdge))).scalars().all() == []


@respx.mock
async def test_rebuild_replaces_graph_edge_evidence_instead_of_leaving_stale_citation(
        auth_client, session):
    document = await _ready_document(auth_client)
    from app.models import Evidence
    evidence = (await session.execute(select(Evidence).where(
        Evidence.document_id == document["id"], Evidence.content != ""
    ).order_by(Evidence.seq))).scalars().first()
    assert evidence is not None

    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        system = json.loads(request.content)["messages"][0]["content"]
        if "关系抽取" in system:
            cited = [evidence.id] if calls == 1 else []
            value = {"entities": [{"name": "A"}, {"name": "B"}], "relations": [{
                "subject": "A", "predicate": "uses", "object": "B",
                "confidence": 0.7, "evidence_ids": cited}]}
        elif "阶段一" in system:
            value = {"entries": []}
        else:  # pragma: no cover - outline is empty
            value = {"sections": []}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(value)}}]})

    respx.post(CHAT).mock(side_effect=handler)
    body = {"evidence_ids": [evidence.id]}
    assert (await auth_client.post("/api/knowledge/build", json=body)).status_code == 201
    assert (await auth_client.post("/api/knowledge/build", json=body)).status_code == 201
    graph = (await auth_client.get("/api/knowledge/graph?entity=A")).json()
    assert graph["edges"][0]["evidence_ids"] == []
    assert graph["edges"][0]["unsupported"] is True
    assert (await session.execute(select(Citation).where(
        Citation.source_kind == "graph_edge"))).scalars().all() == []
    assert graph["edges"][0]["unsupported"] is True


async def test_knowledge_switch_disables_only_the_new_surface(auth_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "knowledge_enabled", False)
    disabled = await auth_client.get("/api/knowledge/graph")
    assert disabled.status_code == 404
    assert disabled.json()["error"]["code"] == "knowledge_disabled"
    # 旧 API 不引用知识层开关；关图谱不能拖坏既有文档指标与路径。
    assert (await auth_client.get("/api/documents")).status_code == 200


@respx.mock
async def test_rejected_review_is_exported_to_fixed_eval_set(
        auth_client, session, monkeypatch, tmp_path):
    _, _, _, edge, _, _, _ = await _seed_knowledge(auth_client, session, monkeypatch)
    response = await auth_client.post(f"/api/reviews/graph_edge/{edge.id}", json={
        "action": "reject", "reason_code": "relation_wrong", "reason_text": "连边不成立"})
    assert response.status_code == 201

    from app.review_export import export_reviews
    output = tmp_path / "reviewed.jsonl"
    count, revision = await export_reviews(session, output)
    assert count == 1 and len(revision) == 64
    sample = json.loads(output.read_text(encoding="utf-8"))
    assert sample["failure_stage"] == "link"
    assert sample["target"]["predicate"] == "uses"
    review = (await session.execute(select(KnowledgeReview))).scalars().one()
    assert review.exported_revision == revision
