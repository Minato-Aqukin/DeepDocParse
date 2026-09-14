"""报告组装：按问题类别与搜索模式分轴统计，不合成单一"质量分"。

计划 §7.4/§14.3 要求把**范围枚举、检索覆盖、证据召回、证据充分性、诚实的
成本**分开报告；这里严格遵守：没有 `score`、没有加权总分，只有逐轴计数与
比率，以及 fast 相对穷查的相对召回（分母为 0 记"不适用"）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .harness import INVARIANTS

REPORT_SCHEMA = "ddp-routing-eval/1#Report"

#: 诚实地报成"未测量"的东西。夹具里测不出来的，不许在别处悄悄变成数字。
NOT_MEASURED = (
    "absolute retrieval quality: the executor is a fixture oracle, not a real index",
    "egress bytes: P5 coordinator records used bytes=0 (needs data-plane counters)",
    "answer generation / citation validity / claim support: no model is called",
    "latency and concurrency: the run is a single-process deterministic pass",
    "recursive federation (A→P→R), path cycles and bounded caches: out of this fixture",
    "multi-organization isolation and real peer authentication: single-org fixture",
)


def _value(hits: int, total: int):
    return None if total == 0 else hits / total


def _question_hits(run: dict) -> tuple[int, int]:
    returns = run["absolute_recall"]
    return returns["hits"], returns["required"]


def _mode_group(runs: list[dict]) -> dict:
    recall_hits = sum(_question_hits(run)[0] for run in runs)
    recall_required = sum(_question_hits(run)[1] for run in runs)
    per_question = [run["absolute_recall"]["value"] for run in runs
                    if run["absolute_recall"]["value"] is not None]
    completeness: dict[str, int] = {}
    sufficiency: dict[str, int] = {}
    counts = {"total_targets": 0, "applicable_targets": 0, "succeeded": 0,
              "excluded": 0, "incomplete": 0}
    for run in runs:
        completeness[run["retrieval_completeness"]] = \
            completeness.get(run["retrieval_completeness"], 0) + 1
        sufficiency[run["evidence_sufficiency"]] = \
            sufficiency.get(run["evidence_sufficiency"], 0) + 1
        for key, value in run["counts"].items():
            counts[key] += value
    return {
        "questions": len(runs),
        "questions_with_required_evidence": sum(
            1 for run in runs if run["absolute_recall"]["required"]),
        "absolute_recall": {
            "hits": recall_hits, "required": recall_required,
            "micro": _value(recall_hits, recall_required),
            "macro_mean": (sum(per_question) / len(per_question)) if per_question else None,
            "not_applicable": sum(1 for run in runs
                                  if run["absolute_recall"]["value"] is None),
        },
        "targets_planned": sum(len(run["targets"]["targets_planned"]) for run in runs),
        "targets_probed": sum(len(run["targets"]["targets_probed"]) for run in runs),
        "remote_targets_probed": sum(run["targets"]["remote_targets_probed"]
                                     for run in runs),
        "probe_requests": sum(run["targets"]["probe_requests"] for run in runs),
        "plan_retrieve_steps": sum(run["targets"]["plan"]["retrieve_steps"]
                                   for run in runs),
        "plan_data_edges": sum(run["targets"]["plan"]["data_edges"] for run in runs),
        "retrieval_completeness": dict(sorted(completeness.items())),
        "evidence_sufficiency": dict(sorted(sufficiency.items())),
        "counts": counts,
        "honesty_violations": sum(len(run["honesty_violations"]) for run in runs),
    }


def _relative_recall(by_id: dict[str, dict[str, dict]], question_ids: list[str],
                     mode: str = "fast") -> dict:
    """§14.3 的快速相对召回：同一评测集 fast 命中数 / 穷查命中数。

    分母（穷查命中数）为 0 时记 `not_applicable` —— 没有分母就没有比率。
    """
    fast_hits = sum(by_id[qid]["fast"]["absolute_recall"]["hits"]
                    for qid in question_ids)
    exhaustive_hits = sum(by_id[qid]["exhaustive_scope"]["absolute_recall"]["hits"]
                          for qid in question_ids)
    applicable = exhaustive_hits > 0
    return {
        "mode": mode,
        "fast_hits": fast_hits,
        "exhaustive_hits": exhaustive_hits,
        "value": (fast_hits / exhaustive_hits) if applicable else None,
        "not_applicable": not applicable,
    }


def evaluate(dataset: dict, runs: list[dict]) -> dict:
    by_id: dict[str, dict[str, dict]] = {}
    for run in runs:
        by_id.setdefault(run["question_id"], {})[run["mode"]] = run
    question_ids = [question["question_id"] for question in dataset["questions"]]
    classes = sorted({question["class"] for question in dataset["questions"]})
    grouped: dict[str, dict[str, dict]] = {cls: {"fast": [], "exhaustive_scope": []}
                                           for cls in classes}
    overall: dict[str, list[dict]] = {"fast": [], "exhaustive_scope": []}
    for run in runs:
        grouped[run["class"]][run["mode"]].append(run)
        overall[run["mode"]].append(run)

    class_report = {}
    for cls in classes:
        ids = [question["question_id"] for question in dataset["questions"]
               if question["class"] == cls]
        class_report[cls] = {
            "fast": _mode_group(grouped[cls]["fast"]),
            "exhaustive_scope": _mode_group(grouped[cls]["exhaustive_scope"]),
            "relative_recall": _relative_recall(by_id, ids),
        }
    mode_report = {
        "fast": _mode_group(overall["fast"]),
        "exhaustive_scope": _mode_group(overall["exhaustive_scope"]),
        "relative_recall": _relative_recall(by_id, question_ids),
    }
    question_rows = []
    for question in dataset["questions"]:
        qid = question["question_id"]
        fast = by_id[qid]["fast"]
        exhaustive = by_id[qid]["exhaustive_scope"]
        question_rows.append({
            "question_id": qid, "class": question["class"],
            "query": question["query"],
            "required_evidence": fast["required_evidence"],
            "evidence_collections": question["evidence_collections"],
            "fast": {key: fast[key] for key in
                     ("retrieved_evidence", "required_retrieved", "required_missing",
                      "absolute_recall", "targets", "retrieval_completeness",
                      "evidence_sufficiency", "counts", "conflict",
                      "kernel_rank_probe", "decoy_evidence_retrieved",
                      "private_decoy_evidence_retrieved", "honesty_violations")},
            "exhaustive_scope": {key: exhaustive[key] for key in
                                 ("retrieved_evidence", "required_retrieved",
                                  "required_missing", "absolute_recall", "targets",
                                  "retrieval_completeness", "evidence_sufficiency",
                                  "counts", "conflict", "kernel_rank_probe",
                                  "decoy_evidence_retrieved",
                                  "private_decoy_evidence_retrieved",
                                  "honesty_violations")},
            "relative_recall": _relative_recall(by_id, [qid])["value"],
        })
    violations = [{"question_id": run["question_id"], "mode": run["mode"],
                   "violations": run["honesty_violations"]}
                  for run in runs if run["honesty_violations"]]
    report = {
        "schema": REPORT_SCHEMA,
        "dataset": {
            "revision": dataset["revision"], "digest": dataset["dataset_digest"],
            "frozen_at": dataset["frozen_at"],
            "nodes": len(dataset["nodes"]), "collections": len(dataset["collections"]),
            "public_collections": sum(
                1 for spec in dataset["collections"].values()
                if spec["publication"] == "published"),
            "private_collections": sum(
                1 for spec in dataset["collections"].values()
                if spec["publication"] == "private"),
            "documents": len(dataset["documents"]), "evidence": len(dataset["evidence"]),
            "questions": len(dataset["questions"]), "classes": len(classes),
            "questions_per_class": {cls: sum(
                1 for question in dataset["questions"] if question["class"] == cls)
                for cls in classes},
            "simulator": dataset["simulator"],
        },
        "kernel": {
            "coordinator_module": "ddp_corpus.federation_tasks",
            "kernel_module": "ddp_core.application",
            "real_path": [
                "routing.targets(manifest)",
                "federation_tasks._select_targets (fast limit / exhaustive all)",
                "federation_tasks._root_budget + routing.RootBudget",
                "federation_tasks._peer_probe_denial (egress gate)",
                "probe.build_probe",
                "routing.plan_steps + plans.validate_plan",
                "coverage.new_entry/record/ledger + validate_ledger",
            ],
            "simulated": ["retrieval execution (FixtureExecutor oracle over frozen evidence)"],
            "fast_candidate_limit": dataset["simulator"]["candidate_limit"],
        },
        "invariants": list(INVARIANTS),
        "honesty": {"violations": violations, "violation_count": len(violations)},
        "questions": question_rows,
        "by_class": class_report,
        "overall": mode_report,
        "not_measured": list(NOT_MEASURED),
    }
    report["summary_markdown"] = render_markdown(dataset, report)
    return report


def render_markdown(dataset: dict, report: dict) -> str:
    lines = [
        "# P6 路由/覆盖评测（合成夹具）", "",
        f"夹具 revision `{dataset['revision']}` · 内容摘要 `{dataset['dataset_digest']}`。",
        "**所有数字都是合成夹具上的路由覆盖结果，不是真实语料的质量结论。**", "",
        "## 总体（fast vs exhaustive_scope）", "",
        "| 轴 | fast | exhaustive_scope |", "|---|---|---|",
    ]
    fast, exhaustive = report["overall"]["fast"], report["overall"]["exhaustive_scope"]
    lines += [
        f"| 计划目标数 | {fast['targets_planned']} | {exhaustive['targets_planned']} |",
        f"| 实际探测目标数 | {fast['targets_probed']} | {exhaustive['targets_probed']} |",
        f"| 远端探测请求 | {fast['probe_requests']} | {exhaustive['probe_requests']} |",
        f"| 计划数据边（hop 代理） | {fast['plan_data_edges']} | {exhaustive['plan_data_edges']} |",
        f"| 绝对召回（micro） | {_fmt(fast['absolute_recall']['micro'])} "
        f"({fast['absolute_recall']['hits']}/{fast['absolute_recall']['required']}) | "
        f"{_fmt(exhaustive['absolute_recall']['micro'])} "
        f"({exhaustive['absolute_recall']['hits']}/{exhaustive['absolute_recall']['required']}) |",
        f"| `retrieval_completeness` 分布 | {_fmt_dist(fast['retrieval_completeness'])} | "
        f"{_fmt_dist(exhaustive['retrieval_completeness'])} |",
        f"| `evidence_sufficiency` 分布 | {_fmt_dist(fast['evidence_sufficiency'])} | "
        f"{_fmt_dist(exhaustive['evidence_sufficiency'])} |",
    ]
    relative = report["overall"]["relative_recall"]
    lines += ["", f"**快速相对召回**（§14.3）：{_fmt(relative['value'])}"
              f"（fast {relative['fast_hits']} / 穷查 {relative['exhaustive_hits']}）"
              + ("；穷查命中为 0，记不适用。" if relative["not_applicable"] else "。"), "",
              "## 按问题类别", "",
              "| 类别 | 题数 | fast 探测 | 穷查探测 | fast 召回 | 穷查召回 | 相对召回 |",
              "|---|---|---|---|---|---|---|"]
    for cls in sorted(report["by_class"]):
        group = report["by_class"][cls]
        f, e = group["fast"], group["exhaustive_scope"]
        rel = group["relative_recall"]
        lines.append(
            f"| {cls} | {f['questions']} | {f['targets_probed']} | {e['targets_probed']} | "
            f"{_fmt(f['absolute_recall']['micro'])} | {_fmt(e['absolute_recall']['micro'])} | "
            f"{_fmt(rel['value'])} |")
    lines += ["", "诚实的成本说明：`probe_requests` 只统计协调者对**远端**目标的 Probe "
              "预占（P5 对本地探测不记账）；`plan_data_edges` 是计划里跨节点证据/查询"
              "数据边数量，作为 hop 代理。字节用量在 P5 恒记 0（未测量，见下）。", "",
              "## 未测量", ""]
    lines += [f"- {item}" for item in report["not_measured"]]
    lines += ["", "## 诚实性", ""]
    if report["honesty"]["violation_count"]:
        lines.append(f"**{report['honesty']['violation_count']} 条违规**："
                     + json.dumps(report["honesty"]["violations"], ensure_ascii=False))
    else:
        lines.append("0 条违规。不变式清单：")
        lines += [f"- {item}" for item in report["invariants"]]
    lines.append("")
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "不适用"
    return f"{value:.1%}"


def _fmt_dist(distribution: dict) -> str:
    if not distribution:
        return "—"
    return ", ".join(f"{key}={value}" for key, value in sorted(distribution.items()))


def default_report_path(dataset: dict, *, reports_dir: Path | None = None) -> Path:
    directory = reports_dir if reports_dir is not None else \
        Path(__file__).resolve().parents[1] / "reports"
    short = dataset["dataset_digest"].split(":", 1)[1][:16]
    return directory / f"routing-{short}.json"


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
