"""DDP-Agent v1 的纯函数：回答断言化与逐篇候选门控。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ddp_core.hits import Hit


_REFERENCE = re.compile(r"\[(\d+)]")
# 中文生成文本经常完全没有句间空白；只按 `标点 + 空白` 切会把
# `结论甲。[1]结论乙。[9]` 合成一条，让甲的证据暗中支持乙。
#
# 引用必须留在前一句：标点后若紧跟 `[n]` 不切，等最后一段连续引用结束再切。
# 英文句点只在有空白时切，避免把 3.14 / v1.2 拆开。
_SENTENCE = re.compile(
    r"(?<=[。！？；;!?])\s*(?!\[\d+])"
    r"|(?<=[.])\s+(?!\[\d+])"
    r"|(?<=\])\s*(?=(?!\[\d+])\S)"
    r"|\n+"
)
_REFERENCE_GAP = re.compile(r"([。！？!?；;])\s+(?=\[\d+])")


@dataclass(frozen=True)
class QueryDecision:
    need_retrieval: bool
    reason: str
    inherited_evidence_ids: list[str] = field(default_factory=list)
    degraded: str | None = None

    def __post_init__(self) -> None:
        # “不检索但没证据”只能走拒答，不能让调用方误认为可用模型常识回答。
        if not self.need_retrieval and not self.inherited_evidence_ids:
            object.__setattr__(self, "degraded", "no_evidence_in_turn")


@dataclass(frozen=True)
class CandidateDecision:
    hit: Hit
    rank: int
    accepted: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "evidence_id": self.hit.get("derived_evidence_id")
            or self.hit.get("evidence_id"),
            "document_id": self.hit["document_id"],
            "chunk_id": self.hit["chunk_id"],
            "rank": self.rank,
            "score": self.hit.get("score"),
            "similarity": self.hit.get("similarity"),
            "accepted": self.accepted,
            "reason": self.reason,
        }


def gate_candidates(hits: list[Hit], *, min_similarity: float,
                    vector_available: bool) -> tuple[list[Hit], list[CandidateDecision]]:
    """逐篇门控并保留全部决定。

    向量可用时用每篇 top similarity 判断整篇是否进入回答；向量不可用时没有可校准
    尺子，关键词候选可以进入，但上层必须保留 embedding_unavailable 降级。
    """
    best: dict[str, float] = {}
    if vector_available:
        for hit in hits:
            similarity = hit.get("similarity")
            if similarity is not None:
                best[hit["document_id"]] = max(best.get(hit["document_id"], -1.0), similarity)

    accepted: list[Hit] = []
    decisions: list[CandidateDecision] = []
    for rank, hit in enumerate(hits):
        if not vector_available:
            keep, reason = True, "keyword_only_no_similarity"
        elif best.get(hit["document_id"], -1.0) > min_similarity:
            keep, reason = True, "document_gate_passed"
        else:
            keep, reason = False, "document_below_similarity"
        decisions.append(CandidateDecision(hit=hit, rank=rank, accepted=keep, reason=reason))
        if keep:
            accepted.append(hit)
    return accepted, decisions


def assertions_from_text(text: str, evidence_ids: list[str]) -> list[dict]:
    """把模型文本投影成 Assertion[]；无有效引用的句子强制 unsupported。"""
    # 模型常输出“结论。 [1]”：句号与引用之间的空格不是断言边界，先收紧；
    # 引用结束才是下一句边界。否则 [1] 会被切成一条空断言，支持关系全丢。
    normalized = _REFERENCE_GAP.sub(r"\1", text.strip())
    parts = [part.strip() for part in _SENTENCE.split(normalized) if part.strip()]
    if not parts and text.strip():
        parts = [text.strip()]
    assertions: list[dict] = []
    for position, raw in enumerate(parts):
        refs = []
        for value in _REFERENCE.findall(raw):
            index = int(value) - 1
            if 0 <= index < len(evidence_ids):
                evidence_id = evidence_ids[index]
                if evidence_id and evidence_id not in refs:
                    refs.append(evidence_id)
        cleaned = _REFERENCE.sub("", raw).strip()
        if not cleaned:
            continue
        assertions.append({
            "position": position,
            "text": cleaned,
            "evidence_ids": refs,
            "unsupported": not bool(refs),
        })
    return assertions
