"""结果取回与永久归档 —— 本层存在的核心理由。

service 只暂存 24h，且完成回调是尽力而为的（gateway 回调失败只记日志，任务照样落终态）。
所以归档必须满足两点：
1. **幂等**：回调与对账可能同时触发同一个任务，用状态机 claim 保证只有一方真干活
2. **可重入**：中途失败要退回 running，让对账下一轮重试，而不是卡在 archiving

归档产物（MinIO，results/{task_id}/）：
  document.md   图片引用已重写为 /api/tasks/{id}/images/{name}
  layout.json   版面结构原文（页码/bbox）
  images/*      从结果里的 data URI 解码落地
"""
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.metering import record_usage
from app.models import Task, utcnow
from app.service_client import ServiceClient
from app.storage import Storage, result_prefix

# markdown 图片引用：![alt](target "title")
_IMG_REF = re.compile(r"(!\[[^\]]*\]\()(<[^>]*>|[^)\s]+)(\s*(?:\"[^\"]*\")?\))")
_DATA_URI = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(?:;charset=[^;,]+)?(;base64)?,(.*)$", re.S)

# archiving 卡这么久就认为是上次进程被杀，允许重新 claim
STALE_ARCHIVING_SECONDS = 120


def claimable_condition(now: datetime | None = None):
    """可被归档流程接管的任务条件（对账与回调共用）。"""
    now = now or utcnow()
    stale = now - timedelta(seconds=STALE_ARCHIVING_SECONDS)
    return or_(Task.status.in_(("pending", "running")),
               and_(Task.status == "archiving", Task.updated_at < stale))


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


async def archive_task(session: AsyncSession, storage: Storage, service: ServiceClient,
                       task_id: str) -> bool:
    """取回 -> 落 MinIO -> 落库。真干活返回 True；被别人 claim 或无需归档返回 False。"""
    claimed = await session.execute(
        update(Task)
        .where(Task.id == task_id, claimable_condition())
        .values(status="archiving", updated_at=utcnow())
    )
    await session.commit()
    if claimed.rowcount == 0:
        return False        # 已归档完成，或另一路（回调/对账）正在处理

    task = await session.get(Task, task_id)
    if task is None:
        return False
    if task.service_task_id is None:
        # claim 已把状态改成 archiving，这里必须放回去，否则这一行会在
        # archiving 与 stale 之间反复摆动、永远落不了终态
        await _release(session, task, "no service task id")
        return False

    try:
        result = await service.get_result(task.service_task_id)
    except Exception as exc:                       # service 不可达/结果已过期
        await _release(session, task, str(exc))
        raise
    if result is None:                             # 409：service 侧还没归档完，下一轮再来
        await _release(session, task, None)
        return False

    prefix = result_prefix(task.id)
    image_base = f"/api/tasks/{task.id}/images"

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
    markdown = _rewrite_image_refs(markdown, names, image_base)
    markdown, _ = await _externalize_inline_images(markdown, storage, prefix, image_base)
    await storage.put(f"{prefix}document.md", markdown.encode(), "text/markdown; charset=utf-8")

    # 3. 版面 JSON（页码/bbox，前端定位与后续重建索引都要）
    layout = result.get("layout_json") or {}
    await storage.put(f"{prefix}layout.json",
                      json.dumps(layout, ensure_ascii=False).encode(), "application/json")

    # 4. 落库 + 计量（页数只有解析完才知道，额度在此刻才真正扣减）
    page_count = len(layout.get("pdf_info") or []) or 1
    task.page_count = page_count
    task.result_prefix = prefix
    task.status = "succeeded"
    task.error = None
    task.archived_at = datetime.now(UTC)
    await record_usage(session, user_id=task.user_id, api_key_id=task.api_key_id,
                       task_id=task.id, kind="parse", pages=page_count)
    await session.commit()
    return True


async def _release(session: AsyncSession, task: Task, error: str | None) -> None:
    """归档没做成：退回 running 让对账重试（不是终态，别写 failed）。"""
    task.status = "running"
    task.error = error
    task.updated_at = utcnow()
    await session.commit()


async def fail_task(session: AsyncSession, task: Task, error: str) -> None:
    task.status = "failed"
    task.error = error
    task.updated_at = utcnow()
    await session.commit()
