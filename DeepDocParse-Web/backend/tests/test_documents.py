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
    return httpx.Response(200, json={"model": settings.embedding_model, "data": [
        {"index": i, "embedding": _fake_vector(t)} for i, t in enumerate(inputs)]})


async def _upload(client, content: bytes = PDF, filename: str = "sample.pdf",
                  mime: str = "application/pdf", headers: dict | None = None) -> dict:
    resp = await client.post("/api/documents", files={"file": (filename, content, mime)},
                             headers=headers or {})
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

    uploader = (await session.get(Document, document["id"])).uploaded_by
    session.add(Document(uploaded_by=uploader, doc_id=DOC_ID, origin="external",
                         filename="a.pdf", object_key=""))
    await session.commit()          # 同 doc_id 不同 origin：唯一约束必须放行

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
async def test_late_archive_cannot_revive_deleted_document(
        auth_client, session, app_state):
    """解析中删除后，迟到 callback 只归档/计解析费，绝不能复活索引或再产生推理费。"""
    _mock_service(status="succeeded")
    document = await _upload(auth_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    assert (await auth_client.delete(f"/api/documents/{document['id']}")).status_code == 204

    assert await archive_job(session, app_state.storage, app_state.service_client, job.id)
    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.deleted_at is not None
    assert row.index_status == "none"

    from app.indexing import index_document
    assert await index_document(
        session, app_state.storage, app_state.http, document["id"]
    ) == 0
    assert (await session.scalar(select(Chunk.id).where(
        Chunk.document_id == document["id"]))) is None
    expensive = (await session.execute(select(UsageRecord).where(
        UsageRecord.kind.in_(("compile_vision", "embed"))))).scalars().all()
    assert expensive == []


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
async def test_corpus_is_shared_between_users(auth_client, client):
    """**语料是整个部署共享的** —— 这条用例在 1b 里被整个反转过来。

    改之前它断言的是"别人的文档看不见（404）"。plan.md §2 已定 2 之后，
    一次部署 = 一份语料 = 一个知识库：账号层只管认证 / 计量 / 限速，**不管授权**。
    所以另一个账号必须能看见、能读、能问。

    留着这条反转记录是有意的：谁哪天把可见性过滤加回去，这里立刻会红，
    而红的时候能从这段说明看到"这不是 bug，是产品决定"。
    """
    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    other = await register(client, username="bob")
    headers = {"Authorization": f"Bearer {other['access_token']}"}

    resp = await client.get(f"/api/documents/{document_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == document_id

    # 列表里也要有 —— 不是"知道 id 就能访问"，是真的在他的文档库里
    listed = await client.get("/api/documents", headers=headers)
    assert listed.status_code == 200
    assert document_id in [d["id"] for d in listed.json()]


@respx.mock
async def test_second_uploader_reuses_the_parse_and_is_recorded(auth_client, client, session):
    """同一份文件第二个人再传：**命中已有解析，不产生第二个 parse_job**；
    但"他也传过"要记下来。

    这是全局去重的核心收益 —— 以前两个人传同一份手册 = 两次解析 + 两次索引 +
    两套 embedding，而 GPU 是按小时租的。
    """
    from sqlalchemy import func, select as sa_select

    from app.models import Document, DocumentUpload, ParseJob

    _mock_service()
    document_id = (await _upload(auth_client))["id"]
    jobs_before = await session.scalar(
        sa_select(func.count(ParseJob.id)).where(ParseJob.document_id == document_id))
    docs_before = await session.scalar(sa_select(func.count(Document.id)))

    other = await register(client, username="carol")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    again = await _upload(client, headers=headers)

    assert again["id"] == document_id, "同一份文件应当复用同一个 Document，而不是新建"
    assert await session.scalar(
        sa_select(func.count(Document.id))) == docs_before, "不该多出一份文档"
    assert await session.scalar(
        sa_select(func.count(ParseJob.id)).where(
            ParseJob.document_id == document_id)) == jobs_before, "不该多出一次解析"

    uploaders = (await session.execute(
        sa_select(DocumentUpload.user_id).where(
            DocumentUpload.document_id == document_id))).scalars().all()
    assert len(set(uploaders)) == 2, f"两个上传者都要记下来，实际 {uploaders}"


@respx.mock
async def test_only_uploader_or_admin_can_delete(auth_client, client, session):
    """**全站唯一残留的授权**：删除权限。

    非上传者删不掉（403，不是 404 —— 文档本来就是全员可见的，
    装作不存在只会让人以为自己找错了 id）。管理员可以。
    """
    from app.models import User

    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    other = await register(client, username="dave")
    headers = {"Authorization": f"Bearer {other['access_token']}"}

    resp = await client.delete(f"/api/documents/{document_id}", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "not_uploader"

    # 看得见但删不掉 —— 两件事要分开
    assert (await client.get(f"/api/documents/{document_id}", headers=headers)).status_code == 200

    # 提成管理员之后可以删
    dave = await session.get(User, other["user_id"]) if "user_id" in other else None
    if dave is None:
        from sqlalchemy import select as sa_select
        dave = await session.scalar(sa_select(User).where(User.username == "dave"))
    dave.is_admin = True
    await session.commit()
    assert (await client.delete(f"/api/documents/{document_id}",
                                headers=headers)).status_code == 204


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


@respx.mock
async def test_upload_engine_follows_setting(auth_client, monkeypatch):
    """上传不指定引擎时，用 DEFAULT_PARSE_ENGINE 而不是写死的 "mineru"。

    引擎名必须在 service 的 models.yaml 里存在，否则 service 返回 404 unknown_engine。
    无 GPU 部署（models.cpu.yaml）注册的是 borndigital —— 本层把 "mineru" 写死，
    产品层第一步（上传）就断，M7 的 CPU 路径因此走不通。
    """
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    await _upload(auth_client)

    body = json.loads(routes["submit"].calls.last.request.content)
    assert body["engine"] == "borndigital"


@respx.mock
async def test_upload_explicit_engine_wins_over_setting(auth_client, monkeypatch):
    """显式传 engine 仍然优先——配置只是缺省值，不能覆盖调用方的明确选择。"""
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    resp = await auth_client.post("/api/documents",
                                  files={"file": ("sample.pdf", PDF, "application/pdf")},
                                  data={"engine": "mineru"})
    assert resp.status_code == 202, resp.text
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "mineru"


@respx.mock
async def test_reparse_engine_follows_setting(auth_client, monkeypatch):
    """重解析不指定引擎时同样走 DEFAULT_PARSE_ENGINE。

    upload 改了而 reparse 没改，是第一版漏掉的那一处：e2e 的重解析场景显式传了
    engine，正好把它遮住，无 GPU 部署上任何不带 engine 的重解析都会 502。
    """
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    again = await auth_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"options": {"lang": "ch"}})
    assert again.status_code == 202, again.text
    assert again.json()["engine"] == "borndigital"
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "borndigital"


@respx.mock
async def test_reparse_explicit_engine_wins_over_setting(auth_client, monkeypatch):
    """显式传 engine 仍然优先（与 upload 同一语义）。"""
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    again = await auth_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"engine": "mineru", "options": {"lang": "ch"}})
    assert again.status_code == 202, again.text
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "mineru"


@respx.mock
async def test_second_uploader_can_also_delete(auth_client, client, session):
    """**第二个上传者也能删。**

    `_may_delete` 判的是 `document_uploads` 整张表而不是 `uploaded_by` 那一个
    字段 —— 全局去重之后第二个传同一份文件的人不会产生新的 Document，
    但他确实也传过。这条正是那段设计的守卫：验收变异实测把 `_may_delete`
    改成只判 `uploaded_by`，**143 个用例一个都没红**。
    """
    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    second = await register(client, username="alsome")
    headers = {"Authorization": f"Bearer {second['access_token']}"}
    again = await _upload(client, headers=headers)
    assert again["id"] == document_id, "前提：第二次上传复用同一份文档"

    # 他不是 uploaded_by，但他传过 —— 必须删得掉
    assert (await client.delete(f"/api/documents/{document_id}",
                                headers=headers)).status_code == 204


@respx.mock
async def test_search_within_a_specific_document_is_not_user_scoped(auth_client, client):
    """指定文档检索也不按用户收作用域。

    验收变异实测：把 `routers/search.py` 里"指定文档"那条分支的归属判定加回去，
    **没有任何用例变红** —— `test_corpus_is_shared_between_users` 只覆盖了
    列表与详情。这条补上检索那一半。
    """
    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    other = await register(client, username="searcher")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.get(f"/api/search?q=test&doc={document_id}", headers=headers)
    # 有没有命中不重要（索引可能还没建），**不能是 404 document_not_found**
    assert resp.status_code == 200, resp.text


@respx.mock
async def test_conversations_can_be_started_on_anyone_s_document(auth_client, client):
    """对别人传的文档也能发起问答 —— 语料共享的直接含义。

    同样是验收变异存活的一处：把 `conversations.py` 的归属判定加回去无人报警。
    （会话**本身**仍然是个人产物，只有创建者看得见自己的会话，那条不变。）
    """
    _mock_service()
    document_id = (await _upload(auth_client))["id"]

    other = await register(client, username="asker")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(f"/api/documents/{document_id}/conversations", headers=headers)
    assert resp.status_code in (200, 201), resp.text


@respx.mock
async def test_reparse_bills_the_person_who_asked_not_the_uploader(
        auth_client, client, session, app_state):
    """**别人重新解析我的文档，页数不能记在我头上。**

    语料共享之后（1b）任何人都能对任一文档点"换参数重解析"/"重建索引"，
    而那是要花钱的（GPU 按小时租，页数是贵的那一项）。按 `uploaded_by` 记账
    等于"谁传的谁买单" —— B 可以任意消耗 A 的额度，而 `/api/*` 这条路
    **没有任何按发起人的限速**（限速中间件只覆盖 `/v1/*`）。

    验收（1b 二次）指出这半虽然代码改对了却**零守卫**：把 `archive.py` 的
    `job.initiated_by or document.uploaded_by` 退回成 `document.uploaded_by`，
    150 个用例一个都没红。抽取那半有守卫，**页数这半没有**。
    """
    _mock_service(status="succeeded")
    document = await _upload(auth_client)
    uploader_job = (await session.execute(select(ParseJob))).scalars().one()
    uploader_id = (await session.get(Document, document["id"])).uploaded_by

    # 换个账号，对**别人的**文档换参数重解析
    other = await register(client, username="reparser")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(f"/api/documents/{document['id']}/reparse",
                             json={"engine": "borndigital", "options": {"scale": 3.0}},
                             headers=headers)
    assert resp.status_code in (200, 202), resp.text

    new_job = (await session.execute(
        select(ParseJob).where(ParseJob.id != uploader_job.id))).scalars().one()
    assert new_job.initiated_by is not None, "新 job 要记下发起人"
    assert new_job.initiated_by != uploader_id, "发起人应当是操作者，不是上传者"

    await archive_job(session, app_state.storage, app_state.service_client, new_job.id)

    billed = {
        u.user_id: u.pages for u in (await session.execute(
            select(UsageRecord).where(UsageRecord.kind == "parse",
                                      UsageRecord.parse_job_id == new_job.id))
        ).scalars().all()
    }
    assert uploader_id not in billed, \
        f"重解析的页数记到了上传者头上（{uploader_id}），别人能随意花掉他的额度：{billed}"
    assert billed.get(new_job.initiated_by), f"应当记在发起人头上，实际 {billed}"

    # **embed 那半同样要钉住。** 建索引也是花钱的，而它走的是另一处
    # record_usage（indexing.py），退回按上传者记账时上面那些断言一条都不会红
    from app.indexing import index_document

    document_row = await session.get(Document, document["id"])
    document_row.current_job_id = new_job.id
    await session.commit()
    await index_document(session, app_state.storage, app_state.http, document["id"])

    embed_billed = {
        u.user_id for u in (await session.execute(
            select(UsageRecord).where(UsageRecord.kind == "embed"))).scalars().all()
    }
    assert uploader_id not in embed_billed, \
        f"建索引的费用记到了上传者头上：{embed_billed}"
    assert new_job.initiated_by in embed_billed, \
        f"embed 应当记在发起人头上，实际 {embed_billed}"
