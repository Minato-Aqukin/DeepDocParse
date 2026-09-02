"""内网入口：模型网关的解析回调，与 control-api 的 outbox 事件。

两条路都只接受**服务身份**（`X-DDP-Actor-Kind: service` + 服务凭据），
用户凭据到不了这里。

## 回调是尽力而为，事件是至少一次

- **解析回调**（网关 -> 本服务）失败只记日志，真正的可靠性由
  `reconcile.py` 的对账保证。
- **outbox 事件**（control-api -> 本服务）会一直重投直到 2xx 或 409，
  所以这里必须**幂等**：`processed_events` 按 event_id 去重，
  重投直接回 409（投递器把 409 当成功，见 Go 侧 deliverOutbox 的注释）。

没有第二条的话，一次网络抖动就会让同一份上传变成两个 Document、
两次解析、两次计费。
"""
import json

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.archive import archive_job, fail_job
from ddp_corpus.control_client import ControlClient
from ddp_corpus.db import get_session
from ddp_corpus.deps import Actor, get_service_client, get_storage, require_service_actor
from ddp_corpus.errors import APIError
from ddp_corpus.ingest import ingest_document
from ddp_corpus.queue import enqueue
from ddp_corpus.models import Document, ParseJob, ProcessedEvent
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import Storage

router = APIRouter()


class ParseCallback(BaseModel):
    task_id: str        # 网关侧的 task_id
    status: str         # succeeded | failed


@router.post("/internal/parse-callback")
async def parse_callback(body: ParseCallback, request: Request,
                         _: Actor = Depends(require_service_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage),
                         service: ServiceClient = Depends(get_service_client)):
    # 同一个网关任务可能对应本层多个 job（网关按 doc_id 去重，
    # 同一份文档从 Web 与对外 API 都提交过）——全部推进
    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.service_task_id == body.task_id)
    )).scalars().all()
    if not jobs:
        # 未知任务不报错：网关不该因为本层的记账问题重试回调
        return {"ok": False, "reason": "unknown task"}

    archived = 0
    for job in jobs:
        if body.status == "failed":
            try:
                live = await service.get_status(body.task_id)
                error = live.get("error") or "parse failed"
            except Exception:      # noqa: BLE001 —— 拿不到详情不该挡住落 failed
                error = "parse failed"
            await fail_job(session, job, error)
            continue

        document = await session.get(Document, job.document_id)
        if document is None or document.origin == "external":
            job.status = "succeeded"        # 外部任务：文件在调用方那儿，不归档别人的结果
            await session.commit()
            continue
        if await archive_job(session, storage, service, job.id):
            archived += 1
            await _schedule_index(session, document.id)

    return {"ok": True, "archived": archived}


class InboundEvent(BaseModel):
    event_id: str
    type: str
    organization_id: str
    payload: dict


@router.post("/internal/events")
async def consume_event(event: InboundEvent, request: Request,
                        _: Actor = Depends(require_service_actor),
                        session: AsyncSession = Depends(get_session),
                        storage: Storage = Depends(get_storage),
                        service: ServiceClient = Depends(get_service_client)):
    """消费 control-api 的 outbox 事件。**幂等。**

    去重先行：先抢 `processed_events` 的主键，抢不到说明这条已经处理过，
    直接回 409（投递器把 409 当成功）。抢到之后再干活 —— 干活失败会让
    事务回滚，占位行也跟着没了，下一次重投还能再来。
    """
    try:
        async with session.begin_nested():
            session.add(ProcessedEvent(event_id=event.event_id, type=event.type,
                                       organization_id=event.organization_id))
    except IntegrityError:
        # 已处理过。**409 而不是 200**：投递器把 409 当成功（不再重投），
        # 但日志与指标上能把"重投"与"首次处理"分开 —— 重投次数突然上升
        # 是投递链路出问题的信号
        raise APIError(409, f"event {event.event_id} already processed",
                       "invalid_request_error", "duplicate_event")

    handler = _HANDLERS.get(event.type)
    if handler is None:
        # 不认识的事件类型**必须回 2xx**：投递器会一直重投 4xx/5xx，
        # 而"corpus 还没升级到认识这个事件"不是投递器能解决的问题。
        # 但要留痕，否则升级漏了没人知道
        await session.commit()
        return {"ok": True, "ignored": event.type,
                "reason": "本服务不认识这个事件类型（可能是 control 先升级了）"}

    result_id = await handler(session, storage, service,
                              ControlClient(request.app.state.http), event)
    processed = await session.get(ProcessedEvent, event.event_id)
    if processed is not None:
        processed.result_id = result_id
    await session.commit()
    return {"ok": True, "result_id": result_id}


async def _on_document_submitted(session, storage, service, control,
                                 event: InboundEvent) -> str:
    p = event.payload
    options = p.get("options") or {}
    if isinstance(options, str):
        options = json.loads(options or "{}")
    document, _job = await ingest_document(
        session, storage, service, control,
        organization_id=event.organization_id,
        actor_id=p["actor_id"],
        object_key=p["object_key"],
        filename=p.get("filename") or "document.pdf",
        mime=p.get("mime") or "application/octet-stream",
        size_bytes=int(p.get("size") or 0),
        # **服务端验过的摘要**，不是客户端声明的那个
        doc_id=p["sha256"],
        engine=p.get("engine") or "",
        options=options,
    )
    # **复活的文档要把索引推回去。** 删除会清空 chunks 并把 index_status 置回
    # none；复活时如果不重新排队，文档看着好好的却永远问不了，而对账只捞
    # pending 状态的 job，自愈不了 —— 只能等用户自己发现去点"重建索引"。
    # 同参数重传会在 ingest 里命中已有 job 直接返回，正是这条路径。
    if document.index_status == "pending" and document.current_job_id:
        await _schedule_index(session, document.id, event.organization_id)
    return document.id


async def _on_document_deleted(session, storage, service, control,
                               event: InboundEvent) -> str | None:
    """control 侧删了文档（例如整个组织被清理）。

    这里**只做软删**，不碰对象 —— 删对象是全项目唯一不可逆的操作，
    必须走带宽限期与 claim 的 GC（`gc.py`）。
    """
    from ddp_corpus.models import utcnow

    document = await session.get(Document, event.payload.get("document_id", ""))
    if document is None:
        return None
    document.deleted_at = utcnow()
    return document.id


_HANDLERS = {
    "DocumentSubmitted": _on_document_submitted,
    "DocumentDeleted": _on_document_deleted,
}


async def _schedule_index(session: AsyncSession, document_id: str,
                          organization_id: str = "") -> None:
    """排一次索引任务（持久队列，见 routers/documents.py 里同名函数的说明）。"""
    await enqueue(session, kind="index", payload={"document_id": document_id},
                  organization_id=organization_id,
                  dedupe_key=f"index:{document_id}")
