"""DDP-Graph v1 的纯函数边界：规范化、合并、边门控与邻域遍历。"""
from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field

GRAPH_VERSION = "ddp-graph/1"
MERGE_UNCERTAIN_THRESHOLD = 0.86
_SEPARATORS = re.compile(r"[\s\-_./·•:：]+")


def normalize_entity_name(value: str) -> str:
    """只做确定性的 Unicode/分隔符归一化，不擅自做语义合并。"""
    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return _SEPARATORS.sub("", normalized)


@dataclass(frozen=True)
class EntityMention:
    name: str
    entity_type: str = "other"
    aliases: tuple[str, ...] = ()
    merged_by: str = "alias"
    merge_confidence: float = 1.0


@dataclass
class EntityGroup:
    canonical_name: str
    normalized_name: str
    entity_type: str
    aliases: list[str] = field(default_factory=list)
    merged_by: str = "none"
    merge_confidence: float = 1.0
    entity_merge_uncertain: bool = False


def merge_mentions(mentions: list[EntityMention]) -> list[EntityGroup]:
    """只自动合并精确规范名/显式 alias；其余相似项保留为低置信独立节点。"""
    groups: list[EntityGroup] = []
    by_key: dict[str, EntityGroup] = {}
    alias_owner: dict[str, EntityGroup] = {}
    for mention in mentions:
        key = normalize_entity_name(mention.name)
        if not key:
            continue
        aliases = [alias for alias in mention.aliases if normalize_entity_name(alias)]
        group = by_key.get(key) or alias_owner.get(key)
        if group is None:
            group = EntityGroup(mention.name.strip(), key, mention.entity_type,
                                aliases=list(dict.fromkeys(aliases)),
                                merged_by=mention.merged_by if aliases else "none",
                                merge_confidence=max(0.0, min(1.0, mention.merge_confidence)),
                                entity_merge_uncertain=bool(
                                    aliases and mention.merge_confidence < MERGE_UNCERTAIN_THRESHOLD))
            groups.append(group)
            by_key[key] = group
        else:
            if group.merged_by != "model":
                group.merged_by = "exact" if key == group.normalized_name else "alias"
            group.aliases = list(dict.fromkeys([
                *group.aliases, mention.name.strip(), *aliases]))
        for alias in aliases:
            alias_owner.setdefault(normalize_entity_name(alias), group)
    return groups


def edge_result(*, subject_id: str | None, predicate: str,
                object_id: str | None, evidence_ids: list[str], confidence: float,
                provider: dict) -> dict:
    """负样本给结构化 not_found；无证据永远不能伪装成有效边。"""
    evidence_ids = list(dict.fromkeys(value for value in evidence_ids if value))
    if not subject_id or not object_id or not predicate.strip():
        return {"status": "not_found", "edge": None}
    return {"status": "ok", "edge": {
        "subject_id": subject_id, "predicate": predicate.strip(), "object_id": object_id,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence_ids": evidence_ids, "unsupported": not bool(evidence_ids),
        "review_state": "unreviewed", "provider": provider,
    }}


def wiki_sentence(*, text: str, evidence_ids: list[str],
                  conflict_group: str | None = None, provider: dict | None = None) -> dict:
    evidence_ids = list(dict.fromkeys(value for value in evidence_ids if value))
    return {"text": text.strip(), "evidence_ids": evidence_ids,
            "unsupported": not bool(evidence_ids), "conflict_group": conflict_group,
            "review_state": "unreviewed", "provider": provider or {}}


def neighbor_ids(center: str, edges: list[tuple[str, str]], depth: int) -> set[str]:
    """无向展示邻域；边本身仍保留 DDP-Graph 的有向语义。"""
    if not 1 <= depth <= 3:
        raise ValueError("depth must be between 1 and 3")
    adjacent: dict[str, set[str]] = {}
    for left, right in edges:
        adjacent.setdefault(left, set()).add(right)
        adjacent.setdefault(right, set()).add(left)
    seen = {center}
    queue = deque([(center, 0)])
    while queue:
        node, level = queue.popleft()
        if level == depth:
            continue
        for neighbor in sorted(adjacent.get(node, ())):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, level + 1))
    return seen
