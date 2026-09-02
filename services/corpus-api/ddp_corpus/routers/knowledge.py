"""DDP-Graph v1：图谱、Wiki、反链与复核队列。"""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.db import get_session
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.errors import APIError
from ddp_corpus.evidence import citation_out, load_citations
from ddp_corpus.knowledge import generate as generate_knowledge
from ddp_corpus.usage import record_usage
from ddp_corpus.models import (
    Assertion, Citation, Evidence, ExtractionItem, GraphEdge, KnowledgeEntity,
    KnowledgeReview, WikiEntry, WikiSection, WikiSentence,
)
from ddp_core.knowledge import neighbor_ids, normalize_entity_name

def require_knowledge_enabled() -> None:
    if not settings.knowledge_enabled:
        raise APIError(404, "knowledge layer is disabled", "invalid_request_error",
                       "knowledge_disabled")


router = APIRouter(dependencies=[Depends(require_knowledge_enabled)])


class BuildIn(BaseModel):
    evidence_ids: list[str] = Field(default_factory=list, max_length=200)


@router.post("/knowledge/build", status_code=201)
async def build_knowledge(body: BuildIn, request: Request,
                          actor: Actor = Depends(current_actor),
                          session: AsyncSession = Depends(get_session)):
    # **限速不在这里。** 图谱/wiki 生成很贵，但那道闸在 control-api 的
    # 领域限速里（按路由类别 + actor 计数，跨副本共享）。两处各限一次
    # 只会让"到底是谁把我限了"变成一个没人答得上的问题
    evidence_ids = list(dict.fromkeys(body.evidence_ids))
    if not evidence_ids:
        evidence_ids = list((await session.execute(
            select(Evidence.id).where(Evidence.derived_from.is_(None), Evidence.content != "")
            .order_by(Evidence.created_at.desc()).limit(settings.knowledge_max_evidence)
        )).scalars().all())
    if len(evidence_ids) > settings.knowledge_max_evidence:
        raise APIError(400, f"一次最多 {settings.knowledge_max_evidence} 条证据",
                       "invalid_request_error", "too_many_evidence")
    provider = {"kind": "knowledge_generation",
                "model": settings.chat_model or "registry-default", "revision": "runtime"}
    try:
        result = await generate_knowledge(
            session, request.app.state.http, evidence_ids, provider=provider)
    except Exception as exc:
        await session.rollback()
        raise APIError(502, f"知识生成失败：{type(exc).__name__}", "upstream_error",
                       "knowledge_generation_failed")
    await record_usage(session, actor_id=actor.id, organization_id=actor.organization_id,
                       kind="knowledge", requests=1)
    await session.commit()
    return result


def _entity_out(row: KnowledgeEntity) -> dict:
    return {
        "id": row.id, "canonical_name": row.canonical_name,
        "normalized_name": row.normalized_name, "entity_type": row.entity_type,
        "aliases": row.aliases or [], "merged_by": row.merged_by,
        "merge_confidence": row.merge_confidence,
        "entity_merge_uncertain": row.entity_merge_uncertain,
        "split_from_id": row.split_from_id, "review_state": row.review_state,
        "provider": row.provider or {},
    }


async def _knowledge_citations(session: AsyncSession, kind: str,
                               source_ids: list[str]) -> dict[str, list[dict]]:
    loaded = await load_citations(session, source_kind=kind, source_ids=source_ids)
    return {source_id: [citation_out(item["document_id"], item)
                        for item in loaded.get(source_id, [])]
            for source_id in source_ids}


@router.get("/knowledge/entities")
async def list_entities(q: str = "", entity_type: str = "",
                        uncertain: bool | None = None,
                        actor: Actor = Depends(current_actor),
                        session: AsyncSession = Depends(get_session)):
    stmt = select(KnowledgeEntity)
    if q:
        term = f"%{q}%"
        stmt = stmt.where(or_(KnowledgeEntity.canonical_name.ilike(term),
                              KnowledgeEntity.normalized_name.ilike(term)))
    if entity_type:
        stmt = stmt.where(KnowledgeEntity.entity_type == entity_type)
    if uncertain is not None:
        stmt = stmt.where(KnowledgeEntity.entity_merge_uncertain == uncertain)
    rows = (await session.execute(
        stmt.order_by(KnowledgeEntity.canonical_name).limit(1000))).scalars().all()
    return {"graph_version": "ddp-graph/1", "entities": [_entity_out(row) for row in rows]}


async def _resolve_entity(value: str, session: AsyncSession) -> KnowledgeEntity:
    row = await session.get(KnowledgeEntity, value)
    if row is None:
        row = (await session.execute(select(KnowledgeEntity).where(
            KnowledgeEntity.normalized_name == normalize_entity_name(value)
        ))).scalar_one_or_none()
    if row is None:
        raise APIError(404, "entity not found", "invalid_request_error", "not_found")
    return row


@router.get("/knowledge/graph")
async def graph(entity: str = "", depth: int = 1,
                actor: Actor = Depends(current_actor),
                session: AsyncSession = Depends(get_session)):
    if not 1 <= depth <= 3:
        raise APIError(400, "depth must be between 1 and 3", "invalid_request_error",
                       "invalid_depth")
    entities = (await session.execute(select(KnowledgeEntity))).scalars().all()
    edges = (await session.execute(select(GraphEdge).order_by(GraphEdge.id))).scalars().all()
    if entity:
        center = await _resolve_entity(entity, session)
        included = neighbor_ids(center.id, [(edge.subject_id, edge.object_id) for edge in edges],
                                depth)
        entities = [row for row in entities if row.id in included]
        edges = [edge for edge in edges
                 if edge.subject_id in included and edge.object_id in included]
    citations = await _knowledge_citations(session, "graph_edge", [edge.id for edge in edges])
    edge_payload = []
    for edge in edges:
        audit = citations.get(edge.id, [])
        live = [item for item in audit if item["resolved"]]
        edge_payload.append({
            "id": edge.id, "subject_id": edge.subject_id, "predicate": edge.predicate,
            "object_id": edge.object_id, "confidence": edge.confidence,
            "evidence_ids": [item["evidence_id"] for item in live],
            "unsupported": edge.unsupported or not bool(live),
            "review_state": edge.review_state, "provider": edge.provider or {},
            "citations": audit,
        })
    return {"graph_version": "ddp-graph/1", "entities": [_entity_out(row) for row in entities],
            "edges": edge_payload}


@router.get("/wiki")
async def list_wiki(actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(WikiEntry, KnowledgeEntity).join(
            KnowledgeEntity, KnowledgeEntity.id == WikiEntry.entity_id)
        .order_by(WikiEntry.title))).all()
    return [{"id": entry.id, "entity": _entity_out(entity), "title": entry.title,
             "outline": entry.outline or [], "provider": entry.provider or {}}
            for entry, entity in rows]


@router.get("/wiki/{entry_id_or_title}")
async def read_wiki(entry_id_or_title: str, actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    entry = await session.get(WikiEntry, entry_id_or_title)
    if entry is None:
        entry = (await session.execute(select(WikiEntry).where(
            WikiEntry.title == entry_id_or_title))).scalar_one_or_none()
    if entry is None:
        raise APIError(404, "wiki entry not found", "invalid_request_error", "not_found")
    entity = await session.get(KnowledgeEntity, entry.entity_id)
    sections = (await session.execute(select(WikiSection).where(
        WikiSection.entry_id == entry.id).order_by(WikiSection.position))).scalars().all()
    sentences = (await session.execute(select(WikiSentence).where(
        WikiSentence.section_id.in_([section.id for section in sections])
    ).order_by(WikiSentence.section_id, WikiSentence.position))).scalars().all() if sections else []
    citations = await _knowledge_citations(
        session, "wiki_sentence", [sentence.id for sentence in sentences])
    by_section: dict[str, list[dict]] = {}
    for sentence in sentences:
        audit = citations.get(sentence.id, [])
        live = [item for item in audit if item["resolved"]]
        by_section.setdefault(sentence.section_id, []).append({
            "id": sentence.id, "text": sentence.text,
            "evidence_ids": [item["evidence_id"] for item in live],
            "unsupported": sentence.unsupported or not bool(live),
            "conflict_group": sentence.conflict_group,
            "review_state": sentence.review_state, "provider": sentence.provider or {},
            "citations": audit,
        })
    return {"entry": {"id": entry.id, "entity": _entity_out(entity), "title": entry.title,
                       "outline": entry.outline or [], "provider": entry.provider or {}},
            "sections": [{"id": section.id, "heading": section.heading,
                           "sentences": by_section.get(section.id, [])}
                          for section in sections]}


@router.get("/evidence/{evidence_id}/backlinks")
async def backlinks(evidence_id: str, actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Citation).where(
        Citation.evidence_id == evidence_id).order_by(Citation.created_at, Citation.id)
    )).scalars().all()
    result = []
    for row in rows:
        label = row.source_id
        if row.source_kind == "assertion":
            target = await session.get(Assertion, row.source_id)
            label = target.text if target else row.source_id
        elif row.source_kind == "graph_edge":
            target = await session.get(GraphEdge, row.source_id)
            label = target.predicate if target else row.source_id
        elif row.source_kind == "wiki_sentence":
            target = await session.get(WikiSentence, row.source_id)
            label = target.text if target else row.source_id
        elif row.source_kind == "extract_field":
            item_id, _, field_name = row.source_id.partition(":")
            target = await session.get(ExtractionItem, item_id)
            label = field_name if target else row.source_id
        result.append({"source_kind": row.source_kind, "source_id": row.source_id,
                       "role": row.role, "label": label})
    return {"evidence_id": evidence_id, "backlinks": result}


@router.get("/reviews")
async def review_queue(limit: int = Query(default=200, ge=1, le=500),
                       actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session)):
    edges = (await session.execute(select(GraphEdge).where(
        GraphEdge.review_state.in_(["unreviewed", "questioned"]))
        .order_by(GraphEdge.id).limit(limit + 1))).scalars().all()
    sentences = (await session.execute(select(WikiSentence).where(
        WikiSentence.review_state.in_(["unreviewed", "questioned"]))
        .order_by(WikiSentence.id).limit(limit + 1))).scalars().all()
    entities = (await session.execute(select(KnowledgeEntity).where(
        KnowledgeEntity.entity_merge_uncertain.is_(True))
        .order_by(KnowledgeEntity.id)
        .limit(limit + 1))).scalars().all()
    extraction_items = (await session.execute(select(ExtractionItem)
        .order_by(ExtractionItem.id)
        .limit(limit + 1))).scalars().all()
    extraction_fields = []
    for item in extraction_items:
        for field_name, field in (item.fields or {}).items():
            if not isinstance(field, dict):
                continue
            state = field.get("review_state", "unreviewed")
            # not_found / error 也需要能被人标注；评测集正需要这些负样本与降级样本。
            if state in ("unreviewed", "questioned"):
                extraction_fields.append({
                    "target_kind": "extract_field",
                    "target_id": f"{item.id}:{field_name}",
                    "label": f"{field_name}: {field.get('value')!s}",
                    "review_state": state,
                })
    items = [
        *[{"target_kind": "graph_edge", "target_id": row.id,
           "label": row.predicate, "review_state": row.review_state} for row in edges],
        *[{"target_kind": "wiki_sentence", "target_id": row.id,
           "label": row.text, "review_state": row.review_state} for row in sentences],
        *[{"target_kind": "entity_merge", "target_id": row.id,
           "label": row.canonical_name, "review_state": row.review_state} for row in entities],
        *extraction_fields,
    ]
    truncated = any(len(rows) > limit for rows in (
        edges, sentences, entities, extraction_items)) or len(items) > limit
    return {"items": items[:limit], "truncated": truncated, "limit": limit}


class ReviewIn(BaseModel):
    action: Literal["pass", "reject", "question"]
    reason_code: str | None = Field(default=None, max_length=64)
    reason_text: str | None = Field(default=None, max_length=1000)


@router.post("/reviews/{target_kind}/{target_id}", status_code=201)
async def review(target_kind: Literal["graph_edge", "wiki_sentence", "entity_merge",
                                      "extract_field"], target_id: str, body: ReviewIn,
                 actor: Actor = Depends(current_actor),
                 session: AsyncSession = Depends(get_session)):
    model = {"graph_edge": GraphEdge, "wiki_sentence": WikiSentence,
             "entity_merge": KnowledgeEntity}.get(target_kind)
    target = await session.get(model, target_id) if model else None
    if target_kind != "extract_field" and target is None:
        raise APIError(404, "review target not found", "invalid_request_error", "not_found")
    if target_kind == "extract_field":
        item_id, separator, field_name = target_id.partition(":")
        item = await session.get(ExtractionItem, item_id)
        if not separator or item is None or field_name not in (item.fields or {}):
            raise APIError(404, "review target not found", "invalid_request_error", "not_found")
    state = {"pass": "passed", "reject": "rejected", "question": "questioned"}[body.action]
    if target is not None:
        target.review_state = state
        if target_kind == "entity_merge" and body.action == "pass":
            target.entity_merge_uncertain = False
    elif target_kind == "extract_field":
        # JSON 列必须整体重新赋值，SQLAlchemy 才会可靠地检测到变更。
        fields = dict(item.fields or {})
        field = dict(fields[field_name])
        field["review_state"] = state
        fields[field_name] = field
        item.fields = fields
    row = KnowledgeReview(target_kind=target_kind, target_id=target_id,
                          action=body.action, reason_code=body.reason_code,
                          reason_text=body.reason_text, reviewer_id=actor.id)
    session.add(row)
    await session.commit()
    return {"id": row.id, "target_kind": target_kind, "target_id": target_id,
            "review_state": state}


class SplitIn(BaseModel):
    alias: str = Field(min_length=1, max_length=255)


@router.post("/knowledge/entities/{entity_id}/split", status_code=201)
async def split_entity(entity_id: str, body: SplitIn, actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session)):
    entity = await session.get(KnowledgeEntity, entity_id)
    if entity is None or body.alias not in (entity.aliases or []):
        raise APIError(404, "merge alias not found", "invalid_request_error", "not_found")
    normalized = normalize_entity_name(body.alias)
    if (await session.execute(select(KnowledgeEntity).where(
            KnowledgeEntity.normalized_name == normalized))).scalar_one_or_none():
        raise APIError(409, "entity already exists", "invalid_request_error", "duplicate_entity")
    entity.aliases = [alias for alias in entity.aliases if alias != body.alias]
    entity.entity_merge_uncertain = False
    separated = KnowledgeEntity(
        canonical_name=body.alias, normalized_name=normalized, entity_type=entity.entity_type,
        aliases=[], merged_by="human", merge_confidence=1.0,
        entity_merge_uncertain=False, split_from_id=entity.id, review_state="passed",
        provider={"kind": "human_split"})
    session.add(separated)
    await session.flush()
    rewired = 0
    edges = (await session.execute(select(GraphEdge).where(or_(
        GraphEdge.subject_id == entity.id, GraphEdge.object_id == entity.id
    )))).scalars().all()
    for edge in edges:
        provider = edge.provider or {}
        if (edge.subject_id == entity.id
                and normalize_entity_name(str(provider.get("source_subject") or "")) == normalized):
            edge.subject_id = separated.id
            rewired += 1
        if (edge.object_id == entity.id
                and normalize_entity_name(str(provider.get("source_object") or "")) == normalized):
            edge.object_id = separated.id
            rewired += 1
    session.add(KnowledgeReview(target_kind="entity_merge", target_id=entity.id,
                                action="split_merge", reason_code="manual_split",
                                reason_text=body.alias, reviewer_id=actor.id))
    await session.commit()
    return {**_entity_out(separated), "rewired_edges": rewired}
