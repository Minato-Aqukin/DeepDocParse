import json

import httpx
import respx
from sqlalchemy import delete, select

from ddp_corpus.models import (
    Chunk, Citation, Evidence, ExtractionItem, ExtractionRun, GraphEdge, KnowledgeEntity,
    KnowledgeReview, Resource, ResourceVersion, WikiEntry, WikiSection, WikiSentence,
)
from tests.conftest import ACTOR, CHAT, ORG, actor_headers
from tests.test_qa import _ask, _conversation, _grounded_doc, _grounded_side_effect, _ready_document


async def _seed_knowledge(actor_client, session, monkeypatch):
    from ddp_corpus.config import settings

    monkeypatch.setattr(settings, "qa_verify_parse", False)
    document = await _ready_document(actor_client)
    cid = await _conversation(actor_client, document["id"])
    respx.post(CHAT).mock(side_effect=_grounded_side_effect(_grounded_doc(("系统使用模型。", [1]))))
    events = dict(await _ask(actor_client, cid))
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
    evidence = await session.get(Evidence, evidence_id)
    session.add_all([
        Citation(evidence_id=evidence_id, source_kind="graph_edge", source_id=edge.id,
                 role="primary", snippet=evidence.content,
                 content_digest=evidence.content_digest, rank=0),
        Citation(evidence_id=evidence_id, source_kind="wiki_sentence", source_id=sentence.id,
                 role="primary", snippet=evidence.content,
                 content_digest=evidence.content_digest, rank=0),
    ])
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.document_id == evidence.document_id))
    assert version is not None
    provenance = {"generated_by": ACTOR, "organization_id": ORG,
                  "source_bindings": [{"resource_id": version.resource_id,
                    "source_version_id": version.id, "document_id": evidence.document_id,
                    "parse_revision": evidence.parse_job_id}]}
    for row in (source, target, edge, entry, sentence):
        row.provider = {**(row.provider or {}), **provenance}
    await session.commit()
    return document, source, target, edge, entry, sentence, evidence_id


@respx.mock
async def test_graph_wiki_and_backlinks_share_the_same_evidence_truth(
        actor_client, session, monkeypatch):
    document, source, target, edge, entry, sentence, evidence_id = await _seed_knowledge(
        actor_client, session, monkeypatch)

    graph = (await actor_client.get(
        f"/api/knowledge/graph?entity={source.id}&depth=1")).json()
    assert graph["graph_version"] == "ddp-graph/1"
    assert {row["id"] for row in graph["entities"]} == {source.id, target.id}
    assert graph["edges"][0]["unsupported"] is False
    assert graph["edges"][0]["evidence_ids"] == [evidence_id]
    assert graph["edges"][0]["citations"][0]["bbox"] is not None

    wiki = (await actor_client.get(f"/api/wiki/{entry.id}")).json()
    payload = wiki["sections"][0]["sentences"][0]
    assert payload["unsupported"] is False and payload["evidence_ids"] == [evidence_id]
    backlinks = (await actor_client.get(f"/api/evidence/{evidence_id}/backlinks")).json()
    assert {row["source_kind"] for row in backlinks["backlinks"]} >= {
        "assertion", "graph_edge", "wiki_sentence"}

    # 同一知识产物的证据失效后，审计 Citation 还在，但不能继续支持边/wiki 句。
    await session.execute(delete(Chunk).where(Chunk.document_id == document["id"]))
    await session.commit()
    stale_graph = (await actor_client.get(
        f"/api/knowledge/graph?entity={source.id}&depth=1")).json()["edges"][0]
    assert stale_graph["unsupported"] is True and stale_graph["evidence_ids"] == []
    assert stale_graph["citations"][0]["resolved"] is False
    stale_wiki = (await actor_client.get(f"/api/wiki/{entry.id}")).json()
    assert stale_wiki["sections"][0]["sentences"][0]["unsupported"] is True


@respx.mock
async def test_review_queue_is_annotation_only_and_uncertain_merge_can_split(
        actor_client, session, monkeypatch):
    document, source, _, edge, _, sentence, _ = await _seed_knowledge(
        actor_client, session, monkeypatch)
    actor_id = ACTOR
    run = ExtractionRun(actor_id=actor_id, organization_id=ORG, name="review fixture", schema_json={},
                        status="succeeded")
    from ddp_corpus.models import ResourceVersion
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.document_id == document["id"]))
    run.resource_context = {"resources": {document["id"]: version.resource_id}, "principal_id": ACTOR}
    session.add(run)
    await session.flush()
    extracted = ExtractionItem(
        run_id=run.id, document_id=document["id"], fields={
            "version": {"status": "found", "value": "1.0", "review_state": "unreviewed"}})
    session.add(extracted)
    await session.commit()

    queue = (await actor_client.get("/api/reviews")).json()["items"]
    assert {row["target_kind"] for row in queue} == {
        "graph_edge", "wiki_sentence", "entity_merge", "extract_field"}
    limited = (await actor_client.get("/api/reviews?limit=2")).json()
    assert len(limited["items"]) == 2 and limited["truncated"] is True
    assert limited["limit"] == 2

    response = await actor_client.post(f"/api/reviews/graph_edge/{edge.id}", json={
        "action": "reject", "reason_code": "relation_wrong", "reason_text": "原文不支持"})
    assert response.status_code == 201 and response.json()["review_state"] == "rejected"
    await session.refresh(edge)
    assert edge.review_state == "rejected"
    review = (await session.execute(select(KnowledgeReview).where(
        KnowledgeReview.target_id == edge.id))).scalars().one()
    assert review.action == "reject" and review.reason_code == "relation_wrong"

    response = await actor_client.post(
        f"/api/reviews/extract_field/{extracted.id}:version", json={"action": "pass"})
    assert response.status_code == 201
    await session.refresh(extracted)
    assert extracted.fields["version"]["review_state"] == "passed"

    response = await actor_client.post(f"/api/knowledge/entities/{source.id}/split",
                                      json={"alias": "DDP"})
    assert response.status_code == 201, response.text
    separated = response.json()
    assert separated["canonical_name"] == "DDP" and separated["split_from_id"] == source.id
    await session.refresh(source)
    assert "DDP" not in source.aliases and source.entity_merge_uncertain is False
    await session.refresh(edge)
    assert edge.subject_id == separated["id"]
    assert separated["rewired_edges"] == 1


async def test_legacy_build_route_is_gone_pointing_to_versioned_wikis(actor_client):
    response = await actor_client.post("/api/knowledge/build", json={"evidence_ids": ["x"]})
    assert response.status_code == 410, response.text
    assert response.json()["error"]["code"] == "legacy_wiki_build_removed"


@respx.mock
async def test_generate_scopes_evidence_by_actor_before_model_prompt(
        actor_client, session, app_state):
    from ddp_corpus.deps import Actor
    from ddp_corpus.knowledge import generate as generate_knowledge
    from ddp_corpus.models import Document, ParseJob

    document = await _ready_document(actor_client)
    mine = (await session.execute(select(Evidence).where(
        Evidence.document_id == document["id"], Evidence.content != ""
    ).order_by(Evidence.seq))).scalars().first()
    assert mine is not None
    foreign_org, foreign_actor = "org-foreign", "actor-foreign"
    foreign_doc = Document(uploaded_by=foreign_actor, organization_id=foreign_org,
                           doc_id="f" * 64, filename="foreign.pdf")
    session.add(foreign_doc)
    await session.flush()
    job = ParseJob(document_id=foreign_doc.id, engine="borndigital",
                   options_hash="v1", status="succeeded")
    session.add(job)
    await session.flush()
    foreign_resource = Resource(owner_id=foreign_actor, uploaded_by=foreign_actor,
                                organization_id=foreign_org, publication="private")
    session.add(foreign_resource)
    await session.flush()
    foreign_version = ResourceVersion(
        resource_id=foreign_resource.id, document_id=foreign_doc.id,
        parse_job_id=job.id, source_digest="f" * 64, version_no=1)
    foreign_evidence = Evidence(
        document_id=foreign_doc.id, parse_job_id=job.id, seq=0,
        atom_key="cross-org", content="foreign text", content_digest="f" * 64,
        kind="text", page_idx=0, bbox=[0, 0, 10, 10], page_size=[100, 100])
    session.add_all([foreign_version, foreign_evidence])
    await session.commit()
    assert foreign_evidence.bbox == [0, 0, 10, 10]

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "关系抽取" in system:
            seen.append(body["messages"][1]["content"])
            value = {"entities": [], "relations": []}
        elif "阶段一" in system:
            value = {"entries": []}
        else:  # pragma: no cover - outline is empty
            value = {"sections": []}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(value)}}]})

    route = respx.post(CHAT).mock(side_effect=handler)
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    provider = {"scope_key": "test", "source_bindings": [], "generated_by": ACTOR,
                "organization_id": ORG, "kind": "knowledge_generation"}
    result = await generate_knowledge(
        session, app_state.http, [mine.id, foreign_evidence.id],
        provider=provider, actor=actor)
    assert result["status"] == "not_found", result
    assert route.call_count == 0
    solo = await generate_knowledge(
        session, app_state.http, [mine.id], provider=provider, actor=actor)
    assert solo["status"] == "ok"
    assert foreign_evidence.id not in seen[0] and mine.id in seen[0]


@respx.mock
async def test_generate_runs_relation_then_storm_outline_and_sentence_with_citations(
        actor_client, session, app_state):
    from ddp_corpus.deps import Actor
    from ddp_corpus.knowledge import generate as generate_knowledge
    from ddp_corpus.models import ResourceVersion

    document = await _ready_document(actor_client)
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
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.document_id == evidence.document_id))
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    provider = {"scope_key": "build-test", "generated_by": ACTOR, "organization_id": ORG,
                "kind": "knowledge_generation",
                "source_bindings": [{"resource_id": version.resource_id,
                                     "source_version_id": version.id,
                                     "document_id": evidence.document_id,
                                     "parse_revision": evidence.parse_job_id}],
                "input_document_ids": [evidence.document_id]}
    result = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=provider, actor=actor)
    assert result == {"status": "ok", "entities": 2, "edges": 1,
                      "relation_status": "ok", "wiki_entries": 1}
    assert route.call_count == 3
    second = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=provider, actor=actor)
    assert second["status"] == "ok"
    assert route.call_count == 6
    assert len((await session.execute(select(GraphEdge))).scalars().all()) == 1
    assert len((await session.execute(select(WikiSection))).scalars().all()) == 1
    assert len((await session.execute(select(WikiSentence))).scalars().all()) == 1
    graph = (await actor_client.get("/api/knowledge/graph?entity=DeepDocParse")).json()
    assert graph["edges"][0]["evidence_ids"] == [evidence.id]
    wiki = (await actor_client.get("/api/wiki/DeepDocParse")).json()
    assert wiki["sections"][0]["sentences"][0]["evidence_ids"] == [evidence.id]


@respx.mock
async def test_generate_negative_sample_returns_not_found_without_inventing_edge(
        actor_client, session, app_state):
    from ddp_corpus.deps import Actor
    from ddp_corpus.knowledge import generate as generate_knowledge

    document = await _ready_document(actor_client)
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
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    provider = {"scope_key": "negative", "source_bindings": [], "generated_by": ACTOR,
                "organization_id": ORG, "kind": "knowledge_generation"}
    result = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=provider, actor=actor)
    assert result == {"status": "ok", "entities": 0, "edges": 0,
                      "relation_status": "not_found", "wiki_entries": 0}
    assert route.call_count == 2
    assert (await session.execute(select(GraphEdge))).scalars().all() == []


@respx.mock
async def test_regenerate_replaces_graph_edge_evidence_instead_of_leaving_stale_citation(
        actor_client, session, app_state):
    from ddp_corpus.deps import Actor
    from ddp_corpus.knowledge import generate as generate_knowledge
    from ddp_corpus.models import ResourceVersion

    document = await _ready_document(actor_client)
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
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.document_id == evidence.document_id))
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    provider = {"scope_key": "rebuild-test", "generated_by": ACTOR,
                "organization_id": ORG, "kind": "knowledge_generation",
                "source_bindings": [{"resource_id": version.resource_id,
                                     "source_version_id": version.id,
                                     "document_id": evidence.document_id,
                                     "parse_revision": evidence.parse_job_id}],
                "input_document_ids": [evidence.document_id]}
    first = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=provider, actor=actor)
    second = await generate_knowledge(
        session, app_state.http, [evidence.id], provider=provider, actor=actor)
    assert first["status"] == "ok" and second["status"] == "ok"
    graph = (await actor_client.get("/api/knowledge/graph?entity=A")).json()
    assert graph["edges"][0]["evidence_ids"] == []
    assert graph["edges"][0]["unsupported"] is True
    assert (await session.execute(select(Citation).where(
        Citation.source_kind == "graph_edge"))).scalars().all() == []


async def test_knowledge_switch_disables_only_the_new_surface(actor_client, monkeypatch):
    from ddp_corpus.config import settings

    monkeypatch.setattr(settings, "knowledge_enabled", False)
    disabled = await actor_client.get("/api/knowledge/graph")
    assert disabled.status_code == 404
    assert disabled.json()["error"]["code"] == "knowledge_disabled"
    # 旧 API 不引用知识层开关；关图谱不能拖坏既有文档指标与路径。
    assert (await actor_client.get("/api/documents")).status_code == 200


def _test_actor():
    from ddp_corpus.deps import Actor

    return Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")


@respx.mock
async def test_accessible_knowledge_scopes_by_provider_org_in_sql(
        actor_client, session, monkeypatch):
    """组织预过滤发生在 SQL 里，不靠事后 Python 门。

    只断言"外组织实体没出现在结果里"钉不住 SQL：`provider_allowed` 同样按组织
    拒掉它，删掉 SQL 谓词用例照样绿。这里盯住发出去的 SQL 本身 ——  citations
    依赖 join 必须带上组织边界（经可见文档谓词），实体预过滤必须带上
    provider 组织谓词。为 NULL-org 历史行开的口子只属于知识实体，不属于引用。
    """
    from ddp_corpus.knowledge_policy import accessible_knowledge
    from ddp_corpus.models import Document

    _, source, _, edge, entry, _, _ = await _seed_knowledge(
        actor_client, session, monkeypatch)
    foreign = KnowledgeEntity(
        id="foreign-entity-id-0001", canonical_name="Foreign", normalized_name="foreign",
        provider={"model": "fixture", "generated_by": "actor-elsewhere",
                  "organization_id": "org-elsewhere", "source_bindings": []})
    legacy = KnowledgeEntity(
        id="legacy-entity-id-0002", canonical_name="Legacy", normalized_name="legacy",
        provider={"model": "fixture"})
    session.add_all([foreign, legacy])
    foreign_doc = Document(uploaded_by="actor-elsewhere", organization_id="org-elsewhere",
                           doc_id="e" * 64, filename="elsewhere.pdf")
    session.add(foreign_doc)
    await session.commit()

    statements: list[str] = []
    real_execute = session.execute

    async def record(statement, *args, **kwargs):
        try:
            statements.append(str(statement.compile(
                compile_kwargs={"literal_binds": True})))
        except Exception:
            statements.append(str(statement))
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", record)
    access = await accessible_knowledge(session, _test_actor())
    assert foreign.id not in access["entities"]
    # Legacy NULL-org rows still load in SQL, then the per-row second gate
    # quarantines them (no attribution, no bindings).
    assert legacy.id not in access["entities"]
    assert source.id in access["entities"] and edge.id in access["edges"]

    joined = "\n".join(statements)
    cites = [text for text in statements
             if "citations" in text and "evidence" in text]
    assert cites, "依赖 join 必须真实执行，否则下面的 SQL 断言无从谈起"
    for text in cites:
        assert "documents" in text, "引用依赖必须经文档表收组织边界"
        assert ORG in text, "引用依赖的 SQL 必须出现调用方组织"
        assert "org-elsewhere" not in text
    assert any("knowledge_entities" in text and ORG in text for text in statements), \
        "实体预过滤的 SQL 必须出现调用方组织"
    assert "org-elsewhere" not in joined


@respx.mock
async def test_provider_gate_goes_dark_on_withdraw_and_reparse(
        actor_client, session, monkeypatch):
    from ddp_corpus.knowledge_policy import accessible_knowledge
    from ddp_corpus.models import ParseJob

    _, source, _, edge, _, _, _ = await _seed_knowledge(
        actor_client, session, monkeypatch)
    actor = _test_actor()
    assert edge.id in (await accessible_knowledge(session, actor))["edges"]
    version = await session.scalar(select(ResourceVersion))
    resource = await session.get(Resource, version.resource_id)
    resource.publication = "withdrawn"
    await session.commit()
    assert (await accessible_knowledge(session, actor))["edges"] == set()
    resource.publication = "private"
    await session.commit()
    assert edge.id in (await accessible_knowledge(session, actor))["edges"]
    job = ParseJob(document_id=version.document_id, engine="borndigital",
                   options_hash="v2", status="succeeded", document_version=2)
    session.add(job)
    await session.flush()
    version.parse_job_id = job.id
    await session.commit()
    assert (await accessible_knowledge(session, actor))["edges"] == set()
    assert source.id not in (await accessible_knowledge(session, actor))["entities"]


@respx.mock
async def test_rejected_review_is_exported_to_fixed_eval_set(
        actor_client, session, monkeypatch, tmp_path):
    _, _, _, edge, _, _, _ = await _seed_knowledge(actor_client, session, monkeypatch)
    response = await actor_client.post(f"/api/reviews/graph_edge/{edge.id}", json={
        "action": "reject", "reason_code": "relation_wrong", "reason_text": "连边不成立"})
    assert response.status_code == 201

    from ddp_corpus.review_export import export_reviews
    output = tmp_path / "reviewed.jsonl"
    count, revision = await export_reviews(session, output)
    assert count == 1 and len(revision) == 64
    sample = json.loads(output.read_text(encoding="utf-8"))
    assert sample["failure_stage"] == "link"
    assert sample["target"]["predicate"] == "uses"
    assert sample["evidence_ids"] != []
    assert sample["evidence"][0]["evidence_id"] == sample["evidence_ids"][0]
    assert "bbox" in sample["evidence"][0] and "page_size" in sample["evidence"][0]
    assert "page_idx" in sample["evidence"][0]
    review = (await session.execute(select(KnowledgeReview))).scalars().one()
    assert review.exported_revision == revision


@respx.mock
async def test_actor_scoped_export_excludes_other_org_rejects(
        actor_client, session, monkeypatch, tmp_path):
    from ddp_corpus.review_export import export_reviews

    _, _, _, edge, _, sentence, evidence_id = await _seed_knowledge(
        actor_client, session, monkeypatch)
    evidence = await session.get(Evidence, evidence_id)
    assert evidence.bbox is not None and evidence.page_size is not None
    for target_kind, target_id in (("graph_edge", edge.id),
                                   ("wiki_sentence", sentence.id)):
        response = await actor_client.post(
            f"/api/reviews/{target_kind}/{target_id}",
            json={"action": "reject", "reason_code": "bbox_wrong"})
        assert response.status_code == 201
    from ddp_corpus.models import Document, ParseJob

    foreign_doc = Document(uploaded_by="actor-elsewhere", organization_id="org-elsewhere",
                           doc_id="d" * 64, filename="foreign.pdf")
    session.add(foreign_doc)
    await session.flush()
    foreign_job = ParseJob(document_id=foreign_doc.id, engine="borndigital",
                           options_hash="v1", status="succeeded")
    session.add(foreign_job)
    await session.flush()
    foreign_resource = Resource(owner_id="actor-elsewhere", uploaded_by="actor-elsewhere",
                                organization_id="org-elsewhere", publication="private")
    session.add(foreign_resource)
    await session.flush()
    foreign_version = ResourceVersion(
        resource_id=foreign_resource.id, document_id=foreign_doc.id,
        parse_job_id=foreign_job.id, source_digest="d" * 64, version_no=1)
    session.add(foreign_version)
    await session.flush()
    foreign_provenance = {
        "model": "fixture", "generated_by": "actor-elsewhere",
        "organization_id": "org-elsewhere",
        "source_bindings": [{"resource_id": foreign_resource.id,
                             "source_version_id": foreign_version.id,
                             "document_id": foreign_doc.id,
                             "parse_revision": foreign_job.id}]}
    foreign_subject = KnowledgeEntity(
        canonical_name="ForeignSub", normalized_name="foreignsub",
        provider=dict(foreign_provenance))
    foreign_object = KnowledgeEntity(
        canonical_name="ForeignObj", normalized_name="foreignobj",
        provider=dict(foreign_provenance))
    session.add_all([foreign_subject, foreign_object])
    await session.flush()
    foreign_edge = GraphEdge(
        subject_id=foreign_subject.id, predicate="foreign-uses",
        object_id=foreign_object.id, confidence=0.1,
        provider=dict(foreign_provenance))
    session.add(foreign_edge)
    await session.flush()
    session.add(KnowledgeReview(target_kind="graph_edge", target_id=foreign_edge.id,
                               action="reject", reason_code="relation_wrong",
                               reviewer_id="actor-elsewhere"))
    await session.commit()
    scoped = tmp_path / "scoped.jsonl"
    count, _ = await export_reviews(session, scoped, actor=_test_actor())
    assert count == 2
    samples = [json.loads(line) for line in scoped.read_text(encoding="utf-8").splitlines()]
    assert {sample["target_kind"] for sample in samples} == {"graph_edge", "wiki_sentence"}
    assert all(sample["evidence"][0]["evidence_id"] == sample["evidence_ids"][0]
               for sample in samples if sample["evidence_ids"])
    assert all("bbox" in item and "page_size" in item and "page_idx" in item
               for sample in samples for item in sample["evidence"])
    outsider = tmp_path / "outsider.jsonl"
    from ddp_corpus.deps import Actor

    other = Actor(id="actor-elsewhere", kind="user", organization_id="org-elsewhere",
                  role="contributor", resource_id=foreign_resource.id)
    other_count, _ = await export_reviews(session, outsider, actor=other)
    assert other_count == 1
    assert json.loads(outsider.read_text(encoding="utf-8"))["target_id"] == foreign_edge.id


@respx.mock
async def test_cross_org_evidence_ids_stay_out_of_reads(
        actor_client, session, monkeypatch):
    from ddp_corpus.models import Document, ParseJob

    await _ready_document(actor_client)
    foreign_doc = Document(uploaded_by="actor-foreign", organization_id="org-foreign",
                           doc_id="e" * 64, filename="foreign.pdf")
    session.add(foreign_doc)
    await session.flush()
    job = ParseJob(document_id=foreign_doc.id, engine="borndigital",
                   options_hash="v1", status="succeeded")
    session.add(job)
    await session.flush()
    foreign_evidence = Evidence(
        document_id=foreign_doc.id, parse_job_id=job.id, seq=0,
        atom_key="cross-org-read", content="foreign text", content_digest="e" * 64,
        kind="text", page_idx=0, bbox=[0, 0, 10, 10], page_size=[100, 100])
    session.add(foreign_evidence)
    await session.commit()
    assert (await actor_client.get(
        f"/api/evidence/{foreign_evidence.id}/backlinks")).status_code == 404
    assert (await actor_client.get(
        "/api/knowledge/graph", headers=actor_headers("actor-elsewhere",
                                                      organization_id="org-foreign"))).json()[
               "entities"] == []
