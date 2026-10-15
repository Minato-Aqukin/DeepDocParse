"""外部 OpenAI 兼容服务（DeepSeek / Kimi / OpenAI / Claude 中继）。

三条不变式，每一条都对应一种"配了托管 API 但行为不对、且**不会报错**"的故障：

1. **鉴权头必须带上**：`api_key_env` 点名的环境变量在调用瞬间取值，
   以 `Authorization: Bearer` 发往上游 —— chat 反代、抽取、vlm-ocr、
   embeddings（含 worker 索引链与检索向量化）、rerank、两个探针，
   一条都不能漏。漏掉的那条收到的 401 会被伪装成"上游故障"。
2. **上游 model 填 adapter**：调用方发的是我们的稳定条目名，
   对端只认 adapter。`adapter or 条目名`，不改写则每个请求 404。
3. **密钥永不落地**：YAML 里只有环境变量的**名**；值不许出现在
   任何错误体、探针响应或日志行里。缺 key 是可见失败
   （同步平面 503 provider_key_missing，探针 unhealthy），绝不裸调。

上游全部 respx mock，不需要真实 key / 真实网络。
"""
import json

import httpx
import pytest
import respx
from httpx import Response

from ddp_gateway.config import (
    PROVIDER_KEY_MISSING, ModelEntry, Registry, chat_request_defaults,
    request_model_id, upstream_auth_headers,
)

KEY = "test-provider-key-value"
ENV = "TEST_PROVIDER_API_KEY"


def _provider_entry(**overrides):
    params = {
        "endpoint": "https://api.deepseek.com",
        "adapter": "deepseek-flash",
        "api_key_env": ENV,
        "capabilities": ["vision"],
    }
    params.update(overrides)
    return ModelEntry(**params)


# ------------------------------------------------------------------ helper 单测

def test_request_model_id_prefers_adapter():
    assert request_model_id("deepseek", _provider_entry()) == "deepseek-flash"
    local = ModelEntry(endpoint="http://vllm:8000")
    assert request_model_id("qwen3-4b-instruct", local) == "qwen3-4b-instruct"


def test_upstream_auth_headers_round_trip(monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    assert upstream_auth_headers(_provider_entry()) == {"Authorization": f"Bearer {KEY}"}
    # 没声明 api_key_env 的条目行为不变：空 dict，不读环境
    assert upstream_auth_headers(ModelEntry(endpoint="http://vllm:8000")) == {}


def test_upstream_auth_headers_missing_key_names_the_var_not_the_value(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(LookupError) as exc:
        upstream_auth_headers(_provider_entry())
    assert ENV in str(exc.value)
    assert KEY not in str(exc.value)


def test_api_key_env_must_look_like_an_env_var():
    with pytest.raises(ValueError, match="api_key_env"):
        ModelEntry(endpoint="http://x:8000", api_key_env="not-a-var-name")
    with pytest.raises(ValueError, match="api_key_env"):
        ModelEntry(endpoint="http://x:8000", api_key_env="9LIVES")
    # 合法的能过
    assert ModelEntry(endpoint="http://x:8000", api_key_env="_X1").api_key_env == "_X1"


def test_request_extras_must_not_shadow_the_envelope():
    for field in ("model", "messages", "stream"):
        with pytest.raises(ValueError, match="request_extras"):
            ModelEntry(endpoint="http://x:8000",
                       options={"request_extras": {field: "evil"}})
    with pytest.raises(ValueError, match="request_extras"):
        ModelEntry(endpoint="http://x:8000", options={"request_extras": ["not-a-dict"]})


def test_chat_request_defaults_merges_declared_extras_only():
    options = {
        "chat_template_kwargs": {"enable_thinking": False},
        "request_extras": {"thinking": {"type": "disabled"}},
        "transcribe_prompt": "Free OCR.",
        "token": "registry-private",
    }
    assert chat_request_defaults(options) == {
        "chat_template_kwargs": {"enable_thinking": False},
        "thinking": {"type": "disabled"},
    }
    assert chat_request_defaults({}) == {}


# ------------------------------------------------------------ chat 反代

@respx.mock
async def test_chat_proxy_sends_auth_and_adapter_model(client, app_state, monkeypatch):
    """chat 反代：带上鉴权头，上游 model 填 adapter（不是条目名）。"""
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))

    resp = await client.post("/v1/chat/completions", json={
        "model": "deepseek", "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    assert resp.status_code == 200, resp.text
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content)["model"] == "deepseek-flash"


@respx.mock
async def test_chat_proxy_decodes_compressed_upstream_body(client, app_state, monkeypatch):
    """托管 API 回 gzip（DeepSeek 实测）时，调用方拿到的必须是明文 JSON、且不带
    content-encoding —— 调用方从没声明过 gzip，原样透传压缩体就是给它一串二进制。"""
    import gzip

    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    plain = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, content=gzip.compress(plain), headers={
            "content-type": "application/json", "content-encoding": "gzip"}))

    async with client.stream("POST", "/v1/chat/completions", json={
            "model": "deepseek", "messages": [], "stream": False}) as resp:
        raw = b"".join([chunk async for chunk in resp.aiter_raw()])
        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
    assert raw == plain


@respx.mock
async def test_chat_proxy_without_api_key_env_sends_no_auth_header(client, app_state):
    """自建运行时（没声明 api_key_env）不带鉴权头 —— 别把内网调用变成"带了个空 key"。"""
    app_state.registry = Registry(vqa_models={
        "local": ModelEntry(endpoint="http://vllm:8000", capabilities=["vision"])})
    route = respx.post("http://vllm:8000/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))

    resp = await client.post("/v1/chat/completions", json={
        "model": "local", "messages": [], "stream": False})
    assert resp.status_code == 200, resp.text
    assert "Authorization" not in route.calls.last.request.headers
    assert json.loads(route.calls.last.request.content)["model"] == "local"


@respx.mock
async def test_chat_proxy_missing_key_is_503_without_calling_upstream(
        client, app_state, monkeypatch):
    """key 缺失 = 503 provider_key_missing，**且不打上游**。

    裸调只会收到对端 401，那会把"密钥没配"伪装成"运行时故障"。
    """
    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={}))

    resp = await client.post("/v1/chat/completions", json={
        "model": "deepseek", "messages": [], "stream": False})
    assert resp.status_code == 503, resp.text
    err = resp.json()["error"]
    assert err["code"] == PROVIDER_KEY_MISSING == "provider_key_missing"
    assert ENV in err["message"]
    assert KEY not in resp.text
    assert route.call_count == 0


@respx.mock
async def test_chat_proxy_request_extras_merge_with_caller_winning(client, app_state, monkeypatch):
    """request_extras 进请求体；调用方显式给的同名键优先（与 chat_template_kwargs 同规则）。"""
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry(options={
        "request_extras": {"thinking": {"type": "disabled"}, "temperature": 0}})})
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]}))

    resp = await client.post("/v1/chat/completions", json={
        "model": "deepseek", "messages": [], "stream": False, "temperature": 0.7})
    assert resp.status_code == 200, resp.text
    forwarded = json.loads(route.calls.last.request.content)
    assert forwarded["thinking"] == {"type": "disabled"}
    assert forwarded["temperature"] == 0.7
    assert forwarded["model"] == "deepseek-flash"


# ------------------------------------------------------------ embeddings

@respx.mock
async def test_embeddings_proxy_sends_auth_and_adapter_model(client, app_state, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}]}))

    resp = await client.post("/v1/embeddings", json={"input": "hi"})
    assert resp.status_code == 200, resp.text
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content)["model"] == "text-embedding-3-small"


@respx.mock
async def test_embeddings_proxy_missing_key_is_503(client, app_state, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={}))

    resp = await client.post("/v1/embeddings", json={"input": "hi"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "provider_key_missing"
    assert KEY not in resp.text
    assert route.call_count == 0


@respx.mock
async def test_retrieval_embed_query_sends_auth_and_adapter(app_state, monkeypatch):
    """抽取定位链的向量化走同一规则（默认 embedding 条目，adapter 改写 + 鉴权）。"""
    from ddp_gateway.services.retrieval import embed_query

    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}]}))

    assert await embed_query(app_state.http, app_state.registry, "q") == [0.5]
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content)["model"] == "text-embedding-3-small"


@respx.mock
async def test_retrieval_embed_query_missing_key_degrades_visibly(app_state, monkeypatch):
    """key 缺失时向量化返回 None —— 调用方据此打 embedding_unavailable，不裸调。"""
    from ddp_gateway.services.retrieval import embed_query

    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={}))

    assert await embed_query(app_state.http, app_state.registry, "q") is None
    assert route.call_count == 0


@respx.mock
async def test_chunk_and_index_sends_auth_and_adapter(worker_ctx, app_state, monkeypatch):
    """worker 分块索引链：每批 embedding 请求都带鉴权 + adapter model。"""
    from ddp_gateway.worker.tasks import chunk_and_index

    monkeypatch.setenv(ENV, KEY)
    worker_ctx["registry"] = app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    store = app_state.task_store
    await store.create("tk-provider", "m-p", "mineru", None, "dhash-provider")
    await store.save_result("tk-provider", {
        "markdown": "x",
        "layout_json": {"pdf_info": [
            {"page_idx": 0, "para_blocks": [
                {"bbox": [1, 2, 3, 4],
                 "lines": [{"spans": [{"content": "块一"}]}]}]}]},
        "images": [],
    })
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={
            "object": "list", "data": [{"index": 0, "embedding": [0.1, 0.2]}]}))

    await chunk_and_index(worker_ctx, "tk-provider")

    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content)["model"] == "text-embedding-3-small"


@respx.mock
async def test_chunk_and_index_missing_key_writes_no_vectors(
        worker_ctx, app_state, monkeypatch, capsys):
    """key 缺失时索引链停在取 key 处：不发请求、不写半截索引（只记一条无密钥日志）。"""
    from ddp_gateway.worker.tasks import chunk_and_index

    monkeypatch.delenv(ENV, raising=False)
    worker_ctx["registry"] = app_state.registry = Registry(embedding_models={
        "openai-embed": ModelEntry(endpoint="https://api.openai.com",
                                   adapter="text-embedding-3-small",
                                   api_key_env=ENV, capabilities=["dense"])})
    store = app_state.task_store
    await store.create("tk-nokey", "m-n", "mineru", None, "dhash-nokey")
    await store.save_result("tk-nokey", {
        "markdown": "x",
        "layout_json": {"pdf_info": [
            {"page_idx": 0, "para_blocks": [
                {"bbox": [1, 2, 3, 4],
                 "lines": [{"spans": [{"content": "块一"}]}]}]}]},
        "images": [],
    })
    route = respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=Response(200, json={}))

    await chunk_and_index(worker_ctx, "tk-nokey")

    assert route.call_count == 0
    assert await app_state.redis.keys("chunk:dhash-nokey:*") == []
    assert KEY not in capsys.readouterr().out


# ------------------------------------------------------------ rerank

@respx.mock
async def test_rerank_proxy_sends_auth(client, app_state, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(rerank_models={
        "cohere": ModelEntry(endpoint="https://api.cohere.com",
                             api_key_env=ENV, capabilities=["rerank"])})
    route = respx.post("https://api.cohere.com/rerank").mock(
        return_value=Response(200, json=[{"index": 0, "score": 0.9}]))

    resp = await client.post("/v1/rerank", json={"query": "q", "texts": ["a"]})
    assert resp.status_code == 200, resp.text
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"


@respx.mock
async def test_rerank_proxy_missing_key_is_503(client, app_state, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(rerank_models={
        "cohere": ModelEntry(endpoint="https://api.cohere.com",
                             api_key_env=ENV, capabilities=["rerank"])})
    route = respx.post("https://api.cohere.com/rerank").mock(
        return_value=Response(200, json=[]))

    resp = await client.post("/v1/rerank", json={"query": "q", "texts": ["a"]})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "provider_key_missing"
    assert KEY not in resp.text
    assert route.call_count == 0


# ------------------------------------------------------------ 抽取

@respx.mock
async def test_extraction_chat_sends_auth_adapter_and_extras(app_state, monkeypatch):
    """抽取的 _chat：鉴权 + adapter model + request_extras（抽值与核对同一条路）。"""
    from ddp_gateway.services import extraction

    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry(
        capabilities=["vision", "instruct"],
        options={"request_extras": {"thinking": {"type": "disabled"}}})})
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "抄写结果"}}]}))
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http,
        registry=app_state.registry, doc_hash="d" * 64, corpus=[])

    assert await extraction._chat(
        ctx, [{"role": "user", "content": "hi"}], instruct=False) == "抄写结果"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    forwarded = json.loads(sent.content)
    assert forwarded["model"] == "deepseek-flash"
    assert forwarded["thinking"] == {"type": "disabled"}


@respx.mock
async def test_extraction_missing_key_becomes_upstream_error(app_state, monkeypatch):
    """key 缺失时抽取字段判 error/upstream_error —— **绝不能是 not_found**。

    not_found 是"文档里没有"，而这里是"我们没配 key"：合成一个就是
    系统故障伪装成事实，本平面最忌讳的输出。
    """
    from ddp_core import extract_format as fmt
    from ddp_gateway.services import extraction

    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry(
        capabilities=["vision", "instruct"])})
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={}))
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http,
        registry=app_state.registry, doc_hash="d" * 64,
        corpus=[{"text": "买方：北极星", "page_idx": 0, "bbox": None,
                 "page_size": [612, 792], "block_type": "text"}])

    result = await extraction.run(ctx, fmt.parse_schema({
        "type": "object", "properties": {
            "buyer": {"type": "string", "description": "买方"}}}))

    assert route.call_count == 0
    field = result["fields"]["buyer"]
    assert field["status"] == "error", field
    assert field["degraded"] == "upstream_error", field


# ------------------------------------------------------------ vlm-ocr

@respx.mock
async def test_vlm_ocr_generic_json_sends_auth_and_only_standard_fields(monkeypatch):
    """vlm-ocr generic-json：带鉴权；请求体只有标准 OpenAI 字段 + 声明的 extras。

    vLLM 私有字段（skip_special_tokens / vllm_xargs / include_stop_str_in_output）
    发给托管 API 会直接 400 —— 这条把"一个字段都不许多发"钉死。
    """

    from ddp_gateway.services import vlm_ocr
    from ddp_paths import FIXTURES

    monkeypatch.setenv(ENV, KEY)
    entry = ModelEntry(endpoint="https://api.moonshot.cn", runtime="vlm-ocr",
                       adapter="kimi-k3", api_key_env=ENV,
                       capabilities=["parse", "vision"],
                       options={"model": "kimi-k3",
                                "request_extras": {"reasoning_effort": "low"}})
    page_content = '{"blocks": [{"type": "text", "text": "X", "bbox": [100, 100, 200, 200]}]}'
    route = respx.post("https://api.moonshot.cn/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": page_content}}]}))
    pdf = (FIXTURES / "sample.pdf").read_bytes()

    async with httpx.AsyncClient(trust_env=False) as http:
        built = await vlm_ocr.recognize(
            http, endpoint=entry.endpoint, model="kimi-k3",
            pdf_bytes=pdf, options=dict(entry.options), entry=entry)

    assert built["pdf_info"][0]["para_blocks"], built
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert set(body) == {"model", "messages", "stream", "reasoning_effort"}, body
    assert body["reasoning_effort"] == "low"


@respx.mock
async def test_vlm_ocr_missing_key_fails_loudly(monkeypatch):
    """key 缺失时 recognize 直接抛（可见失败），而不是吞进"模型不可达"。

    单页失败是静默吞的：key 缺了若跟着吞，最终报的是"模型不可达，
    或返回全为空" —— 配置错误伪装成网络错误，排查方向整个歪掉。
    """

    from ddp_gateway.services import vlm_ocr
    from ddp_paths import FIXTURES

    monkeypatch.delenv(ENV, raising=False)
    entry = _provider_entry()
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={}))
    pdf = (FIXTURES / "sample.pdf").read_bytes()

    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(RuntimeError, match=ENV):
            await vlm_ocr.recognize(
                http, endpoint=entry.endpoint, model="deepseek-flash",
                pdf_bytes=pdf, options={}, entry=entry)
    assert route.call_count == 0


@respx.mock
@pytest.mark.parametrize("options", [{}, {"dialect": "deepseek-ocr2"}],
                         ids=["generic-json", "deepseek-ocr2"])
async def test_vlm_ocr_empty_content_is_a_visible_failure_not_an_empty_layout(
        monkeypatch, options):
    """thinking 模型把 max_tokens 花在推理上、content 空着回来时，
    recognize 必须抛（可见失败），不能交出一份"有块无字"的版面。两种方言都一样。

    "有块无字"的版面能通过 recognize 的全空检查（它只看 para_blocks 有没有），
    于是下游拿到一份"解析成功、实际一个字都没有"的结果，而检索零命中时
    没有任何报错 —— 静默降级。
    """

    from ddp_gateway.services import vlm_ocr
    from ddp_paths import FIXTURES

    monkeypatch.setenv(ENV, KEY)
    entry = _provider_entry()
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "content": "", "reasoning_content": "long chain of thought..."}}]}))
    pdf = (FIXTURES / "sample.pdf").read_bytes()

    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(RuntimeError, match="没有从任何一页识别出内容"):
            await vlm_ocr.recognize(
                http, endpoint=entry.endpoint, model="deepseek-flash",
                pdf_bytes=pdf, options=options, entry=entry)


# ------------------------------------------------------------ 探针

@respx.mock
async def test_capability_probe_sends_auth_and_checks_adapter_id(
        client, app_state, monkeypatch):
    """/v1/capabilities：探针带鉴权去探，且核对 adapter id（不是条目名）。

    拿条目名去核对会**假绿**：清单里没有 `deepseek` 却有 `deepseek-flash` 时
    报 unhealthy（真实请求其实能成），反过来也一样错。
    """
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=Response(200, json={"object": "list", "data": [
            {"id": "deepseek-flash", "object": "model"}]}))

    body = (await client.get("/v1/capabilities")).json()

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"
    channels = {(c["channel"], c["model"]): c for c in body["model_channels"]}
    assert channels[("chat", "deepseek-flash")]["readiness"] == "ready"


@respx.mock
async def test_capability_probe_missing_key_is_unhealthy_without_probing(
        client, app_state, monkeypatch):
    """key 缺失时探针直接 unhealthy，连探都不用探 —— 裸探只会收到 401，
    答的不是"模型服务着吗"而是"没 key 进得去吗"。"""
    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=Response(200, json={"object": "list", "data": [
            {"id": "deepseek-flash", "object": "model"}]}))

    body = (await client.get("/v1/capabilities")).json()

    assert route.call_count == 0
    channels = {(c["channel"], c["model"]): c for c in body["model_channels"]}
    assert channels[("chat", "deepseek-flash")]["readiness"] == "unhealthy"
    assert KEY not in json.dumps(body)


@respx.mock
async def test_readyz_probe_sends_auth(client, app_state, monkeypatch):
    """/readyz：带 api_key_env 的条目带上鉴权去探；响应里永不见 key 值。"""
    monkeypatch.setenv(ENV, KEY)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=Response(200, json={"object": "list", "data": []}))

    resp = await client.get("/readyz")
    body = resp.json()

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}"
    assert body["checks"]["vqa:deepseek"] == "up", body
    assert KEY not in resp.text


@respx.mock
async def test_readyz_probe_missing_key_is_down_naming_only_the_var(
        client, app_state, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    app_state.registry = Registry(vqa_models={"deepseek": _provider_entry()})
    route = respx.get("https://api.deepseek.com/v1/models").mock(
        return_value=Response(200, json={"object": "list", "data": []}))

    resp = await client.get("/readyz")
    body = resp.json()

    assert route.call_count == 0
    assert body["checks"]["vqa:deepseek"].startswith("down "), body
    assert ENV in body["checks"]["vqa:deepseek"]
    assert KEY not in resp.text
    assert resp.status_code == 503
