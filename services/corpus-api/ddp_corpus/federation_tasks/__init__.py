"""P5 协调者：任务需求、规划、审批、执行、覆盖账本与交付回执。

执行权威：工作区计划 v3 §6–§9；接口冻结：`docs/refactor/P5-INTERFACES-v3.md`
§3、§5。这个模块**不复制节点侧实现** —— 本地目标一律经
`federation.run_probe` / `federation.admit` 走同一条检索与证据路径，
本模块只负责编排、外发许可门与账本。

这里的三条边界：

1. **答案生成可以在协调者本地，也可以整项委托给已就绪的远端执行者。** 协调者
   自己的 `rag.answer.cited` 就绪时保留指派给自己的 `answer` 步骤；未就绪则按
   探索许可与根预算探测候选执行节点，把**有界**证据摘录经 `evidence_excerpts`
   数据边外发（先有计划、后有批准），并校验回传绑定是所发证据 id 的子集。任何
   失败都显式带 `answer=null` 与原因，有证据就不许标失败。
2. **不递归联邦。** 只对 ScopeManifest 的直接成员发请求，不展开 child manifest。
3. **交付字节经 `GET /api/v1/deliveries/{id}` 有界下载并本地校验摘要**；
   确认只在本地校验通过后发生，TTL 到期一律 expired。

覆盖语义（§7.2/§7.4，由 `ddp_core.application.coverage` 判定，本模块只填分子
与分母）：fast 的结局永远 `partial`；exhaustive 只有在 manifest `sealed`、
所有目标 succeeded/有依据排除时才可能是 `complete`；没有证据一律
`insufficient`，不读任何模型自报信心。

执行位置（受理即持久）：受理（`POST /tasks`）与 `resume` 只把
`federation_plan` 排进 `corpus.tasks`，`_execute_plan` 由 corpus-worker 领取；
本地目标经 `federation.admit` 排出的 `federation_execute` 也由 worker 执行，
协调者在 `_local_execution_outcome` 里等它落终态。进程重启不再把已受理的
协调/执行任务永远留在 running（企业边界 7）。
`FEDERATION_EXECUTION_INLINE=true` 恢复请求内执行的旧行为，只给没有 worker
的部署与验收夹具用。

包结构（与原 `federation_tasks.py` 同符号的包切分，行为不变）：

- `common` —— 共享常量、事件类型、`_commit`/`_append_event`、行读写与通用投影。
- `consent` —— 探索/执行许可校验与出站目录。
- `intent` —— 意图受理、任务列表、读路径（任务/覆盖/事件）。
- `targets` —— 目标枚举、排序与目录摘要。
- `probes` —— Probe 请求、持久化与复用。
- `plan` —— 根预算、候选探测与计划生成。
- `approval` —— 计划审批。
- `steps` —— 本地/远端执行步骤与委托校验。
- `synthesis` —— 答案/Wiki 合成。
- `execution` —— 执行编排、继续/恢复/取消与清扫。
- `delivery` —— 交付文档与回执。
- `relay` —— 中继委托执行。
"""
from __future__ import annotations

import asyncio as asyncio
import time as time

from ddp_core.application import routing as routing

from ddp_corpus.federation_tasks.approval import approve
from ddp_corpus.federation_tasks.common import (
    ANSWER_OPERATION,
    CACHED_PROBE_PROFILE,
    DELIVERY_RESULT_MAX_BYTES,
    DELIVERY_TTL_SECONDS,
    EVIDENCE_BYTES_PER_TARGET,
    FAST_CANDIDATE_LIMIT,
    GENERATION_OPERATIONS,
    GENERATION_TOKEN_BUDGET,
    GENERATION_WITHHELD_FIELD,
    LOCAL_POLL_DEADLINE_SECONDS,
    LOCAL_POLL_INTERVAL_SECONDS,
    LOCATE_OPERATION,
    MAX_ANSWER_CANDIDATES,
    PEER_POLL_INTERVAL_SECONDS,
    PEER_POLL_MAX_INTERVAL_SECONDS,
    RETRIEVAL_OPERATION,
    SCOPE_TTL_SECONDS,
    SOURCE_POLICY_DENIED,
    WIKI_OPERATION,
    _answer_skeleton,
    _append_event,
    _commit,
    _concurrent_write,
    _coverage_key,
    _entry_from_row,
    _entry_key,
    _enumeration_state,
    _excerpt_reason,
    _generation_excerpt,
    _go_manifest_digest,
    _instant,
    _load_request,
    _manifest_digest_matches,
    _manifest_digest_python,
    _public_result,
    _recorded_conflicts,
    _reservation_step,
    _revocation_sweep,
    _REVISION_PREFIX,
    _RETRYABLE_STATES,
    _scope_identity,
    _status_output,
    _target_digest,
    _target_identity,
    _target_key,
    _ts,
    _unavailable_answer,
    _validate_manifest,
    _EVENT_APPROVED,
    _EVENT_CANCELLED,
    _EVENT_COMPLETED,
    _EVENT_DELIVERY_CONFIRMED,
    _EVENT_DELIVERY_EXPIRED,
    _EVENT_DELIVERY_PENDING,
    _EVENT_FAILED,
    _EVENT_INTENT,
    _EVENT_PLAN_READY,
    _EVENT_RESUMED,
    _EVENT_STARTED,
)
from ddp_corpus.federation_tasks.consent import (
    peer_directory,
    validate_execution_consent,
    validate_exploration_consent,
    _egress_denied,
    _valid_node_list,
    _BUDGET_FIELDS,
    _EXECUTION_FIELDS,
    _EXPLORATION_FIELDS,
)
from ddp_corpus.federation_tasks.delivery import (
    ack_delivery,
    read_delivery,
    _bounded_delivery_document,
    _deliver_result,
    _delivery_receipt,
)
from ddp_corpus.federation_tasks.execution import (
    cancel,
    execute_task,
    heartbeat_request,
    mark_stalled,
    resume,
    run_queued,
    _cancel_step_executions,
    _execute_plan,
    _evidence_key,
    _first_error,
    _load_probe,
    _lookup_remote_receipt_for_cancel,
    _mark_failed,
    _no_longer_running,
    _public_item,
    _resume_continuation_gate,
    _ATTRIBUTED_FIELD,
)
from ddp_corpus.federation_tasks.intent import (
    create_intent,
    list_tasks,
    read_coverage,
    read_events,
    read_task,
    _find_intent_by_key,
    _intent_budget,
    _intent_output,
    _intent_request_digest,
    _list_cursor,
    _list_item,
    _local_manifest,
    _parse_list_cursor,
    _replay_intent,
    _root_budget,
    _subquery_digests,
    _EPOCH,
    _ROOT_TASK_ID,
    TASK_LIST_LIMIT_MAX,
)
from ddp_corpus.federation_tasks.plan import (
    create_plan,
    read_plan,
    _append_delegated_answer_step,
    _append_local_answer_step,
    _append_wiki_steps,
    _answer_probe_key,
    _drop_answer_steps,
    _generation_available,
    _probe_answer_candidates,
    _settled_targets,
)
from ddp_corpus.federation_tasks.probes import (
    _find_reusable_probe,
    _negative_reason,
    _negative_state,
    _persist_remote_probe,
    _probe_key,
    _probe_request,
    _probe_state,
    _probe_targets,
)
from ddp_corpus.federation_tasks.relay import (
    execute_delegation,
    validate_delegation_report,
    _delegation_failed_entries,
    _delegation_failure_state,
    _run_delegate_step,
)
from ddp_corpus.federation_tasks.steps import (
    _admission_body,
    _delegated_answer,
    _delegated_failure,
    _delegated_wiki_draft,
    _fixed_inputs,
    _local_execution_outcome,
    _lookup_local_receipt,
    _lookup_remote_receipt,
    _peer_failure,
    _poll_execution,
    _reason_detail,
    _receipt_binding_error,
    _remote_answer_reason,
    _run_local_step,
    _run_remote_step,
    _step_inputs,
    _unknown_admission,
    _validated_delegated_answer,
    _wiki_failure,
    _REASON_DETAIL_CHARS,
)
from ddp_corpus.federation_tasks.synthesis import (
    _answer_result,
    _fused_evidence_items,
    _grounded_answer,
    _live_recheck_evidence,
    _load_excerpts,
    _no_generator_reason,
    _validate_remote_wiki_draft,
    _wiki_error_reason,
    _wiki_result,
)
from ddp_corpus.federation_tasks.targets import (
    _all_targets,
    _descriptor_index,
    _directory_denial,
    _fast_continuation_targets,
    _fast_stop_reason,
    _gather_descriptors,
    _onward_policies,
    _ordered_targets,
    _peer_probe_denial,
    _plan_selected_targets,
    _policy_forbids,
    _probe_policy_revision,
    _ranking_unreachable_nodes,
    _registry_revisions,
    _select_targets,
    _steps_by_target,
)
from ddp_corpus.models import utcnow as utcnow

__all__ = [
    "ANSWER_OPERATION",
    "CACHED_PROBE_PROFILE",
    "DELIVERY_RESULT_MAX_BYTES",
    "DELIVERY_TTL_SECONDS",
    "EVIDENCE_BYTES_PER_TARGET",
    "FAST_CANDIDATE_LIMIT",
    "GENERATION_OPERATIONS",
    "GENERATION_TOKEN_BUDGET",
    "GENERATION_WITHHELD_FIELD",
    "LOCAL_POLL_DEADLINE_SECONDS",
    "LOCAL_POLL_INTERVAL_SECONDS",
    "LOCATE_OPERATION",
    "MAX_ANSWER_CANDIDATES",
    "PEER_POLL_INTERVAL_SECONDS",
    "PEER_POLL_MAX_INTERVAL_SECONDS",
    "RETRIEVAL_OPERATION",
    "SCOPE_TTL_SECONDS",
    "SOURCE_POLICY_DENIED",
    "TASK_LIST_LIMIT_MAX",
    "WIKI_OPERATION",
    "ack_delivery",
    "approve",
    "asyncio",
    "cancel",
    "create_intent",
    "create_plan",
    "execute_delegation",
    "execute_task",
    "heartbeat_request",
    "list_tasks",
    "mark_stalled",
    "peer_directory",
    "read_coverage",
    "read_delivery",
    "read_events",
    "read_plan",
    "read_task",
    "resume",
    "routing",
    "run_queued",
    "time",
    "utcnow",
    "validate_delegation_report",
    "validate_execution_consent",
    "validate_exploration_consent",
    "_append_event",
    "_answer_result",
    "_commit",
    "_find_intent_by_key",
    "_go_manifest_digest",
    "_load_excerpts",
    "_manifest_digest_matches",
    "_mark_failed",
    "_persist_remote_probe",
    "_poll_execution",
    "_probe_answer_candidates",
    "_probe_key",
    "_ranking_unreachable_nodes",
    "_root_budget",
    "_run_local_step",
    "_select_targets",
    "_ts",
    "_validated_delegated_answer",
]

_PATCH_OWNERS = {
    "_append_event": "common",
    "_answer_result": "synthesis",
    "_run_local_step": "steps",
    "_load_excerpts": "synthesis",
    "_generation_available": "plan",
    "peer_directory": "consent",
    "_find_intent_by_key": "intent",
    "_poll_execution": "steps",
    "FAST_CANDIDATE_LIMIT": "common",
    "GENERATION_TOKEN_BUDGET": "common",
    "DELIVERY_RESULT_MAX_BYTES": "common",
}


def __getattr__(name: str):
    """Lazily resolve owner-module attributes (keeps ``dir()``/``vars()`` complete)."""
    owner = _PATCH_OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{owner}")
    return getattr(module, name)


class _PatchForwardingModule(__import__("types").ModuleType):
    """Package module that forwards patched symbols to every submodule binding them."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        import importlib

        for holder in globals().get("_PATCH_HOLDERS", {}).get(name, ()):
            try:
                setattr(importlib.import_module(f"{__name__}.{holder}"), name, value)
            except ImportError:
                continue


def _collect_patch_holders():
    """Every submodule currently binding a forwarded name gets patched with it."""
    import importlib

    holders: dict[str, list[str]] = {}
    for submodule in (
        "common", "consent", "intent", "targets", "probes", "plan",
        "approval", "steps", "synthesis", "execution", "delivery", "relay",
    ):
        module = importlib.import_module(f"{__name__}.{submodule}")
        for name in set(_PATCH_OWNERS) | set(globals()):
            if name in module.__dict__:
                holders.setdefault(name, []).append(submodule)
    return holders


_PATCH_HOLDERS = _collect_patch_holders()

import sys as _sys

_sys.modules[__name__].__class__ = _PatchForwardingModule
del _sys


def __dir__():
    return sorted(set(globals()) | set(__all__))
