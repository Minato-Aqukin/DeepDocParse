"""知识生成：关系抽取 -> STORM outline -> 逐句写作，全程 evidence id 受限。"""
from __future__ import annotations

import json
import re

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.models import (
    Citation, Evidence, GraphEdge, KnowledgeEntity, WikiEntry, WikiSection, WikiSentence,
)
from ddp_corpus.upstream import chat_request
from ddp_core.knowledge import EntityMention, edge_result, merge_mentions, normalize_entity_name

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

GRAPH_PROMPT = """你只做关系抽取。输入是带稳定 evidence_id 的原文原子。
输出单个 JSON：{"entities":[{"name":"...","type":"...","aliases":[],
"merge_confidence":0.0}],
"relations":[{"subject":"...","predicate":"...","object":"...","confidence":0.0,
"evidence_ids":["..."]}]}
只能使用输入中出现的 evidence_id；没有明确关系就 relations=[]，绝不补常识。"""

OUTLINE_PROMPT = """你为技术语料 Wiki 做资料组织（STORM 阶段一），不写正文。
输出 JSON：{"entries":[{"entity":"实体名","sections":["章节标题"]}]}。
只为输入实体列章节；没有材料的实体可省略。"""

WRITE_PROMPT = """你执行 STORM 阶段二，按 outline 写逐句可引用 Wiki。
输出 JSON：{"sections":[{"heading":"...","sentences":[{"text":"...",
"evidence_ids":["..."],"conflict_group":null}]}]}。
每句只用给定 evidence_id；无法支持仍可保留，但 evidence_ids=[]，系统会标 unsupported。
不同证据冲突时并列写两句并给相同 conflict_group，不自行裁决。"""


def _json_object(text: str) -> dict:
    match = _FENCE.search(text or "")
    candidate = match.group(1).strip() if match else (text or "").strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model output has no JSON object")
    value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output must be an object")
    return value


async def _complete(http: httpx.AsyncClient, system: str, prompt: str) -> dict:
    response = await http.send(chat_request(http, [
        {"role": "system", "content": system}, {"role": "user", "content": prompt},
    ], stream=False))
    if response.status_code != 200:
        raise RuntimeError(f"knowledge model returned {response.status_code}")
    return _json_object(response.json()["choices"][0]["message"]["content"])


def _evidence_prompt(rows: list[Evidence]) -> str:
    return "\n\n".join(f"[{row.id}] {row.content}" for row in rows)


async def generate(session: AsyncSession, http: httpx.AsyncClient,
                   evidence_ids: list[str], *, provider: dict) -> dict:
    rows = (await session.execute(select(Evidence).where(
        Evidence.id.in_(evidence_ids), Evidence.content != ""
    ).order_by(Evidence.id))).scalars().all()
    if not rows:
        return {"status": "not_found", "entities": 0, "edges": 0,
                "relation_status": "not_found", "wiki_entries": 0}
    allowed = {row.id for row in rows}
    graph = await _complete(http, GRAPH_PROMPT, _evidence_prompt(rows))
    mentions = [EntityMention(
        str(item.get("name") or ""), str(item.get("type") or "other"),
        tuple(str(value) for value in item.get("aliases") or []),
        "model" if item.get("aliases") else "none",
        float(item.get("merge_confidence", 0.5 if item.get("aliases") else 1.0)))
        for item in graph.get("entities") or [] if isinstance(item, dict)]
    groups = merge_mentions(mentions)

    entities: dict[str, KnowledgeEntity] = {}
    for group in groups:
        existing = (await session.execute(select(KnowledgeEntity).where(
            KnowledgeEntity.normalized_name == group.normalized_name))).scalar_one_or_none()
        row = existing or KnowledgeEntity(
            canonical_name=group.canonical_name, normalized_name=group.normalized_name,
            entity_type=group.entity_type, aliases=group.aliases, merged_by=group.merged_by,
            merge_confidence=group.merge_confidence,
            entity_merge_uncertain=group.entity_merge_uncertain, provider=provider)
        if existing is None:
            session.add(row)
            await session.flush()
        else:
            # 重建不是 append-only：同一个规范名的新别名与审计信息必须能更新，
            # 但不能因为模型少吐了一次别名就把已有的人审结果抹掉。
            row.aliases = sorted(set(row.aliases or []) | set(group.aliases))
            row.provider = provider
            if row.review_state != "passed":
                row.merged_by = group.merged_by
                row.merge_confidence = group.merge_confidence
                row.entity_merge_uncertain = group.entity_merge_uncertain
        entities[group.normalized_name] = row
        entities.update({normalize_entity_name(alias): row for alias in group.aliases})

    edge_count = 0
    for relation in graph.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        subject = entities.get(normalize_entity_name(str(relation.get("subject") or "")))
        object_ = entities.get(normalize_entity_name(str(relation.get("object") or "")))
        cited = [str(value) for value in relation.get("evidence_ids") or []
                 if str(value) in allowed]
        relation_provider = {
            **provider,
            # 人工把低置信 alias 拆开时，靠生成时的原始提及判断边属于哪一侧。
            "source_subject": str(relation.get("subject") or ""),
            "source_object": str(relation.get("object") or ""),
        }
        result = edge_result(
            subject_id=subject.id if subject else None,
            predicate=str(relation.get("predicate") or ""),
            object_id=object_.id if object_ else None, evidence_ids=cited,
            confidence=float(relation.get("confidence") or 0), provider=relation_provider)
        if result["status"] == "not_found":
            continue
        payload = result["edge"]
        edge = (await session.execute(select(GraphEdge).where(
            GraphEdge.subject_id == payload["subject_id"],
            GraphEdge.predicate == payload["predicate"],
            GraphEdge.object_id == payload["object_id"]))).scalar_one_or_none()
        if edge is None:
            edge = GraphEdge(**{key: payload[key] for key in (
                "subject_id", "predicate", "object_id", "confidence", "unsupported",
                "review_state", "provider")})
            session.add(edge)
            await session.flush()
        else:
            edge.confidence = payload["confidence"]
            edge.provider = relation_provider
            # 边的生成输入变了，旧的人审结论不能冒充对新结果的复核。
            edge.review_state = "unreviewed"
        await _replace_citations(session, "graph_edge", edge.id, cited, rows)
        edge.unsupported = not bool(cited)
        edge_count += 1

    outline = await _complete(http, OUTLINE_PROMPT, json.dumps({
        "entities": [group.canonical_name for group in groups]}, ensure_ascii=False))
    wiki_count = 0
    for item in outline.get("entries") or []:
        entity = entities.get(normalize_entity_name(str(item.get("entity") or "")))
        headings = [str(value).strip() for value in item.get("sections") or [] if str(value).strip()]
        if entity is None or not headings:
            continue
        written = await _complete(http, WRITE_PROMPT, json.dumps({
            "entity": entity.canonical_name, "outline": headings,
            "evidence": [{"evidence_id": row.id, "text": row.content} for row in rows],
        }, ensure_ascii=False))
        entry = (await session.execute(select(WikiEntry).where(
            WikiEntry.entity_id == entity.id))).scalar_one_or_none()
        if entry is None:
            entry = WikiEntry(entity_id=entity.id, title=entity.canonical_name,
                              outline=headings, provider=provider)
            session.add(entry)
            await session.flush()
        else:
            entry.outline, entry.provider = headings, provider
            old_sections = list((await session.execute(select(WikiSection.id).where(
                WikiSection.entry_id == entry.id))).scalars().all())
            old_sentences = list((await session.execute(select(WikiSentence.id).where(
                WikiSentence.section_id.in_(old_sections)))).scalars().all()) \
                if old_sections else []
            if old_sentences:
                await session.execute(delete(Citation).where(
                    Citation.source_kind == "wiki_sentence",
                    Citation.source_id.in_(old_sentences)))
                await session.execute(delete(WikiSentence).where(
                    WikiSentence.id.in_(old_sentences)))
            if old_sections:
                await session.execute(delete(WikiSection).where(
                    WikiSection.id.in_(old_sections)))
            await session.flush()
        for section_position, section_data in enumerate(written.get("sections") or []):
            section = WikiSection(entry_id=entry.id, position=section_position,
                                  heading=str(section_data.get("heading") or "未命名章节"))
            session.add(section)
            await session.flush()
            for position, sentence_data in enumerate(section_data.get("sentences") or []):
                cited = [str(value) for value in sentence_data.get("evidence_ids") or []
                         if str(value) in allowed]
                sentence = WikiSentence(
                    section_id=section.id, position=position,
                    text=str(sentence_data.get("text") or "").strip(),
                    unsupported=not bool(cited),
                    conflict_group=sentence_data.get("conflict_group"), provider=provider)
                if not sentence.text:
                    continue
                session.add(sentence)
                await session.flush()
                await _attach(session, "wiki_sentence", sentence.id, cited, rows)
        wiki_count += 1
    await session.flush()
    return {"status": "ok", "entities": len(groups), "edges": edge_count,
            "relation_status": "ok" if edge_count else "not_found",
            "wiki_entries": wiki_count}


async def _attach(session: AsyncSession, kind: str, source_id: str,
                  evidence_ids: list[str], rows: list[Evidence]) -> None:
    by_id = {row.id: row for row in rows}
    existing = set((await session.execute(select(Citation.evidence_id).where(
        Citation.source_kind == kind, Citation.source_id == source_id,
        Citation.role == "primary"))).scalars().all())
    for rank, evidence_id in enumerate(dict.fromkeys(evidence_ids)):
        evidence = by_id.get(evidence_id)
        if evidence is None or evidence_id in existing:
            continue
        session.add(Citation(
            evidence_id=evidence.id, source_kind=kind, source_id=source_id,
            role="primary", snippet=evidence.content[:500],
            content_digest=evidence.content_digest, rank=rank))


async def _replace_citations(session: AsyncSession, kind: str, source_id: str,
                             evidence_ids: list[str], rows: list[Evidence]) -> None:
    """用本轮模型给出的完整证据集替换旧连接，避免重建后残留假出处。"""
    await session.execute(delete(Citation).where(
        Citation.source_kind == kind, Citation.source_id == source_id,
        Citation.role == "primary"))
    await session.flush()
    await _attach(session, kind, source_id, evidence_ids, rows)
