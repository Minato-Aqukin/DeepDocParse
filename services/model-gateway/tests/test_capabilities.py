"""`GET /v1/capabilities` —— 网关声明的能力必须与它真会做的事一致。

这套用例的重点全在**负向**：能力清单出错的方式几乎都是"多说了"，而多说的
后果在联邦里是把任务派到一台干不了这活的机器上，且不报错。所以下面每条
正向断言都配着一条"少了这个条件就必须不 ready / 必须不出现"。

覆盖：
- 网关**不执行**的 operation（编译 / 带出处问答 / Wiki / 检索）不许出现在
  profiles 里；它们只以模型通道的形式出现，由 corpus-api 自己组合；
- 注册 ≠ 就绪：探测失败、model id 对不上、Redis 不通都不是 ready；
- `no_instruct` 不能冒充遵指令；
- 选路不看健康：默认条目死了，健康的兄弟条目不能把它救成 ready；
- 产出的每一条 profile 都能通过契约 schema（`ddp-discovery/1`）。

上游全部 respx mock，不需要 GPU / 容器。
"""
import json

import httpx
import jsonschema
import pytest
import respx
from httpx import Response

from ddp_gateway.config import ModelEntry, Registry
from ddp_gateway.main import app
from ddp_paths import CONTRACTS

INSTRUCT = ModelEntry(endpoint="http://chat:8000", capabilities=["instruct"])
VISION_OCR = ModelEntry(endpoint="http://ocr:8000", capabilities=["vision", "no_instruct"])
DENSE = ModelEntry(endpoint="http://embed:8080", capabilities=["dense"])
BORNDIGITAL = ModelEntry(endpoint="inproc://borndigital", runtime="borndigital",
                         capabilities=["parse"])

#: OpenAI 运行时的 `/v1/models`。**探针要在这里找到自己会请求的那个 id**，
#: 所以夹具必须带上真实形状，不能拿空 200 糊过去。
def models_body(*ids: str) -> dict:
    return {"object": "list",
            "data": [{"id": i, "object": "model", "owned_by": "test"} for i in ids]}


def _profile_schema() -> dict:
    """契约 schema 的 CapabilityProfile 分支（注入枚举后的成品）。"""
    bundle = json.loads((CONTRACTS / "generated" / "schemas-resolved.json")
                        .read_text(encoding="utf-8"))
    discovery = bundle["schemas"]["ddp-discovery/v1.json"]
    return {"$defs": discovery["$defs"], "$ref": "#/$defs/CapabilityProfile"}


def assert_contract_shaped(body: dict) -> None:
    """每条 profile 都必须是契约里的 CapabilityProfile。

    schema 是 `additionalProperties: false` 的，所以这条也顺带钉死"不许发明
    契约外的字段"（想表达降级只能用 profile 名字，没有 degraded 字段可用）。
    `node_id` 由 control-api 注入，这里补一个假的再校验。
    """
    schema = _profile_schema()
    for profile in body["profiles"]:
        assert "node_id" not in profile, "node_id 由 control-api 注入，网关不得自带"
        jsonschema.validate({**profile, "node_id": "node-test"}, schema)


def _by_operation(body: dict) -> dict[str, dict]:
    return {p["operation"]: p for p in body["profiles"]}


def _channels(body: dict) -> dict[tuple[str, str], dict]:
    return {(c["channel"], c["model"]): c for c in body["model_channels"]}


@respx.mock
async def test_only_operations_the_gateway_executes_are_profiles(client, app_state):
    """网关有端点的三条才进 profiles；模型通道另算。

    **这是本轮修的主缺陷**：此前 `doc.compile` / `rag.answer.cited` /
    `wiki.pages` / `corpus.retrieve` 全在这里，而网关一行编译、问答、检索
    代码都没有 —— 那是拿"手上有个视觉模型"冒充"我会编译整份文档"，
    拿"embedding 活着"冒充"我这儿的 SQL 索引能检索"。
    """
    app_state.registry = Registry(
        parse_engines={"borndigital": BORNDIGITAL},
        vqa_models={"qwen": INSTRUCT, "ocr": VISION_OCR},
        embedding_models={"bge-m3": DENSE},
    )
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("qwen")))
    respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("ocr")))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))

    resp = await client.get("/v1/capabilities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capability_status"] == "observed"
    assert set(_by_operation(body)) == {"doc.parse", "extract.fields"}, \
        "网关没有编译/问答/Wiki/检索的端点，不得声明这些 operation"
    assert_contract_shaped(body)
    for profile in body["profiles"]:
        assert profile["accepting_admissions"] is False
        assert profile["schema"] == "ddp-discovery/1#CapabilityProfile"

    # 模型通道是给 corpus-api 组合用的原料，不是能力声明
    channels = _channels(body)
    assert channels[("chat", "qwen")]["supports"] == {"instruct": True, "vision": False}
    assert channels[("chat", "ocr")]["supports"] == {"instruct": False, "vision": True}
    assert channels[("embedding", "bge-m3")]["readiness"] == "ready"


@respx.mock
async def test_no_instruct_model_cannot_serve_extract_fields(client, app_state):
    """只注册 OCR 专用模型时，抽取平面没有可用模型 —— 不许声明 extract.fields。

    这是项目原有那个坑的联邦版本：OCR 模型抽不出值会被记成 `not_found`，
    系统能力缺失伪装成"文档里没有"。
    """
    app_state.registry = Registry(vqa_models={"ocr": VISION_OCR})
    respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("ocr")))

    body = (await client.get("/v1/capabilities")).json()
    assert "extract.fields" not in _by_operation(body)
    # 但通道要如实报出来：它看得见图，只是不听指令
    assert _channels(body)[("chat", "ocr")]["supports"] == {"instruct": False, "vision": True}


@respx.mock
async def test_no_instruct_beats_instruct_when_both_declared(client, app_state):
    """矛盾配置（两个词都写）必须按 no_instruct 处理。

    正常条目只会二选一；这条防的是"两个词都写上"时，抽值挑中一个只会抄字的
    模型 —— 后果是假的 not_found 与假引用。
    """
    app_state.registry = Registry(vqa_models={
        "confused": ModelEntry(endpoint="http://chat:8000",
                               capabilities=["instruct", "no_instruct"]),
    })
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("confused")))

    body = (await client.get("/v1/capabilities")).json()
    assert "extract.fields" not in _by_operation(body)
    assert _channels(body)[("chat", "confused")]["supports"]["instruct"] is False


@respx.mock
async def test_legacy_entry_without_capabilities_counts_as_instruct(client, app_state):
    """没写 capabilities 的老条目被段名补成 `[vision]`，而抽取平面照样会挑中它。

    判据必须是"没写 no_instruct"而不是"写了 instruct" —— 否则这条能力会被
    报成不存在，而 `/v1/extract` 明明在用它。**方向相反的谎也是谎。**
    """
    app_state.registry = Registry(vqa_models={
        "legacy": ModelEntry(endpoint="http://chat:8000"),      # capabilities 留空
    })
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("legacy")))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["readiness"] == "ready"


@pytest.mark.parametrize("status", [401, 403, 404, 302, 500])
@respx.mock
async def test_non_2xx_health_is_not_ready(client, app_state, status):
    """401/404/重定向/5xx 都不是 ready。**这正是不能复用 readyz 判据的理由**：
    readyz 把 `<500` 都算 up，会把这些全报成健康。"""
    app_state.registry = Registry(vqa_models={"qwen": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(status, headers={"location": "http://elsewhere"}))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["readiness"] == "unhealthy"


@respx.mock
async def test_timeout_is_not_ready(client, app_state):
    app_state.registry = Registry(vqa_models={"qwen": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(
        side_effect=httpx.ConnectTimeout("upstream slow"))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["readiness"] == "unhealthy"


@respx.mock
async def test_runtime_serving_another_model_is_not_ready(client, app_state):
    """200 但 `/v1/models` 里没有我们会请求的 id —— 必须不 ready。

    真机上这就是 `--served-model-name` 写错：运行时活得好好的，每一次真实
    请求却是 404 `model_not_found`（`infra/autodl/README.md` 的排障表有这条）。
    只看状态码的探针会把它报成完全可用。
    """
    app_state.registry = Registry(vqa_models={"qwen": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("some-other-model")))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["readiness"] == "unhealthy"
    assert _channels(body)[("chat", "qwen")]["readiness"] == "unhealthy"


@respx.mock
@pytest.mark.parametrize("payload", [
    {"object": "list"},                       # 没有 data
    {"data": {"id": "qwen"}},                 # data 不是数组
    {"data": ["qwen"]},                       # 元素不是对象
    {"data": [{"name": "qwen"}]},             # 没有 id
])
async def test_unparseable_model_list_is_not_ready(client, app_state, payload):
    """模型清单形状不对时不许放行 —— 宽容的后果是把"活着但服务着别的模型"报成 ready。"""
    app_state.registry = Registry(vqa_models={"qwen": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(return_value=Response(200, json=payload))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["readiness"] == "unhealthy"


@respx.mock
async def test_vlm_ocr_probe_matches_options_model_not_entry_name(client, app_state):
    """`vlm-ocr` 请求时填的是 `options.model`，探针就得核对那个 id。

    拿条目名去核对会**假绿**：清单里没有 `vlm-ocr` 却有 `deepseek-ocr-2` 时
    报 unhealthy（真实请求其实能成），反过来也一样错。
    """
    app_state.registry = Registry(parse_engines={"vlm-ocr": ModelEntry(
        endpoint="http://ocr:8000", runtime="vlm-ocr", capabilities=["parse", "vision"],
        options={"model": "deepseek-ocr-2"})})
    respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("deepseek-ocr-2")))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["doc.parse"]["readiness"] == "ready"


@respx.mock
async def test_vlm_ocr_probe_fails_when_runtime_serves_entry_name_only(client, app_state):
    """同一条目，运行时只服务"条目名"时必须不 ready（真实请求会 404）。"""
    app_state.registry = Registry(parse_engines={"vlm-ocr": ModelEntry(
        endpoint="http://ocr:8000", runtime="vlm-ocr", capabilities=["parse", "vision"],
        options={"model": "deepseek-ocr-2"})})
    respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("vlm-ocr")))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["doc.parse"]["readiness"] == "unhealthy"


@respx.mock
async def test_healthy_sibling_does_not_rescue_a_dead_default(client, app_state):
    """默认条目死了，健康的兄弟条目不能把 operation 救成 ready。

    真实选路（`default_of`）只看 default 标记与能力词，**完全不看健康** ——
    所以"任取一个健康条目"式的实现是在承诺一件不会发生的事：请求照样会被
    发到那个死掉的默认条目上。
    """
    app_state.registry = Registry(vqa_models={
        "dead-default": ModelEntry(endpoint="http://chat:8000", default=True,
                                   capabilities=["instruct"]),
        "healthy-other": ModelEntry(endpoint="http://spare:8000", capabilities=["instruct"]),
    })
    respx.get("http://chat:8000/v1/models").mock(side_effect=httpx.ConnectError("down"))
    respx.get("http://spare:8000/v1/models").mock(
        return_value=Response(200, json=models_body("healthy-other")))

    body = (await client.get("/v1/capabilities")).json()
    extract = _by_operation(body)["extract.fields"]
    assert extract["readiness"] == "unhealthy"
    assert extract["profile"] == "dead-default", "profile 要指向真会被挑中的那个条目"
    # 通道里两条都如实列出，谁健康谁不健康由消费方自己看
    channels = _channels(body)
    assert channels[("chat", "dead-default")]["readiness"] == "unhealthy"
    assert channels[("chat", "dead-default")]["default"] is True
    assert channels[("chat", "healthy-other")]["readiness"] == "ready"
    assert channels[("chat", "healthy-other")]["default"] is False


async def test_inprocess_parse_is_ready_without_network(client, app_state):
    """进程内 CPU 解析（borndigital）没有远端可探，就绪性等于本进程。

    不套 `@respx.mock`：这条路径**一个 HTTP 请求都不该发**。真发了会去连
    inproc:// 或某个不存在的地址，测试会以连接错误暴露出来。
    """
    app_state.registry = Registry(parse_engines={"borndigital": BORNDIGITAL})

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["doc.parse"]["readiness"] == "ready"


async def test_task_store_down_makes_queued_planes_unhealthy(client, app_state):
    """Redis 不通时解析/抽取受理不了 —— 模型再健康也不是 ready。

    受理的第一步是读在途水位（`routers/parse.py` / `routers/extract.py`），
    Redis 挂了那一步就抛。只探模型的实现会把这台机器报成完全可用，而它
    连任务都收不下。
    """
    class DeadStore:
        async def queue_depth(self):
            raise ConnectionError("redis down")

    app_state.registry = Registry(parse_engines={"borndigital": BORNDIGITAL})
    app_state.task_store = DeadStore()

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["doc.parse"]["readiness"] == "unhealthy"


@respx.mock
async def test_rerank_does_not_depend_on_task_store(client, app_state):
    """重排是同步透传，不进队列 —— 不能被 Redis 连坐。"""
    class DeadStore:
        async def queue_depth(self):
            raise ConnectionError("redis down")

    app_state.registry = Registry(rerank_models={
        "bge-reranker": ModelEntry(endpoint="http://rerank:8080", runtime="tei",
                                   capabilities=["rerank"])})
    app_state.task_store = DeadStore()
    respx.get("http://rerank:8080/health").mock(return_value=Response(200))

    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["rerank"]["readiness"] == "ready"


@respx.mock
async def test_engine_versions_only_when_registry_declares_them(client, app_state):
    """`engine_versions` 只填注册表明写的模型标识，不编造版本号。"""
    app_state.registry = Registry(vqa_models={
        "declared": ModelEntry(endpoint="http://chat:8000", capabilities=["instruct"],
                               options={"model": "qwen3-4b-instruct"}),
    })
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("declared")))
    body = (await client.get("/v1/capabilities")).json()
    assert _by_operation(body)["extract.fields"]["engine_versions"] == \
        {"model": "qwen3-4b-instruct"}

    app_state.registry = Registry(vqa_models={"plain": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("plain")))
    body = (await client.get("/v1/capabilities")).json()
    assert "engine_versions" not in _by_operation(body)["extract.fields"]


@respx.mock
async def test_limits_come_from_real_config(client, app_state):
    """限额只报真实配置项。

    **`doc.parse` 故意没有 max_concurrency**：`PARSE_QUEUE_MAX` 是在途水位
    上限而不是并发度，把它报成 max_concurrency 等于告诉 planner 这台机器
    能同时解析 200 份。抽取报的是抽取链自己的并发，不是 chat 反代的信号量。
    """
    from ddp_gateway.config import settings

    app_state.registry = Registry(parse_engines={"borndigital": BORNDIGITAL},
                                  vqa_models={"qwen": INSTRUCT})
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("qwen")))

    body = (await client.get("/v1/capabilities")).json()
    by_op = _by_operation(body)
    assert "limits" not in by_op["doc.parse"]
    assert by_op["extract.fields"]["limits"] == {
        "max_concurrency": settings.extract_concurrency,
        "max_candidates": settings.extract_candidates}
    # chat 反代的信号量是**通道**的限额（语料侧的问答/编译都从这道闸过）
    assert _channels(body)[("chat", "qwen")]["limits"] == {
        "max_concurrency": settings.vqa_max_concurrency}


@respx.mock
async def test_same_endpoint_and_model_is_probed_once(client, app_state):
    """同一个 endpoint + 同一个 model id 只探一次（模型容器的健康检查不便宜）。"""
    shared = ModelEntry(endpoint="http://ocr:8000", capabilities=["vision", "no_instruct"],
                        options={"model": "deepseek-ocr-2"})
    app_state.registry = Registry(
        parse_engines={"vlm-ocr": ModelEntry(
            endpoint="http://ocr:8000", runtime="vlm-ocr", capabilities=["parse", "vision"],
            options={"model": "deepseek-ocr-2"})},
        vqa_models={"deepseek-ocr-2": shared},
    )
    route = respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("deepseek-ocr-2")))

    assert (await client.get("/v1/capabilities")).status_code == 200
    assert route.call_count == 1, "同一个 (endpoint, model) 不该被探两次"


@respx.mock
async def test_shipped_registry_projects_without_fabricating(client, app_state):
    """随仓库发布的 models.yaml（conftest 装的就是它）跑一遍，形状必须合契约。

    它同时钉住一件真实的事：默认 vqa 条目是 OCR 专用模型，所以**抽取平面
    的候选是那个纯文本 instruct 条目**，而不是默认条目。
    """
    respx.get("http://vqa-dsocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("deepseek-ocr-2")))
    respx.get("http://chat-instruct:8000/v1/models").mock(
        return_value=Response(200, json=models_body("qwen3-4b-instruct")))
    respx.get("http://mineru:8000/health").mock(return_value=Response(200))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))

    body = (await client.get("/v1/capabilities")).json()
    assert_contract_shaped(body)
    by_op = _by_operation(body)
    assert by_op["extract.fields"]["profile"] == "qwen3-4b-instruct"
    assert by_op["doc.parse"]["profile"] == "mineru"
    # 默认 chat 条目是 OCR 专用模型 —— 语料侧不指定 model 时拿到的就是它
    default_chat = next(c for c in body["model_channels"]
                        if c["channel"] == "chat" and c["default"])
    assert default_chat["model"] == "deepseek-ocr-2"
    assert default_chat["supports"] == {"instruct": False, "vision": True}


@respx.mock
async def test_response_matches_the_openapi_contract(client, app_state):
    """整个响应体（含 model_channels）必须能过契约里的 200 schema。

    `scripts/check_contract.py` 只管路径与方法这一层，响应形状是这里的活 ——
    两者互补。schema 是 `additionalProperties: false` 的，所以偷偷长出一个
    契约外的字段会在这里红。
    """
    import yaml

    spec = yaml.safe_load((CONTRACTS / "openapi" / "gateway-v1.yaml")
                          .read_text(encoding="utf-8"))
    schema = (spec["paths"]["/v1/capabilities"]["get"]["responses"]["200"]
              ["content"]["application/json"]["schema"])

    app_state.registry = Registry(
        parse_engines={"borndigital": BORNDIGITAL},
        vqa_models={"qwen": INSTRUCT, "ocr": VISION_OCR},
        embedding_models={"bge-m3": DENSE},
        rerank_models={"bge-reranker": ModelEntry(endpoint="http://rerank:8080",
                                                  runtime="tei", capabilities=["rerank"])},
    )
    respx.get("http://chat:8000/v1/models").mock(
        return_value=Response(200, json=models_body("qwen")))
    respx.get("http://ocr:8000/v1/models").mock(side_effect=httpx.ConnectError("down"))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))
    respx.get("http://rerank:8080/health").mock(return_value=Response(404))

    body = (await client.get("/v1/capabilities")).json()
    jsonschema.validate(body, schema)
    # 契约把 operation 枚举收窄到网关真有端点的三条，实现不许超出
    assert set(_by_operation(body)) <= {"doc.parse", "extract.fields", "rerank"}


@respx.mock
async def test_every_readiness_is_a_contract_enum_value(client, app_state):
    """产出的每个 readiness 都必须是 enums.yaml 里的取值（三种语言共用那份）。"""
    from ddp_contracts.enums import CAPABILITY_READINESS_VALUES

    app_state.registry = Registry(parse_engines={"borndigital": BORNDIGITAL},
                                  vqa_models={"qwen": INSTRUCT, "ocr": VISION_OCR},
                                  embedding_models={"bge-m3": DENSE})
    respx.get("http://chat:8000/v1/models").mock(side_effect=httpx.ConnectError("down"))
    respx.get("http://ocr:8000/v1/models").mock(
        return_value=Response(200, json=models_body("ocr")))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))

    body = (await client.get("/v1/capabilities")).json()
    values = set(CAPABILITY_READINESS_VALUES)
    assert {p["readiness"] for p in body["profiles"]} <= values
    assert {c["readiness"] for c in body["model_channels"]} <= values


async def test_capabilities_requires_service_token(app_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway",
                                 trust_env=False) as anon:
        resp = await anon.get("/v1/capabilities")
        assert resp.status_code == 401
