"""对外 API 与 MCP 代理的契约：验 key -> 限速/额度 -> 换 token 转发 -> 计量。

对第三方而言这层必须"透明"：错误体、流式、MCP 会话头都要与直连 service 时一致。
"""
import json
from datetime import timedelta

import httpx
import respx
from sqlalchemy import select

from ddp_corpus.config import settings
from ddp_corpus.models import ApiKey, Document, ParseJob, UsageRecord, utcnow
from tests.conftest import MCP, SERVICE

LAYOUT = {"pdf_info": [{"page_idx": i} for i in range(3)]}


def _auth(key: dict) -> dict:
    return {"Authorization": f"Bearer {key['key']}"}


async def test_rejects_bad_credentials(client, api_key):
    for headers, code in [
        ({}, "missing_token"),
        ({"Authorization": "Bearer not-a-key"}, "invalid_key"),
        ({"Authorization": "Bearer sk-does-not-exist"}, "invalid_key"),
    ]:
        resp = await client.get("/v1/models", headers=headers)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == code


async def test_revoked_and_expired_keys(client, api_key, session):
    key = await session.get(ApiKey, api_key["id"])
    key.revoked_at = utcnow()
    await session.commit()
    resp = await client.get("/v1/models", headers=_auth(api_key))
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "revoked_key"

    key.revoked_at = None
    key.expires_at = utcnow() - timedelta(days=1)
    await session.commit()
    resp = await client.get("/v1/models", headers=_auth(api_key))
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "expired_key"


@respx.mock
async def test_forward_swaps_api_key_for_service_token(client, api_key, session):
    """service 只认内网 token，绝不能看到用户的 sk- key。"""
    route = respx.get(f"{SERVICE}/v1/models").mock(
        return_value=httpx.Response(200, json={"object": "list", "data": []}))

    resp = await client.get("/v1/models", headers=_auth(api_key))
    assert resp.status_code == 200
    forwarded = route.calls.last.request.headers["authorization"]
    assert forwarded == f"Bearer {settings.service_token}"
    assert api_key["key"] not in forwarded

    usage = (await session.execute(select(UsageRecord))).scalars().all()
    assert [(u.kind, u.requests) for u in usage] == [("parse", 1)]


@respx.mock
async def test_parse_submit_creates_task_and_result_meters_pages(client, api_key, session):
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-9"}))
    respx.get(f"{SERVICE}/v1/parse/s-9/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))

    submit = await client.post("/v1/parse", headers=_auth(api_key),
                               json={"file_url": "https://third-party.example/a.pdf"})
    assert submit.status_code == 202 and submit.json()["task_id"] == "s-9"

    document = (await session.execute(select(Document))).scalars().one()
    job = (await session.execute(select(ParseJob))).scalars().one()
    assert document.object_key == "" and document.origin == "external", "外部任务不归档，仅留记录"
    assert job.service_task_id == "s-9" and document.filename == "a.pdf"

    # 取结果：按页计量一次
    for _ in range(2):
        got = await client.get("/v1/parse/s-9/result", headers=_auth(api_key))
        assert got.status_code == 200 and got.json()["markdown"] == "# x"

    pages = [(u.kind, u.pages) for u in (await session.execute(
        select(UsageRecord).where(UsageRecord.pages > 0))).scalars().all()]
    assert pages == [("parse", 3)], "重复取结果不得重复计费"

    key = await session.get(ApiKey, api_key["id"])
    await session.refresh(key)
    assert key.used_pages == 3


@respx.mock
async def test_result_metering_with_shared_service_task(client, auth_client, api_key, session):
    """同一用户既从 Web 传过、又用 key 提交过同一份文档时，本层会有两个 job 指向同一个
    service 任务。取结果必须照常计量，而不是撞上"查出多行"直接 500（真机 e2e 抓到过）。"""
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-shared"}))
    respx.get(f"{SERVICE}/v1/parse/s-shared/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))
    key_row = await session.get(ApiKey, api_key["id"])

    # 先造一份"Web 上传"的文档与 job，再用 key 提交一次 —— 两个 job 同 service_task_id
    web_doc = Document(uploaded_by=key_row.user_id, doc_id="web-doc", origin="web", filename="a.pdf",
                       object_key="sources/x/a.pdf", page_count=3)
    session.add(web_doc)
    await session.commit()
    session.add(ParseJob(document_id=web_doc.id, engine="mineru", options_hash="web",
                         service_task_id="s-shared", status="succeeded", page_count=3))
    await session.commit()

    submit = await client.post("/v1/parse", headers=_auth(api_key),
                               json={"file_url": "https://third-party.example/a.pdf"})
    assert submit.status_code == 202

    got = await client.get("/v1/parse/s-shared/result", headers=_auth(api_key))
    assert got.status_code == 200, got.text
    pages = [(u.kind, u.pages) for u in (await session.execute(
        select(UsageRecord).where(UsageRecord.pages > 0))).scalars().all()]
    assert pages == [("parse", 3)], "必须记在这把 key 自己的任务上，且只记一次"


@respx.mock
async def test_sse_is_streamed_not_buffered(client, api_key):
    """OpenAI 流式必须逐帧透传，且 content-type 保持 text/event-stream。"""
    async def frames():
        for i in range(3):
            yield f'data: {{"i": {i}}}\n\n'.encode()
        yield b"data: [DONE]\n\n"

    respx.post(f"{SERVICE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"},
                                    content=frames()))

    chunks = []
    async with client.stream("POST", "/v1/chat/completions", headers=_auth(api_key),
                             json={"stream": True, "messages": []}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        assert "content-length" not in resp.headers, "流式响应不该带 content-length"
        async for chunk in resp.aiter_bytes():
            if chunk:
                chunks.append(chunk)

    body = b"".join(chunks)
    assert body.count(b"data:") == 4 and body.endswith(b"data: [DONE]\n\n")


@respx.mock
async def test_upstream_error_body_is_openai_shaped(client, api_key):
    respx.post(f"{SERVICE}/v1/chat/completions").mock(
        return_value=httpx.Response(404, json={"error": {"message": "model not found",
                                                         "type": "invalid_request_error",
                                                         "code": "model_not_found"}}))
    resp = await client.post("/v1/chat/completions", headers=_auth(api_key), json={})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "model_not_found"


@respx.mock
async def test_rate_limit_and_quota(client, api_key, session):
    respx.get(f"{SERVICE}/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
    key = await session.get(ApiKey, api_key["id"])
    key.rate_limit_per_min = 2
    await session.commit()

    assert (await client.get("/v1/models", headers=_auth(api_key))).status_code == 200
    assert (await client.get("/v1/models", headers=_auth(api_key))).status_code == 200
    limited = await client.get("/v1/models", headers=_auth(api_key))
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1

    # 额度耗尽 -> 402（与限速区分开：一个是速率问题，一个是余额问题）
    key.rate_limit_per_min = 100
    key.quota_pages, key.used_pages = 10, 10
    await session.commit()
    resp = await client.get("/v1/models", headers=_auth(api_key))
    assert resp.status_code == 402 and resp.json()["error"]["code"] == "quota_exhausted"


@respx.mock
async def test_mcp_proxy_passes_session_header_both_ways(client, api_key, session):
    route = respx.post(f"{MCP}/mcp").mock(return_value=httpx.Response(
        200, headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
        json={"jsonrpc": "2.0", "id": 1, "result": {}}))

    resp = await client.post("/mcp", headers={**_auth(api_key),
                                              "mcp-session-id": "sess-1",
                                              "accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    assert resp.headers["mcp-session-id"] == "sess-1", "响应侧会话头必须带回"
    sent = route.calls.last.request
    assert sent.headers["mcp-session-id"] == "sess-1", "请求侧会话头必须透传"
    assert sent.headers["authorization"] == f"Bearer {settings.service_token}"
    assert json.loads(sent.content)["method"] == "tools/list"

    usage = (await session.execute(select(UsageRecord))).scalars().all()
    assert [u.kind for u in usage] == ["mcp"]


# ---- 1b 语料共享化引入的三个计量回归（验收抓到，各钉一条）----
#
# 共同根因：全局去重之后一个 ParseJob 被多个用户共享，而原来的计量锚点
# （`job.page_count == 0` = "这次任务还没记过账"）是**按任务**的。
# 任务一共享，那个锚点就从"每人各记一次"退化成"整个部署只记一次"。

async def _second_key(session, username: str = "second") -> dict:
    """再造一个用户 + 一把 key。"""
    import hashlib
    import secrets

    from ddp_corpus.models import User, new_id

    user = User(id=new_id(), username=username, password_hash="x")
    session.add(user)
    await session.flush()
    raw = f"sk-{secrets.token_hex(16)}"
    key = ApiKey(id=new_id(), user_id=user.id, name="k",
                 key_hash=hashlib.sha256(raw.encode()).hexdigest(), key_prefix=raw[:10])
    session.add(key)
    await session.commit()
    return {"id": key.id, "key": raw, "user_id": user.id}


@respx.mock
async def test_second_user_of_a_shared_parse_is_still_billed(client, api_key, session):
    """**第二个用户不能白嫖。**

    全局去重让 B 提交一个 A 已经解析过的 file_url 时命中同一个 job，
    而那个 job 的 `page_count` 早就非 0 —— 于是 B **完全不计费**，
    可以无限白嫖解析并绕过 `quota_pages`（验收实测 B used_pages=0）。
    改成按 (用户, job) 判重之后，**恰好还原 1b 之前的计费行为**：
    那时每人各有一份 job，本来就是每人各记一次。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-share"}))
    respx.get(f"{SERVICE}/v1/parse/s-share/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))
    url = {"file_url": "https://third-party.example/shared.pdf"}

    await client.post("/v1/parse", headers=_auth(api_key), json=url)
    await client.get("/v1/parse/s-share/result", headers=_auth(api_key))

    other = await _second_key(session, "freeloader")
    await client.post("/v1/parse", headers=_auth(other), json=url)
    await client.get("/v1/parse/s-share/result", headers=_auth(other))

    billed = {
        u.user_id: u.pages
        for u in (await session.execute(
            select(UsageRecord).where(UsageRecord.pages > 0))).scalars().all()
    }
    assert billed.get(other["user_id"]) == 3, f"第二个用户没被计费：{billed}"
    # 而且各自只记一次 —— 重复取结果仍然不得重复计费
    assert len(billed) == 2, billed


@respx.mock
async def test_polling_someone_elses_task_does_not_bill_the_wrong_person(
        client, api_key, session):
    """**跨用户取结果不能把账记到错的人头上。**

    去掉 `Document.user_id == key.user_id` 之后，`rows[0]` 兜底会选中**别人**的
    job，而 usage 写的是当前 key 的 user —— A 的解析算在 B 头上，
    A 永远不被计费（验收实测 A=0 / B=3）。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-cross"}))
    respx.get(f"{SERVICE}/v1/parse/s-cross/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))

    # A 提交，但**不取结果**
    await client.post("/v1/parse", headers=_auth(api_key), json={
        "file_url": "https://third-party.example/cross.pdf"})

    # B 从没提交过，直接拿着同一个 task_id 取结果
    other = await _second_key(session, "eavesdropper")
    await client.get("/v1/parse/s-cross/result", headers=_auth(other))

    billed = [(u.user_id, u.pages) for u in (await session.execute(
        select(UsageRecord).where(UsageRecord.pages > 0))).scalars().all()]
    assert billed == [], f"没有人该被计费（B 没有自己的 job），实际 {billed}"


@respx.mock
async def test_external_submitter_can_delete_their_own_document(client, api_key, session):
    """**对外平面提交者要能删掉自己提交的文档。**

    `_may_delete` 只查 `document_uploads`，而对外平面建 Document 时曾经漏了
    记归属 —— 于是提交者删自己的东西得到 403，永久。更别扭的是迁移 0006
    给包括 external 在内的全部**存量**文档都补了归属，变成
    "老的删得掉、新的删不掉"。
    """
    from ddp_corpus.models import DocumentUpload

    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-own"}))
    await client.post("/v1/parse", headers=_auth(api_key), json={
        "file_url": "https://third-party.example/mine.pdf"})

    document = (await session.execute(select(Document))).scalars().one()
    uploads = (await session.execute(
        select(DocumentUpload).where(DocumentUpload.document_id == document.id))
    ).scalars().all()
    key_row = await session.get(ApiKey, api_key["id"])
    assert [u.user_id for u in uploads] == [key_row.user_id], \
        "对外平面提交也要记归属，否则提交者自己删不掉"


@respx.mock
async def test_middle_submitter_of_a_three_way_share_is_still_billed(
        client, api_key, session):
    """**三人以上共享同一份外部文档时，中间那些人也要计费。**

    验收（1b 二次）抓到：`job.api_key_id` 每次提交都被覆盖成**最后一个**提交者，
    而 `initiated_by` 只在建 job 时写入 = **第一个**发起人。三个人依次提交同一个
    URL 时，中间那位两层兜底全对不上 —— 一分钱不记，且绕过 `quota_pages`。
    人一多，中间的用户全部免费。

    第三层兜底查 `document_uploads`（"他提交过吗"）补上这个洞，
    而没提交过的人不在那张表里，所以不会重新打开"拿别人的 task_id 白嫖"那个口子
    —— 那条由 `test_polling_someone_elses_task_does_not_bill_the_wrong_person` 钉着。
    """
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-three"}))
    respx.get(f"{SERVICE}/v1/parse/s-three/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))
    url = {"file_url": "https://third-party.example/three.pdf"}

    middle = await _second_key(session, "middle")
    last = await _second_key(session, "last")

    # 依次提交：first（api_key）→ middle → last
    for who in (api_key, middle, last):
        await client.post("/v1/parse", headers=_auth(who), json=url)

    # 中间那位取结果 —— 他既不是 initiated_by 也不是最后的 api_key_id
    await client.get("/v1/parse/s-three/result", headers=_auth(middle))

    billed = {
        u.user_id: u.pages
        for u in (await session.execute(
            select(UsageRecord).where(UsageRecord.pages > 0))).scalars().all()
    }
    assert billed.get(middle["user_id"]) == 3, \
        f"中间的提交者没被计费（两层兜底都对不上他）：{billed}"
