"""真实评测契约测试：隔离守卫 + 脱敏 + 显式失败 + 归属（不运行真实网络）。

本文件只测 `eval/routing/real.py` 的纯逻辑与驱动的契约形状：
- 合成报告（`ddp-routing-eval/1#Report`）与真实报告
  （`ddp-routing-real-eval/1#Report`）永不混用；
- source-cases 的六题永为 `pending` 待复核提示，不变标签；
- Authorization/cookies/预签名查询永不进产物；
- 缺模型/缺索引走显式失败原因，不伪造就绪；
- 来源归属按冻结 scope 校验，越界记 `attribution_mismatch` 而非吞掉；
- 数字出现不产生 claim-support precision。
"""
from __future__ import annotations

import json
import sys
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_DIR))

from routing import real as routing_real  # noqa: E402


def _source_cases_doc():
    return {
        "schema": "ddp-real-source-evaluation-input/1",
        "annotation_origin": "agent_extraction_from_publisher_originals",
        "human_review_status": "pending",
        "sources": [{"id": "pico", "path": "pico-datasheet.pdf",
                     "title": "t", "publisher": "p",
                     "url": "https://example.invalid/pico.pdf",
                     "version": "v1",
                     "sha256": "757ff485227493b9fcc0c2c96c4dea9de020e1d8b2b11e2aa4f9ee8b25aa89eb",
                     "domain": "hw"}],
        "cases": [{
            "id": "pico-cpu-and-memory", "question": "q?",
            "sources": ["pico"],
            "references": [{"source": "pico", "page_index": 3,
                            "excerpt": "Dual-core cortex M0+ at up to 133 MHz.",
                            "facts": {"maximum_core_frequency_mhz": 133}}],
        }],
    }


def _nodes_config(**over):
    config = {
        "schema": "ddp-real-eval-nodes/1",
        "entry": "a",
        "auth": {"username": "eval", "password": "secret"},
        "nodes": {
            "a": {"control": "http://127.0.0.1:30080",
                  "node_id": "node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                  "role": "ingest"},
            "b": {"control": "http://127.0.0.1:30180",
                  "node_id": "node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                  "role": "ingest"},
            "c": {"control": "http://127.0.0.1:30280",
                  "node_id": "node-cccccccccccccccccccccccccccccccccccccccccccccccc",
                  "role": "generate"},
            "p": {"control": "http://127.0.0.1:30380",
                  "node_id": "node-pppppppppppppppppppppppppppppppppppppppppppppppp",
                  "role": "expand"},
            "r": {"control": "http://127.0.0.1:30480",
                  "node_id": "node-rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr",
                  "role": "expand"},
        },
        "placement": {"pico": "a", "esp32": "b", "attention": "b"},
        "generation": {"node": "c"},
    }
    config.update(over)
    return config


def _scope():
    return {
        "manifest": {
            "scope_id": "scope-1",
            "expanded_members": [
                {"origin_node_id": "node-a", "collection_id": "col-a",
                 "operation": "corpus.retrieve"},
                {"origin_node_id": "node-b", "collection_id": "col-b",
                 "operation": "corpus.retrieve"},
            ],
        },
    }


# ------------------------------------------------------- 输入形状与待复核隔离

def test_source_cases_loader_keeps_human_review_pending(tmp_path):
    path = tmp_path / "source-cases.json"
    path.write_text(json.dumps(_source_cases_doc()), encoding="utf-8")
    loaded = routing_real.load_source_cases(path)
    assert loaded["human_review_status"] == "pending"
    hints = routing_real.reference_hints(loaded["cases"][0])
    assert hints and all(
        h["label_kind"] == "agent_extraction_pending_review" for h in hints)
    assert all("precision" not in json.dumps(h) for h in hints)


def test_source_cases_loader_rejects_non_pending_or_wrong_schema(tmp_path):
    doc = _source_cases_doc()
    doc["human_review_status"] = "approved"
    path = tmp_path / "source-cases.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="pending"):
        routing_real.load_source_cases(path)
    doc = _source_cases_doc()
    doc["schema"] = "something-else/9"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        routing_real.load_source_cases(path)


def test_source_sha_must_be_64_hex():
    routing_real.sha256_file_hint(
        sha256="757ff485227493b9fcc0c2c96c4dea9de020e1d8b2b11e2aa4f9ee8b25aa89eb")
    with pytest.raises(ValueError):
        routing_real.sha256_file_hint(sha256="not-a-digest")


# ------------------------------------------------------- 节点配置：五节点角色

def test_node_config_requires_full_abcdr_shape_and_roles():
    parsed = routing_real.validate_node_config(_nodes_config())
    assert parsed["a"]["role"] == "ingest" and parsed["c"]["role"] == "generate"
    assert parsed["p"]["role"] == "expand"
    assert parsed["a"]["sources"] == ["pico"]
    assert sorted(parsed["b"]["sources"]) == ["attention", "esp32"]


def test_node_config_rejects_missing_entry_or_schema():
    with pytest.raises(ValueError, match="entry"):
        routing_real.validate_node_config(
            _nodes_config(entry="zzz"))
    with pytest.raises(ValueError, match="schema"):
        routing_real.validate_node_config(
            _nodes_config(schema="wrong/1"))


def test_node_config_rejects_generate_or_expand_ingest():
    bad = _nodes_config(placement={"pico": "c"})
    with pytest.raises(ValueError, match="must not ingest"):
        routing_real.validate_node_config(bad)
    bad = _nodes_config(placement={"pico": "p"})
    with pytest.raises(ValueError, match="must not ingest"):
        routing_real.validate_node_config(bad)


def test_intent_keys_isolate_long_runs_modes_and_unambiguous_identities():
    first = routing_real.intent_keys("run" * 80, "case", "corpus.retrieve", "fast")
    assert first == routing_real.intent_keys(
        "run" * 80, "case", "corpus.retrieve", "fast")
    second = routing_real.intent_keys(
        "run" * 80, "case", "corpus.retrieve", "exhaustive_scope")
    assert first["intent"] != second["intent"]
    assert first["exec"] != second["exec"]
    assert first["intent"] != first["exec"]
    assert all(1 <= len(value) <= 128 for value in first.values())
    assert routing_real.intent_keys("run-a", "b", "retrieve", "fast") != (
        routing_real.intent_keys("run", "a-b", "retrieve", "fast"))


# ------------------------------------------------------- 合成/真实隔离

def test_real_report_schema_is_distinct_and_forbids_scores():
    report = {"schema": "ddp-routing-real-eval/1#Report", "run_id": "r1"}
    routing_real.assert_not_synthetic_report(report)
    with pytest.raises(ValueError):
        routing_real.assert_not_synthetic_report(
            {"schema": "ddp-routing-eval/1#Report"})
    with pytest.raises(ValueError):
        routing_real.assert_not_synthetic_report(
            {**report, "claim_support_precision": 0.9})
    with pytest.raises(ValueError):
        routing_real.assert_not_synthetic_report(
            {**report, "score": 1.0})


def test_real_report_paths_never_collide_with_synthetic(tmp_path):
    real = routing_real.real_report_path(tmp_path, run_id="abc123")
    assert real.name.startswith("real-routing-")
    assert not real.name.startswith("routing-")
    assert real.parent == tmp_path


# ------------------------------------------------------- 脱敏

def test_redaction_strips_credentials_but_keeps_route_shape():
    headers = routing_real.redact_headers({
        "Authorization": "Bearer secret",
        "Cookie": "session=abc",
        "X-DDP-Peer-Token": "peer-secret",
        "Content-Type": "application/json",
    })
    assert headers["Authorization"] == "[REDACTED]"
    assert headers["Cookie"] == "[REDACTED]"
    assert headers["X-DDP-Peer-Token"] == "[REDACTED]"
    assert headers["Content-Type"] == "application/json"

    url = ("https://objects.invalid/doc.pdf?X-Amz-Signature=deadbeef"
           "&X-Amz-Expires=300&token=abc")
    redacted = routing_real.redact_url(url)
    assert "deadbeef" not in redacted and "abc" not in redacted
    assert "objects.invalid" in redacted and "doc.pdf" in redacted

    record = routing_real.redact_for_log({
        "method": "POST", "url": url,
        "headers": {"Authorization": "Bearer secret"},
        "question": "secret question", "request_body": {"a": 1},
    })
    assert record["headers"]["Authorization"] == "[REDACTED]"
    assert record["question"] == "[WITHHELD]"
    assert record["request_body"] == "[WITHHELD]"
    assert record["method"] == "POST"


# ------------------------------------------------------- 成本只记真实发生

def test_cost_ledger_counts_real_bytes_not_estimates():
    ledger = routing_real.CostLedger(generation_tokens_cap=1024)
    ledger.add_exchange(request_bytes=100, response_bytes=500)
    ledger.add_exchange(request_bytes=0, response_bytes=0)
    ledger.phase("POST /api/uploads", 0.123)
    body = ledger.as_dict()
    assert body["requests"] == 2
    assert body["request_bytes"] == 100
    assert body["response_bytes"] == 500
    assert body["generation_tokens_actual"] is None
    assert body["generation_tokens_cap"] == 1024


# ------------------------------------------------------- 归属必须失败而非吞掉

def test_scope_attribution_rejects_out_of_scope_origin():
    scope = _scope()
    good = [{"origin_node_id": "node-a", "resource_id": "r",
             "source_version_id": "v", "evidence_id": "e",
             "collection_id": "col-a"}]
    routing_real.check_evidence_attribution(good, scope=scope)
    forged = [{"origin_node_id": "node-c", "resource_id": "r",
               "source_version_id": "v", "evidence_id": "e",
               "collection_id": "col-a"}]
    with pytest.raises(ValueError, match="attribution mismatch"):
        routing_real.check_evidence_attribution(forged, scope=scope)


def test_scope_attribution_rejects_rewritten_envelopes():
    scope = _scope()
    item = {"origin_node_id": "node-a", "resource_id": "r",
            "source_version_id": "v", "evidence_id": "e",
            "collection_id": "col-a"}
    routing_real.check_evidence_attribution(
        [
            item,
            {**item, "retrieval_receipt_ref": "another-route"},
            {**item, "origin_node_id": "node-b", "collection_id": "col-b",
             "resource_id": "other"},
        ],
        scope=scope,
    )
    with pytest.raises(ValueError):
        routing_real.check_evidence_attribution(
            [item, {**item, "resource_id": "other"}], scope=scope)


def test_coverage_must_cover_every_scope_target():
    scope = _scope()
    coverage = {"entries": [
        {"target_key": {"origin_node_id": "node-a", "collection_id": "col-a",
                        "operation": "corpus.retrieve"}, "state": "succeeded"},
        {"target_key": {"origin_node_id": "node-b", "collection_id": "col-b",
                        "operation": "corpus.retrieve"}, "state": "succeeded"},
    ]}
    routing_real.check_coverage_entries_cover_scope(coverage, scope)
    with pytest.raises(ValueError, match="drops scope targets"):
        routing_real.check_coverage_entries_cover_scope(
            {"entries": coverage["entries"][:1]}, scope)


def test_generation_ready_requires_envelope_authority():
    body = {
        "capability_status": "observed",
        "identity": {"authority_node_id": "node-c",
                     "environment_id": "node-c", "workspace_id": "org"},
        "profiles": [{"operation": "rag.answer.cited", "readiness": "ready",
                      "accepting_admissions": True}],
    }
    assert len(routing_real.generation_capability_envelope(body, "node-c")) == 1
    with pytest.raises(ValueError):
        routing_real.generation_capability_envelope(
            {**body, "identity": {"authority_node_id": "node-a"}}, "node-c")
    with pytest.raises(ValueError):
        routing_real.generation_capability_envelope(
            {**body, "capability_status": "unknown"}, "node-c")
    assert routing_real.generation_capability_envelope(
        {**body, "profiles": [{**body["profiles"][0], "accepting_admissions": False}]},
        "node-c",
    ) == []




def test_explicit_failure_reasons_are_closed():
    body = routing_real.explicit_failure("local_model_missing", "no chat channel")
    assert body["status"] == "failed"
    for reason in ("index_unavailable", "embedding_unavailable",
                   "resource_index_unavailable", "peer_unavailable",
                   "insufficient_evidence", "attribution_mismatch"):
        assert routing_real.explicit_failure(reason)["reason"] == reason
    with pytest.raises(ValueError):
        routing_real.explicit_failure("ready_anyway")


@pytest.fixture(scope="module")
def real_driver():
    return runpy.run_path(str(EVAL_DIR.parent / "scripts" / "eval_routing_real.py"))


@pytest.mark.parametrize(
    ("state", "reason"),
    [("failed", "task_failed"), ("cancelled", "task_cancelled")],
)
def test_unsuccessful_task_cannot_complete_evaluation(real_driver, state, reason):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"status": state, "result": {"evidence": []}}
        )
    )
    session = object.__new__(real_driver["_Session"])
    with httpx.Client(
        base_url="http://entry.test", transport=transport, trust_env=False
    ) as client:
        entry = SimpleNamespace(alias="a", http=client)
        with pytest.raises(real_driver["_ExplicitFail"]) as failure:
            session._poll_task(entry, "root", {"exchanges": []}, routing_real.CostLedger())
    assert failure.value.reason == reason


