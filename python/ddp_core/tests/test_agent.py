from ddp_core.agent import (
    ConflictMarkupError, QueryDecision, assertions_from_text, conflicts_from_text, gate_candidates,
)
import pytest


@pytest.mark.parametrize("text", ["The answer is 42 [1].", "总数为125件[1]。", "数值为42[1]！", "The answer is 42 [1] ."])
def test_citation_before_sentence_terminator_does_not_invent_unsupported_punctuation(text):
    result = assertions_from_text(text, ["ev-1"])
    assert len(result) == 1
    assert result[0]["evidence_ids"] == ["ev-1"]
    assert result[0]["unsupported"] is False


@pytest.mark.parametrize("text", ["The answer is 42 [1]. This second claim has no evidence.",
                                 "总数为125件[1]。下一句没有出处。",
                                 "数值为42[1][2]。下一句没有出处。"])
def test_citation_before_terminator_does_not_support_the_next_unreferenced_sentence(text):
    result = assertions_from_text(text, ["ev-1", "ev-2"])
    assert len(result) == 2
    assert result[0]["unsupported"] is False
    assert result[1]["evidence_ids"] == []
    assert result[1]["unsupported"] is True
from ddp_core.hits import Hit


def _hit(doc: str, evidence: str, similarity: float | None) -> Hit:
    return Hit(
        chunk_id=f"chunk-{evidence}", document_id=doc, parse_job_id="job", seq=0,
        page_idx=0, bbox=[0, 0, 1, 1], page_size=[10, 10], text=evidence,
        derived_text=None, evidence_id=evidence, derived_evidence_id=None,
        block_type="text", table_html=None, score=0.03, similarity=similarity)


def test_no_retrieval_without_inherited_evidence_is_forced_to_refusal():
    decision = QueryDecision(need_retrieval=False, reason="rewrite")
    assert decision.degraded == "no_evidence_in_turn"


def test_gate_keeps_rejected_documents_and_reasons():
    hits = [_hit("good", "e1", 0.8), _hit("good", "e2", 0.2),
            _hit("bad", "e3", 0.44)]
    accepted, decisions = gate_candidates(
        hits, min_similarity=0.45, vector_available=True)
    assert [h["evidence_id"] for h in accepted] == ["e1", "e2"]
    assert [d.accepted for d in decisions] == [True, True, False]
    assert decisions[-1].reason == "document_below_similarity"


def test_keyword_only_gate_is_visible_but_does_not_invent_similarity():
    accepted, decisions = gate_candidates(
        [_hit("doc", "e1", None)], min_similarity=0.45, vector_available=False)
    assert accepted and decisions[0].reason == "keyword_only_no_similarity"


def test_assertions_force_missing_and_out_of_range_references_unsupported():
    result = assertions_from_text(
        "额定电压是 220 V。[1]\n额定电流未知。[9]\n需要人工确认。", ["ev-1"])
    assert result == [
        {"position": 0, "text": "额定电压是 220 V。", "evidence_ids": ["ev-1"],
         "unsupported": False},
        {"position": 1, "text": "额定电流未知。", "evidence_ids": [],
         "unsupported": True},
        {"position": 2, "text": "需要人工确认。", "evidence_ids": [],
         "unsupported": True},
    ]


def test_reference_after_sentence_whitespace_stays_with_that_assertion():
    result = assertions_from_text("额定电压是 220 V。 [1] 下一句无出处。", ["ev-1"])
    assert result[0]["evidence_ids"] == ["ev-1"]
    assert result[0]["text"] == "额定电压是 220 V。"
    assert result[1]["unsupported"] is True


def test_chinese_sentences_without_whitespace_never_share_evidence():
    result = assertions_from_text("结论甲。[1]结论乙。[9]", ["ev-1"])
    assert result == [
        {"position": 0, "text": "结论甲。", "evidence_ids": ["ev-1"],
         "unsupported": False},
        {"position": 1, "text": "结论乙。", "evidence_ids": [],
         "unsupported": True},
    ]


def test_multiple_references_stay_with_the_same_sentence():
    result = assertions_from_text("联合结论。[1][2]下一句。", ["ev-1", "ev-2"])
    assert result[0]["evidence_ids"] == ["ev-1", "ev-2"]
    assert result[1]["unsupported"] is True


# ---------------------------------------------------- 生成时的矛盾标注（§7.6）

def test_conflict_lines_are_lifted_out_before_assertions():
    ids = ["ev-a", "ev-b", "ev-c"]
    text = ("PM-2 的最大输入电压一处写 240 V。[1]\n另一处写 120 V。[2]\n"
            "CONFLICT: [1] [2]\nCONFLICT: [3] vs [1]")
    body, groups = conflicts_from_text(text, ids)
    assert "CONFLICT" not in body
    assert groups == [["ev-a", "ev-b"], ["ev-c", "ev-a"]]
    claims = assertions_from_text(body, ids)
    assert [claim["evidence_ids"] for claim in claims] == [["ev-a"], ["ev-b"]], \
        "标注行不能变成一条有两个引用支撑的主张"


@pytest.mark.parametrize("line", [
    "Conflict: [1] [2]",            # 小模型不照大小写写
    "conflict：[1] 与 [2]",          # 全角冒号 + 中文连接词
    "- CONFLICT: [1] 和 [2]",        # 列表符号开头
    "* CONFLICT: [2], [1]",
])
def test_conflict_marker_variants_never_become_a_two_citation_claim(line):
    ids = ["ev-a", "ev-b"]
    body, groups = conflicts_from_text("一处写 240 V。[1]\n另一处写 120 V。[2]\n" + line, ids)
    assert sorted(groups[0]) == ["ev-a", "ev-b"] and len(groups) == 1
    claims = assertions_from_text(body, ids)
    assert [claim["evidence_ids"] for claim in claims] == [["ev-a"], ["ev-b"]], \
        "标注行的变体没摘掉，就会变成一条有两个引用支撑的主张"


def test_text_without_conflict_lines_passes_through_unchanged():
    for text in ("A conflict of interest was declared. [1]",
                 "Conflict of interest: none declared. [1]"):
        body, groups = conflicts_from_text(text, ["ev-a"])
        assert groups == [] and body == text


@pytest.mark.parametrize("line", [
    "CONFLICT: [1] [9]",            # 越界引用
    "CONFLICT: [1] [1]",            # 只有一条不同证据
    "CONFLICT: [1]",                # 不足两条
    "CONFLICT: [1] [2] the voltage values",   # 夹带正文
    "CONFLICT:",                    # 空标注
])
def test_unverifiable_conflict_lines_are_rejected_not_dropped(line):
    with pytest.raises(ConflictMarkupError):
        conflicts_from_text("Value is 240 V. [1]\n" + line, ["ev-a", "ev-b"])
