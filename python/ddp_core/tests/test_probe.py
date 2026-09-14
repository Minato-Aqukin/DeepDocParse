import copy
import json

import pytest

from ddp_core.application.plans import canonical_bytes, content_digest, utc_instant
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import (
    INTERNAL_LIMIT_VALUES,
    PROBE_KINDS,
    build_probe,
    probe_digest,
    reusable,
    validate_probe,
)
from ddp_paths import CONTRACTS
from plan_samples import NOW

DIGEST = "sha256:" + "a" * 64
QUERY_DIGEST = "sha256:" + "b" * 64
OBSERVED = utc_instant(NOW)


def fixture(name):
    return json.loads((CONTRACTS / "fixtures" / name).read_text())


def check(**overrides):
    value = {"operation": "corpus.retrieve", "readiness": "ready", "input_validation": "metadata_only"}
    value.update(overrides)
    return value


def retrieval(status="succeeded", limits=()):
    return {"status": status, "collection_ref": "b:robotics", "index_revision": "index-42",
            "candidate_limit": 8, "continuation_ref": None, "evidence_set_ref": "b:evidence-set-31",
            "internal_limits": list(limits)}


def probe(**overrides):
    value = build_probe(
        probe_id="probe-b-21", target_node_id="node-b", task_spec_digest=DIGEST,
        consent_ref="consent-probe-4", probe_kind="evidence_retrieval", capability_check=check(),
        retrieval=retrieval(), can_generate=False, missing_requirements=(), offer=None, observed_at=OBSERVED)
    value.update(overrides)
    validate_probe(value)
    return value


def test_build_and_validate_roundtrip():
    value = probe()
    validate_probe(value)
    assert value["schema"] == "ddp-probe/1"
    assert value["retrieval"]["index_revision"] == "index-42"
    assert "scope_ref" not in value  # 由调用方按需补入
    value["scope_ref"] = "scope-17"
    validate_probe(value)


@pytest.mark.parametrize("name", [
    "valid/probe-evidence-succeeded.json",
    "valid/probe-evidence-partial.json",
    "valid/probe-with-offer.json",
])
def test_contract_valid_probe_fixtures_pass(name):
    validate_probe(fixture(name))


@pytest.mark.parametrize("name", [
    "invalid/probe-internal-limit-claims-success.json",
    "invalid/probe-evidence-without-retrieval.json",
    "invalid/offer-claims-reservation.json",
])
def test_contract_invalid_probe_fixtures_are_rejected(name):
    with pytest.raises(ApplicationError):
        validate_probe(fixture(name))


def test_probe_digest_ignores_observed_at_but_binds_content():
    first = probe()
    second = copy.deepcopy(first)
    second["observed_at"] = "2026-09-12T01:00:00Z"
    assert probe_digest(first) == probe_digest(second)
    changed = copy.deepcopy(first)
    changed["retrieval"]["status"] = "partial"
    changed["retrieval"]["internal_limits"] = ["shard_failed"]
    assert probe_digest(changed) != probe_digest(first)
    assert probe_digest(first) == content_digest(canonical_bytes(
        {key: value for key, value in first.items() if key != "observed_at"}))


def test_internal_limits_require_partial_status():
    with pytest.raises(ApplicationError, match="internal limits"):
        build_probe(
            probe_id="probe-b-21", target_node_id="node-b", task_spec_digest=DIGEST,
            consent_ref="consent-probe-4", probe_kind="evidence_retrieval",
            capability_check=check(), retrieval=retrieval(status="succeeded", limits=["shard_failed"]),
            observed_at=OBSERVED)
    partial = probe(retrieval=retrieval(status="partial", limits=["subset_only"]))
    validate_probe(partial)
    assert set(INTERNAL_LIMIT_VALUES) >= {"shard_failed", "subset_only"}


def test_evidence_retrieval_requires_retrieval_section():
    with pytest.raises(ApplicationError, match="retrieval"):
        build_probe(
            probe_id="probe-b-21", target_node_id="node-b", task_spec_digest=DIGEST,
            consent_ref="consent-probe-4", probe_kind="evidence_retrieval",
            capability_check=check(), retrieval=None, observed_at=OBSERVED)
    capability = build_probe(
        probe_id="probe-b-22", target_node_id="node-b", task_spec_digest=DIGEST,
        consent_ref="consent-probe-4", probe_kind="capability_input",
        capability_check=check(), retrieval=None, observed_at=OBSERVED)
    validate_probe(capability)


def test_can_generate_is_boolean_and_no_can_solve_field():
    with pytest.raises(ApplicationError):
        probe(can_generate="yes")
    with pytest.raises(ApplicationError):
        probe(can_solve=True)


def test_offer_never_reserves_and_must_be_an_interval():
    offer = {"offer_id": "offer-c-3", "target_node_id": "node-c", "plan_digest": DIGEST,
             "capability_revision": "cap-rev-5", "estimated_duration_seconds": {"low": 4, "high": 20},
             "valid_until": "2026-09-13T00:00:00Z", "reservation": False}
    validate_probe(probe(offer=offer))
    reserved = copy.deepcopy(offer)
    reserved["reservation"] = True
    with pytest.raises(ApplicationError, match="reserve"):
        probe(offer=reserved)
    inverted = copy.deepcopy(offer)
    inverted["estimated_duration_seconds"] = {"low": 20, "high": 4}
    with pytest.raises(ApplicationError, match="interval"):
        probe(offer=inverted)
    missing = copy.deepcopy(offer)
    del missing["reservation"]
    with pytest.raises(ApplicationError):
        probe(offer=missing)


def test_unknown_enum_values_and_shapes_are_rejected():
    assert PROBE_KINDS == ("capability_input", "resource_locate", "evidence_retrieval")
    with pytest.raises(ApplicationError):
        probe(retrieval={"status": "completed", "collection_ref": "b:robotics", "index_revision": "index-42"})
    with pytest.raises(ApplicationError):
        probe(capability_check=check(readiness="online"))
    with pytest.raises(ApplicationError):
        probe(target_node_id="B")
    with pytest.raises(ApplicationError):
        probe(task_spec_digest="not-a-digest")
    with pytest.raises(ApplicationError, match="unknown fields"):
        probe(scope="scope-17")


def test_reusable_checks_ttl_query_and_index_revision():
    value = probe()
    value["query_digest"] = QUERY_DIGEST  # 持久行带查询摘要，纯契约对象没有
    assert reusable(value, now=NOW, query_digest=QUERY_DIGEST, index_revision="index-42")
    assert reusable(value, now=NOW + 300)  # 恰好在 TTL 边界上仍可复用
    assert not reusable(value, now=NOW + 301)
    assert not reusable(value, now=NOW, query_digest="sha256:" + "c" * 64)
    assert not reusable(value, now=NOW, index_revision="index-99")


def test_reusable_requires_a_retrieval_revision_and_observed_at():
    capability = build_probe(
        probe_id="probe-b-22", target_node_id="node-b", task_spec_digest=DIGEST,
        consent_ref="consent-probe-4", probe_kind="capability_input",
        capability_check=check(), retrieval=None, observed_at=OBSERVED)
    assert not reusable(capability, now=NOW)
    missing = probe()
    del missing["observed_at"]
    assert not reusable(missing, now=NOW)
