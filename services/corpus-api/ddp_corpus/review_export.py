"""人工驳回标注 -> 固定评测样本；纯数据库逻辑供脚本与测试共用。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.models import (
    Citation, Evidence, ExtractionItem, GraphEdge, KnowledgeEntity, KnowledgeReview, WikiSentence,
)
from ddp_corpus.knowledge_policy import accessible_knowledge, owned_projection

RECOGNITION_REASONS = {"ocr_wrong", "recognition_wrong", "bbox_wrong"}


def failure_stage(row: KnowledgeReview) -> str:
    if row.reason_code in RECOGNITION_REASONS:
        return "recognition"
    if row.target_kind == "extract_field":
        return "extraction"
    return "link"


async def _sample(session: AsyncSession, row: KnowledgeReview) -> dict:
    payload = {
        "review_id": row.id, "target_kind": row.target_kind, "target_id": row.target_id,
        "action": row.action, "reason_code": row.reason_code,
        "reason_text": row.reason_text, "failure_stage": failure_stage(row),
    }
    evidence_rows: dict[str, Evidence] = {}
    if row.target_kind == "graph_edge":
        edge = await session.get(GraphEdge, row.target_id)
        if edge:
            subject = await session.get(KnowledgeEntity, edge.subject_id)
            object_ = await session.get(KnowledgeEntity, edge.object_id)
            payload["target"] = {
                "subject": subject.canonical_name if subject else edge.subject_id,
                "predicate": edge.predicate,
                "object": object_.canonical_name if object_ else edge.object_id,
            }
    elif row.target_kind == "wiki_sentence":
        sentence = await session.get(WikiSentence, row.target_id)
        payload["target"] = {"text": sentence.text if sentence else None}
    elif row.target_kind == "entity_merge":
        entity = await session.get(KnowledgeEntity, row.target_id)
        payload["target"] = ({"canonical_name": entity.canonical_name,
                              "aliases": entity.aliases or []} if entity else {})
    elif row.target_kind == "extract_field":
        item_id, _, field_name = row.target_id.partition(":")
        item = await session.get(ExtractionItem, item_id)
        payload["target"] = {"field": field_name,
                             "result": (item.fields or {}).get(field_name) if item else None}

    evidence_ids = sorted(set((await session.execute(select(Citation.evidence_id).where(
        Citation.source_kind == row.target_kind,
        Citation.source_id == row.target_id))).scalars().all()))
    if evidence_ids:
        evidence_rows = {evidence.id: evidence for evidence in (await session.execute(
            select(Evidence).where(Evidence.id.in_(evidence_ids)))).scalars().all()}
    payload["evidence_ids"] = evidence_ids
    payload["evidence"] = [{
        "evidence_id": evidence_id,
        "bbox": (evidence_rows[evidence_id].bbox if evidence_id in evidence_rows else None),
        "page_size": (evidence_rows[evidence_id].page_size if evidence_id in evidence_rows else None),
        "page_idx": (evidence_rows[evidence_id].page_idx if evidence_id in evidence_rows else None),
    } for evidence_id in evidence_ids]
    return payload


async def accessible_for_export(session, actor, row: KnowledgeReview, access=None) -> bool:
    """Actor-scoped export uses the same read gate as the HTTP review queue."""
    bucket = {"graph_edge": "edges", "wiki_sentence": "sentences",
              "entity_merge": "entities"}.get(row.target_kind)
    if bucket is None:
        return await owned_projection(session, actor, row.target_kind, row.target_id)
    access = access if access is not None else await accessible_knowledge(session, actor)
    return row.target_id in access[bucket]


async def export_reviews(session: AsyncSession, output: Path, *, actor=None) -> tuple[int, str]:
    rows = (await session.execute(select(KnowledgeReview).where(
        KnowledgeReview.action == "reject").order_by(
            KnowledgeReview.created_at, KnowledgeReview.id))).scalars().all()
    if actor is not None:
        # Actor-scoped path filters per row through the read gate the HTTP review
        # queue uses; actor=None keeps the legacy global export for the eval script.
        access = await accessible_knowledge(session, actor)
        kept = []
        for row in rows:
            if row.target_kind not in ("graph_edge", "wiki_sentence", "entity_merge",
                                      "extract_field"):
                continue
            if await accessible_for_export(session, actor, row, access):
                kept.append(row)
        rows = kept
    samples = [await _sample(session, row) for row in rows]
    body = "".join(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n"
                   for sample in samples)
    revision = hashlib.sha256(body.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(output)
    for row in rows:
        row.exported_revision = revision
    await session.commit()
    return len(samples), revision
