"""`GET /internal/capabilities` —— 语料侧能力清单是**组合**出来的，不是转发。

这套用例的重点同样在负向。语料侧多说一句的后果比网关更重：control-api 会把
这份清单当成本节点的对外能力发布出去，planner 据此派活。

覆盖：
- 只按 HTTP 契约消费网关（不可达 / 非 2xx / 格式不符 / 它自己报 unknown -> 整份 unknown）；
- **检索的就绪度来自本层的检索库，不是 embedding 的健康**；
- **编译不能只凭"有个视觉模型"就说整份编译可用**；OCR 专用模型顶不上编译的指令；
- `no_instruct` 不能冒充遵指令；
- 独立配置的 chat/embedding/rerank 端点不继承网关就绪度；
- 上游过期观测 / 不认识的 readiness / 越界字段不许透传成 ready；
- 端点只接受服务身份，profile 形状合契约 schema。
"""
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema
import pytest
import respx
from sqlalchemy import text

import ddp_corpus.db as db
from ddp_corpus.config import settings
from ddp_paths import CONTRACTS
from tests.conftest import SERVICE, actor_headers

GATEWAY_CAP = f"{SERVICE}/v1/capabilities"


@pytest.fixture(autouse=True)
def _neutral_channel_config(monkeypatch):
    """默认形态：三条通道都走网关，本层不指定 model。

    conftest 为编译指纹用例把 `chat_model` 设成了一个假名字，那会让每条
    用例都撞上"网关没有这个 model"的分支 —— 显式归零，让每条用例只测它
    自己那件事。
    """
    for key in ("chat_url", "embedding_url", "rerank_url", "chat_model",
                "embedding_model", "rerank_model"):
        monkeypatch.setattr(settings, key, "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def gw_profile(operation: str, readiness: str = "ready", **extra) -> dict:
    now = _now()
    return {
        "schema": "ddp-discovery/1#CapabilityProfile",
        "operation": operation,
        "profile": "upstream-engine",
        "readiness": readiness,
        "accepting_admissions": False,
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
        **extra,
    }


def gw_channel(channel: str, model: str, *, readiness: str = "ready",
               default: bool = True, **supports) -> dict:
    now = _now()
    return {
        "channel": channel, "model": model, "profile": model, "default": default,
        "readiness": readiness, "supports": supports,
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
    }


#: 常用通道：会遵指令的纯文本模型 / OCR 专用模型 / dense embedding
CHAT_INSTRUCT = gw_channel("chat", "qwen3-4b-instruct", instruct=True, vision=False)
CHAT_OCR = gw_channel("chat", "deepseek-ocr-2", instruct=False, vision=True)
CHAT_VISION_INSTRUCT = gw_channel("chat", "qwen3-vl", instruct=True, vision=True)
EMBED = gw_channel("embedding", "bge-m3", dense=True)


def gateway_body(*, profiles=(), channels=(), status: str = "observed") -> dict:
    return {"capability_status": status, "profiles": list(profiles),
            "model_channels": list(channels)}


def mock_gateway(**kwargs) -> None:
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json=gateway_body(**kwargs)))


async def fetch(client, headers) -> dict:
    resp = await client.get("/internal/capabilities", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def by_operation(client, headers) -> dict[str, dict]:
    body = await fetch(client, headers)
    assert_contract_shaped(body)
    return {p["operation"]: p for p in body["profiles"]}


def assert_contract_shaped(body: dict) -> None:
    """每条 profile 都必须是契约里的 CapabilityProfile（`additionalProperties: false`）。

    `node_id` 由 control-api 注入，这里补一个假的再校验 —— 顺带钉住"语料侧
    不得自带 node_id"和"不得发明契约外的字段"。
    """
    bundle = json.loads((CONTRACTS / "generated" / "schemas-resolved.json")
                        .read_text(encoding="utf-8"))
    discovery = bundle["schemas"]["ddp-discovery/v1.json"]
    schema = {"$defs": discovery["$defs"], "$ref": "#/$defs/CapabilityProfile"}
    for profile in body["profiles"]:
        assert "node_id" not in profile, "node_id 由 control-api 注入，语料侧不得自带"
        # accepting_admissions 现在是真话（开关 + 检索库 + 该操作就绪），
        # 具体取值由 test_accepting_admissions_is_truthful 钉住，这里只查形态。
        assert isinstance(profile["accepting_admissions"], bool)
        jsonschema.validate({**profile, "node_id": "node-test"}, schema)


# ---------------------------------------------------------------- 组合语义

@respx.mock
async def test_composes_local_operations_not_just_relays(client, service_client_headers):
    """网关只声明它自己执行的三条；编译/问答/Wiki/检索由本层组合出来。"""
    mock_gateway(profiles=[gw_profile("doc.parse")],
                 channels=[CHAT_VISION_INSTRUCT, EMBED])

    by_op = await by_operation(client, service_client_headers)
    assert set(by_op) == {"doc.parse", "doc.compile", "rag.answer.cited",
                          "extract.fields", "wiki.pages", "corpus.retrieve"}
    assert by_op["doc.parse"]["readiness"] == "ready"
    assert by_op["doc.parse"]["profile"] == "upstream-engine"
    assert by_op["corpus.retrieve"]["readiness"] == "ready"
    assert by_op["corpus.retrieve"]["profile"] == "hybrid"
    assert by_op["doc.compile"]["profile"] == "with_vision"


@respx.mock
async def test_retrieval_readiness_comes_from_the_store_not_the_embedder(
        client, service_client_headers, engine):
    """**embedding 健康 ≠ 索引可检索。**

    检索真正要读的是本层的 `chunks`；embedding 只决定走不走向量路
    （不可用时关键词路照常工作，`search.py` 打 `embedding_unavailable`）。
    把 embedding 的健康当成检索的就绪度，是"注册即就绪"在检索平面上的变体。
    """
    mock_gateway(channels=[EMBED, CHAT_INSTRUCT])
    # 库能查 -> ready，且因为 embedding 在 -> hybrid
    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["readiness"] == "ready"
    assert by_op["corpus.retrieve"]["profile"] == "hybrid"

    # 库查不了（迁移没跑 / PG 挂了）-> 必须 unhealthy，尽管 embedding 仍然 ready
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE chunks"))
    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["readiness"] == "unhealthy"
    for operation in ("rag.answer.cited", "extract.fields", "wiki.pages"):
        # chat 模型是健康的（上面给了 CHAT_INSTRUCT），所以这几条只能是被
        # 检索库拖下去的 —— 带出处的生成没有检索就没有出处
        assert by_op[operation]["readiness"] == "unhealthy", operation


@respx.mock
async def test_embedding_down_degrades_to_keyword_only_but_stays_ready(
        client, service_client_headers):
    """向量路不可用只降 profile，不把 readiness 抬走也不把它抹掉。

    检索仍然能跑（关键词路），所以报 unhealthy 是另一个方向的谎；但必须
    在契约字段上看得见 —— `CapabilityProfile` 没有 degraded 字段，
    能承载它的只有 `profile`。
    """
    mock_gateway(channels=[gw_channel("embedding", "bge-m3", readiness="unhealthy",
                                      dense=True)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["readiness"] == "ready"
    assert by_op["corpus.retrieve"]["profile"] == "keyword_only"
    assert "engine_versions" not in by_op["corpus.retrieve"], \
        "向量路没工作就不要挂 embedding 模型名"


@respx.mock
async def test_compile_does_not_claim_full_compile_from_a_vision_only_model(
        client, service_client_headers):
    """**只有 OCR 专用模型时，编译不能宣称视觉那一档。**

    编译的视觉步骤发的是一句"只输出 JSON"的指令（`compilation.py`），
    OCR 专用模型会继续抄字 -> `vision_invalid_output`。所以要求
    vision + instruct 两个词；只有 vision 时降到 `text_only`。
    """
    mock_gateway(channels=[CHAT_OCR])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.compile"]["profile"] == "text_only"
    assert by_op["doc.compile"]["readiness"] == "ready", "文本原子照常编译"


@respx.mock
async def test_compile_with_vision_requires_both_words(client, service_client_headers):
    """既看得见图又听得懂指令时才是 `with_vision`；缺哪个词都不算。"""
    mock_gateway(channels=[CHAT_VISION_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.compile"]["profile"] == "with_vision"

    mock_gateway(channels=[CHAT_INSTRUCT])       # 听得懂指令但看不见图
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.compile"]["profile"] == "text_only"


@respx.mock
async def test_compile_switch_off_is_text_only_even_with_a_perfect_model(
        client, service_client_headers, monkeypatch):
    """本层开关关着时，上游多健康都不改变这条路不通的事实。"""
    monkeypatch.setattr(settings, "compile_vision_enabled", False)
    mock_gateway(channels=[CHAT_VISION_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.compile"]["profile"] == "text_only"


@respx.mock
async def test_ocr_only_chat_cannot_serve_generation_operations(
        client, service_client_headers):
    """默认 chat 模型是 OCR 专用时，问答/抽取/Wiki 必须 unhealthy。

    这是随仓库发布的注册表的真实形态（默认 vqa 条目是 deepseek-ocr-2），
    而本层不指定 model 就会拿到它。报 ready 的后果正是这个项目吃过的亏：
    抽不出来被记成 `not_found`，系统能力缺失伪装成"文档里没有"。

    **不是 unknown**：我们确实观测到了这个模型，只是它干不了这活 ——
    观测到的否定结论要说成否定。
    """
    mock_gateway(channels=[CHAT_OCR])
    by_op = await by_operation(client, service_client_headers)
    for operation in ("rag.answer.cited", "extract.fields", "wiki.pages"):
        assert by_op[operation]["readiness"] == "unhealthy", operation
    assert by_op["doc.compile"]["readiness"] == "ready", "编译的文本档不受影响"


@respx.mock
async def test_no_chat_model_at_all_is_not_advertised(client, service_client_headers):
    """一个 chat 模型都没有 = 本节点不做这些 operation，不列（也不编造 configured）。"""
    mock_gateway(channels=[EMBED])
    by_op = await by_operation(client, service_client_headers)
    for operation in ("rag.answer.cited", "extract.fields", "wiki.pages"):
        assert operation not in by_op
    assert by_op["corpus.retrieve"]["readiness"] == "ready", "检索不依赖 chat"


@respx.mock
async def test_configured_chat_model_must_match_a_gateway_channel(
        client, service_client_headers, monkeypatch):
    """本层指定了 `CHAT_MODEL` 时，网关必须真有这个名字。

    对不上时真实请求是 404 `model_not_found`；退回去报 default 的健康
    等于替一个不会发生的请求作证。
    """
    monkeypatch.setattr(settings, "chat_model", "qwen3-vl")
    mock_gateway(channels=[CHAT_INSTRUCT])       # 网关只有另一个模型
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["readiness"] == "unhealthy"

    monkeypatch.setattr(settings, "chat_model", "qwen3-4b-instruct")
    mock_gateway(channels=[CHAT_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["readiness"] == "ready"


@respx.mock
async def test_unflagged_default_channel_is_unknown_not_ready(
        client, service_client_headers):
    """本层不指定 model 而网关没标 default -> 不知道会拿到谁，报 unknown。

    按数组顺序猜"第一个就是 default"是在替网关的选路作证。
    """
    mock_gateway(channels=[gw_channel("chat", "qwen3-4b-instruct", default=False,
                                      instruct=True, vision=False)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["readiness"] == "unknown"


@respx.mock
async def test_verification_variant_needs_a_vision_instruct_model(
        client, service_client_headers, monkeypatch):
    """出处视觉核对做不了时不许叫 `verified`（那是一张假的验证章）。"""
    monkeypatch.setattr(settings, "qa_verify_parse", True)
    mock_gateway(channels=[CHAT_INSTRUCT])       # 看不见图 -> 核对不了
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["profile"] == "unverified"

    mock_gateway(channels=[CHAT_VISION_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["profile"] == "verified"

    monkeypatch.setattr(settings, "qa_verify_parse", False)
    mock_gateway(channels=[CHAT_VISION_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["profile"] == "unverified", "开关关着就不是已核对"


# ------------------------------------------------- 独立端点：观测不到就 unknown

@respx.mock
async def test_independent_chat_endpoint_does_not_inherit_gateway_readiness(
        client, service_client_headers, monkeypatch):
    """独立配置的 chat 端点：网关的 ready 说的是网关自己的模型，与本层无关。"""
    monkeypatch.setattr(settings, "chat_url", "http://independent:1234/v1/chat/completions")
    mock_gateway(profiles=[gw_profile("doc.parse")],
                 channels=[CHAT_VISION_INSTRUCT, EMBED])

    by_op = await by_operation(client, service_client_headers)
    for operation in ("rag.answer.cited", "extract.fields", "wiki.pages"):
        assert by_op[operation]["readiness"] == "unknown", operation
    assert by_op["doc.compile"]["profile"] == "text_only", \
        "看不见的视觉能力不能算 with_vision"
    assert by_op["doc.parse"]["readiness"] == "ready", "不依赖 chat 的操作仍然观测得到"
    assert by_op["corpus.retrieve"]["profile"] == "hybrid"


@respx.mock
async def test_independent_embedding_endpoint_does_not_inherit_gateway_readiness(
        client, service_client_headers, monkeypatch):
    monkeypatch.setattr(settings, "embedding_url", "http://independent:1234/v1/embeddings")
    mock_gateway(profiles=[gw_profile("doc.parse")], channels=[CHAT_INSTRUCT, EMBED])

    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["profile"] == "keyword_only", \
        "观测不到的向量路不能算在内"
    assert by_op["corpus.retrieve"]["readiness"] == "ready"
    assert by_op["rag.answer.cited"]["readiness"] == "ready"


@respx.mock
async def test_disabled_rerank_is_not_advertised(client, service_client_headers,
                                                 monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", False)
    mock_gateway(profiles=[gw_profile("rerank")],
                 channels=[gw_channel("rerank", "bge-reranker-v2-m3", rerank=True)])
    by_op = await by_operation(client, service_client_headers)
    assert "rerank" not in by_op, "本层没开重排就不该宣称这条能力"


@respx.mock
async def test_enabled_rerank_follows_the_channel(client, service_client_headers,
                                                  monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", True)
    mock_gateway(channels=[gw_channel("rerank", "bge-reranker-v2-m3", rerank=True)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rerank"]["readiness"] == "ready"

    # 网关一个 rerank 模型都没有 -> 请求必然 404，这条能力不存在
    mock_gateway(channels=[CHAT_INSTRUCT])
    by_op = await by_operation(client, service_client_headers)
    assert "rerank" not in by_op

    # 段里躺着一个不会重排的条目：健康归健康，不能拿它充数
    mock_gateway(channels=[gw_channel("rerank", "not-a-reranker", rerank=False)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rerank"]["readiness"] == "unhealthy"


@respx.mock
async def test_enabled_independent_rerank_is_unknown(client, service_client_headers,
                                                     monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_url", "http://independent:1234/v1/rerank")
    mock_gateway(channels=[gw_channel("rerank", "bge-reranker-v2-m3", rerank=True)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rerank"]["readiness"] == "unknown"


# ------------------------------------------- 上游证据：过期 / 未知 / 越界不透传

@respx.mock
async def test_expired_upstream_observation_is_not_relayed_as_ready(
        client, service_client_headers):
    """过期记录不是当前能力证明（§5.5）—— 不许按最后一次成功当成现在可用。"""
    stale = _now() - timedelta(minutes=10)
    mock_gateway(profiles=[{
        **gw_profile("doc.parse"),
        "observed_at": (stale - timedelta(seconds=60)).isoformat(),
        "valid_until": stale.isoformat(),
    }])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.parse"]["readiness"] == "unknown"


@respx.mock
async def test_future_dated_upstream_observation_is_not_trusted(
        client, service_client_headers):
    """时间戳在未来（时钟错了或上游在编）同样不算证据。"""
    ahead = _now() + timedelta(hours=1)
    mock_gateway(profiles=[{
        **gw_profile("doc.parse"),
        "observed_at": ahead.isoformat(),
        "valid_until": (ahead + timedelta(seconds=60)).isoformat(),
    }])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.parse"]["readiness"] == "unknown"


@respx.mock
@pytest.mark.parametrize("readiness", ["healthy", "OK", "", None, True, "configured"])
async def test_unknown_upstream_readiness_becomes_unknown(
        client, service_client_headers, readiness):
    """契约枚举之外的取值（以及"只是配置过"）一律按未知处理，绝不当 ready。"""
    mock_gateway(profiles=[gw_profile("doc.parse", readiness=readiness)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.parse"]["readiness"] == "unknown"


@respx.mock
async def test_naive_timestamps_are_not_trusted(client, service_client_headers):
    """没有时区的时间戳无法比较，不猜它是哪个时区。"""
    now = _now().replace(tzinfo=None)
    mock_gateway(profiles=[{
        **gw_profile("doc.parse"),
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
    }])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.parse"]["readiness"] == "unknown"


@respx.mock
async def test_out_of_contract_upstream_fields_are_dropped_not_relayed(
        client, service_client_headers):
    """上游的坏字段丢掉，不原样吐出去。

    control 对整份观测是全有全无：一条 profile 不合法就整份判 unknown，
    一个越界的 limits 会把整个节点的能力清单连坐成"状态未知"。
    """
    mock_gateway(profiles=[gw_profile(
        "doc.parse",
        engine_versions={"model": None, "backend": "pipeline"},
        limits={"max_concurrency": -1, "max_candidates": 0, "max_queue": 200},
        internal_secret="must-not-echo")])

    body = await fetch(client, service_client_headers)
    assert_contract_shaped(body)                       # schema 是 additionalProperties: false
    parse = next(p for p in body["profiles"] if p["operation"] == "doc.parse")
    assert parse["engine_versions"] == {"backend": "pipeline"}
    assert "limits" not in parse
    assert "must-not-echo" not in json.dumps(body)


@respx.mock
async def test_gateway_saying_unknown_is_not_laundered_into_observed(
        client, service_client_headers):
    """网关自己说没有有效证据时，它的 profiles 不算证据。

    透传的后果是把"不知道"洗成"知道"；control 对同一件事的判据更严
    （unknown 带着 profiles 直接判整份无效），洗过的清单会连整份一起废掉。
    """
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json=gateway_body(
        profiles=[gw_profile("doc.parse")], channels=[CHAT_INSTRUCT], status="unknown")))
    resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


@respx.mock
async def test_missing_channel_list_is_unknown_not_absent(client, service_client_headers):
    """网关没给通道清单（老版本网关）-> 模型侧一律 unknown，不猜、也不静默漏掉。"""
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(
        200, json={"capability_status": "observed",
                   "profiles": [gw_profile("doc.parse")]}))
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["readiness"] == "unknown"
    assert by_op["doc.compile"]["profile"] == "text_only"
    assert by_op["corpus.retrieve"]["readiness"] == "ready", "本层的检索不受影响"


async def test_gateway_failure_is_unknown_and_empty(client, service_client_headers):
    """网关 401 -> 不猜，返回空 profiles + unknown。"""
    with respx.mock:
        respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(401))
        resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.status_code == 200
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


@respx.mock
async def test_gateway_404_is_unknown(client, service_client_headers):
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(404))
    resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


@respx.mock
async def test_gateway_timeout_is_unknown(client, service_client_headers):
    respx.get(GATEWAY_CAP).mock(side_effect=httpx.ConnectTimeout("down"))
    resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


@respx.mock
async def test_gateway_malformed_body_is_unknown(client, service_client_headers):
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={"nope": 1}))
    resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


@respx.mock
async def test_gateway_redirect_is_not_followed(client, service_client_headers):
    """禁止重定向：不能拿别处的响应当网关的健康。"""
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(
        302, headers={"location": "http://elsewhere/v1/capabilities"}))
    resp = await client.get("/internal/capabilities", headers=service_client_headers)
    assert resp.json() == {"profiles": [], "capability_status": "unknown"}


# ------------------------------------------------------------------ 其它

@respx.mock
async def test_draining_upstream_is_merged_not_crashed(client, service_client_headers):
    """上游给的是契约枚举里的任何一个值，不只我们内部用的那两三种。

    `draining` 漏进合并表的后果不是判断错，是抛 ValueError -> 500 ->
    上层把这台机器记成"不可达"，比一个诚实的 unhealthy 难查得多。
    """
    mock_gateway(profiles=[gw_profile("doc.parse", readiness="draining")],
                 channels=[gw_channel("chat", "qwen3-4b-instruct", readiness="draining",
                                      instruct=True, vision=False)])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["doc.parse"]["readiness"] == "draining"
    assert by_op["rag.answer.cited"]["readiness"] == "draining"
    assert by_op["corpus.retrieve"]["readiness"] == "ready", "本层的检索与上游排空无关"


@respx.mock
async def test_non_string_upstream_profile_name_is_dropped(client, service_client_headers):
    """上游的 `profile` 不是字符串时丢掉，不 `str()` 出一坨东西发出去。"""
    mock_gateway(profiles=[{**gw_profile("doc.parse"), "profile": {"nope": 1}}])
    body = await fetch(client, service_client_headers)
    assert_contract_shaped(body)
    parse = next(p for p in body["profiles"] if p["operation"] == "doc.parse")
    assert "profile" not in parse


@respx.mock
async def test_malformed_channel_entries_do_not_break_the_endpoint(
        client, service_client_headers):
    """上游给了畸形通道项 -> 跳过它，不 500。

    这条路挂在 control 的握手请求上，一个坏字段不该让整台机器的能力查询
    变成服务端错误（那会被上层记成"节点不可达"，比 unknown 更难查）。
    """
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={
        "capability_status": "observed", "profiles": [],
        "model_channels": ["chat", None, 7, {"channel": "chat"}, CHAT_INSTRUCT]}))
    by_op = await by_operation(client, service_client_headers)
    assert by_op["rag.answer.cited"]["readiness"] == "ready"


@respx.mock
async def test_output_satisfies_the_control_side_temporal_rules(
        client, service_client_headers):
    """control 对整份观测是全有全无：任一条 profile 过期/时间戳反了就整份判
    unknown。所以本层产出的每条都必须 `observed_at <= now < valid_until`，
    **且用的是本层自己的观测时刻**，不是上游那条的时间。"""
    old = _now() - timedelta(minutes=5)
    mock_gateway(profiles=[{**gw_profile("doc.parse"),
                            "observed_at": old.isoformat(),
                            "valid_until": (old + timedelta(minutes=30)).isoformat()}],
                 channels=[CHAT_INSTRUCT])
    body = await fetch(client, service_client_headers)
    now = _now()
    for profile in body["profiles"]:
        observed = datetime.fromisoformat(profile["observed_at"])
        valid_until = datetime.fromisoformat(profile["valid_until"])
        assert observed <= now + timedelta(seconds=1), profile["operation"]
        assert valid_until > now, profile["operation"]
        assert valid_until > observed, profile["operation"]
        assert observed > old + timedelta(minutes=1), "不得沿用上游的观测时刻"


@respx.mock
async def test_every_readiness_is_a_contract_enum_value(client, service_client_headers):
    from ddp_contracts.enums import CAPABILITY_READINESS_VALUES

    mock_gateway(profiles=[gw_profile("doc.parse")],
                 channels=[CHAT_OCR, EMBED])
    body = await fetch(client, service_client_headers)
    assert {p["readiness"] for p in body["profiles"]} <= set(CAPABILITY_READINESS_VALUES)


@respx.mock
async def test_accepting_admissions_is_truthful(client, service_client_headers,
                                                monkeypatch, engine):
    """接单声明必须是真话：开关、检索库、该 operation、受理范围四者缺一即 false。

    恒 false 的写法让能力声明与 admission 端点的实际行为分离；恒 true 更糟
    （收单再失败）。**执行器受理 `corpus.retrieve` 与 `rag.answer.cited`**
    （P5 的 answer 委托）—— 其它 operation 报 true 等于让对账方按一个不存在
    的接单口去派活。
    """
    mock_gateway(channels=[CHAT_INSTRUCT, EMBED])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["accepting_admissions"] is True
    assert by_op["rag.answer.cited"]["accepting_admissions"] is True
    assert {name for name, profile in by_op.items()
            if profile["accepting_admissions"]} == {"corpus.retrieve", "rag.answer.cited"}, \
        "只有 admission 执行器真正受理的 operation 才能报接单"

    # 生成不通（OCR 专用模型不听指令）时 rag.answer.cited 必须收回接单声明，
    # 检索路不受影响 —— 这正是"能力缺失不许洗成 ready"的那条判据。
    mock_gateway(channels=[CHAT_OCR, EMBED])
    by_op = await by_operation(client, service_client_headers)
    assert by_op["corpus.retrieve"]["accepting_admissions"] is True
    assert by_op["rag.answer.cited"]["accepting_admissions"] is False

    monkeypatch.setattr(settings, "federation_admissions_enabled", False)
    body = await fetch(client, service_client_headers)
    assert body["profiles"] and all(p["accepting_admissions"] is False for p in body["profiles"])

    monkeypatch.setattr(settings, "federation_admissions_enabled", True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE chunks"))
    body = await fetch(client, service_client_headers)
    assert body["profiles"] and all(p["accepting_admissions"] is False for p in body["profiles"])


async def test_requires_service_actor(client):
    # 没有身份 -> 401
    assert (await client.get("/internal/capabilities")).status_code == 401
    # 用户身份 -> 403
    resp = await client.get("/internal/capabilities",
                            headers=actor_headers("actor-a", kind="user"))
    assert resp.status_code == 403


def test_helper_never_imports_gateway_registry_code():
    """语料侧只按 HTTP 契约消费网关，不 import 它的内部实现。"""
    import ddp_corpus.capabilities as capabilities

    source = pathlib.Path(capabilities.__file__).read_text(encoding="utf-8")
    assert "ddp_gateway" not in source


async def test_store_probe_reads_the_table_retrieval_reads(engine):
    """库活着但迁移没跑过时 `SELECT 1` 照样成功 —— 所以探针打的是 `chunks`。"""
    from ddp_corpus.capabilities import observe_store

    assert await observe_store() == "ready"
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE chunks"))
    assert await observe_store() == "unhealthy"
    # 库本身还在（`SELECT 1` 仍然通），说明这条不是靠"连不上"混过去的
    async with db.get_sessionmaker()() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
