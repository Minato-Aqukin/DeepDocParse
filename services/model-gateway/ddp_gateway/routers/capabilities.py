"""能力清单 `GET /v1/capabilities` —— 网关真的会执行什么，以及它手上的模型通道什么状态。

计划 §5.2 的三份描述里，`CapabilityProfile` 回答「这个 operation 现在真的能
干活吗」。网关只回答得了其中一部分，所以响应分成两半：

`profiles`
    **只列网关自己执行的 operation**：`/v1/parse` -> `doc.parse`、
    `/v1/extract` -> `extract.fields`、`/v1/rerank` -> `rerank`。
    编译、带出处的问答、Wiki、跨文档检索都住在 corpus-api，网关手上只有
    模型通道；把它们写进这里就是宣称网关会执行它们，planner 会把活派到一个
    只会转发 chat 的进程上（本文件此前正是这么做的：`doc.compile` /
    `rag.answer.cited` / `wiki.pages` / `corpus.retrieve` 四条都在网关的
    profiles 里，而网关一行编译/检索代码都没有）。

`model_channels`
    chat / embedding / rerank 三条通道里**每个注册条目**的真实健康与派生能力。
    **它不是能力声明**，而是 corpus-api 组合语料侧就绪度的原料：语料侧知道
    自己会请求哪个 model（`CHAT_MODEL` 留空就是网关的 default），所以必须由
    它去挑 —— 网关不能替它断言「有一个 instruct 条目」就等于「你会拿到它」。

## 五条不能违反的规则

1. **注册不等于就绪。** readiness 一律来自本次探测，没有任何"注册了就 ready"
   的捷径；候选一个都没有时**不编造** `configured`，直接不列这条 operation。
2. **探测必须对上真的会被请求的那个 model。** `/v1/models` 返回 200 只说明
   运行时活着：vLLM 只认自己 `--served-model-name` 的那个 id，对不上时每一次
   真实请求都是 404 `model_not_found`（`infra/autodl/README.md` 的排障表里
   已经有这一条）。所以 OpenAI 协议的条目必须在 `data[].id` 里真找到它。
3. **一个 operation 的就绪度是它全部依赖的合并，最差者胜。** 解析与抽取是
   异步任务，受理时第一件事是读 Redis 水位（`routers/parse.py`）——
   Redis 不通时模型再健康也受理不了，模型健康 ≠ 这个平面可用。
4. **选路不看健康，所以不许拿"有一个健康的条目"当 ready。** 真实选路
   （`Registry.default_of` / `services/extraction.py::_pick_chat`）只看 default
   标记与能力词，一个健康的兄弟条目不会替死掉的默认条目干活。
5. **`no_instruct` 不能冒充遵指令，`vision` 与 instruct 也不互相顶替。**
   判据与真实选路同源（从 `services/extraction.py` 导入那两个能力词），
   **不是"写了 instruct"而是"没写 no_instruct"** —— 段名会把没写
   `capabilities` 的 vqa 条目补成 `[vision]`，而抽取平面照样会挑中它们。

`accepting_admissions` 恒为 false：admission 受理器还没实现，能力就绪不等于
会接单（I06）。**队列满**属于接单问题而不是就绪问题，所以水位不进 readiness。
`node_id` 不在这里 —— 网关没有自治节点身份，由 control-api 消费
`/internal/capabilities` 时注入。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Request

from ddp_gateway.auth import require_service_token
from ddp_gateway.config import settings
from ddp_gateway.routers.health import _probe_path
from ddp_gateway.services.engines import is_inprocess
# 能力词只有一份：判据必须与真实选路同源，抄第二份就是本项目静默出错过三次的形状
from ddp_gateway.services.extraction import NO_INSTRUCT, VISION

router = APIRouter(tags=["capabilities"], dependencies=[Depends(require_service_token)])

#: 一次健康观测的有效期。**短**是有意的：过期记录按 unknown 处理（§5.5），
#: 不能拿很久以前的一次成功当成现在可用。
OBSERVATION_TTL_SECONDS = 60

#: 唯一一条能问出"你到底在服务哪个模型"的探针路径。
_MODELS_PATH = "/v1/models"

#: readiness 合并顺序（差 -> 好）。**最差者胜**：一条依赖不健康，整个
#: operation 就不健康；没有证据只能是 unknown，不许被 ready 盖过去。
_RANK = ("unhealthy", "unknown", "ready")


def _worst(*states: str) -> str:
    return min(states, key=_RANK.index)


def _follows_instructions(entry) -> bool:
    """会遵循指令（抽值、按 schema 吐 JSON）。

    判据是**没写 `no_instruct`**，不是"写了 instruct" —— 与
    `services/extraction.py::_pick_chat(instruct=True)` 一字不差。
    按 "instruct in capabilities" 筛会把所有靠段名补成 `[vision]` 的老条目
    报成不可用，而 `/v1/extract` 明明在用它们：那是另一个方向的谎。
    """
    return NO_INSTRUCT not in (entry.capabilities or [])


def _sees_images(entry) -> bool:
    """看得见图。与 `_pick_chat(instruct=False)` 同一判据。"""
    return VISION in (entry.capabilities or [])


def _has(word: str):
    return lambda entry: word in (entry.capabilities or [])


#: 网关**自己执行**的 operation ->（注册表段, 选路判据, 探针缺省路径, 要不要任务存储）
#:
#: 这张表只允许出现网关真有端点的 operation。加一条之前先问：网关有没有
#: 这个活的代码？没有就该由真正执行它的服务去声明。
_OPERATIONS: tuple[tuple[str, str, object, str, bool], ...] = (
    # POST /v1/parse -> 异步任务 -> 要 Redis 水位
    ("doc.parse", "parse_engines", _has("parse"), "/health", True),
    # POST /v1/extract -> 异步任务 + 抽值必须遵指令（OCR 专用模型会抽出假的 not_found）
    ("extract.fields", "vqa_models", _follows_instructions, _MODELS_PATH, True),
    # POST /v1/rerank -> 同步透传，不进队列
    ("rerank", "rerank_models", _has("rerank"), "/health", False),
)

#: 通道名 ->（注册表段, 探针缺省路径, 派生能力判据）。
#: 这里列的是**通道里每个条目**，不做选路 —— 谁会被挑中由消费方按自己的配置决定。
_CHANNELS: tuple[tuple[str, str, str, dict], ...] = (
    ("chat", "vqa_models", _MODELS_PATH,
     {"instruct": _follows_instructions, "vision": _sees_images}),
    ("embedding", "embedding_models", "/health", {"dense": _has("dense")}),
    ("rerank", "rerank_models", "/health", {"rerank": _has("rerank")}),
)

#: 真实配置里的限额，没有对应配置项就不填。
#:
#: **`doc.parse` 故意没有 `max_concurrency`**：`PARSE_QUEUE_MAX` 是在途任务
#: 水位上限（`task_store.queue_depth()`），不是并发度，而契约的 `limits` 里
#: 没有队列深度这一项 —— 把 200 报成 max_concurrency 会让 planner 以为这台
#: 机器能同时解析 200 份。宁可不报。
_LIMITS = {
    # 抽取链自己的字段并发（services/extraction.py），**不是** chat 反代的信号量
    "extract.fields": lambda: {"max_concurrency": settings.extract_concurrency,
                               "max_candidates": settings.extract_candidates},
}

#: chat 反代的并发闸（routers/chat.py 的 vqa_semaphore）。语料侧的问答/编译/抽取
#: 全从这道闸过，所以它是**通道**的限额，不是抽取平面的。
_CHANNEL_LIMITS = {
    "chat": lambda: {"max_concurrency": settings.vqa_max_concurrency},
}


def _requested_model(section: str, name: str, entry) -> str:
    """真实请求里填在 `model` 字段上的那个 id。**按调用路径取，不能一律用条目名**：

    - `vlm-ocr` 解析引擎填 `options.model`（`services/engines.py` 缺它直接报错）；
    - chat / embedding / rerank 反代填条目名（`routers/chat.py` 等）；
    - `adapter` 是预留接缝，抽取平面已经优先用它（`services/extraction.py`）。

    取错的后果不是报错而是**假绿**：探针对着一个不会被请求的 id 去核对，
    核对通过，而真实请求照旧 404。
    """
    if section == "parse_engines":
        return entry.adapter or str((entry.options or {}).get("model") or "") or name
    return entry.adapter or name


def _serves(resp: httpx.Response, model_id: str) -> bool:
    """`/v1/models` 的清单里真的有我们会请求的那个 id。

    坏 JSON / 形状不对 / 找不到 id 一律算没服务 —— 这里不能宽容：
    宽容的后果是把一个"活着但服务着别的模型"的运行时报成 ready。
    """
    try:
        body = resp.json()
    except ValueError:
        return False
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return False
    return any(isinstance(m, dict) and m.get("id") == model_id for m in data)


async def _probe(http: httpx.AsyncClient, url: str, model_id: str | None) -> str:
    """严格健康：只有 2xx 算数；重定向不跟随；OpenAI 协议还要核对 model id。

    **不复用 readyz 的判据**：那条把 `<500` 都算 up（进程该不该接流量是另一个
    问题），401/404/302 会被报成健康。
    """
    try:
        resp = await http.get(url, timeout=3.0, follow_redirects=False)
    except httpx.HTTPError:
        return "unhealthy"
    if not (200 <= resp.status_code < 300):
        return "unhealthy"
    if model_id is None:
        return "ready"      # `/health` 协议里没有模型清单，问不出来的事不假装问过
    return "ready" if _serves(resp, model_id) else "unhealthy"


class _Prober:
    """一轮响应内的探测去重。

    `vlm-ocr` 与 vqa 条目可以指向同一个容器（注册表允许一个 endpoint 出现在
    多个段里），模型容器的健康检查往往不便宜。键里带 model id：同一个容器被
    问"你服务 A 吗 / 你服务 B 吗"是两个问题，合并会把其中一个答错。
    """

    def __init__(self, http: httpx.AsyncClient):
        self._http = http
        self._tasks: dict[tuple[str, str | None], asyncio.Task] = {}

    async def state(self, section: str, name: str, entry, fallback: str) -> str:
        if is_inprocess(entry):
            # 进程内引擎（borndigital）没有远端可探：就绪性等于本进程
            return "ready"
        path = _probe_path(entry, fallback)
        url = f"{entry.endpoint}{path}"
        model_id = _requested_model(section, name, entry) if path == _MODELS_PATH else None
        key = (url, model_id)
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(_probe(self._http, url, model_id))
            self._tasks[key] = task
        return await task


async def _task_store_state(state) -> str:
    """任务存储（Redis）能不能读水位。

    `/v1/parse` 与 `/v1/extract` 受理的第一步就是 `queue_depth()`，
    所以这是这两条平面的真实依赖，而不是可选项。redis 的异常体系庞杂，
    与 readyz 同样一律兜住。
    """
    try:
        await state.task_store.queue_depth()
    except Exception:
        return "unhealthy"
    return "ready"


def _selected(registry, section: str, predicate):
    """真实选路**会挑中**的那个条目，挑不到返回 None。

    与 `services/extraction.py::_pick_chat` 同形：先按能力词过滤，再
    `default_of` 取默认（没标 default 就取第一个）。**不挑"健康的那个"** ——
    选路不看健康，谎报 ready 的正是"任取一个健康条目"那种写法。
    """
    usable = {name: entry for name, entry in getattr(registry, section).items()
              if predicate(entry)}
    if not usable:
        return None
    return registry.default_of(usable)


def _engine_versions(entry) -> dict[str, str]:
    """只在注册表显式给了模型标识时才填。**不编造版本号。**"""
    model = entry.adapter or (entry.options or {}).get("model")
    return {"model": str(model)} if model else {}


def _stamp(now: datetime) -> dict[str, str]:
    return {"observed_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=OBSERVATION_TTL_SECONDS)).isoformat()}


async def build_capabilities(state, *, now: datetime | None = None) -> dict:
    """把注册表 + 本次探测投影成 `{capability_status, profiles, model_channels}`。"""
    now = now or datetime.now(timezone.utc)
    prober = _Prober(state.http)
    registry = state.registry

    # 任务存储只探一次，且只在真有任务型 operation 时探
    store_state: str | None = None

    profiles: list[dict] = []
    for operation, section, predicate, fallback, needs_store in _OPERATIONS:
        picked = _selected(registry, section, predicate)
        if picked is None:
            continue        # 一个候选都没有 = 这个 operation 在本部署里不存在
        name, entry = picked
        readiness = await prober.state(section, name, entry, fallback)
        if needs_store:
            if store_state is None:
                store_state = await _task_store_state(state)
            readiness = _worst(readiness, store_state)
        profile = {
            "schema": "ddp-discovery/1#CapabilityProfile",
            "operation": operation,
            "profile": name,
            "readiness": readiness,
            "accepting_admissions": False,
            **_stamp(now),
        }
        versions = _engine_versions(entry)
        if versions:
            profile["engine_versions"] = versions
        limits = _LIMITS.get(operation)
        if limits:
            profile["limits"] = limits()
        profiles.append(profile)

    channels: list[dict] = []
    for channel, section, fallback, supports in _CHANNELS:
        entries = getattr(registry, section)
        if not entries:
            continue
        default_name, _ = registry.default_of(entries)
        limits = _CHANNEL_LIMITS.get(channel)
        for name, entry in entries.items():
            channels.append({
                "channel": channel,
                "model": _requested_model(section, name, entry),
                "profile": name,
                # 消费方按这个标记找"我不指定 model 时会拿到谁"，而不是靠数组顺序
                "default": name == default_name,
                "readiness": await prober.state(section, name, entry, fallback),
                "supports": {word: judge(entry) for word, judge in supports.items()},
                **({"limits": limits()} if limits else {}),
                **_stamp(now),
            })

    # 走到这里说明注册表与探测都执行过了：本次有真实观测证据
    return {"capability_status": "observed", "profiles": profiles,
            "model_channels": channels}


@router.get("/capabilities")
async def list_capabilities(request: Request):
    return await build_capabilities(request.app.state)
