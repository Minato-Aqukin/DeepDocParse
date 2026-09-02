"""结果取回与永久归档 —— 本层存在的核心理由。

service 只暂存 24h，且完成回调是尽力而为的（gateway 回调失败只记日志，任务照样落终态）。
所以归档必须满足两点：
1. **幂等**：回调与对账可能同时触发同一个 job，用状态机 claim 保证只有一方真干活
2. **可重入**：中途失败要退回 running，让对账下一轮重试，而不是卡在 archiving

归档产物（MinIO，results/{job_id}/）：
  document.md   图片引用已重写为 /api/documents/{doc}/jobs/{job}/images/{name}
  layout.json   版面结构原文（页码/bbox）—— 也是本层重建向量索引的唯一输入
  images/*      从结果里的 data URI 解码落地

归档成功后把 document 置为 index_status='pending'，由调用方投递索引任务。
"""
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ddp_core.chunking import page_count_of
from ddp_corpus.usage import record_usage
from ddp_corpus.models import Document, ParseJob, utcnow
from ddp_corpus.service_client import ServiceClient
from ddp_corpus.storage import Storage, job_result_prefix
from ddp_corpus.versions import advance_index_generation

# markdown 图片引用：![alt](target "title")
_IMG_REF = re.compile(r"(!\[[^\]]*\]\()(<[^>]*>|[^)\s]+)(\s*(?:\"[^\"]*\")?\))")
_DATA_URI = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(?:;charset=[^;,]+)?(;base64)?,(.*)$", re.S)

# archiving 卡这么久就认为是上次进程被杀，允许重新 claim
STALE_ARCHIVING_SECONDS = 120


def claimable_condition(now: datetime | None = None):
    """可被归档流程接管的 job 条件（对账与回调共用）。"""
    now = now or utcnow()
    stale = now - timedelta(seconds=STALE_ARCHIVING_SECONDS)
    return or_(ParseJob.status.in_(("pending", "running")),
               and_(ParseJob.status == "archiving", ParseJob.updated_at < stale))


def image_base_url(document_id: str, job_id: str) -> str:
    """图片按 job 隔离：markdown 是某一次解析的产物，指向 document 级路径会串版本。"""
    return f"/api/documents/{document_id}/jobs/{job_id}/images"


def _ext_of(mime: str | None) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        mime or "", ".png")


def _rewrite_image_refs(markdown: str, names: set[str], base_url: str) -> str:
    """把 markdown 里指向图片文件的引用改写到本层的图片代理端点。"""
    def sub(m: re.Match) -> str:
        target = m.group(2).strip("<>")
        if PurePosixPath(target).name in names:
            return f"{m.group(1)}{base_url}/{PurePosixPath(target).name}{m.group(3)}"
        return m.group(0)

    return _IMG_REF.sub(sub, markdown)


async def _externalize_inline_images(markdown: str, storage: Storage, prefix: str,
                                     base_url: str) -> tuple[str, int]:
    """markdown 里内联的 data URI 图片落成对象存储文件。

    归档后的 markdown 不该带 base64：既让结果体积失控，前端渲染也慢得离谱。
    """
    saved = 0
    pending: list[tuple[str, str]] = []      # (原 target, 新文件名)

    for m in _IMG_REF.finditer(markdown):
        target = m.group(2).strip("<>")
        parsed = _DATA_URI.match(target)
        if parsed is None:
            continue
        mime, is_b64, payload = parsed.group(1), parsed.group(2), parsed.group(3)
        if not is_b64:
            continue
        try:
            data = base64.b64decode(payload)
        except (ValueError, TypeError):
            continue
        name = f"inline_{saved}{_ext_of(mime)}"
        await storage.put(f"{prefix}images/{name}", data, mime or "image/png")
        pending.append((target, name))
        saved += 1

    for target, name in pending:
        markdown = markdown.replace(target, f"{base_url}/{name}")
    return markdown, saved


async def archive_job(session: AsyncSession, storage: Storage, service: ServiceClient,
                      job_id: str) -> bool:
    """取回 -> 落 MinIO -> 落库。真干活返回 True；被别人 claim 或无需归档返回 False。"""
    claimed = await session.execute(
        update(ParseJob)
        .where(ParseJob.id == job_id, claimable_condition())
        .values(status="archiving", updated_at=utcnow())
    )
    await session.commit()
    if claimed.rowcount == 0:
        return False        # 已归档完成，或另一路（回调/对账）正在处理

    job = await session.get(ParseJob, job_id)
    if job is None:
        return False
    if job.service_task_id is None:
        # claim 已把状态改成 archiving，这里必须放回去，否则这一行会在
        # archiving 与 stale 之间反复摆动、永远落不了终态
        await release_job(session, job, "no service task id")
        return False

    document = await session.get(Document, job.document_id)
    if document is None:
        await release_job(session, job, "document missing")
        return False

    try:
        result = await service.get_result(job.service_task_id)
    except Exception as exc:                       # service 不可达/结果已过期
        await release_job(session, job, str(exc))
        raise
    if result is None:                             # 409：service 侧还没归档完，下一轮再来
        await release_job(session, job, None)
        return False

    prefix = job_result_prefix(job.id)
    base_url = image_base_url(document.id, job.id)

    # 1. 结果里的图片是 data URI（见 gateway mineru_client._RESULT_FIELDS），解码落盘
    names: set[str] = set()
    for image in result.get("images") or []:
        name, url = image.get("name"), image.get("url") or ""
        if not name:
            continue
        parsed = _DATA_URI.match(url)
        if parsed is None:
            continue
        mime, is_b64, payload = parsed.group(1), parsed.group(2), parsed.group(3)
        data = base64.b64decode(payload) if is_b64 else payload.encode()
        await storage.put(f"{prefix}images/{name}", data, mime or "application/octet-stream")
        names.add(name)

    # 2. markdown：引用重写 + 内联 base64 外置
    markdown = result.get("markdown") or ""
    markdown = _rewrite_image_refs(markdown, names, base_url)
    markdown, _ = await _externalize_inline_images(markdown, storage, prefix, base_url)
    await storage.put(f"{prefix}document.md", markdown.encode(), "text/markdown; charset=utf-8")

    # 3. 版面 JSON —— 前端定位、问答出处、向量索引重建全靠它，必须永久留一份
    layout = result.get("layout_json") or {}
    await storage.put(f"{prefix}layout.json",
                      json.dumps(layout, ensure_ascii=False).encode(), "application/json")

    # 4. 落库 + 计量（页数只有解析完才知道，额度在此刻才真正扣减）。
    # 取结果/写对象可能很久；最终写 Document 前必须锁行重读。用户若在此期间
    # 删除文档，解析结果仍可归档并据实计量，但绝不能把已删除行重新置为 pending。
    document = (await session.execute(
        select(Document).where(Document.id == job.document_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if document is None:
        await release_job(session, job, "document missing after result download")
        return False
    page_count = page_count_of(layout) or 1
    job.page_count = page_count
    job.result_prefix = prefix
    job.status = "succeeded"
    job.error = None
    job.archived_at = datetime.now(UTC)

    # 首次解析成功即成为当前版本；后续重解析要用户显式切换，避免结果在脚下被换掉。
    # deleted_at 是第二道安全闸：即使 callback 随后仍投递 index task，claim 也拒绝删除行。
    if document.deleted_at is None:
        previous_current = document.current_job_id
        effective_current = previous_current or job.id
        if effective_current == job.id:
            generation = await advance_index_generation(
                session, document.id, expected_current_job_id=previous_current,
                deleted=False, values={
                    "current_job_id": effective_current, "page_count": page_count,
                    "index_status": "pending", "index_error": None,
                    "index_lease_until": None, "compile_status": "pending",
                    "compile_degraded": [], "updated_at": utcnow(),
                })
            if generation is None:  # 行锁下只可能是异常的外部状态改写
                raise RuntimeError("document state changed while archive row was locked")
            await session.refresh(document)
        else:
            document.updated_at = utcnow()

    # 记在发起这次解析的人头上；老 job 没这个信息时退回上传者
    await record_usage(session, actor_id=job.initiated_by or document.uploaded_by,
                       organization_id=document.organization_id,
                       api_key_id=job.api_key_id,
                       parse_job_id=job.id, kind="parse", pages=page_count)
    await session.commit()
    return True


async def release_job(session: AsyncSession, job: ParseJob, error: str | None) -> None:
    """归档没做成：退回 running 让对账重试（不是终态，别写 failed）。"""
    job.status = "running"
    job.error = error
    job.updated_at = utcnow()
    await session.commit()


async def fail_job(session: AsyncSession, job: ParseJob, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.updated_at = utcnow()
    await session.commit()
