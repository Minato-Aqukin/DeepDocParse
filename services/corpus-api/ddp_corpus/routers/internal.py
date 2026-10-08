"""内网入口：模型网关的解析回调，与 control-api 的 outbox 事件。

两条路都只接受**服务身份**（`X-DDP-Actor-Kind: service` + 服务凭据），
用户凭据到不了这里。

## 回调是尽力而为，事件是至少一次

- **解析回调**（网关 -> 本服务）失败只记日志，真正的可靠性由
  `reconcile.py` 的对账保证。
- **outbox 事件**（control-api -> 本服务）会一直重投直到 ACK 或确定性拒绝，
  所以这里必须**幂等**：`processed_events` 按 event_id 去重，重投直接回
  `409 duplicate_event`。**只有这个码算 ACK**；DocumentSubmitted 的其它 4xx
  只有落在契约枚举 `ingest_rejection` 里才是终态拒绝，其余一律按暂时故障重投
  （见 Go 侧 `classifyDelivery`）。所以可恢复的冲突不要用那组码。

没有第二条的话，一次网络抖动就会让同一份上传变成两个 Document、
两次解析、两次计费。
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_corpus.archive import archive_job, fail_job
from ddp_corpus.capabilities import collect_capability_profiles
from ddp_corpus.control_client import ControlClient
from ddp_corpus.db import get_session
from ddp_corpus.deps import (
    Actor, get_service_client, get_storage, require_gateway_credentials, require_service_actor,
)
from ddp_corpus.errors import APIError
from ddp_corpus.ingest import ingest_document
from ddp_corpus.queue import enqueue
from ddp_corpus.models import Document, ParseJob, ProcessedEvent, Resource, ResourceVersion
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import Storage

router = APIRouter()


class UploadReclamation(BaseModel):
    object_key: str = Field(min_length=1, max_length=512,
                            pattern=r"^(uploads|tmp-remote-compute)/[^/]+/.+$")
    eligible_at: datetime


@router.post("/internal/upload-reclamation")
async def reclaim_upload(body: UploadReclamation,
                         _: Actor = Depends(require_service_actor),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage)):
    from ddp_corpus.gc import collect_terminal_upload

    reclaimed = await collect_terminal_upload(
        session, storage, object_key=body.object_key, eligible_at=body.eligible_at)
    await session.commit()
    return {"reclaimed": reclaimed}


@router.get("/internal/capabilities")
async def capabilities(request: Request,
                       _: Actor = Depends(require_service_actor)):
    """本节点的能力清单。**node_id 由 control-api 注入**，这里没有。

    上游网关的就绪度只对"本层确实走网关"的那些操作有效；独立配置的
    chat/embedding/rerank 端点观测不到就报 unknown（见 capabilities.py）。
    """
    profiles, status = await collect_capability_profiles(request.app.state.http)
    return {"profiles": profiles, "capability_status": status}


class ParseCallback(BaseModel):
    task_id: str        # 网关侧的 task_id
    status: str         # succeeded | failed


@router.post("/internal/parse-callback")
async def parse_callback(body: ParseCallback, request: Request,
                         _: None = Depends(require_gateway_credentials),
                         session: AsyncSession = Depends(get_session),
                         storage: Storage = Depends(get_storage),
                         service: ServiceClient = Depends(get_service_client)):
    # **只验服务凭据，不要 actor 头。** 模型网关无状态、不认识组织，它的回调
    # 只带 `Authorization`（ddp_gateway/worker/tasks.py::_notify_callback）。
    # 之前这里挂的是 require_service_actor：每一次回调都 401，解析结果全靠
    # 60 秒一轮的对账捡回来 —— 功能"正确"，只是每份文档都晚一分钟，且毫无报错。
    # 调用方的可信度就是 SERVICE_TOKEN 持有者的可信度；成功时本层自己去网关取结果。
    #
    # per-job HMAC 绑住组织边界：ingest.submit_parse 给每个 job 拼了自己的
    # `?token=`（service_client.callback_token，HMAC-SHA256 key=service_token、
    # msg=job.id）。网关把 callback_url 原样回打，token 跟着穿回来。
    # 跨组织伪造回调没有别人的 token —— 验不过就 401，一个字节都不动。
    #
    # 同一个网关任务可能对应本层多个 job（网关按 doc_id 去重，
    # 同一份文档从 Web 与对外 API 都提交过）——任意一个验过即放行整批
    from ddp_corpus.service_client import verify_callback_token
    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.service_task_id == body.task_id)
    )).scalars().all()
    if not jobs:
        # 未知任务不报错：网关不该因为本层的记账问题重试回调
        return {"ok": False, "reason": "unknown task"}
    token = request.query_params.get("token")
    if not any(verify_callback_token(job.id, token) for job in jobs):
        # 缺 token / token 对不上：跨组织伪造或配置错的旧回调 —— 401 且不落任何状态
        raise APIError(401, "invalid parse callback token",
                       "authentication_error", "invalid_callback_token")
    archived = 0
    for job in jobs:
        if body.status == "failed":
            try:
                live = await service.get_status(body.task_id)
                error = live.get("error") or "parse failed"
            except Exception:      # noqa: BLE001 —— 拿不到详情不该挡住落 failed
                error = "parse failed"
            await fail_job(session, job, error)
            await _record_remote_outcome(session, storage, job, ok=False, error=error)
            continue

        document = await session.get(Document, job.document_id)
        if document is None or document.origin == "external":
            job.status = "succeeded"        # 外部任务：文件在调用方那儿，不归档别人的结果
            await session.commit()
            continue
        if await archive_job(session, storage, service, job.id):
            archived += 1
            await _schedule_index(session, document.id, job_id=job.id)
            await _record_remote_outcome(session, storage, job, ok=True)
            # `enqueue` never commits and archive_job committed before it: without this the
            # index task was rolled back with the request session, the worker never got one,
            # and every upload waited for the reconciler to index it (2026-09-24, phase E).
            await session.commit()

    return {"ok": True, "archived": archived}


async def _record_remote_outcome(session, storage, job, *, ok: bool, error: str | None = None):
    """Publish the canonical fixed-version snapshot, never a placeholder receipt."""
    import hashlib

    from ddp_core.bundle import BundleError, build_bundle
    from ddp_corpus.remote_compute_ingest import record_parse_outcome
    from ddp_corpus.remote_compute_models import RemoteCompute, expire_if_due
    from ddp_corpus.routers.bundles import _snapshot
    from ddp_corpus.routers.remote_compute import cleanup_compute

    row = await session.scalar(select(RemoteCompute).where(
        RemoteCompute.parse_job_id == job.id).limit(1).with_for_update()
        .execution_options(populate_existing=True))
    if row is None:
        return
    if expire_if_due(row):
        await session.commit()
        await cleanup_compute(session, storage, row)
        return
    if row.status not in ("content_verified", "running"):
        await cleanup_compute(session, storage, row)
        return
    if not ok:
        await record_parse_outcome(session, compute_id=row.id,
            organization_id=row.organization_id, parse_job_id=job.id, ok=False, error=error)
        await cleanup_compute(session, storage, row)
        return
    # The archive callback precedes indexing. Let durable reconciliation wait for
    # compilation instead of freezing a permanently empty evidence set.
    if job.status != "succeeded" or job.index_status not in ("ready", "failed"):
        return
    fixed = row.manifest_json or {}
    resource = await session.get(Resource, fixed.get("source_resource_id")) \
        if fixed.get("source_resource_id") else None
    version = await session.get(ResourceVersion, fixed.get("source_version_id")) \
        if fixed.get("source_version_id") else None
    if (resource is None or version is None or resource.deleted_at is not None
            or version.deleted_at is not None or version.resource_id != resource.id
            or resource.id != job.resource_id or resource.owner_id != row.actor_id
            or resource.organization_id != row.organization_id
            or version.document_id != job.document_id or version.parse_job_id != job.id
            or version.source_digest != row.input_sha256 or version.size_bytes != row.input_size):
        await record_parse_outcome(session, compute_id=row.id,
            organization_id=row.organization_id, parse_job_id=job.id, ok=False,
            error="fixed_source_unavailable")
        await cleanup_compute(session, storage, row)
        return
    try:
        snapshot = await _snapshot(session, storage, resource, version)
        if snapshot.source["original"] != "present" or snapshot.layout["state"] != "present":
            raise BundleError("fixed_source_unavailable", "original or fixed layout is unavailable")
        bundle = build_bundle(snapshot.source, snapshot.files)
    except BundleError as exc:
        await record_parse_outcome(session, compute_id=row.id,
            organization_id=row.organization_id, parse_job_id=job.id, ok=False, error=exc.code)
        await cleanup_compute(session, storage, row)
        return
    output_sha256 = hashlib.sha256(bundle).hexdigest()
    # Competing callback/reconciler attempts cannot overwrite the winning bytes.
    bundle_key = f"tmp-remote-compute/{row.organization_id}/{row.id}/output-{output_sha256}.zip"
    await storage.put(bundle_key, bundle, "application/zip")
    # Compile degradations stay verbatim in their own list enum; delivery gaps use
    # the `degraded` enum so every consumer renders them from the same contract.
    degraded = []
    if job.index_status == "failed":
        degraded.append("resource_index_unavailable")
    if not snapshot.evidence:
        degraded.append("evidence_unavailable")
    await record_parse_outcome(session, compute_id=row.id,
        organization_id=row.organization_id, parse_job_id=job.id, ok=True,
        bundle_key=bundle_key, output_sha256=output_sha256,
        output_meta={"compile_status": job.compile_status, "index_status": job.index_status,
                     "compile_degraded": sorted(set(job.compile_degraded or [])),
                     "degraded": degraded})
    await cleanup_compute(session, storage, row)


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
    直接回 409 duplicate_event（投递器只把这个码当 ACK）。抢到之后再干活 —— 干活失败会让
    事务回滚，占位行也跟着没了，下一次重投还能再来。
    """
    try:
        async with session.begin_nested():
            session.add(ProcessedEvent(event_id=event.event_id, type=event.type,
                                       organization_id=event.organization_id))
    except IntegrityError:
        # 已处理过。**409 而不是 200**：投递器认 duplicate_event 为 ACK（不再重投），
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
    target_resource_id = p.get("target_resource_id")
    if target_resource_id is not None and (
            not isinstance(target_resource_id, str) or not 1 <= len(target_resource_id) <= 128):
        raise APIError(400, "invalid upload target", "invalid_request_error", "invalid_upload_target")
    options = p.get("options") or {}
    if isinstance(options, str):
        options = json.loads(options or "{}")
    # Temporary file-compute inputs never take the permanent corpus path:
    # they bind to their waiting record and reuse the real parse queue only
    # after full verification, without entering the public catalog.
    if (p.get("purpose") or "permanent") == "temporary_compute":
        if target_resource_id is not None:
            raise APIError(400, "temporary uploads cannot target a resource",
                           "invalid_request_error", "invalid_upload_target")
        from ddp_corpus.remote_compute_ingest import bind_verified_upload
        row, _job = await bind_verified_upload(
            session, storage, service, control,
            organization_id=event.organization_id,
            actor_id=p["actor_id"], actor_kind=p.get("actor_kind") or "user",
            upload_id=p.get("upload_id") or event.event_id,
            object_key=p["object_key"],
            filename=p.get("filename") or "document.pdf",
            mime=p.get("mime") or "application/octet-stream",
            size_bytes=int(p.get("size") or 0),
            sha256=p["sha256"],
            remote_compute_id=p.get("remote_compute_id") or "",
        )
        return row.id
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
        upload_key=event.event_id,
        receipt_key=p.get("upload_id") or event.event_id,
        target_resource_id=target_resource_id,
    )
    # **复活的文档要把索引推回去。** 删除会清空 chunks 并把 index_status 置回
    # none；复活时如果不重新排队，文档看着好好的却永远问不了，而对账只捞
    # pending 状态的 job，自愈不了 —— 只能等用户自己发现去点"重建索引"。
    # 同参数重传会在 ingest 里命中已有 job 直接返回，正是这条路径。
    if _job and _job.index_status == "pending":
        await _schedule_index(session, document.id, event.organization_id, job_id=_job.id)
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
    from ddp_corpus.resources import tombstone_resource
    resources = (await session.execute(select(Resource).join(ResourceVersion).where(
        ResourceVersion.document_id == document.id,
        Resource.organization_id == event.organization_id,
        Resource.deleted_at.is_(None)).distinct())).scalars().all()
    if resources:
        for resource in resources:
            await tombstone_resource(session, resource)
    else:
        mapped = await session.scalar(select(ResourceVersion.id).where(
            ResourceVersion.document_id == document.id).limit(1))
        if not mapped and document.organization_id == event.organization_id:
            document.deleted_at = utcnow()
    return document.id


_HANDLERS = {
    "DocumentSubmitted": _on_document_submitted,
    "DocumentDeleted": _on_document_deleted,
}


async def _schedule_index(session: AsyncSession, document_id: str,
                          organization_id: str = "", *, job_id: str | None = None) -> None:
    """排一次索引任务（持久队列，见 routers/documents.py 里同名函数的说明）。"""
    if job_id is None:
        job_id = await session.scalar(select(Document.current_job_id).where(Document.id == document_id))
    if not job_id:
        return
    job = await session.get(ParseJob, job_id)
    resource = await session.get(Resource, job.resource_id) if job and job.resource_id else None
    await enqueue(session, kind="index", payload={"document_id": document_id, "job_id": job_id},
                  organization_id=resource.organization_id if resource else organization_id,
                  dedupe_key=f"index:{job_id}")
