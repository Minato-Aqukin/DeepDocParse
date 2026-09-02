#!/usr/bin/env python
"""DDP-Graph v1 离线评测：出处、负样本、合并、Wiki 引用与分段归因。

offline 只验证固定代理输出与评测器/契约；GPU 批次三用 live 模型输出替换 `output`。
不报综合分，因为识别错、抽取错、连边错的修复路径完全不同。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_agent  # noqa: E402

DATASET = EVAL_DIR / "datasets" / "graph.json"
AGENT_DATASET = EVAL_DIR / "datasets" / "agent.json"


def ratio(hit: int, total: int) -> str:
    return "—" if not total else f"{hit / total:.1%} ({hit}/{total})"


def agent_drift(data: dict) -> dict[str, list]:
    """「图关掉后既有指标不变」的可判定形式：对阶段 6 记录基线逐字段比。

    **这里原来是假守卫**：`evaluate(agent_data) == evaluate(agent_data)` ——
    同一个纯函数、同一份数据调两次，恒真。实测把 `gate_candidates` 整个短路掉
    （门控后引用精确率 80% -> 50%），这一行照样报 PASS、脚本照样退 0。
    现在比的是**阶段 7 之前记下来的那一列**（`eval/graph.json` 的
    `agent_baseline`，抄自 `docs/EVAL-stage6-offline-report.md`）：
    知识层不许挂进问答链，所以任何一格漂了都得先解释，不能顺手改基线。
    基线里缺某个字段也算漂 —— 新增指标必须显式补基线，不能悄悄溜过去。
    """
    baseline = data.get("agent_baseline") or {}
    measured = asdict(eval_agent.evaluate(
        json.loads(AGENT_DATASET.read_text(encoding="utf-8"))))
    return {key: [list(value), list(baseline.get(key) or [])]
            for key, value in measured.items()
            if list(value) != list(baseline.get(key) or [])}


def evaluate(data: dict) -> dict:
    edges = data["edges"]
    pointback = 0
    attribution = {stage: [0, 0] for stage in ("recognition", "extraction", "link")}
    for row in edges:
        output = row.get("output") or {}
        citation = output.get("citation") or {}
        bbox = citation.get("bbox")
        page_size = citation.get("page_size")
        basis = row["expect"]["basis"]
        good = (output.get("status") == "found" and citation.get("evidence_id")
                and isinstance(bbox, list) and len(bbox) == 4
                and isinstance(page_size, list) and len(page_size) == 2
                and basis in str(citation.get("crop_text") or ""))
        pointback += int(bool(good))
        expected_stage = next(value for value in row["attributes"]
                              if value in attribution)
        attribution[expected_stage][1] += 1
        attribution[expected_stage][0] += int(output.get("error_stage") == expected_stage)

    negatives = data["negative_relations"]
    negative_hit = sum(row.get("output", {}).get("status") == "not_found"
                       for row in negatives)
    merges = data["merges"]
    merge_hit = sum(row["expected_same"] == row["predicted_same"] for row in merges)
    merge_distribution = Counter(row["merged_by"] for row in merges)
    uncertain_split = [row for row in merges if row["uncertain"]]
    split_hit = sum(row["splittable"] for row in uncertain_split)

    wiki = data["wiki_sentences"]
    covered = sum(bool(row["evidence_ids"]) or row["unsupported"] for row in wiki)
    supported = [row for row in wiki if row["evidence_ids"]]
    citation_correct = sum(row["citation_correct"] for row in supported)

    drift = agent_drift(data)
    return {
        "edge_pointback": (pointback, len(edges)),
        "negative_not_found": (negative_hit, len(negatives)),
        "merge_accuracy": (merge_hit, len(merges)),
        "merge_distribution": dict(sorted(merge_distribution.items())),
        "uncertain_splittable": (split_hit, len(uncertain_split)),
        "wiki_sentence_coverage": (covered, len(wiki)),
        "wiki_citation_correctness": (citation_correct, len(supported)),
        "attribution": attribution,
        "agent_drift": drift,
        "graph_off_agent_unchanged": not drift,
    }


def render(data: dict, metrics: dict) -> str:
    lines = [
        "# DDP-Graph 阶段 7 离线评测", "",
        f"数据 revision：`{data.get('revision', 'unknown')}`。固定输出只验结构与指标，"
        "**不是模型真机质量**；live 数字留给 GPU 批次三。", "",
        "| 指标 | 结果 |", "|---|---|",
        f"| 50 条边 bbox 裁图逐条点回 | {ratio(*metrics['edge_pointback'])} |",
        f"| 负样本 `not_found` | {ratio(*metrics['negative_not_found'])} |",
        f"| 实体合并准确率 | {ratio(*metrics['merge_accuracy'])} |",
        f"| 低置信合并可拆 | {ratio(*metrics['uncertain_splittable'])} |",
        f"| Wiki 句级引用覆盖（无引用须 unsupported） | {ratio(*metrics['wiki_sentence_coverage'])} |",
        f"| Wiki 引用正确率 | {ratio(*metrics['wiki_citation_correctness'])} |",
        f"| 图关掉后既有 Agent 指标不变（对阶段 6 基线逐字段） | "
        f"{'PASS' if metrics['graph_off_agent_unchanged'] else 'FAIL'} |",
        "",
        *([] if metrics["graph_off_agent_unchanged"] else [
            "漂掉的字段（实测 / 阶段 6 基线）：`" + json.dumps(
                metrics["agent_drift"], ensure_ascii=False, sort_keys=True) + "`。", ""]),
        "实体合并 `merged_by` 分布：`" + json.dumps(
            metrics["merge_distribution"], ensure_ascii=False, sort_keys=True) + "`。", "",
        "## 错误归因（三分表，不报综合分）", "",
        "| 环节 | 判对 |", "|---|---|",
    ]
    labels = {"recognition": "识别错", "extraction": "抽取错", "link": "连边错"}
    for stage in ("recognition", "extraction", "link"):
        lines.append(f"| {labels[stage]} | {ratio(*metrics['attribution'][stage])} |")
    lines += ["", "GPU 批次三待补：真实关系抽取、实体合并、STORM 句级引用及 MCP 端到端。", ""]
    return "\n".join(lines)


def passes(metrics: dict) -> bool:
    return (
        metrics["edge_pointback"][1] >= 50
        and metrics["edge_pointback"][0] == metrics["edge_pointback"][1]
        and metrics["negative_not_found"][0] == metrics["negative_not_found"][1]
        and metrics["merge_accuracy"][0] == metrics["merge_accuracy"][1]
        and metrics["uncertain_splittable"][0] == metrics["uncertain_splittable"][1]
        and metrics["wiki_sentence_coverage"][0] == metrics["wiki_sentence_coverage"][1]
        and metrics["graph_off_agent_unchanged"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    metrics = evaluate(data)
    report = render(data, metrics)
    print(report)
    if args.markdown:
        args.markdown.write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.markdown}")
    return 0 if passes(metrics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
