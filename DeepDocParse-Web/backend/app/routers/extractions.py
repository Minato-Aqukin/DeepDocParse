"""结构化抽取的对外接口：模板 / 批量执行 / 结果表格 / 导出。

抽取与问答最大的结构差别是**批量**：问答一次一个问题，抽取的真实用法是
"一批同类文档 -> 一张表"。所以这里的核心对象是 run（一次执行，可横跨 N 份文档），
结果是 item（行 = 文档 × 记录序号），前端的表格直接就是它。

**边界写死**：这是单一操作的批量化，不是工作流编排。
不加条件分支、不加步骤串联、不加触发器 —— 那是 Dify 的品类，
在 README「明确不做」里（"永不"）。不写死这条，这个模块会自己滑过去。

铁律 5：后台任务必须自己开 session。请求作用域的 session 在响应返回后就关了，
在后台任务里复用它必炸（问答的 SSE 上踩过两次）。
"""
import asyncio
import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session, get_sessionmaker
from app.deps import current_user, get_storage
from app.errors import APIError
from app.extract_schema import SchemaError, parse_schema, validate_schema
from app.extraction import ExtractContext, extraction_model_meta, run as run_extraction
from app.metering import record_usage
from app.qa import attach_resolution, load_citation_targets
from app.models import (
    Document, ExtractionItem, ExtractionRun, ExtractionTemplate, ParseJob, User, as_aware, utcnow,
)

router = APIRouter()

# 后台抽取任务的强引用集合。事件循环只对运行中的 task 保持弱引用，
# 不持有的话它可能被 GC 掉 —— 而那是静默的（run 永远停在 running）
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# ---------- 模板 ----------

# 线上字段名是 schema_json（与库里的列、前端的字段一致），但**不能直接拿它当
# python 属性名**：pydantic BaseModel 自己有一个 schema_json，同名会被警告并埋下
# 行为歧义。用别名把两件事分开 —— 线上名不变，属性名换成 doc_schema
_SCHEMA_FIELD = ConfigDict(populate_by_name=True)


class TemplateIn(BaseModel):
    model_config = _SCHEMA_FIELD

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    doc_schema: dict = Field(alias="schema_json", serialization_alias="schema_json")


class TemplateOut(BaseModel):
    model_config = _SCHEMA_FIELD

    id: str
    name: str
    description: str
    doc_schema: dict = Field(alias="schema_json", serialization_alias="schema_json")
    field_count: int
    kind: str
    created_at: str
    updated_at: str


def _template_out(row: ExtractionTemplate) -> TemplateOut:
    spec = parse_schema(row.schema_json or {})
    return TemplateOut(
        id=row.id, name=row.name, description=row.description,
        doc_schema=row.schema_json or {}, field_count=len(spec.fields), kind=spec.kind,
        created_at=as_aware(row.created_at).isoformat(),
        updated_at=as_aware(row.updated_at).isoformat(),
    )


def _validate_or_400(schema: dict) -> None:
    """坏 schema 当场 400。

    **不能等到跑起来再说**：一次抽取是 N 次检索 + N 次模型调用，
    跑完再告诉用户"你的 schema 第 3 个字段缺 description"，是在烧他的额度。
    """
    problems = validate_schema(schema)
    if problems:
        raise APIError(400, "schema 不合法：" + "；".join(problems),
                       "invalid_request_error", "invalid_schema")


@router.get("/extractions/templates", response_model=list[TemplateOut])
async def list_templates(user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ExtractionTemplate)
        .where(ExtractionTemplate.user_id == user.id,
               ExtractionTemplate.deleted_at.is_(None))
        .order_by(ExtractionTemplate.updated_at.desc())
    )).scalars().all()
    return [_template_out(r) for r in rows]


@router.post("/extractions/templates", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateIn, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    _validate_or_400(body.doc_schema)
    existing = (await session.execute(
        select(ExtractionTemplate).where(ExtractionTemplate.user_id == user.id,
                                         ExtractionTemplate.name == body.name)
    )).scalar_one_or_none()
    if existing is not None and existing.deleted_at is None:
        raise APIError(409, f"模板名已存在：{body.name}", "invalid_request_error",
                       "duplicate_name")
    if existing is not None:
        # 软删过的同名模板：复活它而不是插一条新行 —— 唯一约束是
        # (user_id, name)，不含 deleted_at，直接插会撞约束报 500
        existing.deleted_at = None
        existing.description = body.description
        existing.schema_json = body.doc_schema
        existing.updated_at = utcnow()
        await session.commit()
        return _template_out(existing)

    row = ExtractionTemplate(user_id=user.id, name=body.name, description=body.description,
                             schema_json=body.doc_schema)
    session.add(row)
    await session.commit()
    return _template_out(row)


@router.put("/extractions/templates/{template_id}", response_model=TemplateOut)
async def update_template(template_id: str, body: TemplateIn,
                          user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    _validate_or_400(body.doc_schema)
    row = await _owned_template(template_id, user, session)
    row.name, row.description, row.schema_json = body.name, body.description, body.doc_schema
    row.updated_at = utcnow()
    await session.commit()
    return _template_out(row)


@router.delete("/extractions/templates/{template_id}", status_code=204)
async def delete_template(template_id: str, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    row = await _owned_template(template_id, user, session)
    # 软删：历史 run 的 template_id 还指着它，硬删会让"这批结果是哪个模板跑的"永久失答。
    # run 本身不受影响 —— 它存的是 schema 快照，不取模板当前值
    row.deleted_at = utcnow()
    await session.commit()


async def _owned_template(template_id: str, user: User,
                          session: AsyncSession) -> ExtractionTemplate:
    row = await session.get(ExtractionTemplate, template_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise APIError(404, "模板不存在", "invalid_request_error", "template_not_found")
    return row


# ---------- 执行 ----------

class RunIn(BaseModel):
    model_config = _SCHEMA_FIELD

    document_ids: list[str] = Field(min_length=1)
    template_id: str | None = None
    # 与 template_id 二选一。给了 schema_json 就用它（临时抽取，不建模板）
    doc_schema: dict | None = Field(default=None, alias="schema_json",
                                    serialization_alias="schema_json")
    name: str = Field(default="", max_length=128)
    verify: bool | None = None


class RunOut(BaseModel):
    model_config = _SCHEMA_FIELD

    id: str
    name: str
    template_id: str | None
    kind: str
    status: str
    document_count: int
    done_count: int
    error: str | None
    field_names: list[str]
    doc_schema: dict = Field(alias="schema_json", serialization_alias="schema_json")
    model_meta: dict
    created_at: str


def _run_out(row: ExtractionRun) -> RunOut:
    spec = parse_schema(row.schema_json or {})
    return RunOut(
        id=row.id, name=row.name, template_id=row.template_id, kind=row.kind,
        status=row.status, document_count=row.document_count, done_count=row.done_count,
        error=row.error, field_names=[f.name for f in spec.fields],
        doc_schema=row.schema_json or {}, model_meta=row.model_meta or {},
        created_at=as_aware(row.created_at).isoformat(),
    )


@router.post("/extractions/runs", response_model=RunOut, status_code=202)
async def create_run(body: RunIn, request: Request, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    if len(body.document_ids) > settings.extract_max_documents:
        raise APIError(400,
                       f"一次最多 {settings.extract_max_documents} 份文档"
                       f"（收到 {len(body.document_ids)} 份）",
                       "invalid_request_error", "too_many_documents")

    await request.app.state.rate_limiter.check(f"extract:{user.id}",
                                               settings.extract_rate_per_min)

    schema = body.doc_schema
    template_id = body.template_id
    if template_id:
        template = await _owned_template(template_id, user, session)
        # **快照，不是引用**：模板改了之后历史 run 的列会对不上号
        # （同一条教训在 Message.model_meta 上吃过一次）
        schema = template.schema_json
    if not schema:
        raise APIError(400, "必须提供 template_id 或 schema_json 之一",
                       "invalid_request_error", "missing_schema")
    _validate_or_400(schema)
    spec = parse_schema(schema)

    documents = (await session.execute(
        select(Document).where(Document.id.in_(body.document_ids),
                               Document.user_id == user.id,
                               Document.deleted_at.is_(None))
    )).scalars().all()
    if not documents:
        raise APIError(404, "没有可抽取的文档（不存在、已删除或不属于你）",
                       "invalid_request_error", "no_documents")

    # 未建索引的文档抽不了。**当场说清楚是哪几份**，不要让它们跑完变成一堆
    # 空结果 —— 空值看起来像"文档里没有"，那是抽取里最危险的误导
    not_ready = [d.filename for d in documents if d.index_status != "ready"]
    if len(not_ready) == len(documents):
        raise APIError(409,
                       "所选文档都还没建好索引，无法抽取："
                       + "、".join(not_ready[:5])
                       + ("…" if len(not_ready) > 5 else ""),
                       "invalid_request_error", "index_not_ready")

    ready = [d for d in documents if d.index_status == "ready"]
    # 部分未就绪：**不静默丢弃**。此前它们只体现在 document_count 变小上，
    # 用户不知道自己勾的 20 份里有 3 份根本没跑 —— 与上面那句注释自相矛盾
    skipped_note = ("；已跳过未建索引的 "
                    + "、".join(not_ready[:5])
                    + ("…" if len(not_ready) > 5 else "")) if not_ready else ""
    row = ExtractionRun(
        user_id=user.id, template_id=template_id, name=body.name or "未命名抽取",
        schema_json=schema, kind=spec.kind, status="pending",
        document_count=len(ready), error=skipped_note.lstrip("；") or None,
        model_meta=extraction_model_meta())
    session.add(row)
    await session.commit()

    # 后台跑。**自己开 session**（铁律 5）：请求作用域的这个在响应返回后就关了。
    # **必须持有强引用**：事件循环只对运行中的 task 保持弱引用，
    # 不存着的话它可能在中途被 GC 掉，而那是静默的（run 停在 running）
    task = asyncio.create_task(_execute_run(
        row.id, [d.id for d in ready], schema,
        storage=get_storage(request), http=request.app.state.http,
        index=request.app.state.search_index, verify=body.verify))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return _run_out(row)


async def _execute_run(run_id: str, document_ids: list[str], schema: dict, *,
                       storage, http, index, verify: bool | None) -> None:
    """后台执行一次 run。**任何异常都要落终态**，否则 run 永远停在 running。"""
    sessionmaker = get_sessionmaker()
    try:
        spec = parse_schema(schema)
    except (SchemaError, Exception) as exc:  # noqa: BLE001
        async with sessionmaker() as session:
            await _fail_run(session, run_id, f"schema 解析失败：{exc}")
        return

    async with sessionmaker() as session:
        await session.execute(update(ExtractionRun).where(ExtractionRun.id == run_id)
                              .values(status="running", updated_at=utcnow()))
        await session.commit()

    semaphore = asyncio.Semaphore(settings.extract_doc_concurrency)

    async def one(document_id: str) -> None:
        async with semaphore:
            async with sessionmaker() as session:
                try:
                    await _extract_one(session, run_id, document_id, spec,
                                       storage=storage, http=http, index=index, verify=verify)
                except Exception as exc:  # noqa: BLE001
                    # 单份文档失败不拖垮整批：落一条 failed 的 item，其余照跑。
                    # 批量里最常见的失败是个别文档解析有问题，不该让整批白跑
                    await session.rollback()
                    session.add(ExtractionItem(
                        run_id=run_id, document_id=document_id, status="failed",
                        error=f"{type(exc).__name__}: {exc}", fields={}))
                    await _bump_done(session, run_id)
                    await session.commit()

    # **收尾必须有保证**：gather 与终态计算此前都裸露在 try 之外，
    # 单份文档的 except 分支自己 commit 失败、或收尾块的 get/commit 失败，
    # run 就永久停在 running，界面无限转圈，只能等进程重启由
    # reset_orphaned_runs() 收尸 —— 与本函数 docstring 的承诺正好相反
    try:
        await asyncio.gather(*(one(d) for d in document_ids))
        async with sessionmaker() as session:
            run = await session.get(ExtractionRun, run_id)
            if run is None:
                return
            statuses = (await session.execute(
                select(ExtractionItem.status).where(ExtractionItem.run_id == run_id)
            )).scalars().all()
            if not statuses:
                run.status = "failed"
                run.error = "没有产出任何结果"
            elif all(s == "failed" for s in statuses):
                run.status = "failed"
            elif any(s in ("failed", "partial") for s in statuses):
                run.status = "partial"
            else:
                run.status = "succeeded"
            run.updated_at = utcnow()
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        async with sessionmaker() as session:
            await _fail_run(session, run_id, f"抽取中断：{type(exc).__name__}: {exc}")


async def _extract_one(session: AsyncSession, run_id: str, document_id: str, spec, *,
                       storage, http, index, verify: bool | None) -> None:
    document = await session.get(Document, document_id)
    if document is None:
        # 抽取期间文档被**硬删**了（gc.py 会这么做；软删时 session.get 照样返回）。
        # **不能落 item**：ExtractionItem.document_id 是非空外键，指向一个已经不存在的
        # 文档会撞 FK 约束 -> commit 抛 IntegrityError -> 外层 except 再插一次再抛 ->
        # 整批 run 被打成 failed。比"进度条差一格"糟得多。
        # （SQLite 默认不强制外键，单测抓不到这条，只有真 PG 上会炸）
        # 只推进度，原因记到 run.error 上
        await _bump_done(session, run_id)
        await session.execute(
            update(ExtractionRun).where(ExtractionRun.id == run_id)
            .values(error=func.coalesce(ExtractionRun.error, "")
                    + f"；文档 {document_id[:8]} 在抽取期间被删除"))
        await session.commit()
        return
    job = await session.get(ParseJob, document.current_job_id) if document.current_job_id else None

    ctx = ExtractContext(session=session, index=index, http=http, storage=storage,
                         document=document, job=job, user_id=document.user_id, verify=verify)
    outcome = await run_extraction(ctx, spec)

    if outcome.records:
        for i, record in enumerate(outcome.records):
            session.add(ExtractionItem(
                run_id=run_id, document_id=document_id,
                parse_job_id=job.id if job else None, record_index=i,
                status=outcome.status, degraded=outcome.degraded, fields=record["fields"]))
    else:
        session.add(ExtractionItem(
            run_id=run_id, document_id=document_id,
            parse_job_id=job.id if job else None, record_index=0,
            status=outcome.status, degraded=outcome.degraded, fields=outcome.fields))

    # 按**字段数**计量：一次抽取 = N 次检索 + N 次模型调用，
    # 按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜
    await record_usage(session, user_id=document.user_id, kind="extract",
                       requests=max(outcome.usage.get("fields", 0), 1))
    await _bump_done(session, run_id)
    await session.commit()


async def _bump_done(session: AsyncSession, run_id: str) -> None:
    await session.execute(
        update(ExtractionRun).where(ExtractionRun.id == run_id)
        .values(done_count=ExtractionRun.done_count + 1, updated_at=utcnow()))


async def _fail_run(session: AsyncSession, run_id: str, reason: str) -> None:
    await session.execute(update(ExtractionRun).where(ExtractionRun.id == run_id)
                          .values(status="failed", error=reason, updated_at=utcnow()))
    await session.commit()


async def reset_orphaned_runs() -> None:
    """启动时把卡在 pending/running 的 run 标成 failed。

    后台任务活在进程内存里，进程一重启它们就没了 —— 而 run 会永远停在 running，
    界面上转圈转到天荒地老。**如实标成失败并说明原因**，用户可以重跑。
    这与 reconcile 处理解析回调丢失是同一类问题，只是抽取没有远端可对账
    （抽取的中间状态不在 service 侧），所以只能这样兜。
    """
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ExtractionRun)
            .where(ExtractionRun.status.in_(("pending", "running")))
            .values(status="failed", updated_at=utcnow(),
                    error="服务重启，抽取中断。结果可能不完整，请重新发起"))
        await session.commit()


# ---------- 结果 ----------

@router.get("/extractions/runs", response_model=list[RunOut])
async def list_runs(limit: int = 50, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ExtractionRun).where(ExtractionRun.user_id == user.id)
        .order_by(ExtractionRun.created_at.desc()).limit(min(limit, 200))
    )).scalars().all()
    return [_run_out(r) for r in rows]


@router.get("/extractions/runs/{run_id}")
async def get_run(run_id: str, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    run = await _owned_run(run_id, user, session)
    items, filenames = await _load_items(session, run_id)
    return {
        # by_alias：线上字段名必须是 schema_json（前端与库里都用这个名），
        # 不加的话前端会拿到一个叫 doc_schema 的字段
        "run": _run_out(run).model_dump(by_alias=True),
        "items": [_item_out(i, filenames) for i in items],
    }


@router.delete("/extractions/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    run = await _owned_run(run_id, user, session)
    await session.execute(delete(ExtractionItem).where(ExtractionItem.run_id == run.id))
    await session.delete(run)
    await session.commit()


async def _owned_run(run_id: str, user: User, session: AsyncSession) -> ExtractionRun:
    run = await session.get(ExtractionRun, run_id)
    if run is None or run.user_id != user.id:
        raise APIError(404, "抽取任务不存在", "invalid_request_error", "run_not_found")
    return run


async def _load_items(session: AsyncSession,
                      run_id: str) -> tuple[list[ExtractionItem], dict[str, str]]:
    items = (await session.execute(
        select(ExtractionItem).where(ExtractionItem.run_id == run_id)
        .order_by(ExtractionItem.created_at, ExtractionItem.record_index)
    )).scalars().all()
    # 文件名一次查完，不要每行查一次（一批 200 份文档就是 200 次往返）
    doc_ids = {i.document_id for i in items}
    filenames = dict((await session.execute(
        select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
    )).all()) if doc_ids else {}
    return items, filenames


def _citation_out(document_id: str, citation: dict,
                  lookup: dict | None = None) -> dict:
    """把对象键换成前端能直接取的 URL —— 与问答平面同一套路径。

    截图受 JWT 保护，`<img src>` 直接取不到（发不出 Authorization 头），
    前端要先 fetch 成 blob URL。复用 `/api/documents/{id}/crops/{job}/{name}`
    这个既有端点，因此这里的形状必须与 conversations._citation_out 一致 ——
    不一致的话前端的 CitationChip 就没法两边共用了。
    """
    # **不能无脑 resolved=True**：抽取结果会被反复打开，而重建索引会改 seq
    # （M9 的分块规则变化就会）。走与问答平面同一道内容比对，指不回去就如实说
    out = attach_resolution(citation, lookup) if lookup is not None else dict(citation)
    out.setdefault("resolved", True)
    key = out.pop("crop_key", None)
    if key:
        job_id, name = key.split("/")[1], key.rsplit("/", 1)[-1]
        out["crop_url"] = f"/api/documents/{document_id}/crops/{job_id}/{name}"
    else:
        out["crop_url"] = None
    return out


def _fields_out(document_id: str, fields: dict, lookup: dict | None = None) -> dict:
    """字段表里的每条出处都要过一遍 URL 转换（库里存的是对象键）+ 定位校验。"""
    out = {}
    for name, cell in (fields or {}).items():
        if not isinstance(cell, dict):
            continue
        item = dict(cell)
        item["citations"] = [_citation_out(document_id, c, lookup)
                             for c in (cell.get("citations") or [])]
        out[name] = item
    return out


def _item_out(item: ExtractionItem, filenames: dict[str, str],
              lookup: dict | None = None) -> dict:
    return {
        "id": item.id,
        "document_id": item.document_id,
        "filename": filenames.get(item.document_id, ""),
        "parse_job_id": item.parse_job_id,
        "record_index": item.record_index,
        "status": item.status,
        "degraded": item.degraded,
        "error": item.error,
        "fields": _fields_out(item.document_id, item.fields or {}, lookup),
    }


# ---------- 导出 ----------

# CSV 注入防护：Excel / WPS 会把以这些字符开头的单元格当公式执行。
# 抽取结果直接来自文档内容（不可信输入），不处理就等于给用户发了一个可执行文件
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """把值变成安全的 CSV 单元格。

    以公式字符开头的前面加一个单引号 —— Excel 会显示原文而不是求值。
    **不能只在前端做**：导出的文件会被转发、归档、二次打开，
    防护必须在产生文件的地方。
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIX):
        return "'" + text
    return text


@router.get("/extractions/runs/{run_id}/export.csv")
async def export_run_csv(run_id: str, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    """导出成 CSV：行 = 记录，列 = schema 字段（每个字段附一列出处页码）。

    出处那一列不是装饰：抽取结果拿去做决策之前，"这个数从第几页来的"
    是唯一能快速复核的线索。导出丢掉它，可验证出处就只活在界面上。
    """
    run = await _owned_run(run_id, user, session)
    spec = parse_schema(run.schema_json or {})
    items, filenames = await _load_items(session, run_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["文档", "记录序号", "状态", "降级"]
    for field in spec.fields:
        # 列名来自用户给的 schema property 名，**同样是不可信输入**。
        # 只防了值单元格而漏掉表头，等于留了一扇一样宽的门
        header += [_csv_safe(field.name), _csv_safe(f"{field.name}·出处页")]
    writer.writerow(header)

    for item in items:
        fields = item.fields or {}
        row = [_csv_safe(filenames.get(item.document_id, item.document_id)),
               item.record_index, item.status, _csv_safe(item.degraded)]
        for field in spec.fields:
            cell = fields.get(field.name) or {}
            row.append(_csv_safe(cell.get("value")))
            citations = cell.get("citations") or []
            # 页码对用户是 1 基的；库里是 0 基
            pages = sorted({c["page_idx"] + 1 for c in citations
                            if c.get("page_idx") is not None})
            row.append(",".join(str(p) for p in pages))
        writer.writerow(row)

    # BOM：Excel 认它才不会把 UTF-8 中文显示成乱码。
    # 这是简体中文环境下最常见的"导出打开是乱码"的原因
    payload = "﻿" + buffer.getvalue()
    filename = f"extraction-{run_id[:8]}.csv"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
