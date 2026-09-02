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

import ddp_corpus.db as db
from ddp_corpus.archive import archive_job
from ddp_corpus.config import settings
from ddp_corpus.models import Chunk, Document, ParseJob, utcnow
from ddp_corpus.reconcile import reconcile_once
from tests.conftest import CONTROL, EMBEDDINGS, SERVICE, as_actor, usage_events

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
    # **稳定文件 URL 现在归 control-api**（凭证住在 control schema，本服务无权写）。
    # 每次都返回同一个 URL —— 这正是它的契约：URL 一变，模型网关的幂等与
    # 向量索引分块键全部失效（ADR #11/#12）
    routes["file_grant"] = respx.post(f"{CONTROL}/internal/file-grants").mock(
        side_effect=lambda request: httpx.Response(200, json={
            "token": "stable-token",
            "url": f"{CONTROL}/files/stable-token",
        }))
    routes["actors"] = respx.get(f"{CONTROL}/internal/actors").mock(
        return_value=httpx.Response(200, json={}))
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
                  mime: str = "application/pdf", headers: dict | None = None,
                  engine: str = "", options: dict | None = None) -> dict:
    """走新的上传链路：对象先在存储里，再投一个 DocumentSubmitted 事件。

    合仓前这里是 `POST /api/documents` 的 multipart —— 那条路已经删了
    （它把整份文件读进一个 bytes，违反不变式 6）。字节流现在由浏览器
    直传对象存储，本服务只收元数据。

    `headers` 仍然支持"换一个人"：从里面取 actor id 传给事件。
    """
    from ddp_corpus.main import app
    from tests.conftest import ACTOR, ORG, submit_document

    actor_id = (headers or {}).get("X-DDP-Actor", ACTOR)
    organization_id = (headers or {}).get("X-DDP-Organization", ORG)
    resp = await submit_document(client, app.state.storage, content, filename=filename,
                                 mime=mime, actor_id=actor_id,
                                 organization_id=organization_id,
                                 engine=engine, options=options)
    assert resp.status_code == 200, resp.text
    document_id = resp.json()["result_id"]

    # 直接按库里的状态返回，**不走 GET** —— GET 会实时问一次网关，
    # 把"刚受理"的 pending 变成 mock 里的 running。旧的 POST 返回体
    # 也是受理那一刻的状态，这里保持同一口径
    from ddp_corpus.db import get_sessionmaker
    from ddp_corpus.routers.documents import _doc_info, _latest_job

    async with get_sessionmaker()() as probe:
        document = await probe.get(Document, document_id)
        job = await _latest_job(probe, document)
        return _doc_info(document, job).model_dump()


async def _callback(client, status: str = "succeeded", task_id: str = "s-1"):
    """网关的解析回调。**必须带服务身份头**，不是只带服务凭据 ——
    `/internal/*` 现在要求 `X-DDP-Actor-Kind: service`（见 ddp_corpus/deps.py）。"""
    from tests.conftest import actor_headers

    return await client.post("/internal/parse-callback",
                             json={"task_id": task_id, "status": status},
                             headers=actor_headers("model-gateway", role="admin",
                                                   kind="service"))


@respx.mock
async def test_upload_passes_content_hash_as_doc_id(actor_client, session):
    """契约关键点：doc_id = 文件内容 sha256，file_url 是本层的稳定 URL（非预签名）。"""
    routes = _mock_service()
    document = await _upload(actor_client)

    assert document["status"] == "pending" and document["doc_id"] == DOC_ID
    body = json.loads(routes["submit"].calls.last.request.content)
    assert body["doc_id"] == DOC_ID, "必须传内容哈希，否则 service 侧向量索引永不命中"
    assert body["callback_url"].endswith("/internal/parse-callback")

    # 稳定文件 URL 的凭证住在 control schema（Go 拥有），所以这里断言的是
    # "本服务确实去要了一个"，而不是本地有没有那一行
    assert routes["file_grant"].called, "必须向 control-api 申请稳定文件 URL"
    assert body["file_url"] == f"{CONTROL}/files/stable-token"
    assert "X-Amz-Signature" not in body["file_url"], "不得用预签名 URL（每次签名不同）"

    # 网关就是靠这个 URL 下载原件的。**它由 control-api 服务**（302 到短期
    # 签名 URL），本服务只负责把它拿到并原样传给网关 —— 所以这里断言的是
    # "传出去的是那个稳定 URL"，端点本身的行为由 Go 侧的用例守


@respx.mock
async def test_list_filters_before_paginating(actor_client, session):
    """回归：status 过滤必须在 SQL 里做，不能先分页再用 Python 丢行。

    旧实现下 `?status=succeeded&limit=1` 只看第一行，恰好是 pending 就返回空，
    而后面明明还有成功的文档 —— 分页语义是坏的。
    """
    _mock_service()
    for i in range(3):
        await _upload(actor_client, content=b"%PDF-1.4 doc " + str(i).encode(),
                      filename=f"doc{i}.pdf")

    # 只把最早那份（列表里排最后）置成 succeeded
    jobs = (await session.execute(select(ParseJob).order_by(ParseJob.created_at))).scalars().all()
    jobs[0].status = "succeeded"
    await session.commit()

    listed = (await actor_client.get("/api/documents", params={"status": "succeeded"})).json()
    assert len(listed) == 1, f"过滤应命中唯一一条 succeeded，实际 {[d['status'] for d in listed]}"

    # 关键点：limit 小于"需要跳过的 pending 数"时仍须返回它
    paged = (await actor_client.get(
        "/api/documents", params={"status": "succeeded", "limit": 1})).json()
    assert len(paged) == 1, "先分页后过滤会在这里返回空"
    assert paged[0]["id"] == listed[0]["id"]

    pending = (await actor_client.get("/api/documents", params={"status": "pending"})).json()
    assert len(pending) == 2 and all(d["status"] == "pending" for d in pending)


@respx.mock
async def test_list_does_not_duplicate_documents_with_tied_job_timestamps(actor_client, session):
    """回归：把 job join 进来时不能让一个文档变成两行。

    按 `GROUP BY document_id HAVING max(created_at)` 再 join 回去的写法，
    在两条 job 的 created_at 撞上（同一微秒）时会 join 出两行，列表页出现重复文档。
    """
    _mock_service()
    document = await _upload(actor_client)
    await actor_client.post(f"/api/documents/{document['id']}/reparse",
                           json={"engine": "mineru", "options": {"backend": "vlm"}})

    jobs = (await session.execute(
        select(ParseJob).where(ParseJob.document_id == document["id"]))).scalars().all()
    assert len(jobs) == 2
    tied = utcnow()
    for job in jobs:                       # 制造完全相同的 created_at
        job.created_at = tied
    await session.commit()

    listed = (await actor_client.get("/api/documents")).json()
    assert [d["id"] for d in listed] == [document["id"]], f"文档被 join 成了多行：{listed}"


@respx.mock
async def test_list_documents_does_not_scale_queries_with_rows(actor_client):
    """回归：列表页曾对每个文档单独查一次 job（一页 200 个文档 = 200+ 次往返）。

    断"查询次数不随行数增长"，而不是断一个具体数字 —— 后者会因为无关重构而脆断。
    """
    _mock_service()
    counts: list[int] = []

    from sqlalchemy import event

    import ddp_corpus.db as db

    for rows in (1, 6):
        while len((await actor_client.get("/api/documents")).json()) < rows:
            n = len((await actor_client.get("/api/documents")).json())
            await _upload(actor_client, content=b"%PDF-1.4 n" + str(n).encode(),
                          filename=f"n{n}.pdf")

        seen = 0

        def _count(*_args, **_kwargs):
            nonlocal seen
            seen += 1

        engine = db.get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            assert len((await actor_client.get("/api/documents")).json()) == rows
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        counts.append(seen)

    assert counts[0] == counts[1], \
        f"查询次数随行数增长（1 行 {counts[0]} 次 / 6 行 {counts[1]} 次）—— N+1 回来了"


# ---------------------------------------------------------------------------
# **上传体积上限的两条用例已迁去 services/control-api。**
#
# 它们原本验的是"别把整个上传体读进内存"。现在字节流由浏览器直传对象存储，
# 本服务连一个字节都收不到 —— 那个风险在结构上不存在了。
#
# 上限改由 control-api 在**签发预签名之前**校验（超限直接 413，
# 连 multipart 都不会开），并在 finalize 时核对对象的真实大小。
# 等价覆盖：Go 的 TestPartSizeFloor / 配置项 MAX_UPLOAD_BYTES，
# 以及本文件下方的 test_corpus_api_accepts_no_file_bodies（结构守卫）。
# ---------------------------------------------------------------------------


@respx.mock
async def test_callback_archives_indexes_and_rewrites_images(actor_client, session, app_state):
    routes = _mock_service()
    document = await _upload(actor_client)

    cb = await _callback(actor_client)
    assert cb.status_code == 200 and cb.json()["archived"] == 1
    assert routes["result"].called

    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["status"] == "succeeded" and detail["page_count"] == 2

    result = (await actor_client.get(f"/api/documents/{document['id']}/result")).json()
    assert "data:image/" not in result["markdown"], "归档后的 markdown 不得残留 base64"
    assert f"/api/documents/{document['id']}/jobs/{result['job_id']}/images/img_0.png" \
        in result["markdown"]
    assert result["images"] == ["img_0.png"]

    img = await actor_client.get(
        f"/api/documents/{document['id']}/jobs/{result['job_id']}/images/img_0.png")
    assert img.status_code == 200 and img.content == base64.b64decode("iVBORw0KGgo=")

    layout = (await actor_client.get(f"/api/documents/{document['id']}/layout")).json()
    assert len(layout["pdf_info"]) == 2

    usage = await usage_events(session, "parse")
    assert [(u["kind"], u["pages"]) for u in usage] == [("parse", 2)]

    # 后台索引任务（BackgroundTasks 在 ASGI 传输里会同步跑完）
    chunks = (await session.execute(select(Chunk))).scalars().all()
    assert chunks and all(c.embedding for c in chunks), "归档后必须建好向量索引"
    doc_row = await session.get(Document, document["id"])
    await session.refresh(doc_row)
    assert doc_row.index_status == "ready"


@respx.mock
async def test_pages_endpoint_groups_blocks_by_page(actor_client):
    """前端左右栏对齐的数据源：块必须带页码与 bbox。"""
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    pages = (await actor_client.get(f"/api/documents/{document['id']}/pages")).json()
    assert [p["page_idx"] for p in pages["pages"]] == [0, 1]
    block = pages["pages"][1]["blocks"][0]
    assert block["bbox"] == [72, 120, 540, 300] and block["page_size"] == [612, 792]
    assert "表格" in block["text"]


@respx.mock
async def test_inline_base64_in_markdown_is_externalized(actor_client):
    """mineru 若把图片内联进 markdown，归档也必须把它外置成对象。"""
    inline = dict(RESULT, markdown="![x](data:image/png;base64,iVBORw0KGgo=)", images=[])
    _mock_service(result=inline)
    document = await _upload(actor_client)
    await _callback(actor_client)

    result = (await actor_client.get(f"/api/documents/{document['id']}/result")).json()
    assert "data:image/" not in result["markdown"]
    assert result["images"] == ["inline_0.png"]


@respx.mock
async def test_duplicate_upload_reuses_document(actor_client):
    routes = _mock_service()
    first = await _upload(actor_client)
    second = await _upload(actor_client)
    assert first["id"] == second["id"]
    assert routes["submit"].call_count == 1, "同一文件重复上传不得再打 service"


@respx.mock
async def test_reparse_creates_new_job_and_keeps_old(actor_client, session):
    """换参数重解析：新版本与旧版本并存，切换 current_job 才影响预览与索引。"""
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    first_job = (await actor_client.get(f"/api/documents/{document['id']}/result")).json()["job_id"]

    respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(202, json={"task_id": "s-2"}))
    respx.get(f"{SERVICE}/v1/parse/s-2").mock(
        return_value=httpx.Response(200, json={"task_id": "s-2", "status": "running"}))
    respx.get(f"{SERVICE}/v1/parse/s-2/result").mock(return_value=httpx.Response(200, json=RESULT))

    again = await actor_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"engine": "mineru", "options": {"backend": "vlm"}})
    assert again.status_code == 202
    second_job = again.json()["id"]
    assert second_job != first_job

    # 同参数再来一次 -> 幂等命中同一个 job
    dup = await actor_client.post(f"/api/documents/{document['id']}/reparse",
                                 json={"engine": "mineru", "options": {"backend": "vlm"}})
    assert dup.json()["id"] == second_job

    jobs = (await actor_client.get(f"/api/documents/{document['id']}/jobs")).json()
    assert {j["id"] for j in jobs} == {first_job, second_job}
    assert [j["is_current"] for j in jobs if j["id"] == first_job] == [True]

    await _callback(actor_client, task_id="s-2")
    switched = await actor_client.put(f"/api/documents/{document['id']}/current-job",
                                     json={"job_id": second_job})
    assert switched.status_code == 200 and switched.json()["current_job_id"] == second_job
    # 旧版本仍然可读
    old = await actor_client.get(f"/api/documents/{document['id']}/result?job={first_job}")
    assert old.status_code == 200


# ---------------------------------------------------------------------------
# **`/files/{token}` 的两条用例已迁去 services/control-api。**
#
# 那个端点现在归控制面：凭证住在 control schema，且实现从"本进程转发字节流"
# 换成了"302 到短期签名 URL"（不变式 6）。
#
# 等价覆盖在 Go 侧：
#   TestDispositionForNeutralisesDangerousMime  —— 非白名单类型一律 attachment
#   TestMIMEAllowlistIsAllowlist                —— 白名单本身
# 它们守的是同一件事：上传 text/html 并 inline 打开就是本站同源 XSS。
# ---------------------------------------------------------------------------


@respx.mock
async def test_web_and_external_planes_do_not_share_rows(actor_client, session, app_state):
    """同一份文档从 Web 传过、又用 key 提交过：必须是两个 Document。

    混用会把 Web 那行的状态打回 pending -> 对账重新归档 -> 同一批页数被重复计费，
    还会覆写用户已归档的结果。
    """
    _mock_service(status="succeeded")
    document = await _upload(actor_client)
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
async def test_resubmit_after_failure_is_not_instantly_expired(actor_client, session, app_state):
    """隔天重传失败的文档：对账不能因为旧的 created_at 立刻把它判成"结果已过期"。"""
    _mock_service()
    document = await _upload(actor_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    job.status, job.error = "failed", "boom"
    job.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 3600)
    await session.commit()

    again = await _upload(actor_client)
    assert again["id"] == document["id"]

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["expired"] == 0, "重传后不得立刻判死"
    await session.refresh(job)
    assert job.status != "failed"


@respx.mock
async def test_reconcile_recovers_lost_callback(actor_client, app_state, session):
    """回调丢了（backend 当时正在重启）也必须能补回来 —— 否则 24h 后结果永久消失。"""
    _mock_service(status="succeeded")
    document = await _upload(actor_client)

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["archived"] == 1 and stats["indexed"] == 1, "对账要同时补齐归档与索引"

    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["status"] == "succeeded" and detail["index_status"] == "ready"


@respx.mock
async def test_archive_is_idempotent(actor_client, session, app_state):
    """回调与对账可能同时到达：第二次必须空转，不能重复计量。"""
    _mock_service(status="succeeded")
    await _upload(actor_client)
    job = (await session.execute(select(ParseJob))).scalars().one()

    assert await archive_job(session, app_state.storage, app_state.service_client, job.id)
    assert not await archive_job(session, app_state.storage, app_state.service_client, job.id)

    usage = await usage_events(session, "parse")
    assert len(usage) == 1, "重复归档不得重复记账"


@respx.mock
async def test_late_archive_cannot_revive_deleted_document(
        actor_client, session, app_state):
    """解析中删除后，迟到 callback 只归档/计解析费，绝不能复活索引或再产生推理费。"""
    _mock_service(status="succeeded")
    document = await _upload(actor_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    assert (await actor_client.delete(f"/api/documents/{document['id']}")).status_code == 204

    assert await archive_job(session, app_state.storage, app_state.service_client, job.id)
    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.deleted_at is not None
    assert row.index_status == "none"

    from ddp_corpus.indexing import index_document
    assert await index_document(
        session, app_state.storage, app_state.http, document["id"]
    ) == 0
    assert (await session.scalar(select(Chunk.id).where(
        Chunk.document_id == document["id"]))) is None
    expensive = (await usage_events(session, "compile_vision")) + (await usage_events(session, "embed"))
    assert expensive == []


@respx.mock
async def test_reconcile_expires_stale_job(actor_client, session, app_state):
    """超过 service 的 24h 暂存窗口 -> 落终态并提示重传（否则永远挂在 running）。"""
    _mock_service()
    await _upload(actor_client)
    job = (await session.execute(select(ParseJob))).scalars().one()
    job.created_at = utcnow() - timedelta(seconds=settings.result_ttl + 60)
    await session.commit()

    stats = await reconcile_once(db.get_sessionmaker(), app_state.storage,
                                 app_state.service_client, app_state.http)
    assert stats["expired"] == 1
    await session.refresh(job)
    assert job.status == "failed" and "expired" in job.error


@respx.mock
async def test_service_failure_marks_job_failed(actor_client):
    _mock_service()
    routes_status = respx.get(f"{SERVICE}/v1/parse/s-1").mock(
        return_value=httpx.Response(200, json={"task_id": "s-1", "status": "failed",
                                               "progress": 1.0, "error": "corrupt pdf"}))
    document = await _upload(actor_client)
    detail = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert routes_status.called
    assert detail["status"] == "failed" and detail["error"] == "corrupt pdf"

    result = await actor_client.get(f"/api/documents/{document['id']}/result")
    assert result.status_code == 409 and result.json()["error"]["code"] == "job_failed"


@respx.mock
async def test_queue_full_surfaces_as_429(actor_client):
    """网关队列满时，本服务必须把 429 原样透出去，而不是记成"解析失败"。

    两者对调用方是完全不同的事：429 是"稍后重试"，failed 是"这份文档废了"。
    合仓后入口是事件消费，所以断言落在 `/internal/events` 的响应上。
    """
    from ddp_corpus.main import app
    from tests.conftest import submit_document

    respx.post(f"{SERVICE}/v1/parse").mock(return_value=httpx.Response(429, json={
        "error": {"message": "parse queue is full", "type": "rate_limit_error",
                  "code": "queue_full"}}))
    respx.post(f"{CONTROL}/internal/file-grants").mock(return_value=httpx.Response(
        200, json={"token": "t", "url": f"{CONTROL}/files/t"}))
    resp = await submit_document(actor_client, app.state.storage, PDF)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "queue_full"


async def test_file_token_must_be_valid(client):
    assert (await client.get("/files/nope")).status_code == 404


@respx.mock
async def test_corpus_is_shared_between_users(actor_client, client):
    """**语料是整个部署共享的** —— 这条用例在 1b 里被整个反转过来。

    改之前它断言的是"别人的文档看不见（404）"。plan.md §2 已定 2 之后，
    一次部署 = 一份语料 = 一个知识库：账号层只管认证 / 计量 / 限速，**不管授权**。
    所以另一个账号必须能看见、能读、能问。

    留着这条反转记录是有意的：谁哪天把可见性过滤加回去，这里立刻会红，
    而红的时候能从这段说明看到"这不是 bug，是产品决定"。
    """
    _mock_service()
    document_id = (await _upload(actor_client))["id"]

    headers = as_actor("actor-bob")

    resp = await client.get(f"/api/documents/{document_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == document_id

    # 列表里也要有 —— 不是"知道 id 就能访问"，是真的在他的文档库里
    listed = await client.get("/api/documents", headers=headers)
    assert listed.status_code == 200
    assert document_id in [d["id"] for d in listed.json()]


@respx.mock
async def test_second_uploader_reuses_the_parse_and_is_recorded(actor_client, client, session):
    """同一份文件第二个人再传：**命中已有解析，不产生第二个 parse_job**；
    但"他也传过"要记下来。

    这是全局去重的核心收益 —— 以前两个人传同一份手册 = 两次解析 + 两次索引 +
    两套 embedding，而 GPU 是按小时租的。
    """
    from sqlalchemy import func, select as sa_select

    from ddp_corpus.models import Document, DocumentUpload, ParseJob

    _mock_service()
    document_id = (await _upload(actor_client))["id"]
    jobs_before = await session.scalar(
        sa_select(func.count(ParseJob.id)).where(ParseJob.document_id == document_id))
    docs_before = await session.scalar(sa_select(func.count(Document.id)))

    headers = as_actor("actor-carol")
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
async def test_only_uploader_or_admin_can_delete(actor_client, client, session):
    """**全站唯一残留的授权**：删除权限。

    非上传者删不掉（403，不是 404 —— 文档本来就是全员可见的，
    装作不存在只会让人以为自己找错了 id）。管理员可以。
    """
    _mock_service()
    document_id = (await _upload(actor_client))["id"]

    # 另一个人：没传过这份文档，角色也只是 contributor
    headers = as_actor("actor-dave")

    resp = await client.delete(f"/api/documents/{document_id}", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "not_uploader"

    # 看得见但删不掉 —— 两件事要分开
    assert (await client.get(f"/api/documents/{document_id}", headers=headers)).status_code == 200

    # **角色够了就能删。** 合仓前这条是改库里的 `is_admin` 布尔位；
    # 现在角色由 control-api 下发，语料侧只看那一个头 —— 换个角色重发即可。
    # 判据也从"是不是 admin"升级成"角色够不够"（reviewer 起），
    # 加新角色时不必回来改这里
    elevated = as_actor("actor-dave", role="reviewer")
    assert (await client.delete(f"/api/documents/{document_id}",
                                headers=elevated)).status_code == 204


@respx.mock
async def test_soft_delete_keeps_usage_and_drops_chunks(actor_client, session, app_state):
    _mock_service(status="succeeded")
    document = await _upload(actor_client)
    await _callback(actor_client)

    assert (await actor_client.delete(f"/api/documents/{document['id']}")).status_code == 204
    assert (await actor_client.get(f"/api/documents/{document['id']}")).status_code == 404

    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.deleted_at is not None, "软删除：对象由 GC 回收，记录留痕"
    assert (await session.execute(select(Chunk))).scalars().all() == []
    usage = await usage_events(session, "parse")
    assert len(usage) == 1, "账单不能因删文档而消失"


@respx.mock
async def test_export_zip_contains_markdown_and_images(actor_client):
    import io
    import zipfile

    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    resp = await actor_client.get(f"/api/documents/{document['id']}/download?format=zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert {"document.md", "layout.json", "images/img_0.png"} <= names
        # 打包里的引用改成相对路径，解压出来直接能看图
        assert "images/img_0.png" in zf.read("document.md").decode()


@pytest.mark.parametrize("fmt,expected", [("md", 200), ("json", 200), ("source", 200),
                                          ("bogus", 400)])
@respx.mock
async def test_download_formats(actor_client, fmt, expected):
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    resp = await actor_client.get(f"/api/documents/{document['id']}/download?format={fmt}")
    assert resp.status_code == expected


@respx.mock
async def test_upload_engine_follows_setting(actor_client, monkeypatch):
    """上传不指定引擎时，用 DEFAULT_PARSE_ENGINE 而不是写死的 "mineru"。

    引擎名必须在 service 的 models.yaml 里存在，否则 service 返回 404 unknown_engine。
    无 GPU 部署（models.cpu.yaml）注册的是 borndigital —— 本层把 "mineru" 写死，
    产品层第一步（上传）就断，M7 的 CPU 路径因此走不通。
    """
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    await _upload(actor_client)

    body = json.loads(routes["submit"].calls.last.request.content)
    assert body["engine"] == "borndigital"


@respx.mock
async def test_upload_explicit_engine_wins_over_setting(actor_client, monkeypatch):
    """显式传 engine 仍然优先——配置只是缺省值，不能覆盖调用方的明确选择。"""
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    await _upload(actor_client, engine="mineru")
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "mineru"


@respx.mock
async def test_reparse_engine_follows_setting(actor_client, monkeypatch):
    """重解析不指定引擎时同样走 DEFAULT_PARSE_ENGINE。

    upload 改了而 reparse 没改，是第一版漏掉的那一处：e2e 的重解析场景显式传了
    engine，正好把它遮住，无 GPU 部署上任何不带 engine 的重解析都会 502。
    """
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    again = await actor_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"options": {"lang": "ch"}})
    assert again.status_code == 202, again.text
    assert again.json()["engine"] == "borndigital"
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "borndigital"


@respx.mock
async def test_reparse_explicit_engine_wins_over_setting(actor_client, monkeypatch):
    """显式传 engine 仍然优先（与 upload 同一语义）。"""
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")
    routes = _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    again = await actor_client.post(f"/api/documents/{document['id']}/reparse",
                                   json={"engine": "mineru", "options": {"lang": "ch"}})
    assert again.status_code == 202, again.text
    assert json.loads(routes["submit"].calls.last.request.content)["engine"] == "mineru"


@respx.mock
async def test_second_uploader_can_also_delete(actor_client, client, session):
    """**第二个上传者也能删。**

    `_may_delete` 判的是 `document_uploads` 整张表而不是 `uploaded_by` 那一个
    字段 —— 全局去重之后第二个传同一份文件的人不会产生新的 Document，
    但他确实也传过。这条正是那段设计的守卫：验收变异实测把 `_may_delete`
    改成只判 `uploaded_by`，**143 个用例一个都没红**。
    """
    _mock_service()
    document_id = (await _upload(actor_client))["id"]

    headers = as_actor("actor-alsome")
    again = await _upload(client, headers=headers)
    assert again["id"] == document_id, "前提：第二次上传复用同一份文档"

    # 他不是 uploaded_by，但他传过 —— 必须删得掉
    assert (await client.delete(f"/api/documents/{document_id}",
                                headers=headers)).status_code == 204


@respx.mock
async def test_search_within_a_specific_document_is_not_user_scoped(actor_client, client):
    """指定文档检索也不按用户收作用域。

    验收变异实测：把 `routers/search.py` 里"指定文档"那条分支的归属判定加回去，
    **没有任何用例变红** —— `test_corpus_is_shared_between_users` 只覆盖了
    列表与详情。这条补上检索那一半。
    """
    _mock_service()
    document_id = (await _upload(actor_client))["id"]

    headers = as_actor("actor-searcher")
    resp = await client.get(f"/api/search?q=test&doc={document_id}", headers=headers)
    # 有没有命中不重要（索引可能还没建），**不能是 404 document_not_found**
    assert resp.status_code == 200, resp.text


@respx.mock
async def test_conversations_can_be_started_on_anyone_s_document(actor_client, client):
    """对别人传的文档也能发起问答 —— 语料共享的直接含义。

    同样是验收变异存活的一处：把 `conversations.py` 的归属判定加回去无人报警。
    （会话**本身**仍然是个人产物，只有创建者看得见自己的会话，那条不变。）
    """
    _mock_service()
    document_id = (await _upload(actor_client))["id"]

    headers = as_actor("actor-asker")
    resp = await client.post(f"/api/documents/{document_id}/conversations", headers=headers)
    assert resp.status_code in (200, 201), resp.text


@respx.mock
async def test_reparse_bills_the_person_who_asked_not_the_uploader(
        actor_client, client, session, app_state):
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
    document = await _upload(actor_client)
    uploader_job = (await session.execute(select(ParseJob))).scalars().one()
    uploader_id = (await session.get(Document, document["id"])).uploaded_by

    # 换个账号，对**别人的**文档换参数重解析
    headers = as_actor("actor-reparser")
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
        u["actor_id"]: u["pages"] for u in await usage_events(session, "parse")
        if u["parse_job_id"] == new_job.id
    }
    assert uploader_id not in billed, \
        f"重解析的页数记到了上传者头上（{uploader_id}），别人能随意花掉他的额度：{billed}"
    assert billed.get(new_job.initiated_by), f"应当记在发起人头上，实际 {billed}"

    # **embed 那半同样要钉住。** 建索引也是花钱的，而它走的是另一处
    # record_usage（indexing.py），退回按上传者记账时上面那些断言一条都不会红
    from ddp_corpus.indexing import index_document

    document_row = await session.get(Document, document["id"])
    document_row.current_job_id = new_job.id
    await session.commit()
    await index_document(session, app_state.storage, app_state.http, document["id"])

    embed_billed = {
        u["actor_id"] for u in await usage_events(session, "embed")
    }
    assert uploader_id not in embed_billed, \
        f"建索引的费用记到了上传者头上：{embed_billed}"
    assert new_job.initiated_by in embed_billed, \
        f"embed 应当记在发起人头上，实际 {embed_billed}"
