"""任务链路契约：上传 -> 稳定 URL -> service -> 回调/对账 -> 归档 -> 预览。

重点覆盖三件事关可靠性的行为：
- 传给 service 的 doc_id 是文件内容哈希（URL 变化不影响幂等与向量索引）
- 归档后 markdown 不再残留 base64，图片落成对象
- 回调丢失时对账能把结果补回来（service 只暂存 24h）
"""
import base64
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

import app.db as db
from app.archive import archive_task
from app.config import settings
from app.models import FileToken, Task, UsageRecord, utcnow
from app.reconcile import reconcile_once
from tests.conftest import SERVICE

PDF = b"%PDF-1.4 fake content for tests"
DOC_ID = hashlib.sha256(PDF).hexdigest()

LAYOUT = {"pdf_info": [{"page_idx": 0, "para_blocks": []}, {"page_idx": 1, "para_blocks": []}]}
RESULT = {
    "markdown": "# 标题\n\n![图](images/img_0.png)\n\n正文",
    "layout_json": LAYOUT,
    "images": [{"name": "img_0.png", "url": "data:image/png;base64,iVBORw0KGgo="}],
}


def _mock_service(status: str = "running", result: dict | None = None) -> dict:
    return {
        "submit": respx.post(f"{SERVICE}/v1/parse").mock(
            return_value=httpx.Response(202, json={"task_id": "s-1"})),
        "status": respx.get(f"{SERVICE}/v1/parse/s-1").mock(
            return_value=httpx.Response(200, json={"task_id": "s-1", "status": status,
                                                   "progress": 0.5, "error": None})),
        "result": respx.get(f"{SERVICE}/v1/parse/s-1/result").mock(
            return_value=httpx.Response(200, json=result or RESULT)),
    }


async def _upload(client, content: bytes = PDF, filename: str = "sample.pdf") -> dict:
    resp = await client.post("/api/tasks/upload",
                             files={"file": (filename, content, "application/pdf")})
    assert resp.status_code == 202, resp.text
    return resp.json()


@respx.mock
async def test_upload_passes_content_hash_as_doc_id(auth_client, session):
    """契约关键点：doc_id = 文件内容 sha256，file_url 是本层的稳定 URL（非预签名）。"""
    routes = _mock_service()
    task = await _upload(auth_client)

    assert task["status"] == "pending" and task["doc_id"] == DOC_ID
    body = json.loads(routes["submit"].calls.last.request.content)
    assert body["doc_id"] == DOC_ID, "必须传内容哈希，否则 service 侧向量索引永不命中"
    assert body["callback_url"].endswith("/internal/parse-callback")

    token = (await session.execute(
        select(FileToken).where(FileToken.task_id == task["id"])
    )).scalars().one()
    assert body["file_url"] == f"{settings.public_base_url}/files/{token.token}"
    assert "X-Amz-Signature" not in body["file_url"], "不得用预签名 URL（每次签名不同）"

    # service 就是靠这个 URL 下载原件的：必须免鉴权可取，且拿到的是原始字节
    got = await auth_client.get(f"/files/{token.token}", headers={"Authorization": ""})
    assert got.status_code == 200 and got.content == PDF


@respx.mock
async def test_callback_archives_and_rewrites_images(auth_client, session, app_state):
    routes = _mock_service()
    task = await _upload(auth_client)

    cb = await auth_client.post("/internal/parse-callback",
                                json={"task_id": "s-1", "status": "succeeded"},
                                headers={"Authorization": f"Bearer {settings.service_token}"})
    assert cb.status_code == 200 and cb.json()["archived"] == 1
    assert routes["result"].called

    detail = (await auth_client.get(f"/api/tasks/{task['id']}")).json()
    assert detail["status"] == "succeeded" and detail["page_count"] == 2

    result = (await auth_client.get(f"/api/tasks/{task['id']}/result")).json()
    assert "data:image/" not in result["markdown"], "归档后的 markdown 不得残留 base64"
    assert f"/api/tasks/{task['id']}/images/img_0.png" in result["markdown"]
    assert result["images"] == ["img_0.png"]

    img = await auth_client.get(f"/api/tasks/{task['id']}/images/img_0.png")
    assert img.status_code == 200 and img.content == base64.b64decode("iVBORw0KGgo=")

    # 版面 JSON 也归档了（前端定位 + 将来重建索引）
    layout = (await auth_client.get(f"/api/tasks/{task['id']}/layout")).json()
    assert len(layout["pdf_info"]) == 2

    # 计量：按页记账
    usage = (await session.execute(select(UsageRecord))).scalars().all()
    assert [(u.kind, u.pages) for u in usage] == [("parse", 2)]


@respx.mock
async def test_inline_base64_in_markdown_is_externalized(auth_client):
    """mineru 若把图片内联进 markdown，归档也必须把它外置成对象。"""
    inline = dict(RESULT, markdown="![x](data:image/png;base64,iVBORw0KGgo=)", images=[])
    _mock_service(result=inline)
    task = await _upload(auth_client)
    await auth_client.post("/internal/parse-callback",
                           json={"task_id": "s-1", "status": "succeeded"},
                           headers={"Authorization": f"Bearer {settings.service_token}"})

    result = (await auth_client.get(f"/api/tasks/{task['id']}/result")).json()
    assert "data:image/" not in result["markdown"]
    assert result["images"] == ["inline_0.png"]


@respx.mock
async def test_callback_handles_shared_service_task(auth_client, client, session, app_state):
    """两个用户传同一份文件 -> service 按 doc_id 去重返回同一个 task_id ->
    本层两行共享 service_task_id。回调必须把两行都归档（而不是撞上 "多行" 直接 500）。"""
    _mock_service()
    first = await _upload(auth_client)

    from tests.conftest import register
    other = await register(client, username="bob")
    bob = {"Authorization": f"Bearer {other['access_token']}"}
    second = await client.post("/api/tasks/upload", headers=bob,
                               files={"file": ("sample.pdf", PDF, "application/pdf")})
    assert second.status_code == 202 and second.json()["id"] != first["id"]

    cb = await client.post("/internal/parse-callback",
                           json={"task_id": "s-1", "status": "succeeded"},
                           headers={"Authorization": f"Bearer {settings.service_token}"})
    assert cb.status_code == 200 and cb.json()["archived"] == 2

    for task in (await session.execute(select(Task))).scalars().all():
        assert task.status == "succeeded", f"{task.id} 未归档"


@respx.mock
async def test_duplicate_upload_reuses_task(auth_client):
    routes = _mock_service()
    first = await _upload(auth_client)
    second = await _upload(auth_client)
    assert first["id"] == second["id"]
    assert routes["submit"].call_count == 1, "同一文件重复上传不得再打 service"


@respx.mock
async def test_file_endpoint_neutralizes_dangerous_mime(auth_client, session):
    """上传方能自选 content-type。原样 inline 回去 = 本站同源存储型 XSS（能偷 JWT）。"""
    _mock_service()
    resp = await auth_client.post(
        "/api/tasks/upload",
        files={"file": ("evil.html", b"<script>steal(localStorage)</script>", "text/html")})
    task_id = resp.json()["id"]

    token = (await session.execute(
        select(FileToken).where(FileToken.task_id == task_id)
    )).scalars().one()
    got = await auth_client.get(f"/files/{token.token}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("application/octet-stream"), "HTML 不得按原类型回"
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]


@respx.mock
async def test_pdf_stays_inline_previewable(auth_client, session):
    """白名单类型必须仍能 inline 预览，且不带 sandbox（sandbox 有弄坏浏览器内置
    PDF 阅读器的风险，而这些类型靠 Content-Type + nosniff 已经不能执行脚本）。"""
    _mock_service()
    task_id = (await _upload(auth_client))["id"]
    token = (await session.execute(
        select(FileToken).where(FileToken.task_id == task_id)
    )).scalars().one()

    got = await auth_client.get(f"/files/{token.token}")
    assert got.headers["content-type"].startswith("application/pdf")
    assert got.headers["content-disposition"].startswith("inline")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in got.headers


@respx.mock
async def test_web_and_external_planes_do_not_share_rows(auth_client, session, app_state):
    """同一份文档从 Web 传过、又用 key 提交过：必须是两行。

    混用会把 Web 那行的状态打回 pending -> 对账重新归档 -> 同一批页数被重复计费，
    还会覆写用户已归档的结果。
    """
    _mock_service(status="succeeded")
    web_task_id = (await _upload(auth_client))["id"]
    await archive_task(session, app_state.storage, app_state.service_client, web_task_id)

    key = (await auth_client.post("/api/keys", json={"name": "k"})).json()
    external = Task(user_id=(await session.get(Task, web_task_id)).user_id,
                    doc_id=DOC_ID, origin="external", filename="a.pdf", object_key="")
    session.add(external)
    await session.commit()          # 同 (user_id, doc_id) 不同 origin：唯一约束必须放行

    rows = (await session.execute(select(Task).where(Task.doc_id == DOC_ID))).scalars().all()
    assert {r.origin for r in rows} == {"web", "external"} and len(rows) == 2
    assert (await session.get(Task, web_task_id)).status == "succeeded", "Web 行不受影响"
    assert key["id"]


@respx.mock
async def test_resubmit_after_failure_is_not_instantly_expired(auth_client, session, app_state):
    """隔天重传失败的文档：对账不能因为旧的 created_at 立刻把它判成"结果已过期"。"""
    _mock_service()
    task_id = (await _upload(auth_client))["id"]
    task = await session.get(Task, task_id)
    task.status, task.error = "failed", "boom"
    task.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 3600)
    await session.commit()

    again = await auth_client.post("/api/tasks/upload",
                                   files={"file": ("sample.pdf", PDF, "application/pdf")})
    assert again.status_code == 202 and again.json()["id"] == task_id

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client)
    assert stats["expired"] == 0, "重传后不得立刻判死"
    await session.refresh(task)
    assert task.status != "failed"


@respx.mock
async def test_reconcile_recovers_lost_callback(auth_client, app_state):
    """回调丢了（backend 当时正在重启）也必须能补回来 —— 否则 24h 后结果永久消失。"""
    _mock_service(status="succeeded")
    task = await _upload(auth_client)

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client)
    assert stats["archived"] == 1

    detail = (await auth_client.get(f"/api/tasks/{task['id']}")).json()
    assert detail["status"] == "succeeded"


@respx.mock
async def test_archive_is_idempotent(auth_client, session, app_state):
    """回调与对账可能同时到达：第二次必须空转，不能重复计量。"""
    _mock_service(status="succeeded")
    task_id = (await _upload(auth_client))["id"]

    assert await archive_task(session, app_state.storage, app_state.service_client, task_id)
    assert not await archive_task(session, app_state.storage, app_state.service_client, task_id)

    usage = (await session.execute(select(UsageRecord))).scalars().all()
    assert len(usage) == 1, "重复归档不得重复记账"


@respx.mock
async def test_reconcile_expires_stale_task(auth_client, session, app_state):
    """超过 service 的 24h 暂存窗口 -> 落终态并提示重传（否则永远挂在 running）。"""
    _mock_service()
    task_id = (await _upload(auth_client))["id"]
    task = await session.get(Task, task_id)
    task.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 60)
    await session.commit()

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client)
    assert stats["expired"] == 1
    await session.refresh(task)
    assert task.status == "failed" and "expired" in task.error


@respx.mock
async def test_service_failure_marks_task_failed(auth_client):
    _mock_service()
    routes_status = respx.get(f"{SERVICE}/v1/parse/s-1").mock(
        return_value=httpx.Response(200, json={"task_id": "s-1", "status": "failed",
                                               "progress": 1.0, "error": "corrupt pdf"}))
    task = await _upload(auth_client)
    detail = (await auth_client.get(f"/api/tasks/{task['id']}")).json()
    assert routes_status.called
    assert detail["status"] == "failed" and detail["error"] == "corrupt pdf"

    result = await auth_client.get(f"/api/tasks/{task['id']}/result")
    assert result.status_code == 409 and result.json()["error"]["code"] == "task_failed"


@respx.mock
async def test_queue_full_surfaces_as_429(auth_client):
    respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(429, json={
        "error": {"message": "parse queue is full", "type": "rate_limit_error",
                  "code": "queue_full"}}))
    resp = await auth_client.post("/api/tasks/upload",
                                  files={"file": ("a.pdf", PDF, "application/pdf")})
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "queue_full"


async def test_file_token_must_be_valid(client, auth_client, session):
    assert (await client.get("/files/nope")).status_code == 404


@respx.mock
async def test_task_isolation_between_users(auth_client, client, session):
    _mock_service()
    task_id = (await _upload(auth_client))["id"]

    from tests.conftest import register
    other = await register(client, username="bob")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.get(f"/api/tasks/{task_id}", headers=headers)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "task_not_found"


@respx.mock
async def test_delete_task_keeps_usage_records(auth_client, session, app_state):
    _mock_service(status="succeeded")
    task_id = (await _upload(auth_client))["id"]
    await archive_task(session, app_state.storage, app_state.service_client, task_id)

    assert (await auth_client.delete(f"/api/tasks/{task_id}")).status_code == 204
    assert (await auth_client.get(f"/api/tasks/{task_id}")).status_code == 404
    usage = (await session.execute(select(UsageRecord))).scalars().all()
    assert len(usage) == 1 and usage[0].task_id is None, "账单不能因删任务而消失"
