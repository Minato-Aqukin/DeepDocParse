"""T47 claim-support scorer: human annotations -> support-rate report.

Inputs:
  claim set   .dev-logs/t47/claims.json  (digest-pinned, frozen after the run)
  annotations one or two exported files from annotate.html
                {"annotator","claim_set_digest","labels":[{"claim_id","label",...}],
                 "conflict_reports":[{"question_id","reported",...}]}

Labels: supports=1, partially_supports=0.5 (reported separately),
does_not_support=0, contradicted=0, cannot_tell=excluded from denominator.

Outputs: docs/refactor/artifacts/claim-support-review-<date>.json + stdout
ledger text. Exit non-zero on digest mismatch / unknown ids / bad labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

LABEL_VALUES = ("supports", "partially_supports", "does_not_support",
                "contradicted", "cannot_tell")
ANNOTATOR_KINDS = ("human", "model")
LABEL_WEIGHT = {"supports": 1.0, "partially_supports": 0.5,
                "does_not_support": 0.0, "contradicted": 0.0,
                "cannot_tell": None}


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def claim_set_digest(claims: list[dict]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(claims)).hexdigest()


def load_claim_set(path: Path) -> dict:
    payload = json.loads(path.read_text())
    claims = payload["claims"]
    actual = claim_set_digest(claims)
    if payload.get("claim_set_digest") != actual:
        raise ValueError(f"claim set digest mismatch in {path}: "
                         f"file says {payload.get('claim_set_digest')}, actual {actual}")
    return payload


def load_annotations(path: Path) -> dict:
    payload = json.loads(path.read_text())
    labels = {}
    for entry in payload.get("labels", []):
        label = entry.get("label")
        if label not in LABEL_VALUES:
            raise ValueError(f"bad label {label!r} for claim {entry.get('claim_id')!r} "
                             f"in {path}")
        labels[entry["claim_id"]] = label
    conflicts = {entry["question_id"]: bool(entry.get("reported"))
                 for entry in payload.get("conflict_reports", [])}
    kind = payload.get("annotator_kind", "human")
    if kind not in ANNOTATOR_KINDS:
        raise ValueError(f"bad annotator_kind {kind!r} in {path} (expected one of {ANNOTATOR_KINDS})")
    return {"annotator": payload.get("annotator", path.stem),
            "annotator_kind": kind,
            "claim_set_digest": payload.get("claim_set_digest", ""),
            "labels": labels, "conflicts": conflicts,
            "complete": payload.get("complete", True),
            "n_judged": payload.get("n_judged"),
            "n_claims": payload.get("n_claims")}


def cohen_kappa(a: dict[str, str], b: dict[str, str]) -> float | None:
    common = sorted(set(a) & set(b))
    if not common:
        return None
    categories = sorted(set(a[c] for c in common) | set(b[c] for c in common))
    if len(categories) < 2:
        return 1.0 if all(a[c] == b[c] for c in common) else 0.0
    observed = sum(1 for c in common if a[c] == b[c]) / len(common)
    expected = sum((sum(1 for c in common if a[c] == k) / len(common))
                   * (sum(1 for c in common if b[c] == k) / len(common))
                   for k in categories)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def score(claim_set: dict, annotations: list[dict]) -> dict:
    claims = claim_set["claims"]
    by_id = {c["claim_id"]: c for c in claims}
    digest = claim_set.get("claim_set_digest", "")
    for ann in annotations:
        if ann["claim_set_digest"] != digest:
            raise ValueError(f"annotator {ann['annotator']!r}: claim-set digest mismatch: "
                             f"annotation targets {ann['claim_set_digest']!r}, expected {digest!r}")
        unknown = sorted(set(ann["labels"]) - set(by_id))
        if unknown:
            raise ValueError(f"annotator {ann['annotator']!r} labels unknown claim(s): "
                             + ", ".join(unknown))
        missing = sorted(set(by_id) - set(ann["labels"]))
        if missing:
            raise ValueError(f"annotator {ann['annotator']!r} missing {len(missing)} claim(s): "
                             + ", ".join(missing))
        if not ann.get("complete", True):
            raise ValueError(f"annotator {ann['annotator']!r}: file marked incomplete "
                             f"(n_judged={ann.get('n_judged')}/{ann.get('n_claims')}); "
                             f"finish annotating before scoring")

    expected = sorted({c["question_id"] for c in claims if c.get("expects_conflict")})
    for ann in annotations:
        unanswered = sorted(set(expected) - set(ann["conflicts"]))
        if unanswered:
            raise ValueError(f"annotator {ann['annotator']!r} left "
                             f"{len(unanswered)} conflict-honesty question(s) unanswered: "
                             + ", ".join(unanswered))

    # Majority / first-annotator adjudication for headline numbers; every
    # annotator's raw labels are preserved in the artifact. Ties break toward
    # the earliest annotator's vote so repeated runs agree bit-for-bit.
    per_claim: dict[str, dict] = {}
    for claim in claims:
        votes = [ann["labels"].get(claim["claim_id"]) for ann in annotations]
        votes = [v for v in votes if v is not None]
        label = (max(sorted(set(votes)), key=lambda v: (votes.count(v), -votes.index(v)))
                 if votes else None)
        per_claim[claim["claim_id"]] = {"votes": votes, "label": label}

    def rate(ids: list[str]) -> dict:
        judged = [i for i in ids if (per_claim[i]["label"] or "cannot_tell")
                  != "cannot_tell" and per_claim[i]["label"] is not None]
        weights = [LABEL_WEIGHT[per_claim[i]["label"]] for i in judged]
        full = sum(1 for w in weights if w == 1.0)
        return {"n": len(ids), "n_judged": len(judged),
                "support_rate": (sum(weights) / len(judged)) if judged else None,
                "full_support_rate": (full / len(judged)) if judged else None}
    groups: dict[str, list[str]] = {}
    for claim in claims:
        groups.setdefault(claim.get("group", "ungrouped"), []).append(claim["claim_id"])

    group_rates = {name: rate(ids) for name, ids in groups.items()}
    overall = rate([c["claim_id"] for c in claims])
    label_counts = {label: 0 for label in LABEL_VALUES}
    for info in per_claim.values():
        if info["label"] is not None:
            label_counts[info["label"]] += 1

    unsupported = sorted(i for i, info in per_claim.items()
                         if info["label"] in ("does_not_support", "contradicted"))

    honesty_votes: list[bool] = []
    for ann in annotations:
        honesty_votes.extend(bool(ann["conflicts"][q]) for q in expected)
    honesty = (sum(honesty_votes) / len(honesty_votes)) if honesty_votes else None

    disagreements = []
    kappa = None
    if len(annotations) == 2:
        a, b = annotations[0]["labels"], annotations[1]["labels"]
        kappa = cohen_kappa(a, b)
        for cid in sorted(set(a) & set(b)):
            if a[cid] != b[cid]:
                disagreements.append({"claim_id": cid, "question_id": by_id[cid]["question_id"],
                                      "claim_text": by_id[cid]["claim_text"],
                                      annotations[0]["annotator"]: a[cid],
                                      annotations[1]["annotator"]: b[cid]})

    questions = sorted({c["question_id"] for c in claims},
                       key=lambda q: next(c.get("question_no", 0)
                                          for c in claims if c["question_id"] == q))
    return {
        "claim_set_digest": digest,
        "n_claims": len(claims), "n_questions": len(questions),
        "n_annotators": len(annotations),
        "annotators": [a["annotator"] for a in annotations],
        "annotator_kinds": {a["annotator"]: a.get("annotator_kind", "human") for a in annotations},
        # Plan §14.3 asks for human review; a model judge is reported, never relabelled as human.
        "human_review": all(a.get("annotator_kind", "human") == "human" for a in annotations),
        "n_judged": overall["n_judged"],
        "support_rate": overall["support_rate"],
        "full_support_rate": overall["full_support_rate"],
        "label_counts": label_counts,
        "groups": group_rates,
        "conflict_expected": expected,
        "conflict_honesty": honesty,
        "kappa": kappa,
        "disagreements": disagreements,
        "unsupported_claims": [
            {"claim_id": cid, "question_id": by_id[cid]["question_id"],
             "claim_text": by_id[cid]["claim_text"],
             "label": per_claim[cid]["label"],
             "evidence_refs": by_id[cid]["evidence_refs"]} for cid in unsupported],
        "per_claim": [{"claim_id": c["claim_id"], "question_id": c["question_id"],
                       "group": c.get("group"), "label": per_claim[c["claim_id"]]["label"],
                       "votes": per_claim[c["claim_id"]]["votes"]} for c in claims],
    }


def ledger_text(result: dict, artifact_name: str) -> str:
    def pct(value) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}%"

    groups = ", ".join(f"{name} {pct(info['support_rate'])} (n={info['n_judged']}/{info['n']})"
                       for name, info in sorted(result["groups"].items()))
    kappa = "n/a (single annotator)" if result["kappa"] is None else f"{result['kappa']:.2f}"
    review = ("T47人工主张支持度" if result.get("human_review", True)
              else "T47主张支持度（模型评审，非人工；判据要求的人工评审仍缺）")
    return (
        f"{review}（`{artifact_name}`，{result['n_annotators']} 标注人 "
        f"{'/'.join(result['annotators'])}，claim set `{result['claim_set_digest'][:19]}…`）："
        f"支持度 {pct(result['support_rate'])}（部分支持计 0.5；完全支持 "
        f"{pct(result['full_support_rate'])}；n={result['n_judged']}/{result['n_claims']}，无法判断已剔除）；分组：{groups}；"
        f"矛盾报告诚实度 {pct(result['conflict_honesty'])}"
        f"（{len(result['conflict_expected'])} 个矛盾题）；"
        f"Cohen's κ={kappa}；分歧 {len(result['disagreements'])} 条，"
        f"无支持 {len(result['unsupported_claims'])} 条（见产物附表）。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Score T47 claim-support annotations.")
    parser.add_argument("claim_set", help="frozen claim set JSON (.dev-logs/t47/claims.json)")
    parser.add_argument("annotations", nargs="+", help="1-2 exported annotation JSON files")
    parser.add_argument("--out-dir", default="docs/refactor/artifacts",
                        help="artifact directory (default: docs/refactor/artifacts)")
    args = parser.parse_args()
    if len(args.annotations) > 2:
        parser.error("at most two annotation files (two annotators)")
    claim_set = load_claim_set(Path(args.claim_set))
    annotations = [load_annotations(Path(p)) for p in args.annotations]
    result = score(claim_set, annotations)
    result["sources"] = {"claim_set": args.claim_set,
                         "annotations": list(args.annotations)}
    name = f"claim-support-review-{date.today().isoformat()}.json"
    out = Path(args.out_dir) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print(ledger_text(result, f"artifacts/{name}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
