"""对外 API 与 MCP 代理的契约：验 key -> 限速/额度 -> 换 token 转发 -> 计量。

对第三方而言这层必须"透明"：错误体、流式、MCP 会话头都要与直连 service 时一致。
"""
import json
from datetime import timedelta

import httpx
import respx
from sqlalchemy import select

from app.config import settings
from app.models import ApiKey, Task, UsageRecord, utcnow
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

    task = (await session.execute(select(Task))).scalars().one()
    assert task.object_key == "" and task.service_task_id == "s-9", "外部任务不归档，仅留记录"
    assert task.filename == "a.pdf"

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
    """同一用户既从 Web 传过、又用 key 提交过同一份文档时，本层会有两行指向同一个
    service 任务。取结果必须照常计量，而不是撞上"查出多行"直接 500（真机 e2e 抓到过）。"""
    respx.post(f"{SERVICE}/v1/parse").mock(
        return_value=httpx.Response(202, json={"task_id": "s-shared"}))
    respx.get(f"{SERVICE}/v1/parse/s-shared/result").mock(
        return_value=httpx.Response(200, json={"markdown": "# x", "layout_json": LAYOUT,
                                               "images": []}))
    key_row = await session.get(ApiKey, api_key["id"])

    # 先造一行"Web 上传"的任务，再用 key 提交一次 —— 两行同 service_task_id
    web_task = Task(user_id=key_row.user_id, doc_id="web-doc", filename="a.pdf",
                    object_key="sources/x/a.pdf", service_task_id="s-shared",
                    status="succeeded", page_count=3)
    session.add(web_task)
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
