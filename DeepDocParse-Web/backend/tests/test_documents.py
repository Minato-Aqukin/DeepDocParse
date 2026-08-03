"""文档链路契约：上传 -> 稳定 URL -> service -> 回调/对账 -> 归档 -> 索引 -> 预览。

重点覆盖事关可靠性与安全的行为：
- 传给 service 的 doc_id 是文件内容哈希（URL 变化不影响幂等与向量索引）
- 归档后 markdown 不再残留 base64，图片落成对象
- 回调丢失时对账能把结果补回来（service 只暂存 24h）
- /files/{token} 不能把用户自选的 MIME 原样 inline 回去（同源 XSS）
- Web 平面与外部 API 平面的行不共用
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
from app.archive import archive_job
from app.config import settings
from app.models import Chunk, Document, FileToken, ParseJob, UsageRecord, utcnow
from app.reconcile import reconcile_once
from tests.conftest import EMBEDDINGS, SERVICE, register

PDF = b"%PDF-1.4 fake content for tests"
DOC_ID = hashlib.sha256(PDF).hexdigest()

LAYOUT = {
    "pdf_info": [
        {"page_idx": 0, "page_size": [612, 792], "para_blocks": [
            {"bbox": [72, 72, 540, 100], "lines": [{"spans": [{"content": "第一页正文"}]}]}]},
        {"page_idx": 1, "page_size": [612, 792], "para_blocks": [
            {"bbox": [72, 120, 540, 300], "lines": [{"spans": [{"content": "第二页的表格数据"}]}]}]},
    ]
}
RESULT = {
    "markdown": "# 标题\n\n![图](images/img_0.png)\n\n正文",
    "layout_json": LAYOUT,
    "images": [{"name": "img_0.png", "url": "data:image/png;base64,iVBORw0KGgo="}],
}


def _mock_service(status: str = "running", result: dict | None = None,
                  embed: httpx.Response | None = None) -> dict:
    routes = {
        "submit": respx.post(f"{SERVICE}/v1/parse").mock(
            return_value=httpx.Response(202, json={"task_id": "s-1"})),
        "status": respx.get(f"{SERVICE}/v1/parse/s-1").mock(
            return_value=httpx.Response(200, json={"task_id": "s-1", "status": status,
                                                   "progress": 0.5, "error": None})),
        "result": respx.get(f"{SERVICE}/v1/parse/s-1/result").mock(
            return_value=httpx.Response(200, json=result or RESULT)),
    }
    routes["embed"] = respx.post(EMBEDDINGS).mock(
        return_value=embed) if embed is not None else respx.post(EMBEDDINGS).mock(
        side_effect=_embed_response)
    return routes


def _fake_vector(text: str) -> list[float]:
    """字符袋向量：共享字越多相似度越高，完全无关的文本相似度趋近 0。

    刻意不用"常量向量 + 一个变化位"那种假向量——那样任意两段文本都高度相似，
    相似度下限（qa_min_similarity）就永远触发不了，`no_hits` 这条路也就测不到。
    """
    vector = [0.0] * settings.embedding_dim
    for char in text:
        vector[ord(char) % settings.embedding_dim] += 1.0
    return vector


def _embed_response(request: httpx.Request) -> httpx.Response:
    """按请求条数返回同样多的向量，维度与 settings 一致。"""
    inputs = json.loads(request.content)["input"]
    if isinstance(inputs, str):
        inputs = [inputs]
    return httpx.Response(200, json={"data": [
        {"index": i, "embedding": _fake_vector(t)} for i, t in enumerate(inputs)]})


async def _upload(client, content: bytes = PDF, filename: str = "sample.pdf",
                  mime: str = "application/pdf") -> dict:
    resp = await client.post("/api/documents", files={"file": (filename, content, mime)})
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _callback(client, status: str = "succeeded", task_id: str = "s-1"):
    return await client.post("/internal/parse-callback",
                             json={"task_id": task_id, "status": status},
                             headers={"Authorization": f"Bearer {settings.service_token}"})


@respx.mock
async def test_upload_passes_content_hash_as_doc_id(auth_client, session):
    """契约关键点：doc_id = 文件内容 sha256，file_url 是本层的稳定 URL（非预签名）。"""
    routes = _mock_service()
    document = await _upload(auth_client)

    assert document["status"] == "pending" and document["doc_id"] == DOC_ID
    body = json.loads(routes["submit"].calls.last.request.content)
    assert body["doc_id"] == DOC_ID, "必须传内容哈希，否则 service 侧向量索引永不命中"
    assert body["callback_url"].endswith("/internal/parse-callback")

    token = (await session.execute(
        select(FileToken).where(FileToken.document_id == document["id"])
    )).scalars().one()
    assert body["file_url"] == f"{settings.public_base_url}/files/{token.token}"
    assert "X-Amz-Signature" not in body["file_url"], "不得用预签名 URL（每次签名不同）"

    # service 就是靠这个 URL 下载原件的：必须免鉴权可取，且拿到的是原始字节
    got = await auth_client.get(f"/files/{token.token}", headers={"Authorization": ""})
    assert got.status_code == 200 and got.content == PDF


@respx.mock
async def test_list_filters_before_paginating(auth_client, session):
    """回归：status 过滤必须在 SQL 里做，不能先分页再用 Python 丢行。

    旧实现下 `?status=succeeded&limit=1` 只看第一行，恰好是 pending 就返回空，
    而后面明明还有成功的文档 —— 分页语义是坏的。
    """
    _mock_service()
    for i in range(3):
        await _upload(auth_client, content=b"%PDF-1.4 doc " + str(i).encode(),
                      filename=f"doc{i}.pdf")

    # 只把最早那份（列表里排最后）置成 succeeded
    jobs = (await session.execute(select(ParseJob).order_by(ParseJob.created_at))).scalars().all()
    jobs[0].status = "succeeded"
    await session.commit()

    listed = (await auth_client.get("/api/documents", params={"status": "succeeded"})).json()
    assert len(listed) == 1, f"过滤应命中唯一一条 succeeded，实际 {[d['status'] for d in listed]}"

    # 关键点：limit 小于"需要跳过的 pending 数"时仍须返回它
    paged = (await auth_client.get(
        "/api/documents", params={"status": "succeeded", "limit": 1})).json()
    assert len(paged) == 1, "先分页后过滤会在这里返回空"
    assert paged[0]["id"] == listed[0]["id"]

    pending = (await auth_client.get("/api/documents", params={"status": "pending"})).json()
    assert len(pending) == 2 and all(d["status"] == "pending" for d in pending)


@respx.mock
async def test_list_does_not_duplicate_documents_with_tied_job_timestamps(auth_client, session):
    """回归：把 job join 进来时不能让一个文档变成两行。

    按 `GROUP BY document_id HAVING max(created_at)` 再 join 回去的写法，
    在两条 job 的 created_at 撞上（同一微秒）时会 join 出两行，列表页出现重复文档。
    """
    _mock_service()
    document = await _upload(auth_client)
    await auth_client.post(f"/api/documents/{document['id']}/reparse",
                           json={"engine": "mineru", "options": {"backend": "vlm"}})

    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document["id"]))).scalars().all()
    assert len(jobs) == 2
    tied = utcnow()
    for job in jobs:                       # 制造完全相同的 created_at
        job.created_at = tied
    await session.commit()

    listed = (await auth_client.get("/api/documents")).json()
    assert [d["id"] for d in listed] == [document["id"]], f"文档被 join 成了多行：{listed}"


@respx.mock
async def test_list_documents_does_not_scale_queries_with_rows(auth_client):
    """回归：列表页曾对每个文档单独查一次 job（一页 200 个文档 = 200+ 次往返）。

    断"查询次数不随行数增长"，而不是断一个具体数字 —— 后者会因为无关重构而脆断。
    """
    _mock_service()
    counts: list[int] = []

    from sqlalchemy import event

    import app.db as db

    for rows in (1, 6):
        while len((await auth_client.get("/api/documents")).json()) < rows:
            n = len((await auth_client.get("/api/documents")).json())
            await _upload(auth_client, content=b"%PDF-1.4 n" + str(n).encode(),
                          filename=f"n{n}.pdf")

        seen = 0

        def _count(*_args, **_kwargs):
            nonlocal seen
            seen += 1

        engine = db.get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            assert len((await auth_client.get("/api/documents")).json()) == rows
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        counts.append(seen)

    assert counts[0] == counts[1], \
        f"查询次数随行数增长（1 行 {counts[0]} 次 / 6 行 {counts[1]} 次）—— N+1 回来了"


@respx.mock
async def test_upload_rejects_oversized_file(auth_client, monkeypatch):
    """上传必须有字节上限。

    上传体要整个进内存（算 sha256 当 doc_id，再原样 put 进 MinIO），没有上限时
    任意登录用户传个大文件就能把进程打爆。超限要 413，且不得建出任何 Document。
    """
    monkeypatch.setattr(settings, "max_upload_bytes", 4096)
    monkeypatch.setattr(settings, "upload_chunk_bytes", 512)
    submit = respx.post(f"{SERVICE}/v1/parse")

    resp = await auth_client.post(
        "/api/documents", files={"file": ("big.pdf", b"x" * 8192, "application/pdf")})
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"] == {
        "message": "file exceeds the 4096 bytes upload limit",
        "type": "invalid_request_error", "code": "file_too_large",
    }, "错误体要说清上限，别给用户一个 '0 MiB'"
    assert not submit.called, "超限的上传不得转发给 service"
    assert (await auth_client.get("/api/documents")).json() == []

    # 边界：正好卡在上限内的必须放行
    _mock_service()
    ok = await auth_client.post(
        "/api/documents", files={"file": ("ok.pdf", b"y" * 4096, "application/pdf")})
    assert ok.status_code == 202, ok.text


@respx.mock
async def test_upload_cap_holds_on_a_hand_rolled_multipart_body(auth_client, monkeypatch):
    """上限不依赖客户端声明的长度。

    这里手工拼 multipart 体、不给出可信的长度声明，超限仍须 413 —— 判断依据是
    解析器累计的实际字节，不是请求头。
    """
    monkeypatch.setattr(settings, "max_upload_bytes", 4096)
    monkeypatch.setattr(settings, "upload_chunk_bytes", 512)

    payload = b"z" * 20000
    boundary = "----ddptest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="lie.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()

    resp = await auth_client.post(
        "/api/documents", content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "file_too_large"


@respx.mock
async def test_callback_archives_indexes_and_rewrites_images(auth_client, session, app_state):
    routes = _mock_service()
    document = await _upload(auth_client)

    cb = await _callback(auth_client)
    assert cb.status_code == 200 and cb.json()["archived"] == 1
    assert routes["result"].called

    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["status"] == "succeeded" and detail["page_count"] == 2

    result = (await auth_client.get(f"/api/documents/{document['id']}/result")).json()
    assert "data:image/" not in result["markdown"], "归档后的 markdown 不得残留 base64"
    assert f"/api/documents/{document['id']}/jobs/{result['job_id']}/images/img_0.png" \
        in result["markdown"]
    assert result["images"] == ["img_0.png"]

    img = await auth_client.get(
        f"/api/documents/{document['id']}/jobs/{result['job_id']}/images/img_0.png")
    assert img.status_code == 200 and img.content == base64.b64decode("iVBORw0KGgo=")

    layout = (await auth_client.get(f"/api/documents/{document['id']}/layout")).json()
    assert len(layout["pdf_info"]) == 2

    usage = (await session.execute(
        select(UsageRecord).where(UsageRecord.kind == "parse"))).scalars().all()
    assert [(u.kind, u.pages) for u in usage] == [("parse", 2)]

    # 后台索引任务（BackgroundTasks 在 ASGI 传输里会同步跑完）
    chunks = (await session.execute(select(Chunk))).scalars().all()
    assert chunks and all(c.embedding for c in chunks), "归档后必须建好向量索引"
    doc_row = await session.get(Document, document["id"])
    await session.refresh(doc_row)
    assert doc_row.index_status == "ready"


@respx.mock
async def test_pages_endpoint_groups_blocks_by_page(auth_client):
    """前端左右栏对齐的数据源：块必须带页码与 bbox。"""
    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    pages = (await auth_client.get(f"/api/documents/{document['id']}/pages")).json()
    assert [p["page_idx"] for p in pages["pages"]] == [0, 1]
    block = pages["pages"][1]["blocks"][0]
    assert block["bbox"] == [72, 120, 540, 300] and block["page_size"] == [612, 792]
    assert "表格" in block["text"]


@respx.mock
async def test_inline_base64_in_markdown_is_externalized(auth_client):
    """mineru 若把图片内联进 markdown，归档也必须把它外置成对象。"""
    inline = dict(RESULT, markdown="![x](data:image/png;base64,iVBORw0KGgo=)", images=[])
    _mock_service(result=inline)
    document = await _upload(auth_client)
    await _callback(auth_client)

    result = (await auth_client.get(f"/api/documents/{document['id']}/result")).json()
    assert "data:image/" not in result["markdown"]
    assert result["images"] == ["inline_0.png"]


@respx.mock
async def test_duplicate_upload_reuses_document(auth_client):
    routes = _mock_service()
    first = await _upload(auth_client)
    second = await _upload(auth_client)
    assert first["id"] == second["id"]
    assert routes["submit"].call_count == 1, "同一文件重复上传不得再打 service"


@respx.mock
async def test_reparse_creates_new_job_and_keeps_old(auth_client, session):
    """换参数重解析：新版本与旧版本并存，切换 current_job 才影响预览与索引。"""
    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)
    first_job = (await auth_client.get(f"/api/documents/{document['id']}/result")).json()["job_id"]

    respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(202, json={"task_id": "s-2"}))
    respx.get(f"{SERVICE}/v1/parse/s-2").mock(
        return_value=httpx.Response(200, json={"task_id": "s-2", "status": "running"}))
    respx.get(f"{SERVICE}/v1/parse/s-2/result").mock(return_value=httpx.Response(200, json=RESULT))

    again = await auth_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"engine": "mineru", "options": {"backend": "vlm"}})
    assert again.status_code == 202
    second_job = again.json()["id"]
    assert second_job != first_job

    # 同参数再来一次 -> 幂等命中同一个 job
    dup = await auth_client.post(f"/api/documents/{document['id']}/reparse",
                                 json={"engine": "mineru", "options": {"backend": "vlm"}})
    assert dup.json()["id"] == second_job

    jobs = (await auth_client.get(f"/api/documents/{document['id']}/jobs")).json()
    assert {j["id"] for j in jobs} == {first_job, second_job}
    assert [j["is_current"] for j in jobs if j["id"] == first_job] == [True]

    await _callback(auth_client, task_id="s-2")
    switched = await auth_client.put(f"/api/documents/{document['id']}/current-job",
                                     json={"job_id": second_job})
    assert switched.status_code == 200 and switched.json()["current_job_id"] == second_job
    # 旧版本仍然可读
    old = await auth_client.get(f"/api/documents/{document['id']}/result?job={first_job}")
    assert old.status_code == 200


@respx.mock
async def test_file_endpoint_neutralizes_dangerous_mime(auth_client, session):
    """上传方能自选 content-type。原样 inline 回去 = 本站同源存储型 XSS（能偷 JWT）。"""
    _mock_service()
    document = await _upload(auth_client, b"<script>steal(localStorage)</script>",
                             "evil.html", "text/html")
    token = (await session.execute(
        select(FileToken).where(FileToken.document_id == document["id"])
    )).scalars().one()

    got = await auth_client.get(f"/files/{token.token}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("application/octet-stream"), "HTML 不得按原类型回"
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]


@respx.mock
async def test_pdf_stays_inline_previewable(auth_client, session):
    """白名单类型必须仍能 inline 预览，且不带 sandbox（sandbox 会弄坏 PDF 预览）。"""
    _mock_service()
    document = await _upload(auth_client)
    token = (await session.execute(
        select(FileToken).where(FileToken.document_id == document["id"])
    )).scalars().one()

    got = await auth_client.get(f"/files/{token.token}")
    assert got.headers["content-type"].startswith("application/pdf")
    assert got.headers["content-disposition"].startswith("inline")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in got.headers


@respx.mock
async def test_web_and_external_planes_do_not_share_rows(auth_client, session, app_state):
    """同一份文档从 Web 传过、又用 key 提交过：必须是两个 Document。

    混用会把 Web 那行的状态打回 pending -> 对账重新归档 -> 同一批页数被重复计费，
    还会覆写用户已归档的结果。
    """
    _mock_service(status="succeeded")
    document = await _upload(auth_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    await archive_job(session, app_state.storage, app_state.service_client, job.id)

    user_id = (await session.get(Document, document["id"])).user_id
    session.add(Document(user_id=user_id, doc_id=DOC_ID, origin="external",
                         filename="a.pdf", object_key=""))
    await session.commit()          # 同 (user_id, doc_id) 不同 origin：唯一约束必须放行

    rows = (await session.execute(
        select(Document).where(Document.doc_id == DOC_ID))).scalars().all()
    assert {r.origin for r in rows} == {"web", "external"} and len(rows) == 2
    await session.refresh(job)
    assert job.status == "succeeded", "Web 那条不受影响"


@respx.mock
async def test_resubmit_after_failure_is_not_instantly_expired(auth_client, session, app_state):
    """隔天重传失败的文档：对账不能因为旧的 created_at 立刻把它判成"结果已过期"。"""
    _mock_service()
    document = await _upload(auth_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    job.status, job.error = "failed", "boom"
    job.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 3600)
    await session.commit()

    again = await auth_client.post("/api/documents",
                                   files={"file": ("sample.pdf", PDF, "application/pdf")})
    assert again.status_code == 202 and again.json()["id"] == document["id"]

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["expired"] == 0, "重传后不得立刻判死"
    await session.refresh(job)
    assert job.status != "failed"


@respx.mock
async def test_reconcile_recovers_lost_callback(auth_client, app_state, session):
    """回调丢了（backend 当时正在重启）也必须能补回来 —— 否则 24h 后结果永久消失。"""
    _mock_service(status="succeeded")
    document = await _upload(auth_client)

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["archived"] == 1 and stats["indexed"] == 1, "对账要同时补齐归档与索引"

    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["status"] == "succeeded" and detail["index_status"] == "ready"


@respx.mock
async def test_archive_is_idempotent(auth_client, session, app_state):
    """回调与对账可能同时到达：第二次必须空转，不能重复计量。"""
    _mock_service(status="succeeded")
    await _upload(auth_client)
    job = (await session.execute(select(ParseJob))).scalars().one()

    assert await archive_job(session, app_state.storage, app_state.service_client, job.id)
    assert not await archive_job(session, app_state.storage, app_state.service_client, job.id)

    usage = (await session.execute(
        select(UsageRecord).where(UsageRecord.kind == "parse"))).scalars().all()
    assert len(usage) == 1, "重复归档不得重复记账"


@respx.mock
async def test_reconcile_expires_stale_job(auth_client, session, app_state):
    """超过 service 的 24h 暂存窗口 -> 落终态并提示重传（否则永远挂在 running）。"""
    _mock_service()
    await _upload(auth_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    job.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 60)
    await session.commit()

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["expired"] == 1
    await session.refresh(job)
    assert job.status == "failed" and "expired" in job.error


@respx.mock
async def test_service_failure_marks_job_failed(auth_client):
    _mock_service()
    routes_status = respx.get(f"{SERVICE}/v1/parse/s-1").mock(
        return_value=httpx.Response(200, json={"task_id": "s-1", "status": "failed",
                                               "progress": 1.0, "error": "corrupt pdf"}))
    document = await _upload(auth_client)
    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert routes_status.called
    assert detail["status"] == "failed" and detail["error"] == "corrupt pdf"

    result = await auth_client.get(f"/api/documents/{document['id']}/result")
    assert result.status_code == 409 and result.json()["error"]["code"] == "job_failed"


@respx.mock
async def test_queue_full_surfaces_as_429(auth_client):
    respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(429, json={
        "error": {"message": "parse queue is full", "type": "rate_limit_error",
                  "code": "queue_full"}}))
    resp = await auth_client.post("/api/documents",
                                  files={"file": ("a.pdf", PDF, "application/pdf")})
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "queue_full"


async def test_file_token_must_be_valid(client):
    assert (await client.get("/files/nope")).status_code == 404


@respx.mock
async def test_document_isolation_between_users(auth_client, client):
    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    other = await register(client, username="bob")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.get(f"/api/documents/{document_id}", headers=headers)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "document_not_found"


@respx.mock
async def test_soft_delete_keeps_usage_and_drops_chunks(auth_client, session, app_state):
    _mock_service(status="succeeded")
    document = await _upload(auth_client)
    await _callback(auth_client)

    assert (await auth_client.delete(f"/api/documents/{document['id']}")).status_code == 204
    assert (await auth_client.get(f"/api/documents/{document['id']}")).status_code == 404

    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.deleted_at is not None, "软删除：对象由 GC 回收，记录留痕"
    assert (await session.execute(select(Chunk))).scalars().all() == []
    usage = (await session.execute(
        select(UsageRecord).where(UsageRecord.kind == "parse"))).scalars().all()
    assert len(usage) == 1, "账单不能因删文档而消失"


@respx.mock
async def test_export_zip_contains_markdown_and_images(auth_client):
    import io
    import zipfile

    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    resp = await auth_client.get(f"/api/documents/{document['id']}/download?format=zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert {"document.md", "layout.json", "images/img_0.png"} <= names
        # 打包里的引用改成相对路径，解压出来直接能看图
        assert "images/img_0.png" in zf.read("document.md").decode()


@pytest.mark.parametrize("fmt,expected", [("md", 200), ("json", 200), ("source", 200),
                                          ("bogus", 400)])
@respx.mock
async def test_download_formats(auth_client, fmt, expected):
    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)
    resp = await auth_client.get(f"/api/documents/{document['id']}/download?format={fmt}")
    assert resp.status_code == expected
