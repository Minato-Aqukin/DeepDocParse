import copy
import json

import pytest

from ddp_core.application.coverage import (
    MAX_EXCLUSION_BASIS_CHARS,
    MAX_REQUIREMENT_CHARS,
    completeness,
    conflict,
    ledger,
    merge_conflicts,
    new_entry,
    record,
    sufficiency,
    target_key,
    validate_entry,
    validate_ledger,
    validate_manifest,
    version_conflicts,
)
from ddp_core.application.plans import utc_instant
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import build_probe
from ddp_paths import CONTRACTS
from plan_samples import NOW

DIGEST = "sha256:" + "a" * 64
QUERY = "sha256:" + "b" * 64
OBSERVED = utc_instant(NOW)
SCOPE = "scope-17"


def fixture(name):
    return json.loads((CONTRACTS / "fixtures" / name).read_text())


def probe(probe_id="probe-b-21", node="node-b", status="succeeded", limits=(), missing=(),
          kind="evidence_retrieval", observed=None, can_generate=False):
    retrieval = None
    if kind == "evidence_retrieval":
        retrieval = {"status": status, "collection_ref": "b:robotics", "index_revision": "index-42",
                     "evidence_set_ref": "b:evidence-set-31", "internal_limits": list(limits)}
    return build_probe(
        probe_id=probe_id, target_node_id=node, task_spec_digest=DIGEST, consent_ref="consent-probe-4",
        probe_kind=kind, capability_check={"operation": "corpus.retrieve", "readiness": "ready"},
        retrieval=retrieval, can_generate=can_generate, missing_requirements=list(missing),
        observed_at=observed or OBSERVED)


def entry(origin="node-b", collection="b:robotics", operation="corpus.retrieve"):
    return new_entry(target_key(origin, collection, operation), SCOPE, QUERY)


def manifest(state="sealed", members=(), unexpanded=()):
    return {"schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": SCOPE, "caller_scope_hash": DIGEST,
            "created_at": OBSERVED, "valid_until": "2030-01-01T00:00:00Z",
            "registry_revision_vector": [{"node_id": "node-b", "registry_revision": 3, "fetched_at": OBSERVED}],
            "expanded_members": list(members), "unexpanded_subtrees": list(unexpanded),
            "enumeration_state": state, "manifest_digest": DIGEST}


def test_target_key_and_new_entry_shape():
    key = target_key("node-b", "b:robotics", "corpus.retrieve")
    assert key == {"origin_node_id": "node-b", "collection_id": "b:robotics", "operation": "corpus.retrieve"}
    created = new_entry(key, SCOPE, QUERY)
    assert created["state"] == "planned"
    assert created["attempts"] == 0 and created["probe_receipts"] == [] and created["exclusion_basis"] is None
    validate_entry(created)
    with pytest.raises(ApplicationError):
        target_key("Node-B", "b:robotics", "corpus.retrieve")


def test_record_success_binds_receipt_revision_and_evidence():
    original = entry()
    updated = record(original, probe(), now=NOW)
    assert updated["state"] == "succeeded"
    assert updated["attempts"] == 1
    assert updated["probe_receipts"] == ["probe-b-21"]
    assert updated["actual_index_revision"] == "index-42"
    assert updated["evidence_refs"] == ["b:evidence-set-31"]
    # 传入对象不被修改：账本更新不是原地洗数据。
    assert original["state"] == "planned" and original["attempts"] == 0
    validate_entry(updated)


def test_record_internal_limits_force_partial_even_with_explicit_success():
    limited = probe(status="partial", limits=["shard_failed"])
    updated = record(entry(), limited, state="succeeded", now=NOW)
    assert updated["state"] == "partial"
    assert record(entry(), limited, now=NOW)["state"] == "partial"


def test_record_execution_limits_force_partial_without_a_probe():
    """执行回执自报 internal_limits 时没有 probe 可依 —— 也必须压成 partial。"""
    done = record(entry(), probe(), now=NOW)
    assert done["state"] == "succeeded"
    limited = record(done, None, state="succeeded", limits=["truncated_by_limit"], now=NOW)
    assert limited["state"] == "partial", "T85：执行自报截断不许在账本上洗成成功"
    assert limited["attempts"] == done["attempts"] + 1
    # 未知限制词当场拒绝，不静默忽略。
    with pytest.raises(ApplicationError):
        record(done, None, state="succeeded", limits=["not_a_limit"], now=NOW)


def test_exclusion_basis_is_explicitly_bounded_never_truncated():
    """旧实现直接 join 任意长度：PostgreSQL 的 String(160) 会 DataError 500。"""
    over_long_item = probe(kind="capability_input",
                           missing=["x" * (MAX_REQUIREMENT_CHARS + 1)])
    with pytest.raises(ApplicationError, match="missing requirement"):
        record(entry(), over_long_item, now=NOW)
    over_long_join = probe(kind="capability_input",
                           missing=["x" * MAX_REQUIREMENT_CHARS] * 5)
    with pytest.raises(ApplicationError, match="exclusion basis"):
        record(entry(), over_long_join, now=NOW)
    valid = record(entry(), probe(kind="capability_input",
                                  missing=["wiki.pages not registered"]), now=NOW)
    assert valid["exclusion_basis"] == "wiki.pages not registered"
    # 已持久化的行同样受上限约束（校验层，不只是派生层）。
    with pytest.raises(ApplicationError, match="exclusion basis"):
        validate_entry({**entry(), "state": "unsupported",
                        "exclusion_basis": "y" * (MAX_EXCLUSION_BASIS_CHARS + 1)})


def test_record_unsupported_derives_basis_from_missing_requirements():
    capability = probe(kind="capability_input", missing=["wiki.pages not registered"])
    updated = record(entry(operation="wiki.pages"), capability, now=NOW)
    assert updated["state"] == "unsupported"
    assert updated["exclusion_basis"] == "wiki.pages not registered"
    validate_entry(updated)
    with pytest.raises(ApplicationError, match="exclusion basis"):
        record(entry(), None, state="unsupported", now=NOW)


def test_record_succeeded_requires_receipt_and_actual_index_revision():
    with pytest.raises(ApplicationError):
        record(entry(), probe(kind="capability_input"), state="succeeded", now=NOW)
    with pytest.raises(ApplicationError):
        record({**entry(), "probe_receipts": ["probe-b-21"]}, None, state="succeeded", now=NOW)
    with pytest.raises(ApplicationError):
        record(entry(), probe(kind="capability_input"), state="succeeded", now=NOW)


def test_record_rejects_a_stale_probe_as_new_success():
    stale = probe(observed=utc_instant(NOW - 3600))
    with pytest.raises(ApplicationError, match="expired"):
        record(entry(), stale, now=NOW)


def test_record_failure_keeps_last_error_and_counts_an_attempt():
    updated = record(entry(), None, state="unreachable", error="connect timeout", now=NOW)
    assert updated["state"] == "unreachable" and updated["attempts"] == 1
    assert updated["last_error"] == "connect timeout"
    again = record(updated, None, state="unreachable", now=NOW)
    assert again["last_error"] == "connect timeout" and again["attempts"] == 2
    planned = record(entry(), None, now=NOW)
    assert planned["state"] == "planned" and planned["attempts"] == 0


def test_completeness_fast_never_returns_complete():
    succeeded = record(entry(), probe(), now=NOW)
    assert completeness("sealed", "fast", [succeeded]) == "partial"
    assert completeness("sealed", "exhaustive_scope", [succeeded]) == "complete"
    assert completeness("partial", "exhaustive_scope", [succeeded]) == "partial"
    assert completeness("sealed", "exhaustive_scope", []) == "not_started"


def test_completeness_is_a_conjunction_over_deduplicated_targets():
    done = record(entry(), probe(), now=NOW)
    excluded = record(entry(collection="b:legacy", operation="wiki.pages"), probe(kind="capability_input",
                                                                                  missing=["not registered"]),
                      now=NOW)
    assert excluded["state"] == "unsupported"
    # unsupported 有依据：进排除数，不阻塞 complete（§7.4 与 complete 夹具同义）。
    assert completeness("sealed", "exhaustive_scope", [done, excluded]) == "complete"
    for gap in ("failed", "unreachable", "denied", "not_attempted", "revoked", "in_flight", "partial", "planned"):
        assert completeness("sealed", "exhaustive_scope", [done, record(entry(collection="b:other"), None,
                                                                        state=gap, now=NOW)]) == "partial"
    without_basis = {**entry(collection="b:legacy"), "state": "unsupported"}
    assert completeness("sealed", "exhaustive_scope", [done, without_basis]) == "partial"


def test_completeness_requires_every_subquery_of_a_target():
    first = record(entry(), probe(probe_id="probe-b-21"), now=NOW)
    second = record(entry(), None, state="failed", error="shard down", now=NOW)
    assert completeness("sealed", "exhaustive_scope", [first, second]) == "partial"


def test_sufficiency_uses_rules_not_confidence():
    evidence = record(entry(), probe(), now=NOW)
    assert sufficiency([]) == "unknown"
    assert sufficiency([entry()]) == "insufficient"
    assert sufficiency([evidence], bindings=["ev-1"]) == "sufficient_by_policy"
    assert sufficiency([evidence], bindings=["ev-1"], conflicting=True) == "conflicting"
    # 矛盾不能把"不足"/"未知"改写成"矛盾"：那会藏掉不足信号并放行生成闸。
    assert sufficiency([evidence], conflicting=True) == "insufficient"
    assert sufficiency([], conflicting=True) == "unknown"
    with pytest.raises(ApplicationError):
        sufficiency([], conflicting="yes")


def test_ledger_counts_and_allof_guards():
    done = record(entry(), probe(), now=NOW)
    excluded = record(entry(collection="b:legacy", operation="wiki.pages"),
                      probe(kind="capability_input", missing=["not registered"]), now=NOW)
    value = ledger(root_task_id="task-9", scope_ref=SCOPE, search_mode="exhaustive_scope",
                   enumeration_state="sealed", entries=[done, excluded])
    assert value["retrieval_completeness"] == "complete"
    assert value["counts"] == {"total_targets": 2, "applicable_targets": 1, "succeeded": 1,
                               "excluded": 1, "incomplete": 0}
    validate_ledger(value)
    fast = ledger(root_task_id="task-9", scope_ref=SCOPE, search_mode="fast",
                  enumeration_state="sealed", entries=[done])
    assert fast["retrieval_completeness"] == "partial"
    validate_ledger(fast)
    gap = record(entry(collection="b:other"), None, state="unreachable", error="offline", now=NOW)
    incomplete = ledger(root_task_id="task-9", scope_ref=SCOPE, search_mode="exhaustive_scope",
                        enumeration_state="sealed", entries=[done, gap])
    assert incomplete["retrieval_completeness"] == "partial"
    assert incomplete["counts"]["incomplete"] == 1
    with pytest.raises(ApplicationError, match="another scope"):
        ledger(root_task_id="task-9", scope_ref="scope-other", search_mode="fast",
               enumeration_state="sealed", entries=[done])
    with pytest.raises(ApplicationError):
        ledger(root_task_id="task-9", scope_ref=SCOPE, search_mode="fast",
               enumeration_state="sealed", entries=[{**done, "state": "mystery"}])


def test_ledger_computes_evidence_sufficiency_from_coverage_rows():
    done = record(entry(), probe(), now=NOW)
    assert ledger(root_task_id="t", scope_ref=SCOPE, search_mode="fast", enumeration_state="sealed",
                  entries=[done])["evidence_sufficiency"] == "sufficient_by_policy"
    assert ledger(root_task_id="t", scope_ref=SCOPE, search_mode="fast", enumeration_state="sealed",
                  entries=[entry()])["evidence_sufficiency"] == "insufficient"


@pytest.mark.parametrize("name", [
    "valid/coverage-entry-succeeded.json",
    "valid/coverage-entry-unsupported.json",
])
def test_contract_valid_entry_fixtures_pass(name):
    validate_entry(fixture(name))


@pytest.mark.parametrize("name", [
    "invalid/coverage-succeeded-without-receipt.json",
    "invalid/coverage-unsupported-without-basis.json",
])
def test_contract_invalid_entry_fixtures_are_rejected(name):
    with pytest.raises(ApplicationError):
        validate_entry(fixture(name))


@pytest.mark.parametrize("name", [
    "valid/coverage-ledger-exhaustive-complete.json",
    "valid/coverage-ledger-fast-partial.json",
    "valid/coverage-ledger-conflicting.json",
    "valid/coverage-ledger-insufficient-with-conflicts.json",
])
def test_contract_valid_ledger_fixtures_pass(name):
    validate_ledger(fixture(name))


@pytest.mark.parametrize("name", [
    "invalid/coverage-fast-claims-complete.json",
    "invalid/coverage-complete-without-sealed.json",
    "invalid/coverage-complete-with-incomplete-count.json",
    "invalid/coverage-conflicts-not-reported.json",
    "invalid/coverage-conflicting-without-record.json",
])
def test_contract_invalid_ledger_fixtures_are_rejected(name):
    with pytest.raises(ApplicationError):
        validate_ledger(fixture(name))


def test_ledger_matches_the_complete_contract_fixture():
    sample = fixture("valid/coverage-ledger-exhaustive-complete.json")
    value = ledger(root_task_id=sample["root_task_id"], scope_ref=sample["scope_ref"],
                   search_mode=sample["search_mode"], enumeration_state=sample["enumeration_state"],
                   entries=sample["entries"])
    assert value["retrieval_completeness"] == "complete"
    assert value["counts"] == sample["counts"]


def test_manifest_allof_sealed_cannot_keep_unexpanded_subtrees():
    validate_manifest(manifest())
    validate_manifest(manifest(state="partial", unexpanded=[{"node_id": "node-x", "reason": "timeout"}]))
    with pytest.raises(ApplicationError, match="unexpanded"):
        validate_manifest(manifest(state="sealed", unexpanded=[{"node_id": "node-x", "reason": "timeout"}]))
    sealed = fixture("valid/scope-manifest-sealed.json")
    validate_manifest(sealed)
    validate_manifest(fixture("valid/scope-manifest-partial.json"))
    with pytest.raises(ApplicationError):
        validate_manifest(fixture("invalid/scope-sealed-with-unexpanded.json"))


def test_record_deep_copies_and_keeps_used_budget():
    original = {**entry(), "used_budget": {"requests": 3, "bytes": 100}}
    updated = record(original, probe(), now=NOW)
    updated["used_budget"]["requests"] = 4
    assert original["used_budget"]["requests"] == 3


# ------------------------------------------------------------ 证据矛盾（§7.6）

def envelope(evidence_id, *, version, digest, resource="res-1", origin="node-b", page=0, seq=3,
             source_type="source"):
    return {"evidence_id": evidence_id, "origin_node_id": origin, "resource_id": resource,
            "source_version_id": version, "excerpt_digest": "sha256:" + digest * 64,
            "source_type": source_type,
            "locator": {"kind": "page_block", "physical_page_index": page, "seq": seq}}


def test_version_divergence_needs_two_versions_and_two_texts_at_one_locator():
    diverged = [envelope("ev-new", version="v2", digest="b"), envelope("ev-old", version="v1", digest="a")]
    assert version_conflicts(diverged) == [
        {"basis": "version_divergence", "evidence_refs": ["ev-new", "ev-old"],
         "semantic_review": "needs_review"}]
    # 版本不同但正文相同：不是矛盾。
    assert version_conflicts([envelope("e1", version="v1", digest="a"),
                              envelope("e2", version="v2", digest="a")]) == []
    # 同一版本两段不同正文：块序不同本来就是两处，不比较。
    assert version_conflicts([envelope("e1", version="v1", digest="a", seq=1),
                              envelope("e2", version="v2", digest="b", seq=2)]) == []
    # 不同资源同一定位：不同资料不按版本规则比较（那一路靠生成标注）。
    assert version_conflicts([envelope("e1", version="v1", digest="a", resource="r1"),
                              envelope("e2", version="v2", digest="b", resource="r2")]) == []
    # 生成物不能制造或掩盖原文矛盾。
    assert version_conflicts([envelope("e1", version="v1", digest="a"),
                              envelope("e2", version="v2", digest="b", source_type="generated")]) == []
    # 缺定位的条目不参与，不猜。
    broken = envelope("e3", version="v3", digest="c")
    broken["locator"] = {"kind": "page_block"}
    assert version_conflicts([envelope("e1", version="v1", digest="a"), broken]) == []


def test_ledger_with_conflicts_reports_conflicting_and_only_downgrades():
    entry = record(new_entry(target_key("node-b", "b:robotics", "corpus.retrieve"), SCOPE, QUERY),
                   probe(), now=NOW)
    base = dict(root_task_id="task-1", scope_ref=SCOPE, search_mode="exhaustive_scope",
                enumeration_state="sealed", entries=[entry])
    assert ledger(**base)["evidence_sufficiency"] == "sufficient_by_policy"
    assert "conflicts" not in ledger(**base), "没有矛盾时不写空字段"
    found = conflict("generation_reported", ["ev-2", "ev-1"])
    value = ledger(**base, conflicts=[found, dict(found)])
    assert value["evidence_sufficiency"] == "conflicting"
    assert value["conflicts"] == [{"basis": "generation_reported", "evidence_refs": ["ev-1", "ev-2"],
                                   "semantic_review": "needs_review"}], "同依据同引用去重"
    validate_ledger(value)
    # 没有证据的账本不会因为一条矛盾记录被写成"充分"或"矛盾"：unknown 优先，
    # 矛盾记录照样写上。
    empty = ledger(root_task_id="task-1", scope_ref=SCOPE, search_mode="fast",
                   enumeration_state="partial", entries=[], conflicts=[found])
    assert empty["evidence_sufficiency"] == "unknown" and empty["conflicts"]
    validate_ledger(empty)
    # 有目标但没有绑定：insufficient 优先（第五次验收复现的形状）。
    unbound = record(new_entry(target_key("node-b", "b:robotics", "corpus.retrieve"), SCOPE, QUERY),
                     None, state="failed", error="shard down", now=NOW)
    thin = ledger(**{**base, "entries": [unbound]}, conflicts=[found])
    assert thin["evidence_sufficiency"] == "insufficient" and thin["conflicts"]
    validate_ledger(thin)


def test_validate_ledger_keeps_conflict_records_and_axis_in_lockstep():
    entry = record(new_entry(target_key("node-b", "b:robotics", "corpus.retrieve"), SCOPE, QUERY),
                   probe(), now=NOW)
    value = ledger(root_task_id="task-1", scope_ref=SCOPE, search_mode="exhaustive_scope",
                   enumeration_state="sealed", entries=[entry],
                   conflicts=[conflict("version_divergence", ["ev-1", "ev-2"])])
    hidden = copy.deepcopy(value)
    hidden["evidence_sufficiency"] = "sufficient_by_policy"
    with pytest.raises(ApplicationError):
        validate_ledger(hidden)
    for kept in ("insufficient", "unknown"):
        coexisting = copy.deepcopy(value)
        coexisting["evidence_sufficiency"] = kept
        validate_ledger(coexisting)   # 不足/未知与矛盾记录并存是合法的
    bare = copy.deepcopy(value)
    del bare["conflicts"]
    with pytest.raises(ApplicationError):
        validate_ledger(bare)
    single = copy.deepcopy(value)
    single["conflicts"][0]["evidence_refs"] = ["ev-1"]
    with pytest.raises(ApplicationError):
        validate_ledger(single)


def test_conflict_records_need_two_distinct_refs_and_a_known_basis():
    with pytest.raises(ApplicationError):
        conflict("generation_reported", ["ev-1", "ev-1"])
    with pytest.raises(ApplicationError):
        conflict("model_confidence", ["ev-1", "ev-2"])
    assert merge_conflicts([], None, [conflict("version_divergence", ["b", "a"])]) == [
        {"basis": "version_divergence", "evidence_refs": ["a", "b"], "semantic_review": "needs_review"}]
