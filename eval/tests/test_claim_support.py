"""Scorer unit tests (T47): synthetic annotations only, behaviour-true.

Every test builds a tiny in-memory claim set + annotation file(s) and checks the
scorer's observable behavior: rates, groups, honesty, kappa, disagreement lists.
No live stack, no fixtures from .dev-logs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_DIR))

from claims.score import (  # noqa: E402
    LABEL_VALUES,
    cohen_kappa,
    load_annotations,
    ledger_text,
    load_claim_set,
    score,
)


def _claim_set(tmp_path: Path, claims: list[dict]) -> Path:
    payload = {"claim_set_digest": "sha256:" + "0" * 64, "claims": claims}
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(payload))
    # Patch the digest so it verifies: compute real one.
    from claims.score import claim_set_digest

    payload["claim_set_digest"] = claim_set_digest(claims)
    path.write_text(json.dumps(payload))
    return path


def _ann_file(tmp_path: Path, name: str, labels: dict, conflicts: dict | None = None,
              digest: str = "", annotator: str = "a1") -> Path:
    payload = {"annotator": annotator, "claim_set_digest": digest,
               "labels": [{"claim_id": k, "label": v} for k, v in labels.items()],
               "conflict_reports": [{"question_id": k, "reported": v}
                                    for k, v in (conflicts or {}).items()]}
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def _claims() -> list[dict]:
    return [
        {"claim_id": "c1", "question_id": "q1", "group": "single",
         "claim_text": "t1", "evidence_refs": ["e1"],
         "excerpts": [{"evidence_id": "e1", "excerpt": "x", "origin": "A",
                       "page": 1, "resource": "r"}]},
        {"claim_id": "c2", "question_id": "q1", "group": "single",
         "claim_text": "t2", "evidence_refs": ["e2"],
         "excerpts": [{"evidence_id": "e2", "excerpt": "y", "origin": "A",
                       "page": 1, "resource": "r"}]},
        {"claim_id": "c3", "question_id": "q2", "group": "contradiction",
         "claim_text": "t3", "evidence_refs": ["e3"],
         "excerpts": [{"evidence_id": "e3", "excerpt": "z", "origin": "B",
                       "page": 1, "resource": "r2"}],
         "expects_conflict": True},
    ]


def test_support_rate_counts_full_and_half_weights(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "partially_supports", "c3": "supports"},
                    {"q2": True}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    assert result["n_claims"] == 3
    assert result["n_judged"] == 3
    assert result["support_rate"] == pytest.approx((1 + 0.5 + 1) / 3)
    assert result["full_support_rate"] == pytest.approx(2 / 3)
    assert result["label_counts"] == {"supports": 2, "partially_supports": 1,
                                      "does_not_support": 0, "contradicted": 0,
                                      "cannot_tell": 0}


def test_per_group_rates_split_single_and_contradiction(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "does_not_support", "c3": "supports"},
                    {"q2": True}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    assert result["groups"]["single"]["support_rate"] == pytest.approx(0.5)
    assert result["groups"]["contradiction"]["support_rate"] == pytest.approx(1.0)
    assert result["groups"]["single"]["n"] == 2


def test_contradicted_label_scores_zero_and_lists_unsupported(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "contradicted", "c2": "cannot_tell", "c3": "supports"},
                    {"q2": False}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    # contradicted=0, cannot_tell excluded from denominator
    assert result["support_rate"] == pytest.approx((0 + 1) / 2)
    assert result["n_judged"] == 2
    assert [u["claim_id"] for u in result["unsupported_claims"]] == ["c1"]
    assert result["conflict_honesty"] == pytest.approx(0.0)


def test_conflict_honesty_counts_only_expected_questions(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "supports", "c3": "supports"},
                    {"q1": True, "q2": True}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    # q1 does not expect a conflict; extra report must not inflate honesty
    assert result["conflict_honesty"] == pytest.approx(1.0)
    assert result["conflict_expected"] == ["q2"]


def test_kappa_perfect_agreement_is_one_and_disputes_empty(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    labels = {"c1": "supports", "c2": "supports", "c3": "does_not_support"}
    a1 = _ann_file(tmp_path, "a1.json", labels, {"q2": True}, digest, "a1")
    a2 = _ann_file(tmp_path, "a2.json", labels, {"q2": True}, digest, "a2")
    result = score(load_claim_set(cs), [load_annotations(a1), load_annotations(a2)])
    assert result["kappa"] == pytest.approx(1.0)
    assert result["disagreements"] == []
    assert result["n_annotators"] == 2


def test_kappa_partial_and_disagreement_lists_claim(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    # Hand example: a=3x supports, b=2x supports + 1x does_not.
    # obs=2/3, exp=1*2/3+0*1/3=2/3 -> kappa exactly 0.0.
    labels1 = {"c1": "supports", "c2": "supports", "c3": "supports"}
    labels2 = {"c1": "supports", "c2": "does_not_support", "c3": "supports"}
    assert cohen_kappa(labels1, labels2) == pytest.approx(0.0)
    a1 = _ann_file(tmp_path, "a1.json", labels1, {"q2": True}, digest, "a1")
    a2 = _ann_file(tmp_path, "a2.json", labels2, {"q2": True}, digest, "a2")
    result = score(load_claim_set(cs), [load_annotations(a1), load_annotations(a2)])
    assert result["kappa"] == pytest.approx(0.0)
    assert len(result["disagreements"]) == 1
    dispute = result["disagreements"][0]
    assert dispute["claim_id"] == "c2"
    assert dispute["a1"] == "supports"
    assert dispute["a2"] == "does_not_support"


def test_missing_claim_id_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "supports"}, {"q2": True}, digest)
    with pytest.raises(ValueError, match="missing .*c3"):
        score(load_claim_set(cs), [load_annotations(ann)])


def test_unanswered_conflict_honesty_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "supports", "c3": "supports"},
                    {}, digest)
    with pytest.raises(ValueError, match="[Uu]nanswered.*q2"):
        score(load_claim_set(cs), [load_annotations(ann)])


def test_incomplete_file_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    payload = {"annotator": "a1", "claim_set_digest": digest,
               "labels": [{"claim_id": "c1", "label": "supports"},
                          {"claim_id": "c2", "label": "supports"},
                          {"claim_id": "c3", "label": "supports"}],
               "conflict_reports": [{"question_id": "q2", "reported": True}],
               "complete": False, "n_judged": 2, "n_claims": 3}
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="[Ii]ncomplete"):
        score(load_claim_set(cs), [load_annotations(path)])


def test_degraded_claim_is_reported_not_dropped(tmp_path):
    claims = _claims() + [{"claim_id": "c4", "question_id": "q3", "group": "single",
                           "claim_text": "[degraded] no evidence",
                           "evidence_refs": [], "excerpts": [],
                           "degraded": True, "degraded_reason": "insufficient_evidence"}]
    cs = _claim_set(tmp_path, claims)
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "supports", "c3": "supports",
                     "c4": "does_not_support"},
                    {"q2": True}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    assert result["n_claims"] == 4
    assert [u["claim_id"] for u in result["unsupported_claims"]] == ["c4"]
    assert result["per_claim"][3]["claim_id"] == "c4"


def test_two_annotator_tie_breaks_to_first_annotator(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    a1 = _ann_file(tmp_path, "a1.json",
                   {"c1": "supports", "c2": "supports", "c3": "supports"},
                   {"q2": True}, digest, "a1")
    a2 = _ann_file(tmp_path, "a2.json",
                   {"c1": "does_not_support", "c2": "supports", "c3": "supports"},
                   {"q2": True}, digest, "a2")
    first = score(load_claim_set(cs), [load_annotations(a1), load_annotations(a2)])
    swapped = score(load_claim_set(cs), [load_annotations(a2), load_annotations(a1)])
    by_id = {p["claim_id"]: p for p in first["per_claim"]}
    assert by_id["c1"]["label"] == "supports"
    assert by_id["c1"]["votes"] == ["supports", "does_not_support"]
    by_swapped = {p["claim_id"]: p for p in swapped["per_claim"]}
    assert by_swapped["c1"]["label"] == "does_not_support"


def test_single_annotator_has_no_kappa(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json",
                    {"c1": "supports", "c2": "supports", "c3": "supports"},
                    {"q2": True}, digest)
    result = score(load_claim_set(cs), [load_annotations(ann)])
    assert result["kappa"] is None
    assert result["n_annotators"] == 1


def test_digest_mismatch_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    ann = _ann_file(tmp_path, "a.json", {"c1": "supports"}, {}, "sha256:" + "f" * 64)
    with pytest.raises(ValueError, match="[Dd]igest"):
        score(load_claim_set(cs), [load_annotations(ann)])


def test_unknown_claim_id_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json", {"nope": "supports"}, {}, digest)
    with pytest.raises(ValueError, match="unknown claim"):
        score(load_claim_set(cs), [load_annotations(ann)])


def test_invalid_label_is_rejected(tmp_path):
    cs = _claim_set(tmp_path, _claims())
    digest = json.loads(cs.read_text())["claim_set_digest"]
    ann = _ann_file(tmp_path, "a.json", {"c1": "looks_good"}, {}, digest)
    with pytest.raises(ValueError, match="[Ll]abel"):
        load_annotations(ann)


def test_label_values_cover_five_buttons():
    assert sorted(LABEL_VALUES) == sorted(["supports", "partially_supports",
                                           "does_not_support", "contradicted",
                                           "cannot_tell"])


def test_model_annotators_are_never_reported_as_human_review(tmp_path):
    claims = _claims()
    cs = load_claim_set(_claim_set(tmp_path, claims))
    labels = {"c1": "supports", "c2": "supports", "c3": "supports"}
    human = load_annotations(_ann_file(tmp_path, "h.json", labels, {"q2": True},
                                       digest=cs["claim_set_digest"], annotator="h1"))
    model_path = tmp_path / "m.json"
    payload = json.loads(_ann_file(tmp_path, "m0.json", labels, {"q2": True},
                                   digest=cs["claim_set_digest"], annotator="m1").read_text())
    payload["annotator_kind"] = "model"
    model_path.write_text(json.dumps(payload))
    model = load_annotations(model_path)

    only_human = score(cs, [human])
    assert only_human["human_review"] is True
    assert ledger_text(only_human, "x.json").startswith("T47人工主张支持度")

    mixed = score(cs, [human, model])
    assert mixed["human_review"] is False
    assert mixed["annotator_kinds"] == {"h1": "human", "m1": "model"}
    assert "模型评审，非人工" in ledger_text(mixed, "x.json")
    assert "T47人工主张支持度" not in ledger_text(mixed, "x.json")

    payload["annotator_kind"] = "robot"
    model_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="annotator_kind"):
        load_annotations(model_path)
