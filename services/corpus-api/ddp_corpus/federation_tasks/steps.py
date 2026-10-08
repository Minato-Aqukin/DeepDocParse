"""P5 coordinator slice; see ``ddp_corpus.federation_tasks`` package docstring."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ddp_corpus.federation_models import FederationRequest
    from ddp_corpus.federation_peers import PeerDirectory

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.application import routing
from ddp_corpus.deps import Actor

from ddp_corpus.errors import APIError
from ddp_core.application.ports import ApplicationError
from ddp_corpus.federation_peers import Delegation, PeerUnavailable
from ddp_contracts.enums import FEDERATED_ANSWER_REASON_VALUES
from ddp_corpus.models import ResourceVersion, utcnow
import asyncio
from ddp_core.application import coverage as coverage_kernel, plans
from ddp_corpus import federation, policy
import hashlib
import re
from sqlalchemy import select
import time

from ddp_corpus.federation_tasks.common import (
    LOCAL_POLL_DEADLINE_SECONDS,
    LOCAL_POLL_INTERVAL_SECONDS,
    LOCATE_OPERATION,
    PEER_POLL_INTERVAL_SECONDS,
    PEER_POLL_MAX_INTERVAL_SECONDS,
    _excerpt_reason,
    _reservation_step,
    _ts,
)
from ddp_corpus.federation_tasks.consent import (peer_directory)

# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _step_inputs(query: str) -> list[dict]:
    content = query.encode("utf-8")
    return [{"ref": "query", "digest": plans.content_digest(content),
             "size_bytes": len(content)}]

async def _fixed_inputs(session: AsyncSession, actor: Actor, task_spec: dict,
                        target: dict) -> list[dict]:
    """固定资源目标：把真实的 source_digest 带上，但**这里的执行者仍无法本地
    重算它**（`_local_digest` 只认问题文本），所以会如实落到 waiting_input，
    而不是被当成 content_verified。"""
    inputs = _step_inputs(task_spec.get("query") or "")
    if target["operation"] != LOCATE_OPERATION:
        return inputs
    resource = await policy.require_resource(session, actor, target["collection_id"])
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.resource_id == resource.id, ResourceVersion.deleted_at.is_(None)
    ).order_by(ResourceVersion.version_no.desc()).limit(1))
    digest = ("sha256:" + version.source_digest
              if version is not None and len(version.source_digest or "") == 64
              else plans.content_digest(target["collection_id"].encode("utf-8")))
    inputs.append({"ref": target["collection_id"], "digest": digest,
                   "size_bytes": int(version.size_bytes) if version is not None else 0})
    return inputs

def _admission_body(*, root_task_id: str, plan: dict, task_spec: dict,
                    consent: dict, step: dict, inputs: list[dict],
                    generation: int, evidence: list[dict] | None = None) -> dict:
    # **收的是裸值而不是 ORM 行**：本地目标等终态时 `_local_execution_outcome`
    # 会 rollback 结束读事务，行对象随之过期；循环里再摸 `row.*` 会 MissingGreenlet。
    key = f"{root_task_id}:{step['step_id']}"
    if evidence:
        # A generation step's business fact includes the evidence it was given. A resume
        # that fused more evidence must not adopt the answer generated without it (the
        # executor's request digest does not cover `evidence`); an unchanged evidence set
        # keeps the same key, so a lost reply still reconciles instead of regenerating.
        key += ":" + hashlib.sha256(plans.canonical_bytes(sorted(
            [item["evidence_id"], item["digest"]] for item in evidence))).hexdigest()[:24]
    body = {
        "schema": "ddp-plan-admission/1#AdmissionRequest",
        # **业务幂等键，不含 delegation_generation**。把代次写进键里会让丢响应后的
        # 对账永远 404，于是 resume 把一次已受理的执行重做成第二次执行（T81/T82）。
        # 代次只进请求体：同键同体由执行者复用回执，同键异体当场 409。
        "idempotency_key": key,
        "root_task_id": root_task_id,
        "step_id": step["step_id"],
        "delegation_generation": generation,
        "task_spec": task_spec,
        "plan": plan,
        "execution_consent": consent,
        "inputs": inputs,
    }
    if step["operation"] == "delegate":
        body["delegation_path"] = [plan["root_coordinator_node_id"]]
    if evidence:
        # 类型化数据边的载荷：有界、逐条带摘要。空就不带这个键，保持取数步骤的
        # 请求摘要口径不变。
        body["evidence"] = evidence
    return body

def _unknown_admission(exc: PeerUnavailable) -> bool:
    """这次受理的结果未知吗（可能已受理但回执丢了）。

    传输层失败（status=None）与 5xx 都算未知：前者可能发生在请求已送达之后，
    后者可能发生在执行者写库之后。未知就必须先对账，不许直接换键重做（T82）。
    4xx 是对方的明确答复，不是未知。
    """
    return exc.status is None or exc.status >= 500

async def _lookup_local_receipt(session: AsyncSession, actor: Actor,
                                key: str) -> dict | None:
    """本地对账：404 返回 None，其余错误原样抛出。

    `APIError` 是 `HTTPException` 的子类，404 在 `status_code` 上，不在
    `status` 上。写错字段时找不到行会变成 `AttributeError` -> 500，任务停在
    running（N3 复核）——所以这里只有一个字段可认。
    """
    try:
        return await federation.lookup_admission(
            session, actor, key, issuer_node_id=federation.local_node_id())
    except APIError as exc:
        if exc.status_code == 404:
            return None
        raise

def _receipt_binding_error(receipt: dict | None, *, key: str, root_task_id: str,
                           step_id: str, plan_digest: str,
                           executor_node_id: str) -> str | None:
    """回执必须逐字段绑定本次 (root, step, 业务键, 当前计划修订, 执行者)。

    对账（lookup）拿回的回执可能来自另一个 root/step/计划 —— 对端键被复用、
    对方在乱回、或者本地镜像里混进了别人的受理行。**不绑定就采用**等于把一次
    外来执行、以及它的证据接进本任务。任何一项对不上都按"没有有效回执"处理，
    并把出错的字段写进可见的机器原因；绝不轮询或融合外来的 executor_task。
    """
    if not isinstance(receipt, dict):
        return "receipt_binding_mismatch:schema"
    for field, expected in (("root_task_id", root_task_id), ("step_id", step_id),
                            ("idempotency_key", key), ("plan_digest", plan_digest),
                            ("executor_node_id", executor_node_id)):
        if receipt.get(field) != expected:
            return f"receipt_binding_mismatch:{field}"
    return None

async def _lookup_remote_receipt(client, key: str) -> dict | None:
    """远端对账：404 返回 None，其余错误原样抛出（由调用方映射成可见状态）。"""
    try:
        return await client.lookup(key)
    except PeerUnavailable as exc:
        if exc.status == 404:
            return None
        raise

async def _run_local_step(session: AsyncSession, actor: Actor, *, root_task_id: str,
                          plan: dict, task_spec: dict, consent: dict, step: dict,
                          target: dict, generation: int, now: datetime, http,
                          index, reconcile: bool,
                          budget: routing.RootBudget | None = None,
                          spend=None
                          ) -> tuple[str, str | None, list[dict],
                                                            str | None, list[str]]:
    """本地目标一律经 `federation.admit` 走同一条执行路径。

    `reconcile=True`（补做/重放）时先按**业务幂等键**对账：已经受理过的步骤
    直接复用执行行，绝不为同一个 (root, step) 造第二条执行记录。
    """
    inputs = await _fixed_inputs(session, actor, task_spec, target)
    body = _admission_body(root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                           consent=consent, step=step, inputs=inputs,
                           generation=generation)
    key = body["idempotency_key"]
    expected = {"key": key, "root_task_id": root_task_id,
                "step_id": step["step_id"], "plan_digest": plan["plan_digest"],
                "executor_node_id": federation.local_node_id()}
    if reconcile:
        receipt = await _lookup_local_receipt(session, actor, key)
        if receipt is not None:
            mismatch = _receipt_binding_error(receipt, **expected)
            if mismatch is not None:
                return "failed", mismatch, [], None, []
            return await _local_execution_outcome(
                session, actor, receipt, now=now, http=http, index=index,
                retry_expired=True)
    try:
        if spend is not None:
            await spend(kind="request", amount=1)
        receipt, created = await federation.admit(session, actor, body, now=now,
                                                  http=http, index=index)
    except APIError:
        raise
    except Exception:                      # noqa: BLE001 —— 录取结果未知，先对账
        await session.rollback()
        # 对账本身也可能炸。四种形状都要有明确结局，绝不能带着异常逃逸成
        # 500 并把任务永远留在 running（N3）。
        try:
            receipt = await _lookup_local_receipt(session, actor, key)
        except APIError as exc:
            # 对账得到明确答复（非 404）：如实记失败，错误码就是答复。
            return "failed", exc.code, [], None, []
        except Exception:                  # noqa: BLE001 —— 连对账都不确定
            return "unreachable", "admission_lookup_failed", [], None, []
        if receipt is None:
            return "unreachable", "admission_unknown", [], None, []
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            return "failed", mismatch, [], None, []
        created = False                    # 这是对账捡回来的旧受理
    mismatch = _receipt_binding_error(receipt, **expected)
    if mismatch is not None:
        return "failed", mismatch, [], None, []
    # created=False（同键重放/对账）时连 `lease_expired` 一起补做：用户看到的
    # 是一次 resume，不能因为回执来自旧执行就把失败永久留下。
    return await _local_execution_outcome(
        session, actor, receipt, now=now, http=http, index=index,
        retry_expired=reconcile or not created)

async def _local_execution_outcome(session: AsyncSession, actor: Actor,
                                   receipt: dict, *, now: datetime, http, index,
                                   retry_expired: bool = False
                                   ) -> tuple[str, str | None, list[dict], str | None, list[str]]:
    """把一次本地受理回执翻成覆盖账本结局。

    - `queued`：受理时已经**持久化 + 排队**（执行行与队列任务同一个事务）。
      协调者自己就在 worker 进程里，直接调 `federation.execute` 把它跑完 ——
      不占第二个并发位，也不需要第二个会话（SQLite 单测只有一条连接）。
      队列里的 `federation_execute` 任务仍然在，作为进程崩溃的恢复路径：
      它被领取时 `execute` 看到终态会原样返回，不会重复执行。
    - `claimed/running`（别的 worker 已经领走）：等它落终态；有租约围栏，
      崩溃由回收清扫 + `execute` 的过期租约接管兜底。
    - `failed` 且原因是清扫留下的非业务事实（`lease_expired` / `queue_task_*`）
      且本次是补做（resume）：重排一次执行而不是把目标永久钉死。队列任务
      耗尽重试死掉的执行由 `reconcile` 对账落成 `failed/queue_task_failed`，
      没有这一步它会永远停在 queued。
    - `cancelled` 是终态：目标记 `not_attempted`，不重试、不伪造结果。
    """
    if receipt.get("state") != "accepted":
        return "not_attempted", str(receipt.get("state") or "not_accepted"), [], None, []
    executor_task_id = str(receipt.get("executor_task_id") or "")
    if not executor_task_id:
        return "failed", "invalid_admission_receipt", [], None, []
    execution = await federation.require_execution(session, actor, executor_task_id)
    status = federation.execution_status(execution)
    if retry_expired and status["state"] == "failed" \
            and status.get("error") in federation.RETRYABLE_EXECUTION_ERRORS:
        if await federation.retry_execution(session, actor, executor_task_id, now=now):
            execution = await federation.require_execution(session, actor, executor_task_id)
            status = federation.execution_status(execution)
    if status["state"] == "queued":
        await federation.execute(session, actor, execution, now=utcnow(), http=http,
                                 index=index, heartbeat=True)
        execution = await federation.require_execution(session, actor, executor_task_id)
        status = federation.execution_status(execution)
    deadline = time.monotonic() + LOCAL_POLL_DEADLINE_SECONDS
    while status["state"] in ("claimed", "running"):
        if time.monotonic() >= deadline:
            return "unreachable", "local_execution_timeout", [], None, []
        # 结束读事务：SQLite 的共享连接不结束就看不到另一个会话的提交
        # （PG 上多一次 rollback 只是结束只读事务）。
        if session.in_transaction():
            await session.rollback()
        await asyncio.sleep(LOCAL_POLL_INTERVAL_SECONDS)
        execution = await federation.require_execution(session, actor, executor_task_id)
        status = federation.execution_status(execution)
    if status["state"] == "cancelled":
        return "not_attempted", "cancelled", [], None, []
    if status["state"] != "succeeded":
        return "failed", str(status.get("error") or status["state"]), [], None, []
    stored = execution.result_json or {}
    evidence = list(stored.get("evidence") or [])
    index_revision = (stored.get("result") or {}).get("index_revision")
    return ("succeeded", None, evidence, index_revision,
            list(status.get("internal_limits") or []))

async def _poll_execution(client, executor_task_id: str, *, deadline_ts: float,
                           budget: routing.RootBudget | None = None,
                           spend=None) -> dict:
    """Poll within the approved deadline; every physical attempt is prepaid."""
    remaining = max(0.0, deadline_ts - _ts(utcnow()))
    deadline = time.monotonic() + remaining
    interval = PEER_POLL_INTERVAL_SECONDS
    polls = 0
    while True:
        if time.monotonic() >= deadline:
            return {"state": "unreachable", "error": "peer_execution_timeout"}
        try:
            if spend is not None:
                await spend(kind="request", amount=1)
            elif budget is not None:
                budget.reserve("request")
        except ApplicationError as exc:
            if exc.code != "budget_exhausted":
                raise
            return {"state": "unreachable", "error": f"budget_exhausted:polls={polls}"}
        polls += 1
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                status = await client.execution(executor_task_id)
        except TimeoutError:
            return {"state": "unreachable", "error": "peer_execution_timeout"}
        if status.get("state") in ("succeeded", "failed", "cancelled"):
            return status
        lease = status.get("lease_until")
        if lease is not None:
            try:
                if plans.instant(lease) <= _ts(utcnow()):
                    return {"state": "unreachable", "error": "lease_expired"}
            except ApplicationError:
                return {"state": "unreachable", "error": "invalid_execution_lease"}
        wait = deadline - time.monotonic()
        if wait <= 0:
            return {"state": "unreachable", "error": "peer_execution_timeout"}
        await asyncio.sleep(min(interval, wait))
        interval = min(interval * 2, PEER_POLL_MAX_INTERVAL_SECONDS)
        if time.monotonic() >= deadline:
            return {"state": "unreachable", "error": "peer_execution_timeout"}

async def _run_remote_step(peers: PeerDirectory, *, root_task_id: str, plan: dict,
                           task_spec: dict, consent: dict, step: dict, target: dict,
                           generation: int, reconcile: bool,
                           budget: routing.RootBudget | None = None,
                           spend=None, receipt_refs=None
                           ) -> tuple[str, str | None,
                                                                        list[dict],
                                                                        str | None,
                                                                        list[str]]:
    """远端执行者的 admission + 轮询。

    未知结果（传输失败/5xx）先按业务键 lookup 对账；只有 lookup 确认从未受理
    （404）才允许重试，重试仍用同一个业务键、只前进 delegation_generation。
    `reconcile=True` 的补做路径进入前也先对账，防止把丢失回执的成功执行重做。
    """
    node_id = target["origin_node_id"]
    try:
        client = peers.client(node_id)
        body = _admission_body(root_task_id=root_task_id, plan=plan, task_spec=task_spec,
                               consent=consent, step=step,
                               inputs=_step_inputs(task_spec.get("query") or ""),
                               generation=generation)
        key = body["idempotency_key"]
        expected = {"key": key, "root_task_id": root_task_id,
                    "step_id": step["step_id"], "plan_digest": plan["plan_digest"],
                    "executor_node_id": node_id}
        if reconcile and spend is not None:
            await spend(kind="request", amount=1)
        receipt = await _lookup_remote_receipt(client, key) if reconcile else None
        if receipt is None:
            # egress 前先持久记账：admission 外发一次 request + hops（数据边一跳）。
            if spend is not None:
                await spend(kind="request", amount=1)
                await spend(kind="hops", amount=2, step_id=_reservation_step(step))
                await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(body)))
            try:
                receipt = await client.admit(body, idempotency_key=key)
            except PeerUnavailable as exc:
                if not _unknown_admission(exc):
                    raise
                # 对账本身也是 egress：先记账再发 lookup。
                if spend is not None:
                    await spend(kind="request", amount=1)
                receipt = await _lookup_remote_receipt(client, key)
                if receipt is None:
                    # 对账证明从未受理；这次的未知结果如实上报，补做时再来。
                    raise
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            # 对端把别的 root/step/计划修订的回执放在我们的键下（lookup 或受理
            # 响应都算）。采用它就等于轮询并融合一次外来执行；按"没有有效回执"
            # 处理并如实上报，绝不把外来 executor_task 接进本任务。
            return "failed", mismatch, [], None, []
        if receipt.get("state") != "accepted":
            return "not_attempted", str(receipt.get("state") or "not_accepted"), [], None, []
        executor_task_id = str(receipt.get("executor_task_id") or "")
        if not executor_task_id:
            return "failed", "invalid_admission_receipt", [], None, []
        if receipt_refs is not None:
            receipt_refs.append("admission:" + receipt["admission_id"])
        status = await _poll_execution(client, executor_task_id, budget=budget,
                                       spend=spend, deadline_ts=plans.instant(plan["budget"]["deadline"]))
        if status.get("state") == "unreachable":
            return "unreachable", str(status.get("error") or "peer_unreachable"), [], None, []
        if status.get("state") == "cancelled":
            # 对端把它显式取消了：这是终态，不是"没试过"，但对本任务的覆盖
            # 语义是 not_attempted（取消的目标不该被算成检索失败）。
            return "not_attempted", "cancelled", [], None, []
        if status.get("state") != "succeeded":
            return "failed", str(status.get("error") or status.get("state")), [], None, []
        evidence: list[dict] = []
        set_ref = status.get("evidence_set_ref")
        if set_ref:
            if spend is not None:
                await spend(kind="request", amount=1)
            evidence = list((await client.evidence_set(str(set_ref))).get("items") or [])
            if spend is not None:
                await spend(kind="bytes",
                            amount=len(plans.canonical_bytes({"items": evidence})))
        return ("succeeded", None, evidence, status.get("actual_index_revision"),
                list(status.get("internal_limits") or []))
    except PeerUnavailable as exc:
        state = "unreachable" if exc.status is None else (
            "denied" if exc.status == 403 else "failed")
        return state, exc.code or "peer_unavailable", [], None, []

def _delegated_failure(reason: str) -> dict:
    """委托没成功时的答案字段：空答案 + 显式原因 + failed。"""
    return {**federation.unavailable_answer(reason), "validation_state": "failed"}

_REASON_DETAIL_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

def _reason_detail(value) -> str:
    """对端给的状态/错误码只当**细节**显示：限定字符集与长度。

    对端完全控制这些字符串；原样写进结果就等于让对端往用户界面里塞任意文本，
    也让答案原因变成一个无法枚举的开放集合。代码本身永远是本节点声明过的那几个。
    """
    text = _REASON_DETAIL_CHARS.sub("_", str(value or ""))[:64]
    return text or "unknown"

def _peer_failure(exc: PeerUnavailable) -> dict:
    """远端生成节点连不上或回错：代码固定，对端错误码/HTTP 状态进细节。"""
    detail = exc.code or (f"http_{exc.status}" if exc.status is not None else "transport")
    return _delegated_failure(f"peer_unavailable:{_reason_detail(detail)}")

def _remote_answer_reason(reason) -> str:
    """远端答案文档自报的 answer_reason。

    是本契约声明过的代码就照用（远端也跑同一份契约）；带细节时只保留允许带细节的
    代码并清洗细节。认不出来的一律归到 `delegated_answer_rejected:细节`，不透传。
    """
    text = str(reason or "")
    head, separator, detail = text.partition(":")
    if head in FEDERATED_ANSWER_REASON_VALUES:
        if separator and head in federation.ANSWER_REASONS_WITH_DETAIL and detail:
            return f"{head}:{_reason_detail(detail)}"
        return head
    if not text:
        return "delegated_answer_rejected"
    return f"delegated_answer_rejected:{_reason_detail(text)}"

def _validated_delegated_answer(document, *, evidence_ids: list[str]) -> dict:
    """校验远端回传的答案文档；任何越界/缺失引用都拒绝，不修补、不降级采用。

    - 引用绑定的 evidence id 必须是**本次发送证据 id 的子集** —— 对端报一个
      我们没发过的 id 就是伪造证据引用，整份答案作废；
    - `semantic_review` 一律重写成 `needs_review`：远端说 passed 不是人审；
    - 只复制已知字段，绝不把对端的任意 JSON 透传进任务结果。
    """
    if not isinstance(document, dict):
        return _delegated_failure("delegated_answer_missing")
    answer = document.get("answer")
    if document.get("validation_state") != "passed" or not isinstance(answer, str) \
            or not answer.strip():
        return _delegated_failure(_remote_answer_reason(document.get("answer_reason")))
    bindings = document.get("claim_evidence_bindings")
    if not isinstance(bindings, list) or not bindings:
        return _delegated_failure("delegated_bindings_missing")
    allowed = set(evidence_ids)
    validated = []
    for binding in bindings:
        if not isinstance(binding, dict):
            return _delegated_failure("delegated_binding_out_of_scope")
        refs = binding.get("evidence_refs")
        claim = binding.get("claim_text")
        if not isinstance(refs, list) or not refs or not set(refs) <= allowed \
                or not isinstance(claim, str) or not claim.strip() \
                or binding.get("structural_validation") != "passed":
            return _delegated_failure("delegated_binding_out_of_scope")
        validated.append({
            "claim_id": str(binding.get("claim_id") or f"claim-{len(validated) + 1}"),
            "claim_text": claim,
            "evidence_refs": [str(ref) for ref in refs],
            "structural_validation": "passed",
            "semantic_review": "needs_review",
        })
    # 对端标出的矛盾同样只收"引用落在所发证据里、至少两条不同证据"的；依据一律
    # 重写成 generation_reported —— 版本分歧由本协调者按规则自己算，不信对端自报。
    raw_conflicts = document.get("conflicts", [])
    if not isinstance(raw_conflicts, list):
        return _delegated_failure("delegated_conflict_out_of_scope")
    conflicts = []
    for item in raw_conflicts:
        refs = item.get("evidence_refs") if isinstance(item, dict) else None
        if (not isinstance(refs, list) or len(set(map(str, refs))) < 2
                or not {str(ref) for ref in refs} <= allowed):
            return _delegated_failure("delegated_conflict_out_of_scope")
        conflicts.append(coverage_kernel.conflict(
            "generation_reported", sorted({str(ref) for ref in refs})))
    provider = document.get("provider")
    provider_out = ({**provider, "location": "remote"} if isinstance(provider, dict) else None)
    return {
        "answer": answer.strip(),
        "answer_reason": None,
        "claim_evidence_bindings": validated,
        "conflicts": coverage_kernel.merge_conflicts(conflicts),
        "provider": provider_out,
        "disclosure": {"remote": True, "payload": ["question", "selected_evidence"]},
        "validation_state": "passed",
    }

async def _delegated_answer(row: FederationRequest, *, plan: dict, step: dict,
                            fused: list[dict], excerpts: dict[str, str],
                            actor: Actor,
                            budget: routing.RootBudget | None = None,
                            spend=None) -> dict:
    """把整项答案委托给已就绪的远端执行者（计划里的 `answer-1`）。

    只发**有界且逐条带摘要**的证据摘录（`evidence_excerpts` 数据边），绝不发
    源文件；发之前逐条用 `excerpt_reason` 过一遍，任何缺失/越界都显式拒绝。
    受理与对账复用取数步骤那一套幂等语义；只采纳绑定落回所发证据 id 的结果。
    """
    executor = step["executor_node_id"]
    evidence: list[dict] = []
    for item in fused:
        evidence_id = str(item.get("evidence_id") or "")
        text = excerpts.get(evidence_id)
        reason = _excerpt_reason(text)
        if reason is not None:
            return _delegated_failure(reason)
        evidence.append({"evidence_id": evidence_id, "excerpt": text,
                         "digest": plans.content_digest(text.encode("utf-8"))})
    if not evidence:
        return _delegated_failure("insufficient_evidence")
    if len(evidence) > federation.ADMISSION_EVIDENCE_LIMIT:
        # 有界：不截断证据去凑数 —— 被截掉的引用会变成无根引用。
        return _delegated_failure("evidence_delegation_over_limit")
    body = _admission_body(
        root_task_id=row.root_task_id, plan=plan, task_spec=row.task_spec_json,
        consent=row.execution_consent_json, step=step,
        inputs=_step_inputs(row.task_spec_json.get("query") or ""),
        generation=int(row.delegation_generation or 0), evidence=evidence)
    key = body["idempotency_key"]
    expected = {"key": key, "root_task_id": row.root_task_id, "step_id": step["step_id"],
                "plan_digest": plan["plan_digest"], "executor_node_id": executor}
    peers = peer_directory(actor, Delegation(root_task_id=row.root_task_id,
                                             task_spec_digest=row.task_spec_digest))
    try:
        client = peers.client(executor)
        # 先对账再受理：resume/重放不得为同一个 (root, step) 触发第二次生成。
        try:
            if spend is not None:
                await spend(kind="request", amount=1)
            receipt = await _lookup_remote_receipt(client, key)
            if receipt is None:
                if spend is not None:
                    await spend(kind="request", amount=1)
                    await spend(kind="hops", amount=1, step_id=_reservation_step(step))
                    await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(body)))
                    await spend(kind="generation_tokens", step_id=_reservation_step(step),
                                amount=int(plan["budget"].get("max_generation_tokens", 0)))
                receipt = await client.admit(body, idempotency_key=key)
        except PeerUnavailable as exc:
            receipt = None
            if _unknown_admission(exc):
                try:
                    if spend is not None:
                        await spend(kind="request", amount=1)
                    receipt = await _lookup_remote_receipt(client, key)
                except PeerUnavailable:
                    receipt = None
            if receipt is None:
                return _peer_failure(exc)
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            return _delegated_failure(mismatch)
        if receipt.get("state") != "accepted":
            return _delegated_failure(
                f"delegated_admission_not_accepted:{_reason_detail(receipt.get('state'))}")
        executor_task_id = str(receipt.get("executor_task_id") or "")
        if not executor_task_id:
            return _delegated_failure("invalid_admission_receipt")
        status = await _poll_execution(client, executor_task_id, budget=budget,
                                       spend=spend, deadline_ts=plans.instant(plan["budget"]["deadline"]))
        if status.get("state") != "succeeded":
            # 超时（本节点合成的 peer_execution_timeout）、对端 failed/cancelled：
            # 代码固定，具体是哪一种进细节 —— 界面能分清，又不会冒出未声明的代码。
            return _delegated_failure(f"delegated_execution_failed:"
                                      f"{_reason_detail(status.get('error') or status.get('state'))}")
        return _validated_delegated_answer(
            status.get("answer"), evidence_ids=[item["evidence_id"] for item in evidence])
    except PeerUnavailable as exc:
        return _peer_failure(exc)
    finally:
        await peers.aclose()

def _wiki_failure(reason: str) -> dict:
    """Wiki 生成没成功时的答案字段：空答案 + 显式原因 + failed。"""
    head = str(reason or "").partition(":")[0]
    if reason not in FEDERATED_ANSWER_REASON_VALUES and head not in (
            "peer_unavailable", "delegated_admission_not_accepted",
            "delegated_execution_failed", "receipt_binding_mismatch",
            "delegated_answer_rejected"):
        # 未声明代码不伪装成契约原因：这类只能是内部 bug，进 upstream_error。
        reason = "upstream_error"
    return {**federation.unavailable_answer(reason), "validation_state": "failed"}

async def _delegated_wiki_draft(row: FederationRequest, *, plan: dict, step: dict,
                                items: list[dict], actor: Actor,
                                budget: routing.RootBudget | None = None,
                                spend=None) -> dict:
    """把 wiki 生成委托给已就绪的 C：有界证据摘录经 `evidence_excerpts` 边外发。

    受理与对账复用 answer 委托同一套幂等语义；C 只出原始页面草稿（`wiki_draft`），
    版本化提交永远由 A 完成。未知受理只对账不盲重发。
    """
    executor = step["executor_node_id"]
    evidence = []
    for item in items:
        # Main 冻结契约：payload evidence_id = source_ref(真实信封)，
        # source_envelope.evidence_id 保留原始 ID（helper 校验绑定一致）。
        envelope = {key: value for key, value in item.items() if key != "excerpt"}
        from ddp_corpus import federated_wiki as _fw
        ref = _fw.source_ref(envelope)
        text = str(item.get("excerpt") or "")
        if not text:
            return _wiki_failure("evidence_excerpt_unavailable")
        payload = {"evidence_id": ref, "excerpt": text,
                   "digest": plans.content_digest(text.encode("utf-8")),
                   "source_envelope": envelope}
        grant = item.get("derivative_grant")
        if isinstance(grant, str) and grant:
            payload["derivative_grant"] = grant
        evidence.append(payload)
    body = _admission_body(
        root_task_id=row.root_task_id, plan=plan, task_spec=row.task_spec_json,
        consent=row.execution_consent_json, step=step,
        inputs=_step_inputs(row.task_spec_json.get("query") or ""),
        generation=int(row.delegation_generation or 0), evidence=evidence)
    key = body["idempotency_key"]
    expected = {"key": key, "root_task_id": row.root_task_id, "step_id": step["step_id"],
                "plan_digest": plan["plan_digest"], "executor_node_id": executor}
    peers = peer_directory(actor, Delegation(root_task_id=row.root_task_id,
                                             task_spec_digest=row.task_spec_digest))
    try:
        client = peers.client(executor)
        try:
            if spend is not None:
                await spend(kind="request", amount=1)
            receipt = await _lookup_remote_receipt(client, key)
            if receipt is None:
                if spend is not None:
                    await spend(kind="request", amount=1)
                    await spend(kind="hops", amount=2, step_id=_reservation_step(step))
                    await spend(kind="egress_bytes", amount=len(plans.canonical_bytes(body)))
                    await spend(kind="generation_tokens", step_id=_reservation_step(step),
                                amount=int(plan["budget"].get("max_generation_tokens", 0)))
                receipt = await client.admit(body, idempotency_key=key)
        except PeerUnavailable as exc:
            receipt = None
            if _unknown_admission(exc):
                try:
                    if spend is not None:
                        await spend(kind="request", amount=1)
                    receipt = await _lookup_remote_receipt(client, key)
                except PeerUnavailable:
                    receipt = None
            if receipt is None:
                return _wiki_failure(f"peer_unavailable:{_reason_detail(exc.code or 'transport')}")
        mismatch = _receipt_binding_error(receipt, **expected)
        if mismatch is not None:
            return _wiki_failure(mismatch)
        if receipt.get("state") != "accepted":
            return _wiki_failure(
                f"delegated_admission_not_accepted:{_reason_detail(receipt.get('state'))}")
        executor_task_id = str(receipt.get("executor_task_id") or "")
        if not executor_task_id:
            return _wiki_failure("invalid_admission_receipt")
        status = await _poll_execution(client, executor_task_id, budget=budget,
                                       spend=spend, deadline_ts=plans.instant(plan["budget"]["deadline"]))
        if status.get("state") != "succeeded":
            return _wiki_failure(f"delegated_execution_failed:"
                                 f"{_reason_detail(status.get('error') or status.get('state'))}")
        draft = status.get("wiki_draft")
        if not isinstance(draft, dict) or draft.get("validation_state") not in ("passed", "failed"):
            return _wiki_failure("delegated_answer_missing")
        if draft["validation_state"] == "failed":
            return _wiki_failure(
                f"delegated_answer_rejected:{_reason_detail(draft.get('error'))}")
        # C 原始 kernel 草稿不静默丢关系/审计字段：pages/relations/provider/
        # limits/protocol/decoder/semantic_review/source_type 全量带回 A，
        # 提交前再按本次证据编号域校验（`_validate_remote_wiki_draft`）。
        return {"pages": draft.get("pages"), "relations": draft.get("relations", []),
                "provider": draft.get("provider"),
                "limits": draft.get("limits") or {},
                "protocol": draft.get("protocol"),
                "decoder_revision": draft.get("decoder_revision"),
                "semantic_review": draft.get("semantic_review", "needs_review"),
                "source_type": draft.get("source_type", "generated"),
                "validation_state": "passed"}
    except PeerUnavailable as exc:
        return _wiki_failure(f"peer_unavailable:{_reason_detail(exc.code or 'transport')}")
    finally:
        await peers.aclose()
