"""人工驳回标注 -> 固定评测样本；纯数据库逻辑供脚本与测试共用。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Citation, ExtractionItem, GraphEdge, KnowledgeEntity, KnowledgeReview, WikiSentence,
)

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

    citations = (await session.execute(select(Citation.evidence_id).where(
        Citation.source_kind == row.target_kind,
        Citation.source_id == row.target_id))).scalars().all()
    payload["evidence_ids"] = sorted(set(citations))
    return payload


async def export_reviews(session: AsyncSession, output: Path) -> tuple[int, str]:
    rows = (await session.execute(select(KnowledgeReview).where(
        KnowledgeReview.action == "reject").order_by(
            KnowledgeReview.created_at, KnowledgeReview.id))).scalars().all()
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
