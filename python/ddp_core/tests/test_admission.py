import copy
import json

import pytest

from ddp_core.application.admission import receipt, request_digest, reuse, validate_receipt
from ddp_core.application.plans import canonical_bytes, content_digest
from ddp_core.application.ports import ApplicationError
from ddp_paths import CONTRACTS

DIGEST = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
KEY = "task-9:retrieve-1:gen0"


def fixture(name):
    return json.loads((CONTRACTS / "fixtures" / name).read_text())


def body():
    return {"schema": "ddp-plan-admission/1#AdmissionRequest", "idempotency_key": KEY,
            "root_task_id": "task-9", "step_id": "retrieve-1", "delegation_generation": 0,
            "task_spec": {"operation": "rag.answer.cited"}, "plan": {"plan_id": "plan-1"},
            "execution_consent": {"consent_id": "consent-7"},
            "inputs": [{"ref": "query", "digest": DIGEST, "size_bytes": 23}]}


def base(**overrides):
    value = {"schema": "ddp-plan-admission/1#AdmissionReceipt",
             "admission_id": "adm-7", "issuer_node_id": "node-a", "executor_node_id": "node-c",
             "root_task_id": "task-9", "step_id": "retrieve-1", "delegation_generation": 0,
             "idempotency_key": KEY, "request_digest": DIGEST, "plan_digest": DIGEST_B,
             "state": "accepted", "input_validation": "content_verified", "receipt_revision": 1,
             "effective_policy_ref": "policy-rev-11", "executor_task_id": "c-task-55",
             "verified_input_manifest_digest": DIGEST, "accepted_at": "2026-09-13T00:00:00Z",
             "quota_decision_ref": "quota-3"}
    value.update(overrides)
    return value


def builder_kwargs(**overrides):
    value = dict(admission_id="adm-7", issuer_node_id="node-a", executor_node_id="node-c",
                 root_task_id="task-9", step_id="retrieve-1", delegation_generation=0,
                 idempotency_key=KEY, request_digest=DIGEST, plan_digest=DIGEST_B,
                 state="accepted", input_validation="content_verified", receipt_revision=1,
                 effective_policy_ref="policy-rev-11", executor_task_id="c-task-55",
                 verified_input_manifest_digest=DIGEST, accepted_at="2026-09-13T00:00:00Z")
    value.update(overrides)
    return value


def test_request_digest_is_canonical_and_binds_the_whole_body():
    first = body()
    reordered = dict(reversed(list(first.items())))
    assert request_digest(first) == request_digest(reordered)
    assert request_digest(first) == content_digest(canonical_bytes(first))
    changed = copy.deepcopy(first)
    changed["step_id"] = "answer-1"
    assert request_digest(changed) != request_digest(first)
    with pytest.raises(ApplicationError):
        request_digest(["not", "an", "object"])


def test_reuse_reports_create_reuse_and_conflict():
    existing = {"idempotency_key": KEY, "request_digest": DIGEST}
    assert reuse(None, idempotency_key=KEY, request_digest=DIGEST) == "create"
    assert reuse(existing, idempotency_key=KEY, request_digest=DIGEST) == "reuse"
    assert reuse(existing, idempotency_key="other-key", request_digest=DIGEST) == "create"
    with pytest.raises(ApplicationError) as exc:
        reuse(existing, idempotency_key=KEY, request_digest=DIGEST_B)
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.parametrize("name", ["valid/admission-accepted.json", "valid/admission-waiting-input.json"])
def test_contract_valid_receipt_fixtures_pass(name):
    validate_receipt(fixture(name))


@pytest.mark.parametrize("name", [
    "invalid/admission-accepted-metadata-only.json",
    "invalid/admission-accepted-without-verified-input.json",
    "invalid/admission-waiting-with-verified-input.json",
])
def test_contract_invalid_receipt_fixtures_are_rejected(name):
    with pytest.raises(ApplicationError):
        validate_receipt(fixture(name))


def test_receipt_builder_enforces_the_accepted_triplet():
    value = receipt(**builder_kwargs())
    validate_receipt(value)
    assert value["schema"] == "ddp-plan-admission/1#AdmissionReceipt"
    for overrides, code in (
        ({"executor_task_id": None}, "input_not_verified"),
        ({"verified_input_manifest_digest": None}, "input_not_verified"),
        ({"input_validation": "metadata_only"}, "input_not_verified"),
        ({"accepted_at": None}, "protocol_incompatible"),
    ):
        with pytest.raises(ApplicationError) as exc:
            receipt(**builder_kwargs(**overrides))
        assert exc.value.code == code


def test_waiting_input_must_not_carry_a_verified_input_digest():
    value = base(state="waiting_input", verified_input_manifest_digest=None, accepted_at=None,
                 input_validation="metadata_only")
    validate_receipt(value)
    with pytest.raises(ApplicationError, match="verified input"):
        validate_receipt({**value, "verified_input_manifest_digest": DIGEST})


def test_unknown_state_is_pending_reconciliation_not_a_failure():
    value = base(state="unknown", executor_task_id=None, verified_input_manifest_digest=None,
                 accepted_at=None, input_validation="metadata_only")
    validate_receipt(value)
    assert value["state"] == "unknown"


def test_receipt_rejects_bad_bounds_nodes_digests_and_unknown_fields():
    with pytest.raises(ApplicationError):
        validate_receipt(base(delegation_generation=-1))
    with pytest.raises(ApplicationError):
        validate_receipt(base(receipt_revision=0))
    with pytest.raises(ApplicationError):
        validate_receipt(base(executor_node_id="node C"))
    with pytest.raises(ApplicationError):
        validate_receipt(base(request_digest="deadbeef"))
    with pytest.raises(ApplicationError):
        validate_receipt(base(state="accepted_queued"))
    with pytest.raises(ApplicationError, match="unknown fields"):
        validate_receipt(base(can_solve=True))
