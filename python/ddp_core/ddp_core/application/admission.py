"""正式接单回执与幂等对账（计划 §6.6、T80、T82）。

受理不等于算力预留，也不等于计算成功：`accepted` 必须同时有执行任务、已校验
输入摘要、`input_validation=content_verified` 与受理时刻；`waiting_input` 阶段
不得占 GPU，也不得已有校验摘要。`unknown` 是"回执丢了、待对账"，不是失败 ——
调用方不能据此把有副作用的步骤换个节点重做（T82）。
"""
from __future__ import annotations

from ddp_contracts.enums import ADMISSION_STATE_VALUES, INPUT_VALIDATION_VALUES

from ddp_core.application import plans
from ddp_core.application.plans import canonical_bytes, content_digest
from ddp_core.application.ports import ApplicationError

_REQUIRED = ("schema", "admission_id", "issuer_node_id", "executor_node_id", "root_task_id",
             "step_id", "delegation_generation", "idempotency_key", "request_digest",
             "plan_digest", "state", "input_validation", "receipt_revision", "effective_policy_ref")
_OPTIONAL = ("executor_task_id", "verified_input_manifest_digest", "accepted_at", "quota_decision_ref")


def reject(code="protocol_incompatible", message="invalid admission receipt"):
    raise ApplicationError(code, message)


def _obj(value, required, optional=(), name="object"):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        reject(message=f"invalid {name}: missing or unknown fields")


def _string(value, *, node=False, checksum=False, empty=False, name="field"):
    if not isinstance(value, str) or (not empty and not value) or len(value) > 65536:
        reject(message=f"invalid {name}")
    if node and not plans.NODE.fullmatch(value):
        reject(message="invalid node identity")
    if checksum and not plans.DIGEST.fullmatch(value):
        reject(message="invalid content digest")


def _integer(value, minimum=0, name="integer"):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        reject(message=f"invalid {name}")


def _epoch(value, name="instant"):
    try:
        return plans.instant(value)
    except ApplicationError:
        reject(message=f"{name} must be an RFC 3339 timestamp")


def request_digest(body: dict) -> str:
    """canonical digest：同键异摘要必须能当场对出来，不能靠对象身份或字段顺序。"""
    if not isinstance(body, dict):
        reject(message="admission request must be an object")
    return content_digest(canonical_bytes(body))


def validate_receipt(receipt: dict) -> None:
    """校验 ddp-plan-admission/1#AdmissionReceipt 的字段与两条 allOf 规则。"""
    _obj(receipt, _REQUIRED, _OPTIONAL, name="admission receipt")
    if receipt["schema"] != "ddp-plan-admission/1#AdmissionReceipt":
        reject(message="unsupported admission schema")
    for key in ("admission_id", "root_task_id", "step_id", "idempotency_key", "effective_policy_ref"):
        _string(receipt[key], name=key)
    _string(receipt["issuer_node_id"], node=True, name="issuer node")
    _string(receipt["executor_node_id"], node=True, name="executor node")
    _integer(receipt["delegation_generation"], name="delegation generation")
    _integer(receipt["receipt_revision"], 1, name="receipt revision")
    _string(receipt["request_digest"], checksum=True, name="request digest")
    _string(receipt["plan_digest"], checksum=True, name="plan digest")
    if receipt["state"] not in ADMISSION_STATE_VALUES:
        reject(message="unknown admission state")
    if receipt["input_validation"] not in INPUT_VALIDATION_VALUES:
        reject(message="unknown input validation")
    if receipt.get("executor_task_id") is not None:
        _string(receipt["executor_task_id"], name="executor task id")
    if receipt.get("quota_decision_ref") is not None:
        _string(receipt["quota_decision_ref"], name="quota decision ref")
    if receipt.get("verified_input_manifest_digest") is not None:
        _string(receipt["verified_input_manifest_digest"], checksum=True, name="verified input manifest digest")
    if receipt.get("accepted_at") is not None:
        _epoch(receipt["accepted_at"], "accepted_at")
    if receipt["state"] == "accepted":
        if not receipt.get("executor_task_id") or not receipt.get("verified_input_manifest_digest"):
            # T78：只看文件描述就受理，等于信任客户端声明的哈希。
            reject("input_not_verified", "accepted receipt requires an executor task and a verified input manifest")
        if receipt["input_validation"] != "content_verified":
            reject("input_not_verified", "accepted receipt requires content-verified input")
        if receipt.get("accepted_at") is None:
            reject(message="accepted receipt requires an acceptance time")
    if receipt["state"] == "waiting_input" and receipt.get("verified_input_manifest_digest") is not None:
        reject(message="waiting_input receipt cannot already carry a verified input digest")


def reuse(existing: dict | None, *, idempotency_key, request_digest) -> str:
    """同键同摘要返回 "reuse"，同键异摘要抛 `idempotency_conflict`，未知键新建。"""
    _string(idempotency_key, name="idempotency key")
    _string(request_digest, checksum=True, name="request digest")
    if existing is None:
        return "create"
    if not isinstance(existing, dict):
        reject(message="existing admission must be an object")
    if existing.get("idempotency_key") != idempotency_key:
        return "create"
    if existing.get("request_digest") != request_digest:
        raise ApplicationError("idempotency_conflict", "same idempotency key with a different request body")
    return "reuse"


def receipt(*, admission_id, issuer_node_id, executor_node_id, root_task_id, step_id,
            delegation_generation, idempotency_key, request_digest, plan_digest,
            state, input_validation, receipt_revision, effective_policy_ref,
            executor_task_id=None, verified_input_manifest_digest=None,
            accepted_at=None, quota_decision_ref=None) -> dict:
    """构造一个契约合法的 AdmissionReceipt；不合法当场拒绝，不落库半截状态。"""
    value = {
        "schema": "ddp-plan-admission/1#AdmissionReceipt",
        "admission_id": admission_id,
        "executor_task_id": executor_task_id,
        "issuer_node_id": issuer_node_id,
        "executor_node_id": executor_node_id,
        "root_task_id": root_task_id,
        "step_id": step_id,
        "delegation_generation": delegation_generation,
        "idempotency_key": idempotency_key,
        "request_digest": request_digest,
        "plan_digest": plan_digest,
        "verified_input_manifest_digest": verified_input_manifest_digest,
        "state": state,
        "input_validation": input_validation,
        "accepted_at": accepted_at,
        "receipt_revision": receipt_revision,
        "quota_decision_ref": quota_decision_ref,
        "effective_policy_ref": effective_policy_ref,
    }
    validate_receipt(value)
    return value
