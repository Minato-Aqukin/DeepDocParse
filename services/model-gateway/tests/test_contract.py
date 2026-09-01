"""契约测试 —— 两个用途：
1. 对内：固化 gateway 对 openapi.yaml 的实现（backend 联调的依据）
2. 对上游：mineru / deepseek-ocr.rs 升级版本前必须先跑绿，才允许换镜像版本

运行：cd gateway && pip install -e .[dev] && pytest
上游全部 respx mock（契约形态来自 docs/mineru-api-contract.md 的实测记录），
不需要 GPU / 容器；真环境 e2e 用 dev compose。
"""
import hashlib
import json

import pytest
import respx
from httpx import Response

from ddp_gateway.config import settings
from ddp_gateway.worker.tasks import poll_and_archive

from ddp_paths import fixture

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
    # v2：归档成功后追加分块索引链（models.yaml 注册了 embedding_models）
    assert ("chunk_and_index", task_id) in app_state.arq.jobs


@respx.mock
async def test_parse_doc_id_stabilizes_identity(client, app_state):
    """M5 契约变更：URL 每次变（预签名签名不同）但 doc_id 相同 -> 复用任务、分块键稳定。

    不修这个，backend 一上 MinIO 预签名 URL，幂等与 v2 向量索引在生产就永远失效。
    """
    routes = _mock_upstream()
    url_a, url_b = f"{FILE_URL}?X-Amz-Signature=aaa", f"{FILE_URL}?X-Amz-Signature=bbb"
    for url in (url_a, url_b):
        respx.get(url).mock(return_value=Response(200, content=b"%PDF-1.4 fake"))

    doc_id = "c0ffee" * 10
    r1 = await client.post("/v1/parse", json={"file_url": url_a, "doc_id": doc_id})
    r2 = await client.post("/v1/parse", json={"file_url": url_b, "doc_id": doc_id})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["task_id"] == r2.json()["task_id"], "同 doc_id 必须复用同一任务"
    assert routes["submit"].call_count == 1, "复用命中时不得再打 mineru"

    task_id = r1.json()["task_id"]
    task = await app_state.task_store.get(task_id)
    assert task["doc_hash"] == hashlib.sha256(doc_id.encode()).hexdigest(), \
        "分块键必须由 doc_id 决定"

    # 只拿得到裸 URL 的调用方（ask_document）必须命中同一任务，否则会重复解析
    r3 = await client.post("/v1/parse", json={"file_url": url_a})
    assert r3.json()["task_id"] == task_id, "URL 别名未生效：同一文档会被解析两次"
    assert routes["submit"].call_count == 1

    # 且必须能拿到真实身份：自己哈希 URL 算出来的键与索引键不一致
    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["doc_hash"] == task["doc_hash"], \
        "状态响应必须暴露 doc_hash，否则 URL-only 调用方检索永远零命中"
    assert status["doc_hash"] != hashlib.sha256(url_a.encode()).hexdigest()


@respx.mock
async def test_parse_without_doc_id_falls_back_to_url(client, app_state):
    """回归：不传 doc_id 时身份仍是 sha256(file_url)，不同 URL 各自成任务（mcp_server 依赖此行为）。"""
    routes = _mock_upstream()
    url_b = f"{FILE_URL}?v=2"
    respx.get(url_b).mock(return_value=Response(200, content=b"%PDF-1.4 fake"))

    r1 = await client.post("/v1/parse", json={"file_url": FILE_URL})
    r2 = await client.post("/v1/parse", json={"file_url": url_b})
    assert r1.json()["task_id"] != r2.json()["task_id"]
    assert routes["submit"].call_count == 2

    task = await app_state.task_store.get(r1.json()["task_id"])
    assert task["doc_hash"] == hashlib.sha256(FILE_URL.encode()).hexdigest()


@respx.mock
async def test_parse_queue_backpressure(client, app_state):
    """在途任务数达到 PARSE_QUEUE_MAX 时返回 429（OpenAI 风格错误体）。"""
    import time as _time

    from ddp_gateway.services.task_store import QUEUE_INFLIGHT_KEY

    await app_state.redis.zadd(
        QUEUE_INFLIGHT_KEY,
        {f"stuck-{i}": _time.time() for i in range(settings.parse_queue_max)})
    resp = await client.post("/v1/parse", json={"file_url": "https://files.example.com/other.pdf"})
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["type"] == "rate_limit_error" and err["code"] == "queue_full"


@respx.mock
async def test_queue_depth_self_heals_from_lost_releases(client, app_state, monkeypatch):
    """回归：worker 被杀导致释放丢失时，水位必须自己恢复，不能把解析平面永久锁死。

    旧实现是裸 INCR/DECR 计数器 —— 漏一次 DECR 就永久多算一个，涨到 PARSE_QUEUE_MAX
    之后 /v1/parse 恒定 429 且只能手工 DEL 键。这里模拟 200 个"受理了但从没释放"的任务。
    """
    import time as _time

    from ddp_gateway.services.task_store import QUEUE_INFLIGHT_KEY

    store = app_state.task_store
    stale = _time.time() - settings.queue_inflight_ttl - 1
    await app_state.redis.zadd(
        QUEUE_INFLIGHT_KEY, {f"abandoned-{i}": stale for i in range(settings.parse_queue_max)})

    assert await store.queue_depth() == 0, "超过 inflight_ttl 的在途成员必须被淘汰"
    _mock_upstream()
    resp = await client.post("/v1/parse", json={"file_url": FILE_URL})
    assert resp.status_code == 202, "水位自愈后必须能重新受理"


async def test_release_slot_is_idempotent(app_state):
    """回归：ARQ 重试同一任务会重复释放；按 id 记名必须让重复释放变成空操作。

    旧实现的 DECR 会把水位越减越低（要靠钳零兜底），并发下会放行超额任务。
    """
    store = app_state.task_store
    await store.create("t-1", "m-1", "mineru", None, "h-1")
    await store.create("t-2", "m-2", "mineru", None, "h-2")
    assert await store.queue_depth() == 2

    for _ in range(3):
        await store.release_slot("t-1")
    assert await store.queue_depth() == 1, "重复释放不得影响其它在途任务"


@respx.mock
async def test_removed_engine_does_not_500(client, worker_ctx, app_state):
    """回归：引擎在任务受理后被从 models.yaml 摘掉（任务还在 24h 窗口内）。

    旧实现直接 `parse_engines[task["engine"]]`，查状态 500、worker 抛 KeyError
    被 ARQ 重试 5 次后放弃 —— 任务永远停在 pending，水位也不归还。
    """
    _mock_upstream()
    task_id = (await client.post("/v1/parse", json={"file_url": FILE_URL})).json()["task_id"]
    assert await app_state.task_store.queue_depth() == 1

    app_state.registry.parse_engines.pop("mineru")

    resp = await client.get(f"/v1/parse/{task_id}")
    assert resp.status_code == 200, f"引擎摘掉后查状态不该 500：{resp.text}"
    assert resp.json()["status"] == "pending", "查不到实时状态就退回存储态"

    await poll_and_archive(worker_ctx, task_id)
    assert (await client.get(f"/v1/parse/{task_id}")).json()["status"] == "failed"
    assert await app_state.task_store.queue_depth() == 0, "落终态必须归还水位"


@respx.mock
async def test_empty_vqa_registry_is_404_not_500(client, app_state):
    """回归：vqa_models 为空时不指定 model 会走 default_of，
    旧实现在协程里抛 StopIteration —— 变成一个语焉不详的 500。"""
    app_state.registry.vqa_models.clear()
    resp = await client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_auth_required(app_state):
    """无/错 service token -> 401，统一 OpenAI 风格错误体。"""
    import httpx

    from ddp_gateway.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway", trust_env=False) as anon:
        resp = await anon.post("/v1/parse", json={"file_url": FILE_URL})
        assert resp.status_code == 401 and "error" in resp.json()
        resp = await anon.post(
            "/v1/parse", json={"file_url": FILE_URL},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401 and "error" in resp.json()
        # 所有 /v1/* 平面都必须验 token
        for path in ("/v1/chat/completions", "/v1/embeddings"):
            resp = await anon.post(path, json={})
            assert resp.status_code == 401, f"{path} 无 token 应 401"
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
    endpoint = app_state.registry.vqa_models["deepseek-ocr-2"].endpoint  # 跟随 models.yaml
    upstream = respx.post(f"{endpoint}/v1/chat/completions").mock(
        return_value=Response(200, content=SSE_BODY, headers={"content-type": "text/event-stream"})
    )

    # 1. 流式透传：SSE 字节与 content-type 原样到达
    payload = {
        "model": "deepseek-ocr-2",
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
    assert forwarded["model"] == "deepseek-ocr-2"

    # 3. 未知 model -> OpenAI 风格 404，不打上游
    before = upstream.call_count
    resp = await client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["type"] == "invalid_request_error" and err["code"] == "model_not_found"
    assert upstream.call_count == before

    # 4. 并发满载 -> 429 快速失败
    import asyncio

    from ddp_gateway.main import app as _app
    _app.state.vqa_semaphore = asyncio.Semaphore(0)
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "vqa_overloaded"

    # 5. /v1/models 来自注册表
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["deepseek-ocr-2", "qwen3-4b-instruct"]


@respx.mock
async def test_chat_stream_error_releases_semaphore(client, app_state):
    """回归（M2 验收发现的泄漏）：上游流中途断开必须归还并发 permit + 关闭连接，
    否则 VQA_MAX_CONCURRENCY 次断流后 /v1/chat/completions 永久 429。"""
    import asyncio

    import httpx

    from ddp_gateway.main import app as _app

    endpoint = app_state.registry.vqa_models["deepseek-ocr-2"].endpoint

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            raise httpx.ReadError("upstream died mid-stream")

    route = respx.post(f"{endpoint}/v1/chat/completions").mock(
        return_value=Response(200, stream=BrokenStream(),
                              headers={"content-type": "text/event-stream"})
    )

    _app.state.vqa_semaphore = asyncio.Semaphore(1)  # 容量 1：泄漏一次即封死，放大信号
    payload = {"model": "deepseek-ocr-2", "stream": True, "messages": []}
    with pytest.raises(Exception):
        await client.post("/v1/chat/completions", json=payload)

    # permit 已归还：下一个请求必须正常，而不是 429
    route.mock(return_value=Response(200, content=SSE_BODY,
                                     headers={"content-type": "text/event-stream"}))
    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.content == SSE_BODY


# ---------- M4: embedding 平面 + 分块索引链 + metrics ----------


@respx.mock
async def test_embeddings_passthrough(client, app_state):
    """OpenAI Embeddings 协议透传：默认 model 注入、未知 model 404、上游不可达 502。"""
    import httpx

    endpoint = app_state.registry.embedding_models["bge-m3"].endpoint
    upstream = respx.post(f"{endpoint}/v1/embeddings").mock(
        return_value=Response(200, json={
            "object": "list", "model": "bge-m3",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * 8}],
        }))

    resp = await client.post("/v1/embeddings", json={"input": "你好世界"})
    assert resp.status_code == 200
    assert resp.json()["data"][0]["embedding"] == [0.1] * 8
    forwarded = json.loads(upstream.calls.last.request.content)
    assert forwarded["model"] == "bge-m3", "缺省 model 必须注入注册表 default"

    before = upstream.call_count
    resp = await client.post("/v1/embeddings", json={"input": "x", "model": "nope"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"
    assert upstream.call_count == before

    upstream.mock(side_effect=httpx.ConnectError("down"))
    resp = await client.post("/v1/embeddings", json={"input": "x"})
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "embedding_unreachable"

    # 回归：非 UTF-8 / 非法 JSON body 必须是 400，不是 500
    # （UnicodeDecodeError 与 JSONDecodeError 同为 ValueError 子类，需一并捕获）
    for bad in (b"\x7b\x22input\x22\x3a\x22\xc6\xf5\x22\x7d", b"{not json"):
        for path in ("/v1/embeddings", "/v1/chat/completions"):
            resp = await client.post(path, content=bad,
                                     headers={"content-type": "application/json"})
            assert resp.status_code == 400, f"{path} 收到坏 body 应 400，实得 {resp.status_code}"
            assert resp.json()["error"]["code"] == "invalid_json"


def test_layout_chunking():
    """结构感知分块：页内合并至上限、不跨页、bbox 外接、空块跳过。"""
    from ddp_core.chunking import layout_to_chunks

    def block(text, bbox):
        return {"bbox": bbox, "lines": [{"spans": [{"content": text}]}]}

    layout = {"pdf_info": [
        {"page_idx": 0, "para_blocks": [
            block("a" * 500, [0, 0, 10, 10]),
            block("b" * 500, [5, 5, 20, 20]),   # 超 800 上限 -> 与前块分开
            block("", [0, 0, 1, 1]),            # 空块跳过
            block("c" * 100, [30, 30, 40, 40]),
        ]},
        {"page_idx": 1, "para_blocks": [block("d" * 100, [1, 1, 2, 2])]},
    ]}
    chunks = layout_to_chunks(layout, max_chars=800)
    assert [c["page_idx"] for c in chunks] == [0, 0, 1], "不得跨页合并"
    assert chunks[0]["text"] == "a" * 500
    assert chunks[1]["text"] == "b" * 500 + "\n" + "c" * 100, "页内应合并至上限"
    assert chunks[1]["bbox"] == [5, 5, 40, 40], "bbox 取外接矩形"


@pytest.mark.parametrize("placeholder", ["change-me", "", "  CHANGE-ME  "])
def test_placeholder_service_token_refuses_to_start(monkeypatch, placeholder):
    """占位 SERVICE_TOKEN 必须启动即失败。

    它是 gateway 唯一的鉴权凭据，是占位值就意味着所有 /v1/* 对任何能连上这个
    端口的人开放 —— 而运行时不会有任何异常，只是安静地没有鉴权。
    """
    from ddp_gateway.config import assert_secrets_configured

    monkeypatch.setattr(settings, "service_token", placeholder)
    with pytest.raises(RuntimeError, match="SERVICE_TOKEN"):
        assert_secrets_configured()

    monkeypatch.setattr(settings, "allow_insecure_defaults", True)
    assert_secrets_configured()      # 有逃生口，但必须显式打开


def test_single_oversized_block_is_split():
    """回归：单块超过 max_chars 必须切开。

    "与 Web 层同规则"这句话现在是**结构上成立**的：两侧 import 的是同一份
    `ddp_core.chunking`（铁律 7），不再是两份需要人工对齐的实现。

    不切的话它会被原样送进 embedding 运行时，由后者按模型最大长度静默截断——
    块尾内容从此检索不到，且全程没有报错。
    """
    from ddp_core.chunking import layout_to_chunks

    body = "这是一段很长的正文。" * 200      # 2000 字
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"bbox": [10, 10, 100, 500], "lines": [{"spans": [{"content": body}]}]}]}]}

    chunks = layout_to_chunks(layout, max_chars=300)
    assert len(chunks) > 1, "超长块没有被切开"
    assert all(len(c["text"]) <= 300 for c in chunks), \
        f"切完仍有超限块：{[len(c['text']) for c in chunks]}"
    assert "".join(c["text"] for c in chunks).replace("\n", "") == body, "切分不得丢内容"
    assert all(c["page_idx"] == 0 and c["bbox"] == [10, 10, 100, 500]
               and c["page_size"] == [612, 792] for c in chunks), "出处三件套要跟着每一段"


@respx.mock
async def test_chunk_and_index_worker(worker_ctx, app_state):
    """worker 分块索引链：分块 -> 批量 embedding（按 index 排序）-> 写 chunk 键（带 TTL）。
    fakeredis 无 RediSearch：ensure_chunk_index 须优雅返回 False 而非崩溃。"""
    from ddp_gateway.worker.tasks import chunk_and_index

    import struct

    store = app_state.task_store
    await store.create("tk9", "m-9", "mineru", None, "dhash9")
    # 两块分处两页 -> 两个 chunk（分块不跨页），才能验证按 index 归位
    await store.save_result("tk9", {
        "markdown": "x",
        "layout_json": {"pdf_info": [
            {"page_idx": 0, "para_blocks": [
                {"bbox": [1, 2, 3, 4], "lines": [{"spans": [{"content": "块一"}]}]}]},
            {"page_idx": 1, "para_blocks": [
                {"bbox": [5, 6, 7, 8], "lines": [{"spans": [{"content": "块二"}]}]}]},
        ]},
        "images": [],
    })
    endpoint = app_state.registry.embedding_models["bge-m3"].endpoint
    respx.post(f"{endpoint}/v1/embeddings").mock(
        return_value=Response(200, json={"object": "list", "data": [
            {"index": 1, "embedding": [0.2, 0.2]},   # 故意乱序，验证按 index 归位
            {"index": 0, "embedding": [0.1, 0.1]},
        ]}))

    await chunk_and_index(worker_ctx, "tk9")

    keys = sorted([k.decode() if isinstance(k, bytes) else k
                   for k in await app_state.redis.keys("chunk:dhash9:*")])
    assert len(keys) == 2, "两页各一个 chunk"
    first = await app_state.redis.hgetall("chunk:dhash9:0")
    first = {(k.decode() if isinstance(k, bytes) else k): v for k, v in first.items()}
    text = first["text"].decode() if isinstance(first["text"], bytes) else first["text"]
    assert text == "块一" and int(first["page_idx"]) == 0
    assert await app_state.redis.ttl(keys[0]) > 0, "chunk 必须带 TTL（可重建缓存）"
    assert len(first["vec"]) == 2 * 4, "FLOAT32 打包长度 = dim*4 字节"
    # index=0 的向量必须归到第 0 个 chunk，而不是按返回顺序拿到 [0.2, 0.2]
    assert struct.unpack("<2f", first["vec"]) == pytest.approx((0.1, 0.1))


@respx.mock
async def test_chunk_and_index_batches_embeddings(worker_ctx, app_state, monkeypatch):
    """回归：chunk 数超过 embedding_batch_size 必须分批请求（TEI 的 max-client-batch-size
    会整批拒绝超限请求），且向量按原顺序拼接、与 chunk 一一对应。"""
    from ddp_gateway.config import settings
    from ddp_gateway.worker.tasks import chunk_and_index

    monkeypatch.setattr(settings, "embedding_batch_size", 2)

    # 5 个页面各一个 chunk（不跨页合并）-> batch=2 时应产生 3 次请求（2+2+1）
    pages = [{"page_idx": i, "para_blocks": [
        {"bbox": [0, 0, 10, 10], "lines": [{"spans": [{"content": f"页 {i} 的内容"}]}]},
    ]} for i in range(5)]
    store = app_state.task_store
    await store.create("tkb", "m-b", "mineru", None, "dhashb")
    await store.save_result("tkb", {"markdown": "x", "layout_json": {"pdf_info": pages},
                                    "images": []})

    endpoint = app_state.registry.embedding_models["bge-m3"].endpoint
    counter = {"n": 0}

    def respond(request):
        texts = json.loads(request.content)["input"]
        assert len(texts) <= 2, "单次请求条数不得超过 embedding_batch_size"
        counter["n"] += 1
        # 乱序返回，逼实现依赖 index 而非返回顺序
        data = [{"index": i, "embedding": [float(counter["n"]), float(i)]}
                for i in reversed(range(len(texts)))]
        return Response(200, json={"object": "list", "data": data})

    respx.post(f"{endpoint}/v1/embeddings").mock(side_effect=respond)

    await chunk_and_index(worker_ctx, "tkb")

    assert counter["n"] == 3, f"5 chunk / batch=2 应发 3 次请求，实际 {counter['n']}"
    keys = [k.decode() if isinstance(k, bytes) else k
            for k in await app_state.redis.keys("chunk:dhashb:*")]
    assert len(keys) == 5, "5 个 chunk 都必须写入索引"
    # 第 5 个 chunk（第 3 批第 1 条）的向量应为 [3.0, 0.0]——证明分批后未错位
    import struct
    vec = await app_state.redis.hget("chunk:dhashb:4", "vec")
    assert struct.unpack("<2f", vec) == (3.0, 0.0), "分批拼接后向量与 chunk 顺序必须对齐"


async def test_save_chunks_clears_stale(app_state):
    """回归（M4 验收发现）：重解析后 chunk 变少时，旧下标的残留键仍会被检索命中。"""
    store = app_state.task_store
    await store.save_chunks("dh", [{"text": f"c{i}", "page_idx": i, "bbox": None,
                                    "page_size": [612, 792]} for i in range(4)],
                            [[0.1, 0.2]] * 4)
    assert len(await app_state.redis.keys("chunk:dh:*")) == 4
    await store.save_chunks("dh", [{"text": "only", "page_idx": 0, "bbox": None,
                                    "page_size": [612, 792]}], [[0.3, 0.4]])
    keys = [k.decode() if isinstance(k, bytes) else k
            for k in await app_state.redis.keys("chunk:dh:*")]
    assert keys == ["chunk:dh:0"], f"旧分块必须清掉，实际残留 {sorted(keys)}"


async def test_metrics_exposed(client):
    """M4 可观测：/metrics 暴露 Prometheus 指标（无鉴权，内网抓取）。"""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert b"http" in resp.content


async def test_parse_engine_defaults_to_registry_default(client, app_state):
    """不传 engine 时取注册表里标了 default 的那条，而不是写死的 "mineru"。

    这条防的是无 GPU 路径整条断掉：models.cpu.yaml 只注册 borndigital，
    路由层一旦把 "mineru" 写成请求模型的默认值，所有缺省请求都会 404 unknown_engine，
    而 default: true 标记形同虚设 —— M7 的 CPU quickstart 因此从来没真正通过过。
    """
    from ddp_gateway.config import ModelEntry, Registry

    app_state.registry = Registry(parse_engines={
        "borndigital": ModelEntry(endpoint="inproc://borndigital",
                                  runtime="borndigital", default=True),
    })
    resp = await client.post("/v1/parse", json={"file_url": FILE_URL})
    assert resp.status_code == 202, resp.text
    task = await app_state.task_store.get(resp.json()["task_id"])
    assert task["engine"] == "borndigital", "落库的引擎名必须是注册表选出来的那个"


async def test_parse_explicit_unknown_engine_still_404(client, app_state):
    """显式点名一个没注册的引擎仍然是调用方的错，不能被"缺省回落"悄悄吞掉。"""
    from ddp_gateway.config import ModelEntry, Registry

    app_state.registry = Registry(parse_engines={
        "borndigital": ModelEntry(endpoint="inproc://borndigital",
                                  runtime="borndigital", default=True),
    })
    resp = await client.post("/v1/parse", json={"file_url": FILE_URL, "engine": "mineru"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_engine"


async def test_parse_empty_registry_reports_unknown_engine(client, app_state):
    """一个引擎都没注册时给明确错误，而不是 500（default_of 会抛 LookupError）。"""
    from ddp_gateway.config import Registry

    app_state.registry = Registry(parse_engines={})
    resp = await client.post("/v1/parse", json={"file_url": FILE_URL})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_engine"
