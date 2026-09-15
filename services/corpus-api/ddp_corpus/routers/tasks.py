"""P5 协调者入口端点（`P5-INTERFACES-v3.md` §3）。

**这些是入口端点，不是 peer 端点**：认证走 `deps.current_actor`（服务凭据 +
control-api 下发的 actor 上下文头），control-api 会按 `corpusPrefixes` 转发。
节点到节点的调用全部在 `routers/federation.py`（额外要 X-DDP-Peer-Token）。

形状与 `packages/contracts/openapi/federation-tasks-v1.yaml` 的
`TaskIntentInput` / `PlanningRequest` / `ApprovalRequest` / `ExecutionRequest`
一一对应；写操作按契约要求带 `Idempotency-Key`，缺了直接 422。
"""
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus import federation_tasks
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, current_actor
from ddp_corpus.models import utcnow

router = APIRouter(prefix="/api/v1")
Digest = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TaskIntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_spec: dict[str, Any]
    exploration_consent: dict[str, Any]
    scope_manifest: dict[str, Any] | None = None


class PlanningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_task_id: str = Field(min_length=1, max_length=64)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_digest: str = Digest
    execution_consent: dict[str, Any]


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_task_id: str = Field(min_length=1, max_length=64)
    plan_digest: str = Digest


class DeliveryAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_manifest_digest: str = Digest


def _index(request: Request):
    return getattr(request.app.state, "search_index", None)


def _http(request: Request):
    return getattr(request.app.state, "http", None)


@router.post("/task-intents", status_code=201)
async def create_task_intent(body: TaskIntentInput, actor: Actor = Depends(current_actor),
                             session: AsyncSession = Depends(get_session),
                             idempotency_key: str = Header(min_length=1, max_length=128)):
    return await federation_tasks.create_intent(
        session, actor, task_spec=body.task_spec,
        exploration_consent=body.exploration_consent,
        scope_manifest=body.scope_manifest, now=utcnow(),
        idempotency_key=idempotency_key)


@router.post("/task-plans")
async def create_task_plan(body: PlanningRequest, request: Request,
                           actor: Actor = Depends(current_actor),
                           session: AsyncSession = Depends(get_session)):
    return await federation_tasks.create_plan(
        session, actor, body.root_task_id, now=utcnow(), http=_http(request),
        index=_index(request))


@router.post("/task-plans/{root_task_id}/approve")
async def approve_task_plan(root_task_id: str, body: ApprovalRequest,
                            actor: Actor = Depends(current_actor),
                            session: AsyncSession = Depends(get_session)):
    return await federation_tasks.approve(
        session, actor, root_task_id, plan_digest=body.plan_digest,
        execution_consent=body.execution_consent, now=utcnow())


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=federation_tasks.TASK_LIST_LIMIT_MAX),
                     cursor: str | None = Query(None, min_length=1, max_length=256),
                     actor: Actor = Depends(current_actor),
                     session: AsyncSession = Depends(get_session)):
    """本人任务列表（契约 `listTasks`）：只带状态轴，结果按 id 读。"""
    return await federation_tasks.list_tasks(session, actor, limit=limit, cursor=cursor)


@router.post("/tasks")
async def submit_task(body: ExecutionRequest, request: Request,
                      actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session),
                      idempotency_key: str = Header(min_length=1, max_length=128)):
    """受理已批准计划。**202 = 新受理（异步执行）**；200 = 同键重放的权威状态。

    契约（federation-tasks-v1.yaml POST /tasks）把这两个码分开：只有
    created 的受理才回 202，客户端据此知道"现在还没有结果，要轮询"。
    真正的执行排在 `corpus.tasks` 的 `federation_plan` 上，由 corpus-worker
    领取 —— 受理进程重启不会让任务永远停在 running（企业边界 7）。
    """
    status, created = await federation_tasks.execute_task(
        session, actor, body.root_task_id, plan_digest=body.plan_digest,
        idempotency_key=idempotency_key, now=utcnow(), http=_http(request),
        index=_index(request))
    return JSONResponse(status, status_code=202 if created else 200)


@router.get("/tasks/{root_task_id}")
async def read_task(root_task_id: str, actor: Actor = Depends(current_actor),
                    session: AsyncSession = Depends(get_session)):
    return await federation_tasks.read_task(session, actor, root_task_id)


@router.get("/tasks/{root_task_id}/coverage")
async def read_task_coverage(root_task_id: str, actor: Actor = Depends(current_actor),
                             session: AsyncSession = Depends(get_session)):
    return await federation_tasks.read_coverage(session, actor, root_task_id)


@router.get("/tasks/{root_task_id}/events")
async def read_task_events(root_task_id: str, after: int = 0,
                           actor: Actor = Depends(current_actor),
                           session: AsyncSession = Depends(get_session)):
    return await federation_tasks.read_events(session, actor, root_task_id,
                                              after=max(0, after))


@router.post("/tasks/{root_task_id}/resume", status_code=202)
async def resume_task(root_task_id: str, request: Request,
                      actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session)):
    return await federation_tasks.resume(
        session, actor, root_task_id, now=utcnow(), http=_http(request),
        index=_index(request))


@router.post("/tasks/{root_task_id}/cancel")
async def cancel_task(root_task_id: str, actor: Actor = Depends(current_actor),
                      session: AsyncSession = Depends(get_session)):
    return await federation_tasks.cancel(session, actor, root_task_id, now=utcnow())


@router.get("/deliveries/{delivery_id}")
async def read_delivery(delivery_id: str, actor: Actor = Depends(current_actor),
                        session: AsyncSession = Depends(get_session)):
    """交付字节读取：有界 JSON，`Cache-Control: no-store`；读取不是确认。"""
    body = await federation_tasks.read_delivery(session, actor, delivery_id, now=utcnow())
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.post("/deliveries/{delivery_id}/ack")
async def ack_delivery(delivery_id: str, body: DeliveryAckRequest,
                       actor: Actor = Depends(current_actor),
                       session: AsyncSession = Depends(get_session),
                       idempotency_key: str = Header(min_length=1, max_length=128)):
    return await federation_tasks.ack_delivery(
        session, actor, delivery_id,
        result_manifest_digest=body.result_manifest_digest,
        idempotency_key=idempotency_key, now=utcnow())
