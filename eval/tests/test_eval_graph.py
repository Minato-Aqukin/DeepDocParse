import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_DIR))

import eval_graph  # noqa: E402


def test_stage7_graph_fixture_meets_each_separate_acceptance_metric():
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    metrics = eval_graph.evaluate(data)
    assert metrics["edge_pointback"] == (50, 50)
    assert metrics["negative_not_found"] == (10, 10)
    assert metrics["merge_distribution"] == {
        "alias": 6, "exact": 8, "model": 3, "none": 3}
    assert metrics["graph_off_agent_unchanged"] is True
    assert eval_graph.passes(metrics)


def test_graph_evaluator_rejects_a_fake_crop_even_when_bbox_exists():
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    data["edges"][0]["output"]["citation"]["crop_text"] = "unrelated pixels"
    metrics = eval_graph.evaluate(data)
    assert metrics["edge_pointback"] == (49, 50)
    assert not eval_graph.passes(metrics)


def test_graph_off_row_goes_red_when_an_existing_agent_metric_drifts():
    """「图关掉后既有指标不变」必须真的能红。

    **这一行原来是恒真的**：`evaluate(agent_data) == evaluate(agent_data)`，
    同一个纯函数、同一份数据调两次。实测把 `ddp_core.agent.gate_candidates`
    整个短路掉（门控后引用精确率 80% -> 50%），报表照样印 PASS、脚本照样退 0。
    现在比的是阶段 6 记录的基线，这里把基线挪一格来证明它会红 ——
    改生产代码做变异会漂同一格，效果一样。
    """
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    data["agent_baseline"]["gate_precision_after"] = [4, 8]
    metrics = eval_graph.evaluate(data)
    assert metrics["graph_off_agent_unchanged"] is False
    assert metrics["agent_drift"] == {"gate_precision_after": [[4, 5], [4, 8]]}
    assert not eval_graph.passes(metrics)
    assert "漂掉的字段" in eval_graph.render(data, metrics)


def test_a_new_agent_metric_without_a_baseline_counts_as_drift():
    """新增指标必须显式补基线，不能靠"基线里没有这一格"悄悄溜过去。"""
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    del data["agent_baseline"]["refusal_after"]
    metrics = eval_graph.evaluate(data)
    assert metrics["agent_drift"] == {"refusal_after": [[4, 4], []]}
    assert not eval_graph.passes(metrics)


def test_graph_gate_fails_when_citations_all_present_but_wrong():
    """覆盖 100% 但正确率 0% 必须红：门禁以前只看覆盖，错引能拿 exit 0。"""
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    for row in data["wiki_sentences"]:
        if row["evidence_ids"]:
            row["citation_correct"] = False
    metrics = eval_graph.evaluate(data)
    assert metrics["wiki_sentence_coverage"] == (20, 20)
    assert metrics["wiki_citation_correctness"] == (0, 16)
    assert not eval_graph.passes(metrics)


def test_graph_gate_fails_on_a_partially_wrong_citation_set():
    """正确率差一条也不行：门禁要求全等，不是"差不多"。"""
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    data["wiki_sentences"][0]["citation_correct"] = False
    metrics = eval_graph.evaluate(data)
    assert metrics["wiki_sentence_coverage"] == (20, 20)
    assert metrics["wiki_citation_correctness"] == (15, 16)
    assert not eval_graph.passes(metrics)


def test_unattributed_edge_row_scores_a_named_failure_not_a_traceback():
    """attributes 里没有已知环节的行：记名失败走门禁红，不抛 StopIteration。"""
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    data["edges"][0]["attributes"] = ["manual"]
    metrics = eval_graph.evaluate(data)
    assert metrics["attribution_errors"] == ["edge-001"]
    assert not eval_graph.passes(metrics)
    assert "edge-001" in eval_graph.render(data, metrics)


def test_attribution_still_splits_clean_rows_after_an_unattributed_one():
    """坏行跳过之后，好行的三分表计数不受影响。"""
    data = json.loads((EVAL_DIR / "datasets" / "graph.json").read_text(encoding="utf-8"))
    data["edges"][0]["attributes"] = ["manual"]
    metrics = eval_graph.evaluate(data)
    assert sum(hit for hit, _ in metrics["attribution"].values()) == 49
    assert sum(total for _, total in metrics["attribution"].values()) == 49
