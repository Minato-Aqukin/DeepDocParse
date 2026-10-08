"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from datetime import datetime

from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_peers import Delegation, PeerDirectory, PeerUnavailable
from ddp_core.application import plans

from ddp_corpus.federation_tasks.common import (_ts)

def _egress_denied(message: str) -> APIError:
    return APIError(403, message, "invalid_request_error", "egress_denied")

def peer_directory(actor: Actor, delegation: Delegation) -> PeerDirectory:
    """按登记的目录建出站客户端。测试 monkeypatch 它注入 ASGI transport。

    目录配置坏掉是部署问题（管理员配错 JSON / endpoint），不是调用方错误：
    503 并说明是哪条登记坏了，而不是 500。

    **`delegation` 是必填位置参数，不是可选项**：节点凭证的范围约束从它取
    （`root_task_id` 必带，探测再加 `task_spec_digest`）。做成可选的话，忘了传
    的调用点会拿到一个签不出凭证的目录，表现是每个远端目标都 unreachable ——
    一条"本节点配置不全"被伪装成"对端连不上"，正是本项目最怕的那类静默失败。
    """
    try:
        return PeerDirectory.from_settings(actor=actor, delegation=delegation)
    except PeerUnavailable as exc:
        raise APIError(503, str(exc), "server_error", "peer_directory_invalid") from None

# ---------------------------------------------------------------------------
# 许可校验（探索/执行）—— 全部 Fail Closed，失败一律 egress_denied
# ---------------------------------------------------------------------------


_EXPLORATION_FIELDS = {"schema", "consent_id", "granted_by", "granted_at", "valid_until",
                       "egress_mode", "allowed_payload", "allowed_recipients",
                       "trust_domain_revision", "budget"}

_EXECUTION_FIELDS = {"schema", "consent_id", "plan_digest", "granted_by", "granted_at",
                     "valid_until", "allowed_recipients", "allowed_edges",
                     "output_locations", "retention"}

_BUDGET_FIELDS = {"max_probe_requests", "max_egress_bytes", "max_discovery_requests"}

def _valid_node_list(value) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and plans.NODE.fullmatch(item) for item in value)
            and len(set(value)) == len(value))

def validate_exploration_consent(consent, task_spec: dict, *, now: datetime) -> dict:
    """结构 + 绑定校验；**缺、过期、与 TaskSpec 对不上都 403 egress_denied**。

    `trust_domain` 在 P4 密钥交换完成前没有可核验的固定接收方集合，按
    "没有有效许可"处理，而不是静默放行。本地目标不走这个门（它们不出网）。
    """
    if not isinstance(consent, dict) or set(consent) - _EXPLORATION_FIELDS:
        raise _egress_denied("exploration consent is missing or has unknown fields")
    if consent.get("schema") != "ddp-task-probe/1#ExplorationConsent":
        raise _egress_denied("unsupported exploration consent schema")
    if not isinstance(consent.get("consent_id"), str) or not consent["consent_id"].strip():
        raise _egress_denied("exploration consent has no consent_id")
    if not isinstance(consent.get("granted_by"), str) or not consent["granted_by"].strip():
        raise _egress_denied("exploration consent has no granted_by")
    try:
        plans.instant(consent.get("granted_at"))
        valid_until = plans.instant(consent.get("valid_until"))
    except ApplicationError:
        raise _egress_denied("exploration consent has invalid timestamps") from None
    if valid_until <= _ts(now):
        raise _egress_denied("exploration consent has expired")
    mode = consent.get("egress_mode")
    if mode not in ("local_only", "listed_nodes", "trust_domain"):
        raise _egress_denied("unknown exploration egress mode")
    payload = consent.get("allowed_payload")
    if (not isinstance(payload, list) or len(set(payload)) != len(payload)
            or any(item not in plans.PROBE_PAYLOADS for item in payload)):
        raise _egress_denied("exploration consent has invalid allowed_payload")
    recipients = consent.get("allowed_recipients", [])
    if not _valid_node_list(recipients):
        raise _egress_denied("exploration consent has invalid allowed_recipients")
    budget = consent.get("budget")
    if not isinstance(budget, dict) or set(budget) - _BUDGET_FIELDS:
        raise _egress_denied("exploration consent has an invalid budget")
    for key in ("max_probe_requests", "max_egress_bytes"):
        if type(budget.get(key)) is not int or budget[key] < 0:
            raise _egress_denied(f"exploration budget {key} must be a nonnegative integer")
    if "max_discovery_requests" in budget and (
            type(budget["max_discovery_requests"]) is not int
            or budget["max_discovery_requests"] < 0):
        raise _egress_denied("exploration budget max_discovery_requests must be nonnegative")
    if mode == "local_only":
        if payload or recipients or any(budget.values()):
            raise _egress_denied(
                "local_only exploration forbids payloads, recipients and remote budget")
    elif mode == "trust_domain":
        raise _egress_denied("trust_domain exploration has no verifiable recipient set yet")
    elif not recipients:
        raise _egress_denied("listed_nodes exploration requires fixed recipients")
    refs = task_spec.get("consent_refs") or {}
    if refs.get("exploration") != consent["consent_id"]:
        raise _egress_denied("task spec does not reference this exploration consent")
    return consent

def validate_execution_consent(consent, *, now: datetime) -> dict:
    """执行许可的结构与有效期校验；失败 403 egress_denied（缺/过期/形状坏）。"""
    if not isinstance(consent, dict) or set(consent) - _EXECUTION_FIELDS:
        raise _egress_denied("execution consent is missing or has unknown fields")
    if consent.get("schema") != "ddp-plan-admission/1#ExecutionConsent":
        raise _egress_denied("unsupported execution consent schema")
    for key in ("consent_id", "granted_by"):
        if not isinstance(consent.get(key), str) or not consent[key].strip():
            raise _egress_denied(f"execution consent has no {key}")
    if not isinstance(consent.get("plan_digest"), str) \
            or not plans.DIGEST.fullmatch(consent["plan_digest"]):
        raise _egress_denied("execution consent has an invalid plan_digest")
    try:
        plans.instant(consent.get("granted_at"))
        valid_until = plans.instant(consent.get("valid_until"))
    except ApplicationError:
        raise _egress_denied("execution consent has invalid timestamps") from None
    if valid_until <= _ts(now):
        raise _egress_denied("execution consent has expired")
    if not _valid_node_list(consent.get("allowed_recipients")) \
            or not consent["allowed_recipients"]:
        raise _egress_denied("execution consent needs a fixed nonempty recipient set")
    edges = consent.get("allowed_edges")
    if not isinstance(edges, list) or any(not isinstance(item, str) or not item for item in edges) \
            or len(set(edges)) != len(edges):
        raise _egress_denied("execution consent has invalid allowed_edges")
    if consent.get("retention") not in plans.RETENTION:
        raise _egress_denied("execution consent has an invalid retention class")
    if "output_locations" in consent and not isinstance(consent["output_locations"], list):
        raise _egress_denied("execution consent has invalid output_locations")
    return consent
