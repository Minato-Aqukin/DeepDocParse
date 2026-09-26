"""真实联邦评测的纯逻辑：模式常量、脱敏、成本记账、归属校验、失败显式化。

本模块无任何网络 I/O，可被 `eval/tests/test_routing_real_contract.py`
直接断言。真实 HTTP 流程见 `scripts/eval_routing_real.py`。

合成夹具（`eval/routing/*`）与真实评测在此彻底分开：
- 合成报告 schema：`ddp-routing-eval/1#Report`
- 真实报告 schema：`ddp-routing-real-eval/1#Report`
两者永不共用同一文件名前缀与同一输出目录下的可比数字。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlunsplit

#: 节点配置 schema（`scripts/eval_routing_real.py --nodes` 的文件形状，
#: 完整说明见该驱动的模块 docstring）。
NODE_CONFIG_SCHEMA = "ddp-real-eval-nodes/1"
#: 节点角色：ingest 可入库（placement 唯一合法目标）；generate 只生成不存源；
#: expand 只经信任展开参与，不可被驱动直接投放上传冒充。
NODE_ROLES = frozenset({"ingest", "generate", "expand"})

SYNTHETIC_REPORT_SCHEMA = "ddp-routing-eval/1#Report"
REAL_REPORT_SCHEMA = "ddp-routing-real-eval/1#Report"
REAL_INPUT_SCHEMA = "ddp-real-source-evaluation-input/1"

#: 真实评测产物强制的人工复核状态。提取的六题只是 agent 摘录，
#: 永不写成人类标注。
HUMAN_REVIEW_STATUS = "pending"

#: 真实评测允许的显式失败原因。缺模型/缺索引必须落到这些原因之一，
#: 永不把 DB/readyz 洗成"就绪"。`attribution_mismatch` 是来源归属错误的
#: 专用失败：必须失败而非吞掉。
EXPLICIT_FAILURE_REASONS = frozenset({
    "local_model_missing",
    "insufficient_evidence",
    "upstream_error",
    "no_model_output",
    "budget_exceeded",
    "unsupported_generation",
    "evidence_excerpt_unavailable",
    "peer_unavailable",
    "index_unavailable",
    "embedding_unavailable",
    "resource_index_unavailable",
    "probe_receipt_missing",
    "attribution_mismatch",
    "degraded",
    "task_failed",
    "task_cancelled",
})

#: 日志/产物中必须脱敏的请求头（大小写不敏感）。
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie",
    "x-ddp-peer-token", "x-ddp-api-key", "x-api-key", "api-key",
})

#: 预签名/凭据类查询参数：只保留参数名，值一律打码。
_SENSITIVE_QUERY_KEYS = frozenset({
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "signature", "token", "key", "accesskey", "secretkey", "sig",
})


def redact_headers(headers: dict) -> dict:
    """脱敏出站/入站头：凭据一律记为 `[REDACTED]`，其余原样保留。"""
    out = {}
    for name, value in dict(headers or {}).items():
        if str(name).lower() in _SENSITIVE_HEADERS or "token" in str(name).lower():
            out[name] = "[REDACTED]"
        else:
            out[name] = value
    return out


def redact_url(url: str) -> str:
    """脱敏 URL：host/path 保留用于复核连通性，查询串的值全部打码。

    预签名 URL 的查询参数（签名/过期/凭据）永不进评测产物。
    """
    parts = urlsplit(url or "")
    if not parts.query:
        return url
    kept = []
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _SENSITIVE_QUERY_KEYS or "sig" in lowered or "token" in lowered:
            kept.append(f"{key}=[REDACTED]")
        else:
            kept.append(f"{key}=[REDACTED]")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), ""))


def redact_for_log(record: dict) -> dict:
    """一条请求日志记录的脱敏投影：method/path/latency 保留，凭据清零。"""
    out = dict(record or {})
    if "headers" in out and isinstance(out["headers"], dict):
        out["headers"] = redact_headers(out["headers"])
    if "url" in out and isinstance(out["url"], str):
        out["url"] = redact_url(out["url"])
    for key in ("request_body", "response_body", "excerpt", "question", "payload"):
        if key in out:
            out[key] = "[WITHHELD]"
    return out


@dataclass
class CostLedger:
    """实际开销账本：只记真实发生的 requests/bytes/latency。

    bytes 按 httpx 实际收发字节累计；tokens 只记模型侧如实返回的
    usage（拿不到就记 None，调用方另记确定性上限为 cap，永不把上限
    写成实际消耗）。
    """

    requests: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    generation_tokens_actual: int | None = None
    generation_tokens_cap: int | None = None
    phases: dict = field(default_factory=dict)

    def add_exchange(self, *, request_bytes: int = 0, response_bytes: int = 0) -> None:
        self.requests += 1
        self.request_bytes += int(request_bytes or 0)
        self.response_bytes += int(response_bytes or 0)

    def phase(self, name: str, seconds: float) -> None:
        self.phases[name] = round(float(seconds), 3)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "generation_tokens_actual": self.generation_tokens_actual,
            "generation_tokens_cap": self.generation_tokens_cap,
            "phases_seconds": dict(self.phases),
        }




def explicit_failure(reason: str, detail: str = "") -> dict:
    """构造显式失败结果：原因必须在允许集合内，永不伪装成就绪。"""
    if reason not in EXPLICIT_FAILURE_REASONS:
        raise ValueError(f"unknown explicit failure reason {reason!r}")
    return {"status": "failed", "reason": reason, "detail": detail[:200]}


def reference_hints(case: dict) -> list[dict]:
    """把 source-cases.json 的 references 降级为“待复核提示”，不是标签。

    返回的每条都带 `label_kind: "agent_extraction_pending_review"`，
    调用方必须原样写入产物并保持 `human_review_status=pending`。
    """
    out = []
    for ref in case.get("references", []) or []:
        out.append({
            "label_kind": "agent_extraction_pending_review",
            "source": ref.get("source"),
            "page_index": ref.get("page_index"),
            "excerpt": ref.get("excerpt"),
            "facts": ref.get("facts", {}),
        })
    return out


def load_source_cases(path: str | Path) -> dict:
    """加载 source-cases.json：只做形状校验，不做任何“标注正确性”断言。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != REAL_INPUT_SCHEMA:
        raise ValueError(f"unexpected source-cases schema {data.get('schema')!r}")
    if data.get("human_review_status") != HUMAN_REVIEW_STATUS:
        raise ValueError("source-cases must stay human_review_status=pending")
    if not isinstance(data.get("sources"), list) or not isinstance(data.get("cases"), list):
        raise ValueError("source-cases needs sources[] and cases[]")
    return data


def real_report_path(output_dir: str | Path, *, run_id: str) -> Path:
    """真实报告路径：前缀 `real-routing-`，永不与合成 `routing-<digest>.json` 碰撞。"""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", run_id).strip("-") or "run"
    return Path(output_dir) / f"real-routing-{safe}.json"


def assert_not_synthetic_report(report: dict) -> None:
    """真实/合成隔离守卫：真实产物里不许出现合成口径的单一质量分。"""
    if report.get("schema") != REAL_REPORT_SCHEMA:
        raise ValueError("real report must carry ddp-routing-real-eval/1#Report")
    for forbidden in ("score", "quality_score", "claim_support_precision",
                      "human_label_accuracy", "recall_denominator_assumed"):
        if forbidden in report:
            raise ValueError(f"real report must not fabricate {forbidden!r}")


def sha256_file_hint(*, sha256: str) -> str:
    lowered = (sha256 or "").lower()
    if len(lowered) != 64 or any(c not in "0123456789abcdef" for c in lowered):
        raise ValueError("source sha256 must be 64 hex chars")
    return lowered


def now_seconds() -> float:
    return time.monotonic()


def validate_node_config(config: dict) -> dict:
    """校验节点配置：返回 `{node_alias: {control, node_id, role, sources}}`。

    完整 schema 见 `scripts/eval_routing_real.py` 模块 docstring
   （`schema="ddp-real-eval-nodes/1"`，`entry`、`placement` 必填）。
    - 每个节点必须带 `control`（其 control-api 基址）；
    - `node_id` 在运行期从该 control 的权威描述
     （`GET /api/v1/federation/node` 的 `authority_node_id`）逐一比对，
      这里只做形状校验；
    - `role` 缺省 `ingest`；`placement` 把每个 source id 落到 exactly 一个
      ingest 节点；`role=generate` 的 C 与 `role=expand` 的 P/R 永不出现在
      placement（驱动永不向它们上传 PDF 冒充来源）。
    违反一律 ValueError（驱动转 exit 2，不猜、不 fallback）。"""
    if not isinstance(config, dict):
        raise ValueError("node config must be an object")
    if config.get("schema") != NODE_CONFIG_SCHEMA:
        raise ValueError(
            f"node config schema must be {NODE_CONFIG_SCHEMA!r}")
    nodes = config.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("node config needs nodes{}")
    entry = config.get("entry")
    if not isinstance(entry, str) or entry not in nodes:
        raise ValueError("node config entry must name one of nodes{}")
    placement = config.get("placement")
    if not isinstance(placement, dict) or not placement:
        raise ValueError(
            "node config placement must map every source id to an ingest node")
    for sid, alias in placement.items():
        if not isinstance(alias, str) or alias not in nodes:
            raise ValueError(f"placement of {sid!r} targets unknown node {alias!r}")
    out = {}
    for alias, spec in nodes.items():
        if not isinstance(spec, dict) or not spec.get("control"):
            raise ValueError(f"node {alias!r} needs control base url")
        role = spec.get("role", "ingest")
        if role not in NODE_ROLES:
            raise ValueError(f"node {alias!r} has unknown role {role!r}")
        out[alias] = {
            "control": str(spec["control"]).rstrip("/"),
            "node_id": spec.get("node_id"),
            "role": role,
            "sources": sorted(sid for sid, target in placement.items() if target == alias),
        }
    for alias, spec in out.items():
        if spec["role"] != "ingest" and spec["sources"]:
            raise ValueError(
                f"node {alias!r} with role {spec['role']!r} must not ingest sources")
    ingest_targets = {alias for alias, spec in out.items() if spec["role"] == "ingest"}
    for sid, alias in placement.items():
        if alias not in ingest_targets:
            raise ValueError(
                f"source {sid!r} is placed on non-ingest node {alias!r}")
    return out


def intent_keys(run_id: str, case_id: str, operation: str, mode: str) -> dict:
    """同一 case 在不同 operation/mode 下的幂等键必须不同体。

    同键异体是 409 `idempotency_conflict`；键里不带 operation/mode 会让
    第二种跑法永远撞上第一种的 intent。返回 `{"intent": ..., "exec": ...}`，
    长度收进服务端 1..128 限制。"""
    identity = json.dumps([run_id, case_id, operation, mode],
                          ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(identity).hexdigest()
    return {"intent": f"real-{digest}", "exec": f"exec-real-{digest}"}


def check_scope_targets_cover_placement(scope: dict, *, expected: list[dict]) -> None:
    """scope 覆盖检查：placement 期望的每个 `(origin_node_id, collection_id)`
    都必须出现在 scope manifest 的 expanded_members 里，否则显式失败。"""
    members = {(m.get("origin_node_id"), m.get("collection_id"))
               for m in (scope.get("manifest") or scope).get("expanded_members", [])}
    for item in expected or []:
        key = (item.get("origin_node_id"), item.get("collection_id"))
        if key not in members:
            raise ValueError(f"scope does not cover placement target {key!r}")


def check_coverage_entries_cover_scope(coverage: dict, scope: dict) -> None:
    """覆盖账本分母检查：账本 entries 必须覆盖 scope 的每个 expanded_member。"""
    wanted = {(m.get("origin_node_id"), m.get("collection_id"), m.get("operation"))
              for m in (scope.get("manifest") or scope).get("expanded_members", [])}
    seen = {(e.get("target_key", {}).get("origin_node_id"),
             e.get("target_key", {}).get("collection_id"),
             e.get("target_key", {}).get("operation"))
            for e in coverage.get("entries", [])}
    missing = wanted - seen
    if missing:
        raise ValueError(f"coverage ledger drops scope targets {sorted(missing)!r}")
def check_evidence_attribution(evidence: list[dict], *, scope: dict) -> None:
    """联邦归属检查：每条证据的 `origin_node_id` 必须落在 scope 的来源集合里，
    且 `(origin_node_id, collection_id)`（当证据带 collection_id 时）必须落在
    expanded_members 里；C 的转述永不提升为来源证据。
    越界一律抛 ValueError（调用方记 `attribution_mismatch`，不吞掉）。"""
    members = {(m.get("origin_node_id"), m.get("collection_id"))
               for m in (scope.get("manifest") or scope).get("expanded_members", [])}
    origins = {m.get("origin_node_id") for m in
               (scope.get("manifest") or scope).get("expanded_members", [])}
    seen: dict[tuple, tuple] = {}
    for item in evidence or []:
        key = (item.get("origin_node_id"), item.get("evidence_id"))
        source_binding = tuple(item.get(field) for field in (
            "resource_id", "source_version_id", "parse_revision", "source_digest",
            "locator", "source_type", "derived_from",
        ))
        if item.get("origin_node_id") not in origins:
            raise ValueError(
                f"attribution mismatch: evidence {item.get('evidence_id')!r} "
                f"claims origin {item.get('origin_node_id')!r} "
                f"outside the frozen scope origins {sorted(origins)!r}")
        locator = (item.get("origin_node_id"), item.get("collection_id"))
        if item.get("collection_id") is not None and locator not in members:
            raise ValueError(
                f"attribution mismatch: evidence {item.get('evidence_id')!r} "
                f"from {locator!r} is outside the frozen scope")
        if key in seen and seen[key] != source_binding:
            raise ValueError(f"attribution envelope rewritten for {key!r}")
        seen[key] = source_binding


def generation_ready_profiles(profiles: list[dict]) -> list[dict]:
    """C 生成就绪的纯判断：`operation=rag.answer.cited` 且 `readiness=ready`。

    只看 corpus 侧档案自身的 operation/readiness（corpus 档案本来就没有
    node_id）。节点归属由 `generation_capability_envelope` 在 control 外层
    身份信封上校验，永不对档案臆造 node_id。传 endpoint 配置、注册表名字、
    模型名字都不算就绪证明。"""
    return [p for p in profiles or []
            if isinstance(p, dict) and p.get("operation") == "rag.answer.cited"
            and p.get("readiness") == "ready"
            and p.get("accepting_admissions") is True]


def generation_capability_envelope(body: dict, expected_node_id: str) -> list[dict]:
    """校验某节点 control 能力响应的外层信封，再筛就绪档案。

    真实形状（`discovery_handlers.go:116`）：`{identity: {environment_id,
    authority_node_id, workspace_id}, profile: {issuer, subject}, profiles:
    [无 node_id 的单条 CapabilityProfile], capability_status, ...}`。
    要求 `capability_status == "observed"`（control 对上游未知/过期一律报
    unknown，不把配置当 ready），且 `identity.authority_node_id` 必须等于
    生成节点 C 的权威 id（向 C 自身 control 取，不要求 C 出现在 entry 的
    全域档案里）。不满足一律 ValueError（调用方记 `local_model_missing`）。"""
    if not isinstance(body, dict):
        raise ValueError("capabilities response is not an object")
    if body.get("capability_status") != "observed":
        raise ValueError(
            f"capability_status is {body.get('capability_status')!r}, not observed")
    identity = body.get("identity") or {}
    authority = identity.get("authority_node_id")
    if authority != expected_node_id:
        raise ValueError(
            f"capability authority {authority!r} is not "
            f"generation node {expected_node_id!r}")
    return generation_ready_profiles(body.get("profiles") or [])
