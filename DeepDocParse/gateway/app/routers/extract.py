"""结构化抽取平面 /v1/extract —— 契约见 ../../openapi.yaml 与 docs/extract-format.md。

职责边界与解析平面一致：路由只做**受理 + 校验 + 状态透传**，
真正的编排（检索 -> 抽值 -> 裁剪 -> 核对）在 worker/tasks.py 的 ARQ 链里。

为什么是异步任务而不是同步返回：一个 30 字段的 schema 就是 30 次检索 + 30 次模型调用。
同步接口在这种量级上必然超时，而超时的表现是"什么都没有"——比慢得多更糟。
"""
import hashlib
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth import require_service_token
from app.config import settings
from app.errors import APIError
from app.services import extract_format as fmt

router = APIRouter(tags=["extract"], dependencies=[Depends(require_service_token)])

_TERMINAL = {"succeeded", "failed"}


class ExtractRequest(BaseModel):
    # doc_hash 与 file_url 二选一。doc_hash 优先：它直接命中已有的解析缓存与向量索引，
    # 批量抽取（同一批文档跑多个 schema）必须走这条，否则每次都重新解析
    doc_hash: str | None = None
    file_url: str | None = None
    doc_id: str | None = None
    schema_: dict = {}
    engine: str = ""
    options: dict = {}
    callback_url: str | None = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data):
        # 契约里的字段名是 schema，而 `schema` 在 pydantic v1 时代是 BaseModel 的方法名。
        # v2 已经不冲突，但显式改名仍更稳妥 —— 契约名不能变（已冻结）
        if "schema" in data:
            data.setdefault("schema_", data.pop("schema"))
        super().__init__(**data)


def _doc_hash(file_url: str, doc_id: str | None = None) -> str:
    """与解析平面同一套身份算法（parse.py::_doc_hash）。

    两处必须一致：抽取要靠 doc_hash 找到解析平面建好的分块索引，
    算法一旦漂移就会永久零命中 —— 而零命中表现为"所有字段都抽不到"，
    看起来像模型不行，是最难排查的一类故障。
    """
    return hashlib.sha256((doc_id or file_url).encode()).hexdigest()


@router.post("/extract", status_code=202)
async def submit_extract(req: ExtractRequest, request: Request):
    state = request.app.state

    # schema 校验在请求路径上强制。坏 schema 当场 400 —— 跑完一轮抽取
    # （N 次检索 + N 次模型调用）再说不合规，是在烧别人的钱
    problems = fmt.validate_schema(req.schema_)
    if problems:
        raise APIError(400, "schema 不合法：" + "；".join(problems),
                       "invalid_request_error", "invalid_schema")
    spec = fmt.parse_schema(req.schema_)
    if len(spec.fields) > settings.extract_max_fields:
        raise APIError(400,
                       f"字段数 {len(spec.fields)} 超过上限 {settings.extract_max_fields}",
                       "invalid_request_error", "too_many_fields")

    if not req.doc_hash and not req.file_url:
        raise APIError(400, "必须提供 doc_hash 或 file_url 之一",
                       "invalid_request_error", "missing_document")

    doc_hash = req.doc_hash or _doc_hash(req.file_url, req.doc_id)

    if await state.task_store.queue_depth() >= settings.parse_queue_max:
        raise APIError(429, "queue is full, retry later", "rate_limit_error", "queue_full")

    task_id = uuid.uuid4().hex
    await state.task_store.create_extract(task_id, doc_hash=doc_hash, callback_url=req.callback_url,
                                          payload={
                                              "schema": req.schema_,
                                              "file_url": req.file_url or "",
                                              "engine": req.engine,
                                              "options": req.options,
                                          })
    await state.arq.enqueue_job("run_extraction", task_id)
    return {"task_id": task_id, "status": "pending"}


@router.get("/extract/{task_id}")
async def get_extract_status(task_id: str, request: Request):
    task = await request.app.state.task_store.get_extract(task_id)
    if task is None:
        raise APIError(404, f"extract task not found: {task_id}",
                       "invalid_request_error", "task_not_found")
    status = task.get("status", "pending")
    try:
        progress = float(task.get("progress") or 0.0)
    except ValueError:
        progress = 0.0
    return {
        "task_id": task_id,
        "status": status,
        "progress": 1.0 if status in _TERMINAL else progress,
        "error": task.get("error") or None,
        "doc_hash": task.get("doc_hash") or None,
    }


@router.get("/extract/{task_id}/result")
async def get_extract_result(task_id: str, request: Request):
    store = request.app.state.task_store
    task = await store.get_extract(task_id)
    if task is None:
        raise APIError(404, f"extract task not found: {task_id}",
                       "invalid_request_error", "task_not_found")
    if task.get("status") == "failed":
        raise APIError(409, task.get("error") or "extraction failed",
                       "upstream_error", "task_failed")
    result = await store.load_extract_result(task_id)
    if result is None:
        raise APIError(409, "result not ready yet, poll status first",
                       "invalid_request_error", "result_not_ready")
    return result
