"""P6 路由/覆盖评测器自身的回归与变异确认。

评测脚本报出的数字会被写进报告当成证据，所以评测器自己也要有能被"改红"
的用例：这里每一条不变式都有正反两面 —— 先证明正常夹具全绿，再把被守的
那一行改掉，确认真的抛 `CoverageHonestyError`，最后在原对象上继续跑其余
用例（每个变异都用副本/局部注入，不污染共享夹具）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_DIR))

from routing import dataset as routing_dataset  # noqa: E402
from routing import executor as routing_executor  # noqa: E402
from routing import harness, report as routing_report  # noqa: E402
from routing.harness import CoverageHonestyError  # noqa: E402


@pytest.fixture(scope="module")
def frozen() -> dict:
    return routing_dataset.load_frozen()


def _question(frozen: dict, question_id: str) -> dict:
    return next(question for question in frozen["questions"]
                if question["question_id"] == question_id)


# --------------------------------------------------------------- 夹具自检

def test_frozen_dataset_is_the_frozen_shape_and_digest_verifies(frozen):
    assert frozen["dataset_digest"].startswith("sha256:")
    assert len(frozen["nodes"]) == 6
    public = [spec for spec in frozen["collections"].values()
              if spec["publication"] == "published"]
    private = [spec for spec in frozen["collections"].values()
               if spec["publication"] == "private"]
    assert len(public) == 12 and len(private) == 1
    assert 20 <= len(frozen["questions"]) <= 30
    for cls in routing_dataset.CLASSES:
        assert sum(1 for question in frozen["questions"] if question["class"] == cls) >= 3
    required = [evidence_id for question in frozen["questions"]
                for evidence_id in question["required_evidence"]]
    assert len(required) >= 20 and len(required) == len(set(required))
    scope = frozen["scopes"][routing_dataset.SCOPE_REF]
    assert scope["enumeration_state"] == "sealed"
    in_scope = {(member["origin_node_id"], member["collection_id"])
                for member in scope["expanded_members"]}
    for question in frozen["questions"]:
        for evidence_id in question["required_evidence"]:
            meta = frozen["evidence"][evidence_id]
            assert (meta["origin_node_id"], meta["collection_id"]) in in_scope


def test_frozen_dataset_loader_rejects_tampered_content(frozen, tmp_path):
    tampered = copy.deepcopy(frozen)
    tampered["revision"] = "tampered-after-freeze"
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        routing_dataset.load_frozen(path)


def test_builder_rejects_evidence_the_simulator_cannot_retrieve(monkeypatch):
    """变异：把必需证据换成与问题零重叠的文本 → 构造器必须拒绝。"""
    tampered = copy.deepcopy(routing_dataset._QUESTIONS)
    question = next(item for item in tampered if item["question_id"] == "q-local-01")
    question["required"] = [{"collection": "col-a-core",
                             "text": "Nothing here overlaps with that query."}]
    monkeypatch.setattr(routing_dataset, "_QUESTIONS", tampered)
    with pytest.raises(ValueError, match="cannot retrieve required"):
        routing_dataset.build_dataset()


# --------------------------------------------------------------- 基线全绿

def test_exhaustive_finds_every_annotated_required_evidence(frozen):
    """穷查是覆盖参照：每个标注必需证据都必须被找到，且账本 complete。"""
    for question in frozen["questions"]:
        run = harness.run_question(frozen, question, "exhaustive_scope")
        assert run["required_missing"] == [], question["question_id"]
        assert run["retrieval_completeness"] == "complete", question["question_id"]
        assert run["counts"]["incomplete"] == 0, question["question_id"]
        assert run["honesty_violations"] == []


def test_fast_never_claims_complete_and_stays_within_the_candidate_limit(frozen):
    limit = harness.coordinator.FAST_CANDIDATE_LIMIT
    for question in frozen["questions"]:
        run = harness.run_question(frozen, question, "fast")
        assert run["retrieval_completeness"] == "partial", question["question_id"]
        assert len(run["targets"]["targets_planned"]) <= min(
            limit, run["targets"]["targets_total"])
        assert run["honesty_violations"] == []


def test_runs_are_deterministic(frozen):
    question = _question(frozen, "q-near-01")
    first = harness.run_question(frozen, question, "exhaustive_scope")
    second = harness.run_question(frozen, question, "exhaustive_scope")
    assert first == second


# --------------------------------------------------------- 诚实性变异确认

def test_fast_claiming_complete_is_rejected(frozen):
    """变异：让内核账本对 fast 返回 complete → 评测器必须当场红。"""
    real_ledger = harness.coverage_kernel.ledger

    def dishonest(**kwargs):
        ledger = dict(real_ledger(**kwargs))
        ledger["retrieval_completeness"] = "complete"
        return ledger

    question = _question(frozen, "q-local-01")
    with pytest.raises(CoverageHonestyError) as error:
        harness.run_question(frozen, question, "fast", ledger_fn=dishonest)
    assert "complete" in str(error.value)


def test_complete_claim_with_a_non_succeeded_target_is_rejected(frozen):
    """变异：账本自称 complete、计数也洗成 0，但被 deny 的目标还在 → 必须红。

    这个变体绕得过 `coverage.validate_ledger`（它只看计数），只有评测器的
    逐目标复算能抓住 —— 正因如此需要一条用例钉住复算本身。
    """
    real_ledger = harness.coverage_kernel.ledger

    def lying(**kwargs):
        ledger = copy.deepcopy(real_ledger(**kwargs))
        ledger["retrieval_completeness"] = "complete"
        total = ledger["counts"]["total_targets"]
        ledger["counts"] = {"total_targets": total, "applicable_targets": total,
                            "succeeded": total, "excluded": 0, "incomplete": 0}
        return ledger

    question = _question(frozen, "q-bonly-01")
    consent = harness.local_only_consent()
    with pytest.raises(CoverageHonestyError, match="complete retrieval but"):
        harness.run_question(frozen, question, "exhaustive_scope",
                             consent=consent, ledger_fn=lying)


def test_sufficiency_claim_without_evidence_is_rejected(frozen):
    """变异：零证据却写 sufficient_by_policy → 必须红。"""
    real_ledger = harness.coverage_kernel.ledger

    def lying(**kwargs):
        ledger = copy.deepcopy(real_ledger(**kwargs))
        ledger["evidence_sufficiency"] = "sufficient_by_policy"
        return ledger

    question = _question(frozen, "q-none-01")
    with pytest.raises(CoverageHonestyError, match="evidence_sufficiency"):
        harness.run_question(frozen, question, "exhaustive_scope", ledger_fn=lying)


def test_executor_without_an_audit_log_is_rejected(frozen):
    """变异：执行者不暴露调用日志 → 不能证明"deny 目标零调用"，拒绝运行。"""

    class SilentExecutor(routing_executor.FixtureExecutor):
        def __init__(self, dataset):
            super().__init__(dataset)
            self.calls = None

        def retrieve(self, *, target, query, candidate_limit):
            return routing_executor.RetrievalResult(target["collection_id"], [])

    question = _question(frozen, "q-local-01")
    with pytest.raises(CoverageHonestyError, match="auditable"):
        harness.run_question(frozen, question, "fast",
                             executor=SilentExecutor(frozen))


def test_removing_required_evidence_from_retrieval_drops_recall(frozen):
    """变异：检索不再返回必需证据 → 召回必须掉，而不是被洗成命中。"""
    question = _question(frozen, "q-local-01")
    evidence_id = question["required_evidence"][0]
    baseline = harness.run_question(frozen, question, "exhaustive_scope")
    assert baseline["absolute_recall"]["value"] == 1.0
    mutated = routing_dataset.clone_without_evidence(frozen, [evidence_id])
    after = harness.run_question(mutated, question, "exhaustive_scope")
    assert after["absolute_recall"]["value"] == 0.0
    assert after["required_missing"] == [evidence_id]


def test_out_of_scope_evidence_from_the_executor_fails_the_run(frozen):
    """变异：执行者多塞一条越界（私有）证据 → 立即违规。"""

    class LeakyExecutor(routing_executor.FixtureExecutor):
        def retrieve(self, *, target, query, candidate_limit):
            result = super().retrieve(target=target, query=query,
                                      candidate_limit=candidate_limit)
            result.items = list(result.items) + [self._envelope("ev-q-private-01-p01")]
            return result

    question = _question(frozen, "q-private-01")
    with pytest.raises(CoverageHonestyError, match="out-of-scope"):
        harness.run_question(frozen, question, "fast", executor=LeakyExecutor(frozen))


def test_private_collection_is_refused_even_when_asked_directly(frozen):
    executor = routing_executor.FixtureExecutor(frozen)
    with pytest.raises(routing_executor.FixtureScopeError):
        executor.retrieve(
            target={"origin_node_id": "node-a", "collection_id": "col-a-private",
                    "operation": "corpus.retrieve"},
            query="SR-1 vault master key", candidate_limit=8)


def test_budget_blocked_targets_are_not_attempted(frozen):
    """变异：把远端探测预算压到 0 → 远端一个调用都没有，本地照常。"""
    question = _question(frozen, "q-bonly-01")
    consent = harness.default_consent(frozen)
    consent["budget"] = {"max_probe_requests": 0, "max_egress_bytes": 0}
    run = harness.run_question(frozen, question, "fast", consent=consent)
    assert run["executor_calls"], "local targets must still be probed"
    assert all(call["origin_node_id"] == frozen["local_node_id"]
               for call in run["executor_calls"])
    assert run["targets"]["probe_requests"] == 0
    states = set(run["target_outcomes"].values())
    assert "not_attempted" in states
    assert run["absolute_recall"]["value"] == 0.0


# ------------------------------------------------------------- 隐私与局部

def test_local_only_consent_denies_remote_targets_without_a_single_call(frozen):
    question = _question(frozen, "q-bonly-01")
    run = harness.run_question(frozen, question, "exhaustive_scope",
                               consent=harness.local_only_consent())
    called = {call["collection_id"] for call in run["executor_calls"]}
    assert called == {"col-a-core", "col-a-ops"}
    assert list(run["target_outcomes"].values()).count("denied") == 10
    assert run["retrieval_completeness"] == "partial"
    assert run["absolute_recall"]["value"] == 0.0


def test_private_decoy_is_never_probed_or_returned(frozen):
    question = _question(frozen, "q-private-01")
    for mode in ("fast", "exhaustive_scope"):
        run = harness.run_question(frozen, question, mode)
        assert run["private_decoy_evidence_retrieved"] == []
        assert "col-a-private" not in {call["collection_id"]
                                       for call in run["executor_calls"]}
        assert run["absolute_recall"]["value"] == 1.0


# --------------------------------------------------------- 语义轴与报告分轴

def test_conflicting_versions_axis_is_reported_honestly(frozen):
    question = _question(frozen, "q-conflict-01")
    run = harness.run_question(frozen, question, "exhaustive_scope")
    assert run["conflict"]["observed"] is True
    # 内核的 sufficiency 有 conflicting 轴，但协调者的 ledger 目前不计算它：
    # 账本按"有证据"记 sufficient_by_policy，冲突判定只在直接内核调用上可见。
    assert run["conflict"]["kernel_conflicting_axis"] == "conflicting"
    assert run["evidence_sufficiency"] == "sufficient_by_policy"


def test_summary_hidden_questions_sit_outside_kernel_summary_ranking(frozen):
    hidden = [question for question in frozen["questions"]
              if question["class"] == "summary-hidden"]
    assert len(hidden) == 3
    for question in hidden:
        run = harness.run_question(frozen, question, "fast")
        probe = run["kernel_rank_probe"]
        assert probe["best_rank"] is not None, question["question_id"]
        assert probe["best_rank"] > probe["candidate_limit"], question["question_id"]
        assert run["absolute_recall"]["value"] == 0.0


def test_report_has_separate_axes_and_relative_recall(frozen, tmp_path):
    runs = [harness.run_question(frozen, question, mode)
            for question in frozen["questions"]
            for mode in ("fast", "exhaustive_scope")]
    report = routing_report.evaluate(frozen, runs)
    assert report["honesty"]["violation_count"] == 0
    assert report["dataset"]["digest"] == frozen["dataset_digest"]
    assert set(report["overall"]) == {"fast", "exhaustive_scope", "relative_recall"}
    fast, exhaustive = report["overall"]["fast"], report["overall"]["exhaustive_scope"]
    # 成本轴分开报告，且 fast 严格更省。
    assert fast["targets_probed"] < exhaustive["targets_probed"]
    assert fast["probe_requests"] < exhaustive["probe_requests"]
    # 召回轴分开报告，相对召回按 §14.3 口径复算。
    fast_hits = sum(run["absolute_recall"]["hits"] for run in runs
                    if run["mode"] == "fast")
    exhaustive_hits = sum(run["absolute_recall"]["hits"] for run in runs
                          if run["mode"] == "exhaustive_scope")
    relative = report["overall"]["relative_recall"]
    assert relative["fast_hits"] == fast_hits
    assert relative["exhaustive_hits"] == exhaustive_hits
    assert relative["value"] == pytest.approx(fast_hits / exhaustive_hits)
    assert "not_measured" in report and report["not_measured"]
    for cls in routing_dataset.CLASSES:
        group = report["by_class"][cls]
        assert set(group) == {"fast", "exhaustive_scope", "relative_recall"}
    path = tmp_path / "routing-report.json"
    routing_report.write_report(report, path)
    restored = routing_report.load_report(path)
    assert restored["summary_markdown"] == report["summary_markdown"]


def test_cli_exits_non_zero_when_an_invariant_breaks(frozen, monkeypatch, capsys):
    from routing import run as routing_run

    def explode(*args, **kwargs):
        raise harness.CoverageHonestyError("synthetic violation")

    monkeypatch.setattr(routing_run.harness, "run_question", explode)
    assert routing_run.main([]) == 2
    assert "honesty violation" in capsys.readouterr().err


def test_cli_baseline_run_is_green(frozen, capsys):
    from routing import run as routing_run

    assert routing_run.main(["--no-write"]) == 0
    assert "0 条违规" in capsys.readouterr().out
