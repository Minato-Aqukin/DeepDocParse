"""Agent 评测器的空分母守卫：自定义 --dataset 下 refusal/candidates 可以为空。

空样本时 render 印"—"、passes_acceptance 判 False，
绝不能抛 ZeroDivisionError 把评测崩掉。
"""
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_DIR))

import eval_agent  # noqa: E402


def _load_dataset():
    return json.loads((EVAL_DIR / "datasets" / "agent.json").read_text(encoding="utf-8"))


def test_empty_refusal_cases_render_dash_and_fail_instead_of_crash():
    dataset = _load_dataset()
    dataset["refusal_cases"] = []
    metrics = eval_agent.evaluate(dataset)
    assert metrics.refusal_before == (0, 0)
    assert metrics.refusal_after == (0, 0)
    assert not eval_agent.passes_acceptance(metrics)
    report = eval_agent.render(metrics, revision=dataset["revision"])
    assert "| 拒答正确率（改造前） | — |" in report
    assert "| 拒答正确率（Deep Agent） | — |" in report
    assert "拒答正确率变化：—" in report


def test_empty_gate_candidates_render_dash_and_fail_instead_of_crash():
    dataset = _load_dataset()
    dataset["candidates"] = []
    metrics = eval_agent.evaluate(dataset)
    assert metrics.gate_precision_before == (0, 0)
    assert metrics.gate_precision_after == (0, 0)
    assert not eval_agent.passes_acceptance(metrics)
    report = eval_agent.render(metrics, revision=dataset["revision"])
    assert "| 门控前引用精确率 | — |" in report
    assert "| 门控后引用精确率 | — |" in report
    assert "门控精确率变化：—" in report


def test_empty_everything_still_renders_and_fails():
    dataset = _load_dataset()
    dataset.update({
        "decision_cases": [], "candidates": [], "refusal_cases": [],
        "answer": {"text": "x", "evidence_order": [],
                   "relevant_evidence_ids": []},
    })
    metrics = eval_agent.evaluate(dataset)
    assert not eval_agent.passes_acceptance(metrics)
    assert "—" in eval_agent.render(metrics, revision=dataset["revision"])


def test_shipped_dataset_still_passes_after_zero_guards():
    """守卫不能把正常数据集洗绿/洗红： shipped 数据集照旧通过。"""
    dataset = _load_dataset()
    metrics = eval_agent.evaluate(dataset)
    assert eval_agent.passes_acceptance(metrics)
    report = eval_agent.render(metrics, revision=dataset["revision"])
    assert "（不得下降）" in report
