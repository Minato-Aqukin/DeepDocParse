"""联邦 Probe 回执的纯校验、摘要与复用判定（计划 §6.4）。

这一层不查库、不发请求：`retrieval.status=succeeded` 只说明本次检索按声明配置
跑完；节点内部有分片失败、索引落后或只查了子集时必须报 `internal_limits`，
此时外层只能是 `partial`（T85）。摘要排除 `observed_at` —— 同一探测内容在不同
时刻重放必须得到同一个摘要，否则"复用同一次探测"就无从按修订比对。
"""
from __future__ import annotations

from ddp_contracts.enums import (
    CAPABILITY_READINESS_VALUES,
    COVERAGE_TARGET_STATE_VALUES,
    INPUT_VALIDATION_VALUES,
)

from ddp_core.application import plans
from ddp_core.application.plans import canonical_bytes, content_digest
from ddp_core.application.ports import ApplicationError

PROBE_KINDS = ("capability_input", "resource_locate", "evidence_retrieval")
# internal_limits 是 schema 里的内联枚举，enums.yaml 没有对应生成常量，
# 只能在这里落一份；schema 变动时要同步改这里。
INTERNAL_LIMIT_VALUES = ("shard_failed", "index_lagging", "subset_only", "truncated_by_limit")
_DEFAULT_TTL_SECONDS = 300


def reject(code="protocol_incompatible", message="invalid probe result"):
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


def _enum(value, allowed, name):
    if value not in allowed:
        reject(message=f"unknown {name}")


def _epoch(value, name="instant"):
    try:
        return plans.instant(value)
    except ApplicationError:
        reject(message=f"{name} must be an RFC 3339 timestamp")


def _validate_offer(offer):
    _obj(offer, ("offer_id", "target_node_id", "plan_digest", "valid_until", "reservation"),
         ("capability_revision", "estimated_duration_seconds"), name="offer")
    _string(offer["offer_id"], name="offer id")
    _string(offer["target_node_id"], node=True, name="offer target node")
    _string(offer["plan_digest"], checksum=True, name="offer plan digest")
    _epoch(offer["valid_until"], "offer valid_until")
    if offer["reservation"] is not False:
        # const false：Offer 只是短时可执行意向，不默认占 GPU。
        reject(message="offers never reserve capacity")
    if "capability_revision" in offer:
        _string(offer["capability_revision"], name="capability revision")
    if "estimated_duration_seconds" in offer:
        estimate = offer["estimated_duration_seconds"]
        _obj(estimate, ("low", "high"), name="duration estimate")
        for key in ("low", "high"):
            value = estimate[key]
            if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
                reject(message="duration estimate must be nonnegative numbers")
        if estimate["low"] > estimate["high"]:
            reject(message="duration estimate must be an interval")


def _validate_retrieval(retrieval):
    _obj(retrieval, ("status", "collection_ref", "index_revision"),
         ("candidate_limit", "continuation_ref", "evidence_set_ref", "internal_limits"), name="retrieval")
    _enum(retrieval["status"], COVERAGE_TARGET_STATE_VALUES, "retrieval status")
    _string(retrieval["collection_ref"], name="collection ref")
    _string(retrieval["index_revision"], name="index revision")
    if "candidate_limit" in retrieval:
        if type(retrieval["candidate_limit"]) is not int or retrieval["candidate_limit"] < 1:
            reject(message="candidate_limit must be a positive integer")
    for key in ("continuation_ref", "evidence_set_ref"):
        if retrieval.get(key) is not None:
            _string(retrieval[key], empty=True, name=key)
    limits = retrieval.get("internal_limits", [])
    if not isinstance(limits, list):
        reject(message="internal_limits must be an array")
    for item in limits:
        _enum(item, INTERNAL_LIMIT_VALUES, "internal limit")
    if limits and retrieval["status"] != "partial":
        # T85 的机器版本：内部不完整却在外层报成功，是覆盖诚实性要求为 0 的错误。
        reject("partial_retrieval", "internal limits require retrieval status partial")


def validate_probe(probe: dict) -> None:
    """校验 ddp-task-probe/1#ProbeResult 的字段、枚举与全部 allOf 规则。"""
    _obj(probe, ("schema", "probe_id", "target_node_id", "task_spec_digest", "consent_ref",
                 "probe_kind", "capability_check", "can_generate", "observed_at"),
         ("scope_ref", "retrieval", "missing_requirements", "offer"), name="probe result")
    if probe["schema"] != "ddp-probe/1":
        reject(message="unsupported probe schema")
    _string(probe["probe_id"], name="probe id")
    _string(probe["target_node_id"], node=True, name="target node")
    _string(probe["task_spec_digest"], checksum=True, name="task spec digest")
    if probe.get("scope_ref") is not None:
        _string(probe["scope_ref"], empty=True, name="scope ref")
    _string(probe["consent_ref"], name="consent ref")
    _enum(probe["probe_kind"], PROBE_KINDS, "probe kind")
    check = probe["capability_check"]
    _obj(check, ("operation", "readiness"), ("input_validation",), name="capability check")
    _string(check["operation"], name="capability operation")
    _enum(check["readiness"], CAPABILITY_READINESS_VALUES, "capability readiness")
    if "input_validation" in check:
        _enum(check["input_validation"], INPUT_VALIDATION_VALUES, "input validation")
    if type(probe["can_generate"]) is not bool:
        # 不是 can_solve：它只回答"能否跑生成"，不回答"这次任务能不能成"。
        reject(message="can_generate must be a boolean capability, not a success claim")
    retrieval = probe.get("retrieval")
    if retrieval is not None:
        _validate_retrieval(retrieval)
    elif probe["probe_kind"] == "evidence_retrieval":
        reject(message="evidence retrieval probe requires the retrieval section")
    missing = probe.get("missing_requirements", [])
    if not isinstance(missing, list) or len(missing) > 1000:
        reject(message="missing_requirements must be an array")
    for item in missing:
        _string(item, empty=True, name="missing requirement")
    if probe.get("offer") is not None:
        _validate_offer(probe["offer"])
    _epoch(probe["observed_at"], "observed_at")


def build_probe(*, probe_id, target_node_id, task_spec_digest, consent_ref,
                probe_kind, capability_check, retrieval=None, can_generate=False,
                missing_requirements=(), offer=None, observed_at) -> dict:
    """构造一个契约合法的 ProbeResult；scope_ref 由调用方按需补入。"""
    probe = {
        "schema": "ddp-probe/1",
        "probe_id": probe_id,
        "target_node_id": target_node_id,
        "task_spec_digest": task_spec_digest,
        "consent_ref": consent_ref,
        "probe_kind": probe_kind,
        "capability_check": capability_check,
        "retrieval": retrieval,
        "can_generate": can_generate,
        "missing_requirements": list(missing_requirements),
        "offer": offer,
        "observed_at": observed_at,
    }
    validate_probe(probe)
    return probe


def probe_digest(probe: dict) -> str:
    """canonical digest，不含 observed_at —— 时刻不参与"这两次探测是不是同一次"。"""
    if not isinstance(probe, dict):
        reject(message="probe must be an object")
    return content_digest(canonical_bytes({key: value for key, value in probe.items() if key != "observed_at"}))


def reusable(probe: dict, *, now, query_digest=None, index_revision=None, ttl_seconds=_DEFAULT_TTL_SECONDS) -> bool:
    """探测结果只有满足有效期与修订一致才可复用；否则一律重新探测。

    `query_digest` 可能来自持久行而不是纯契约对象；缺失按"对不上"处理，
    不把不相关的旧检索洗成新证据。没有 `retrieval.index_revision` 的
    能力探测对证据路不可复用。
    """
    if not isinstance(probe, dict):
        reject(message="probe must be an object")
    if type(ttl_seconds) is not int or ttl_seconds < 0:
        reject(message="ttl must be a nonnegative integer")
    if type(now) not in (int, float) or isinstance(now, bool):
        reject(message="now must be a timestamp")
    observed = probe.get("observed_at")
    if not isinstance(observed, str):
        return False
    try:
        age = now - _epoch(observed, "observed_at")
    except ApplicationError:
        return False
    if age > ttl_seconds:
        return False
    if query_digest is not None and probe.get("query_digest") != query_digest:
        return False
    retrieval = probe.get("retrieval") or {}
    revision = retrieval.get("index_revision")
    if not revision:
        return False
    if index_revision is not None and revision != index_revision:
        return False
    return True
