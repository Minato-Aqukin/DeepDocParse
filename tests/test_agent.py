from ddp_core.agent import QueryDecision, assertions_from_text, gate_candidates
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
