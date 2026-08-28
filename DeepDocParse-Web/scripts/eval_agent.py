#!/usr/bin/env python
"""DDP-Agent v1 评测：分别报告判定、门控、断言引用与拒答。

offline 模式使用 eval/agent.json 中固定的代理输出，调用生产的门控与断言解析函数，
验证数据链和指标本身会变红；它不代表模型质量。GPU 批次二必须用真实问答输出重跑，
并把 live 数字与本报表分开列出。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from ddp_core.agent import assertions_from_text, gate_candidates  # noqa: E402

DATASET = ROOT / "eval" / "agent.json"


@dataclass(frozen=True)
class AgentMetrics:
    retrieval_misses: tuple[int, int]
    redundant_retrievals: tuple[int, int]
    gate_precision_before: tuple[int, int]
    gate_precision_after: tuple[int, int]
    sentence_coverage: tuple[int, int]
    citation_correctness: tuple[int, int]
    refusal_before: tuple[int, int]
    refusal_after: tuple[int, int]
    unsupported_violations: tuple[int, int]


def _hit(item: dict, rank: int) -> dict:
    return {
        "chunk_id": f"chunk-{rank}", "document_id": item["document_id"],
        "parse_job_id": "eval-job", "seq": rank, "page_idx": rank,
        "bbox": [0, rank * 10, 100, rank * 10 + 8], "page_size": [100, 200],
        "text": item["id"], "score": 1.0 / (61 + rank),
        "similarity": item.get("similarity"), "evidence_id": item["id"],
    }


def evaluate(dataset: dict, *, min_similarity: float = 0.55) -> AgentMetrics:
    decisions = dataset["decision_cases"]
    required = [case for case in decisions if case["expected_need_retrieval"]]
    reusable = [case for case in decisions if not case["expected_need_retrieval"]]
    misses = sum(not case["predicted_need_retrieval"] for case in required)
    redundant = sum(case["predicted_need_retrieval"] for case in reusable)

    candidates = dataset["candidates"]
    relevant = {item["id"] for item in candidates if item["relevant"]}
    hits = [_hit(item, rank) for rank, item in enumerate(candidates)]
    accepted, _ = gate_candidates(
        hits, min_similarity=min_similarity, vector_available=True)
    accepted_ids = {hit["evidence_id"] for hit in accepted}

    answer = dataset["answer"]
    assertions = assertions_from_text(answer["text"], answer["evidence_order"])
    supported = [item for item in assertions if item["evidence_ids"]]
    correct = sum(all(evidence_id in set(answer["relevant_evidence_ids"])
                      for evidence_id in item["evidence_ids"])
                  for item in supported)
    unsupported_violations = sum(
        bool(item["evidence_ids"]) == bool(item["unsupported"]) for item in assertions)

    refusals = dataset["refusal_cases"]
    return AgentMetrics(
        retrieval_misses=(misses, len(required)),
        redundant_retrievals=(redundant, len(reusable)),
        gate_precision_before=(len(relevant), len(candidates)),
        gate_precision_after=(len(relevant & accepted_ids), len(accepted_ids)),
        sentence_coverage=(len(supported), len(assertions)),
        citation_correctness=(correct, len(supported)),
        refusal_before=(sum(case["baseline_ok"] for case in refusals), len(refusals)),
        refusal_after=(sum(case["agent_ok"] for case in refusals), len(refusals)),
        unsupported_violations=(unsupported_violations, len(assertions)),
    )


def _rate(value: tuple[int, int]) -> str:
    hit, total = value
    return "—" if not total else f"{hit / total:.1%} ({hit}/{total})"


def render(metrics: AgentMetrics, *, revision: str, mode: str = "offline") -> str:
    before = metrics.refusal_before[0] / metrics.refusal_before[1]
    after = metrics.refusal_after[0] / metrics.refusal_after[1]
    gate_before = metrics.gate_precision_before[0] / metrics.gate_precision_before[1]
    gate_after = metrics.gate_precision_after[0] / metrics.gate_precision_after[1]
    return "\n".join([
        f"# Deep Agent 评测报表（mode={mode}）", "",
        f"数据 revision：`{revision}`。offline 是固定代理输出的结构评测，**不是模型真机质量**。", "",
        "| 指标 | 结果 |", "|---|---|",
        f"| 漏检率（该检索却没检索） | {_rate(metrics.retrieval_misses)} |",
        f"| 冗余检索率（可继承却又检索） | {_rate(metrics.redundant_retrievals)} |",
        f"| 门控前引用精确率 | {_rate(metrics.gate_precision_before)} |",
        f"| 门控后引用精确率 | {_rate(metrics.gate_precision_after)} |",
        f"| 句级引用覆盖率 | {_rate(metrics.sentence_coverage)} |",
        f"| 句级引用正确率 | {_rate(metrics.citation_correctness)} |",
        f"| 拒答正确率（改造前） | {_rate(metrics.refusal_before)} |",
        f"| 拒答正确率（Deep Agent） | {_rate(metrics.refusal_after)} |",
        f"| unsupported 不变式违反率 | {_rate(metrics.unsupported_violations)} |", "",
        f"门控精确率变化：{gate_before:.1%} → {gate_after:.1%}（{gate_after-gate_before:+.1%}）。",
        f"拒答正确率变化：{before:.1%} → {after:.1%}（{after-before:+.1%}，不得下降）。", "",
        "GPU 批次二待补：用真实判定模型、embedding、rerank、chat/VQA 输出替换固定输出，",
        "并按同一列定义报告 live 数字。", "",
    ])


def passes_acceptance(metrics: AgentMetrics) -> bool:
    before = metrics.refusal_before
    after = metrics.refusal_after
    gate_before = metrics.gate_precision_before
    gate_after = metrics.gate_precision_after
    return (
        metrics.unsupported_violations[0] == 0
        and after[0] / after[1] >= before[0] / before[1]
        and gate_after[0] / gate_after[1] >= gate_before[0] / gate_before[1]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--markdown")
    args = parser.parse_args()
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    metrics = evaluate(dataset)
    report = render(metrics, revision=dataset.get("revision", "unknown"))
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.markdown}")
    return 0 if passes_acceptance(metrics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
