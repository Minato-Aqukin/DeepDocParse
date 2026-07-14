"""契约测试 —— 两个用途：
1. 对内：固化 gateway 对 openapi.yaml 的实现（backend 联调的依据）
2. 对上游：mineru / deepseek-ocr.rs 升级版本前必须先跑绿，才允许换镜像版本

运行：cd gateway && pip install -e .[dev] && pytest
上游全部 respx mock（契约形态来自 docs/mineru-api-contract.md 的实测记录），
不需要 GPU / 容器；真环境 e2e 用 dev compose。
"""
import json

import pytest
import respx
from httpx import Response

from app.config import settings
from app.worker.tasks import poll_and_archive

MINERU = "http://mineru:8000"
FILE_URL = "https://files.example.com/sample.pdf"

# mineru middle_json 的最小骨架：块必须带页码 + bbox（ask_document 与 v2 索引依赖）
MIDDLE_JSON = {
    "pdf_info": [
        {
            "page_idx": 0,
            "page_size": [612, 792],
            "para_blocks": [
                {"type": "text", "bbox": [72, 72, 540, 100], "lines": []},
                {"type": "table", "bbox": [72, 120, 540, 300], "lines": []},
            ],
        }
    ]
}


def _mock_upstream() -> dict:
    """注册 mineru mock 路由，返回可再配置的 route 句柄。"""
    routes = {
        "file": respx.get(FILE_URL).mock(return_value=Response(200, content=b"%PDF-1.4 fake")),
        "submit": respx.post(f"{MINERU}/tasks").mock(
            return_value=Response(202, json={"task_id": "m-1", "status": "pending"})
        ),
        "status": respx.get(f"{MINERU}/tasks/m-1").mock(
            return_value=Response(200, json={"task_id": "m-1", "status": "processing"})
        ),
        "result": respx.get(f"{MINERU}/tasks/m-1/result").mock(
            return_value=Response(
                200,
                json={
                    "task_id": "m-1",
                    "status": "completed",
                    "results": {
                        "sample.pdf": {
                            "md_content": "# 标题\n\n正文与表格。",
                            "middle_json": json.dumps(MIDDLE_JSON, ensure_ascii=False),
                            "images": {"img_0.png": "data:image/png;base64,iVBORw0KGgo="},
                        }
                    },
                },
            )
        ),
        "callback": respx.post("http://backend/callback").mock(return_value=Response(200)),
    }
    return routes


@respx.mock
async def test_parse_lifecycle(client, worker_ctx, app_state, monkeypatch):
    """提交 -> 202 + task_id -> 轮询至 succeeded -> result 含 markdown/layout_json/images。
    断言 layout_json 中块带页码与 bbox（ask_document 与 v2 索引依赖此结构）。"""
    monkeypatch.setattr(settings, "poll_initial_delay", 0.01)
    monkeypatch.setattr(settings, "poll_max_delay", 0.02)
    routes = _mock_upstream()

    # 1. 受理（不传 options：backend=pipeline 应来自注册表引擎默认，验证合并逻辑）
    resp = await client.post("/v1/parse", json={
        "file_url": FILE_URL,
        "callback_url": "http://backend/callback",
    })
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]
    submit_body = routes["submit"].calls.last.request.content
    assert b'name="backend"' in submit_body and b"pipeline" in submit_body, \
        "注册表引擎默认 options 必须透传给 mineru"
    assert app_state.arq.jobs == [("poll_and_archive", task_id)], "受理必须只入队一次后处理链"
    assert routes["submit"].called, "必须转发到 mineru"
    # 幂等：同 file_url 再提交返回同一 task_id，不再打 mineru
    resp2 = await client.post("/v1/parse", json={"file_url": FILE_URL})
    assert resp2.status_code == 202 and resp2.json()["task_id"] == task_id
    assert routes["submit"].call_count == 1

    # 2. 状态透传（mineru processing -> 契约 running）
    resp = await client.get(f"/v1/parse/{task_id}")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "running" and 0 <= body["progress"] <= 1

    # 结果未就绪时取 result -> 409
    resp = await client.get(f"/v1/parse/{task_id}/result")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "result_not_ready"

    # 3. mineru 完成，worker 驱动归档
    routes["status"].mock(return_value=Response(200, json={"task_id": "m-1", "status": "completed"}))
    # 归档前：mineru 已 completed 但结果未落地 -> 对外必须仍是 running（status=succeeded ⇒ result 可取）
    resp = await client.get(f"/v1/parse/{task_id}")
    assert resp.json()["status"] == "running"
    await poll_and_archive(worker_ctx, task_id)

    # 4. 终态 + 结果形态
    resp = await client.get(f"/v1/parse/{task_id}")
    assert resp.json()["status"] == "succeeded"
    resp = await client.get(f"/v1/parse/{task_id}/result")
    assert resp.status_code == 200
    result = resp.json()
    assert result["markdown"].startswith("# 标题")
    page = result["layout_json"]["pdf_info"][0]
    assert page["page_idx"] == 0
    assert all(len(block["bbox"]) == 4 for block in page["para_blocks"])
    assert result["images"] == [{"name": "img_0.png", "url": "data:image/png;base64,iVBORw0KGgo="}]
    # 回调通知 backend（带 service token）
    assert routes["callback"].called
    cb_req = routes["callback"].calls.last.request
    assert cb_req.headers["Authorization"] == f"Bearer {settings.service_token}"
    assert json.loads(cb_req.content) == {"task_id": task_id, "status": "succeeded"}
    # 队列水位归零
    assert await app_state.task_store.queue_depth() == 0


@respx.mock
async def test_parse_queue_backpressure(client, app_state):
    """queue_depth 达到 PARSE_QUEUE_MAX 时返回 429（OpenAI 风格错误体）。"""
    await app_state.redis.set("queue_depth", settings.parse_queue_max)
    resp = await client.post("/v1/parse", json={"file_url": "https://files.example.com/other.pdf"})
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["type"] == "rate_limit_error" and err["code"] == "queue_full"


async def test_auth_required(app_state):
    """无/错 service token -> 401，统一 OpenAI 风格错误体。"""
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway", trust_env=False) as anon:
        resp = await anon.post("/v1/parse", json={"file_url": FILE_URL})
        assert resp.status_code == 401 and "error" in resp.json()
        resp = await anon.post(
            "/v1/parse", json={"file_url": FILE_URL},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401 and "error" in resp.json()
        # 探针不需要鉴权
        resp = await anon.get("/healthz")
        assert resp.status_code == 200


SSE_BODY = (
    b'data: {"id":"c-1","choices":[{"delta":{"content":"Answer"}}]}\n\n'
    b'data: {"id":"c-1","choices":[{"delta":{"content":": 42"}}]}\n\n'
    b"data: [DONE]\n\n"
)


@respx.mock
async def test_chat_completions_openai_compat(client, app_state):
    """image_url + text 的标准 OpenAI 请求可用；流式 SSE 原样透传；未知 model -> 404。"""
    endpoint = app_state.registry.vqa_models["deepseek-ocr"].endpoint  # 跟随 models.yaml
    upstream = respx.post(f"{endpoint}/v1/chat/completions").mock(
        return_value=Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})
    )

    # 1. 流式透传：SSE 字节与 content-type 原样到达
    payload = {
        "model": "deepseek-ocr",
        "stream": True,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            {"type": "text", "text": "图里写了什么？"},
        ]}],
    }
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/event-stream"
    assert resp.content == SSE_BODY

    # 2. 省略 model -> 注入注册表 default 后转发
    resp = await client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 200
    forwarded = json.loads(upstream.calls.last.request.content)
    assert forwarded["model"] == "deepseek-ocr"

    # 3. 未知 model -> OpenAI 风格 404，不打上游
    before = upstream.call_count
    resp = await client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["type"] == "invalid_request_error" and err["code"] == "model_not_found"
    assert upstream.call_count == before

    # 4. 并发满载 -> 429 快速失败
    import asyncio

    from app.main import app as _app
    _app.state.vqa_semaphore = asyncio.Semaphore(0)
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "vqa_overloaded"

    # 5. /v1/models 来自注册表
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["deepseek-ocr"]


@respx.mock
async def test_chat_stream_error_releases_semaphore(client, app_state):
    """回归（M2 验收发现的泄漏）：上游流中途断开必须归还并发 permit + 关闭连接，
    否则 VQA_MAX_CONCURRENCY 次断流后 /v1/chat/completions 永久 429。"""
    import asyncio

    import httpx

    from app.main import app as _app

    endpoint = app_state.registry.vqa_models["deepseek-ocr"].endpoint

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            raise httpx.ReadError("upstream died mid-stream")

    route = respx.post(f"{endpoint}/v1/chat/completions").mock(
        return_value=Response(200, stream=BrokenStream(),
                              headers={"content-type": "text/event-stream"})
    )

    _app.state.vqa_semaphore = asyncio.Semaphore(1)  # 容量 1：泄漏一次即封死，放大信号
    payload = {"model": "deepseek-ocr", "stream": True, "messages": []}
    with pytest.raises(Exception):
        await client.post("/v1/chat/completions", json=payload)

    # permit 已归还：下一个请求必须正常，而不是 429
    route.mock(return_value=Response(200, content=SSE_BODY,
                                     headers={"content-type": "text/event-stream"}))
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.content == SSE_BODY


# ---------- M3: MCP ask_document（gateway 交互用 respx mock，gateway 行为由上面的测试保证） ----------

GW = "http://gw.test"


def _ask_fn():
    import server as mcp_server

    tool = mcp_server.ask_document
    return getattr(tool, "fn", tool)  # fastmcp FunctionTool -> 原始协程函数


@pytest.fixture
def mcp_gateway(monkeypatch):
    import server as mcp_server

    monkeypatch.setattr(mcp_server, "GATEWAY", GW)
    return mcp_server


@respx.mock
async def test_ask_document_retry_pattern(mcp_gateway):
    """未解析的大文档：首次调用返回'解析中'提示；解析完成后再调用返回带出处答案。"""
    ask = _ask_fn()
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t1"}))
    status = respx.get(f"{GW}/v1/parse/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "running",
                                         "progress": 0.5, "error": None}))

    out1 = await ask("http://files.example.com/big.pdf", "总收入是多少？")
    assert "解析中" in out1 and "t1" in out1 and "重试" in out1

    # 稍后重试：同 URL 幂等复用任务，已 succeeded -> 短文档全文作证据
    status.mock(return_value=Response(200, json={"task_id": "t1", "status": "succeeded",
                                                 "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t1/result").mock(
        return_value=Response(200, json={"markdown": "# 财报\n\n总收入 42 亿元。",
                                         "layout_json": {}, "images": []}))
    out2 = await ask("http://files.example.com/big.pdf", "总收入是多少？")
    assert "总收入 42 亿元" in out2 and "出处" in out2 and "/v1/parse/t1/result" in out2


@respx.mock
async def test_ask_document_image_direct(mcp_gateway):
    """图片 URL：不走解析平面，直接 VQA 秒回。"""
    ask = _ask_fn()
    png = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                        "01f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
    respx.get("http://files.example.com/chart.png").mock(
        return_value=Response(200, content=png, headers={"content-type": "image/png"}))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "图中数值为 42"}}]}))
    parse_route = respx.post(f"{GW}/v1/parse")

    out = await ask("http://files.example.com/chart.png", "图中数值？")
    assert "图中数值为 42" in out and "出处" in out
    assert vqa.called and not parse_route.called


@respx.mock
async def test_ask_document_bm25_crop_verify(mcp_gateway):
    """长文档：BM25 命中版面块（带页码+bbox）-> 裁剪原 PDF 区域 -> VQA 验证 -> 带出处返回。"""
    from pathlib import Path

    ask = _ask_fn()
    pdf_bytes = (Path(__file__).parent / "fixtures" / "sample.pdf").read_bytes()
    layout = {"pdf_info": [{
        "page_idx": 0,
        "page_size": [612, 792],
        "para_blocks": [
            {"type": "title", "bbox": [69, 71, 375, 98],
             "lines": [{"spans": [{"content": "DeepDocParse contract test"}]}]},
            {"type": "text", "bbox": [69, 118, 200, 140],
             "lines": [{"spans": [{"content": "The answer to everything is 42"}]}]},
        ],
    }]}
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t2"}))
    respx.get(f"{GW}/v1/parse/t2").mock(
        return_value=Response(200, json={"task_id": "t2", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t2/result").mock(
        return_value=Response(200, json={"markdown": "x" * 4000, "layout_json": layout,
                                         "images": []}))
    respx.get("http://files.example.com/long.pdf").mock(
        return_value=Response(200, content=pdf_bytes))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "42"}}]}))

    out = await ask("http://files.example.com/long.pdf", "what is the answer to everything")
    assert "42" in out and "第 1 页" in out and "bbox" in out
    assert "视觉验证" in out, "首个命中块必须走裁剪+VQA 验证路径"
    assert vqa.called
    # 发给 VQA 的必须是裁剪出的 PNG data URI
    sent = json.loads(vqa.calls.last.request.content)
    image_part = sent["messages"][0]["content"][0]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")


@respx.mock
async def test_ask_document_untokenizable_doc_no_crash(mcp_gateway):
    """回归（M3 验收发现）：全部版面块 token 化为空（非中英文种/全符号）时
    不得让 BM25 除零崩溃，应退化为开头片段。"""
    ask = _ask_fn()
    layout = {"pdf_info": [{
        "page_idx": 0,
        "page_size": [612, 792],
        "para_blocks": [
            {"type": "text", "bbox": [10, 10, 100, 30],
             "lines": [{"spans": [{"content": "안녕하세요 세계"}]}]},
            {"type": "text", "bbox": [10, 40, 100, 60],
             "lines": [{"spans": [{"content": "©®™ ★☆ ……"}]}]},
        ],
    }]}
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t3"}))
    respx.get(f"{GW}/v1/parse/t3").mock(
        return_value=Response(200, json={"task_id": "t3", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t3/result").mock(
        return_value=Response(200, json={"markdown": "허" * 4000, "layout_json": layout,
                                         "images": []}))

    out = await ask("http://files.example.com/korean.pdf", "질문")
    assert "开头片段" in out and "완전" not in out  # 正常返回退化文案，而不是崩溃
