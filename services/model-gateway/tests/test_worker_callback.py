"""worker 回调与抓取错误映射。

覆盖四件事：
1. 回调重验：落库时允许、POST 前已不再允许的 callback_url 不得发出
   （一字不漏地保住 SERVICE_TOKEN），任务本身照常成功；
2. 查询串原样：POST 打到完整存储 URL（含 ?token= 这类验签串），不归一化不剥参；
3. FetchNotAllowedError（ValueError 子类，旧 except 接不住）-> failed，
   前缀 fetch_not_allowed，且错误里不带 file_url/token；
4. FileTooLargeError -> failed，前缀 file_too_large，与 SSRF 拒绝可区分。

直接调 poll_and_archive（见 test_layout 的 borndigital 用例），不走 HTTP 受理层。
"""
import json

import respx
from httpx import Response

from ddp_core.fetch_policy import FetchNotAllowedError, FileTooLargeError
from ddp_gateway.config import settings
from ddp_gateway.services.mineru_client import MineruClient
from ddp_gateway.worker.tasks import poll_and_archive
MINERU = "http://mineru:8000"

_RESULT = {
    "task_id": "m-1",
    "status": "completed",
    "results": {
        "sample.pdf": {
            "md_content": "# 标题\n\n正文。",
            "middle_json": "{}",
            "images": {},
        }
    },
}


def _mock_mineru_done():
    """mineru 侧已完成且结果可取：worker 一次轮询即进归档。"""
    respx.get(f"{MINERU}/tasks/m-1").mock(
        return_value=Response(200, json={"task_id": "m-1", "status": "completed"}))
    respx.get(f"{MINERU}/tasks/m-1/result").mock(
        return_value=Response(200, json=_RESULT))


async def _run_parse(worker_ctx, task_id, callback_url):
    """落库一个 mineru 引擎任务并跑完 worker 归档链，返回终态 task。"""
    store = worker_ctx["task_store"]
    await store.create(task_id, "m-1", "mineru", callback_url, "d" * 64)
    await poll_and_archive(worker_ctx, task_id)
    return await store.get(task_id)


@respx.mock
async def test_callback_rechecked_before_post(worker_ctx, monkeypatch):
    """基座收紧后：已落库的旧 callback_url 不再允许 -> 不 POST，任务照常成功。"""
    monkeypatch.setattr(settings, "callback_allowed_base", "http://corpus-api:8081/internal/")
    _mock_mineru_done()
    route = respx.post("http://backend/callback").mock(return_value=Response(200))

    task = await _run_parse(worker_ctx, "cb-skip-1", "http://backend/callback")

    assert task["status"] == "succeeded", task
    assert not route.called, "基座外的回调必须一字不发（含 SERVICE_TOKEN）"
    assert await worker_ctx["task_store"].queue_depth() == 0


@respx.mock
async def test_callback_posts_full_url_with_query_verbatim(worker_ctx, monkeypatch):
    """允许的回调：POST 打到完整原文 URL，?token= 查询串一字不少。"""
    monkeypatch.setattr(settings, "callback_allowed_base", "http://backend/callback")
    _mock_mineru_done()
    url = "http://backend/callback?token=abc123&sig=x"
    route = respx.post(url).mock(return_value=Response(200))

    task = await _run_parse(worker_ctx, "cb-query-1", url)

    assert task["status"] == "succeeded", task
    assert route.called, "允许基座下的回调必须发出"
    req = route.calls.last.request
    assert str(req.url) == url, f"查询串必须原样保留：{req.url}"
    assert req.headers["Authorization"] == f"Bearer {settings.service_token}"
    assert json.loads(req.content) == {"task_id": "cb-query-1", "status": "succeeded"}


@respx.mock
async def test_fetch_not_allowed_fails_task_without_url(worker_ctx, monkeypatch):
    """SSRF 拒绝（ValueError 子类）：failed + fetch_not_allowed 前缀，不带 URL/token。"""
    respx.get(f"{MINERU}/tasks/m-1").mock(
        return_value=Response(200, json={"task_id": "m-1", "status": "completed"}))

    async def _denied(self, endpoint, native_id):
        raise FetchNotAllowedError(
            "不允许抓取这个地址（目标不是公网地址）：http://169.254.169.254/x?token=secret-token")

    monkeypatch.setattr(MineruClient, "fetch_result", _denied)

    task = await _run_parse(worker_ctx, "fetch-deny-1", None)

    assert task["status"] == "failed", task
    assert task["error"].startswith("fetch_not_allowed:"), task["error"]
    assert "secret-token" not in task["error"] and "169.254" not in task["error"], task["error"]
    assert await worker_ctx["task_store"].queue_depth() == 0


@respx.mock
async def test_file_too_large_fails_task_with_distinct_prefix(worker_ctx, monkeypatch):
    """超上限：failed + file_too_large 前缀，与 SSRF 拒绝可区分。"""
    respx.get(f"{MINERU}/tasks/m-1").mock(
        return_value=Response(200, json={"task_id": "m-1", "status": "completed"}))

    async def _huge(self, endpoint, native_id):
        raise FileTooLargeError("文件超过处理上限（200MB）。它要完整进内存，请换更大的上限或改用 mineru 引擎")

    monkeypatch.setattr(MineruClient, "fetch_result", _huge)

    task = await _run_parse(worker_ctx, "too-large-1", None)

    assert task["status"] == "failed", task
    assert task["error"].startswith("file_too_large:"), task["error"]
    assert await worker_ctx["task_store"].queue_depth() == 0
