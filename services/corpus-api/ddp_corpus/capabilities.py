"""语料侧能力清单的生产者：`GET /internal/capabilities` 的实现。

control-api 拿它当**固定配置的能力生产者**（`packages/contracts/ddp/
discovery-control-format.md`），再注入权威 `node_id` 并做 schema 校验。
所以这里返回的 profile 里**没有 node_id**。

`accepting_admissions` 必须**真话**：它是 P5 执行者是否接单的声明，而不是
一句恒假的口号。判据有四条（见 `_accepting_admissions`）：开关开着、检索库
真的可查、该 operation 自身就绪，而且该 operation 就是执行者受理的那一条
（当前是 `corpus.retrieve` 与 `rag.answer.cited`）。任何一条不成立都报 false
—— 把"我会接单"写在能力上、实际 admission 却 401/拒绝，是另一种静默失败。

**`rag.answer.cited` 的接单是有代价的**：执行者只有生成真的就绪
（`answer_generation_ready`，与协调者规划用的同一条观测）才敢报 true；
实际 admission 也会按同一条判据复核。否则远端协调者按 `can_generate` 派来的
answer 步骤会在收款后被 `capability_unsupported` 拒掉。

## 这些 operation 是本层执行的，所以就绪度要由本层组合

网关只执行 `doc.parse` / `extract.fields` / `rerank`（它自己有端点）。
编译、带出处的问答、Wiki、跨文档检索都在本服务里跑，网关手上只有模型通道。
因此本模块**不是转发器**：它把三类观测合起来算 readiness ——

1. **本层自己的检索库**（`chunks` 可查）。没有它，检索、问答、抽取、Wiki
   全都干不了活，而它与模型健康毫无关系。
2. **网关的模型通道**（`model_channels`）：本层会请求哪个 model 是**本层的
   配置**决定的（`CHAT_MODEL` 留空就是网关的 default），所以按名字/默认标记
   自己解析，不能拿"网关有某个 instruct 条目"当"我会拿到它"。
3. **本层自己的开关**（`COMPILE_VISION_ENABLED` / `RERANK_ENABLED` …）。
   开关关着时那条路就是不通的，与上游多健康无关。

**readiness 取最差者**。可选依赖（向量、视觉核对）只改 `profile` 的名字，
绝不把 readiness 抬回 ready —— 那正是"降级必须可见"的这一层落点：
`corpus.retrieve` 的 `keyword_only` 与 `doc.compile` 的 `text_only`
在契约字段上看得见，而 `CapabilityProfile` 里没有 degraded 字段可用。

## 只消费网关的 HTTP 契约

模型能力来自 `services/model-gateway` 的 `GET /v1/capabilities`。本模块
**只按 HTTP 契约消费，绝不 import 网关的注册表代码** —— 网关注册表是它的
内部实现，跨服务 import 会让两侧的类型与过滤规则悄悄分叉。

## 观测不到、过期、不认识的取值，一律不许透传成 ready

- 网关自己说 `capability_status != observed` 时，它手上也没有有效证据，
  拿它的 profiles 当 observed 是把"不知道"洗成"知道"（control 对同一件事
  的判据更严：unknown 带着 profiles 直接判整份无效）。
- 网关给的 `valid_until` 已过期 -> 按 §5.5 当 unknown，**不按最后一次成功**。
- 网关给的 readiness 不在契约枚举里 -> 当 unknown，不当 ready。
- 独立配置的 chat / embedding / rerank 端点（`config.chat_url` 等，ADR #17）
  观测不到就如实报 unknown：那时网关的就绪度说的是**网关自己**的模型，
  与本层真正要打的端点无关。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ddp_contracts.enums import CAPABILITY_READINESS_VALUES
from ddp_corpus.config import settings
from ddp_corpus.db import get_sessionmaker
from ddp_corpus.models import Chunk

#: 一次观测的有效期。**短**：它是"现在"的证据，不是一个长期结论。
OBSERVATION_TTL_SECONDS = 60

#: 时钟偏移容忍。网关与本服务可能不在同一台机器上，几秒的偏移不该让
#: 一条刚做出来的观测被判成过期；但也只容忍到"不至于把过期记录洗白"的程度。
CLOCK_SKEW_SECONDS = 5

#: 执行者的 admission 真正受理的 operation profile（`federation` 的步骤
#: operation 名与 profile 名不同：`retrieve`↔`corpus.retrieve`、
#: `answer`↔`rag.answer.cited`）。`_accepting_admissions` 只对这些报 true。
ADMISSIBLE_OPERATION_PROFILES = {"corpus.retrieve", "rag.answer.cited"}

#: readiness 合并顺序（差 -> 好）。最差者胜。
#:
#: **`draining` 必须在这里面。** 上游给的取值可以是契约枚举里的任何一个，
#: 而不只是我们内部用的那两三种；漏掉一个的后果不是判断错，是
#: `_RANK.index()` 抛 ValueError -> 能力查询 500 -> 上层记成"节点不可达"，
#: 比一个诚实的 unhealthy 难查得多。排在 `unknown` 左边是有意的：
#: 知道它在排空，比"没有证据"更该被当成不可用。
_RANK = ("unhealthy", "draining", "unknown", "ready")


def _worst(*states: str) -> str:
    return min(states, key=_RANK.index)


def _stamp(now: datetime) -> dict[str, str]:
    """本次观测的时间戳。**永远是我们自己的观测时刻** —— 即使结论来自上游，
    "什么时候知道的"也必须是本层的，否则上游的陈旧时间会被当成新证据。"""
    return {"observed_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=OBSERVATION_TTL_SECONDS)).isoformat()}


def _accepting_admissions(operation: str, readiness: str, store: str) -> bool:
    """这个 operation 现在真的接单吗。

    **四条缺一不可**：

    1. `FEDERATION_ADMISSIONS_ENABLED` 开着（部署方可以整体关掉接单）；
    2. 本层检索库可查（`observe_store`）—— 库都连不上时接单只会把任务
       收进来再失败，不如当场说不接；
    3. 该 operation 自己就绪。`readiness` 与接单是分开的两件事
       （ready 但排空/配额满时不接单是常态），但**报 healthy 之外的
       状态却接单**是直接的谎：任务收进来也执行不了；
    4. **该 operation 就是执行者真正受理的那一条**。P5 的 admission
       执行器受理 `corpus.retrieve` 与 `rag.answer.cited`
       （`federation.SUPPORTED_OPERATIONS`），后者还要求生成通道真的就绪；
       其它 profile（编译 / 抽取 / Wiki / 解析）由本层同步执行、从不经
       admission —— 给它们标 true 是让对账方按一个不存在的接单口去派活，
       而端点必然拒绝。

    留空一份"恒 false"的写法正是这轮要修的：能力声明说了接单，端点却
    因为别的原因拒绝，对账方只能靠试错发现。
    """
    return bool(operation in ADMISSIBLE_OPERATION_PROFILES
                and settings.federation_admissions_enabled and store == "ready"
                and readiness == "ready")


async def answer_generation_ready(http: httpx.AsyncClient | None, *,
                                  now: datetime | None = None) -> bool:
    """本层现在真的能生成带出处答案吗（规划与接单共用的一条判据）。

    只认 `collect_capability_profiles` 的组合结论：模型名、`chat_url` 单独
    配过、库健康，任何一项都不能当就绪证据。观测不到（网关不在/报 unknown/
    没有该 profile）一律 False —— 读不出来就按不可用处理，绝不猜。

    协调者用它决定要不要保留本地 answer 步；执行者接单前用它复核
    （`FEDERATION_ADMISSIONS_ENABLED` 之外，生成能力就绪是 answer 受理的
    硬前提，收进来再 `upstream_error` 是把能力缺失伪装成执行失败）。
    """
    if http is None:
        return False
    try:
        profiles, status = await collect_capability_profiles(http, now=now)
    except Exception:                      # noqa: BLE001 —— 可用性探测不许打挂调用方
        return False
    if status != "observed":
        return False
    return any(profile.get("operation") == "rag.answer.cited"
               and profile.get("readiness") == "ready" for profile in profiles)


# --------------------------------------------------------------------------
# 依赖一：本层自己的检索库
# --------------------------------------------------------------------------

async def observe_store() -> str:
    """检索库能不能被真的查一次。

    **不是 `SELECT 1`**：库活着但迁移没跑过时，`SELECT 1` 照样成功，而检索
    会 500。所以打的是检索真正要读的那张表（`chunks`）。

    它**不**回答"某个集合的索引建到哪了" —— 那是 CollectionDescriptor 的
    `index_revision` 与证据探测（计划 §5.2 / §6.4）的事，节点级能力声明
    回答不了，也不该假装回答。
    """
    try:
        async with get_sessionmaker()() as session:
            await session.execute(select(Chunk.id).limit(1))
    except Exception:
        # DBAPI / SQLAlchemy / 网络异常体系庞杂，探针一律兜住（与 readyz 同）
        return "unhealthy"
    return "ready"


# --------------------------------------------------------------------------
# 依赖二：网关的模型通道
# --------------------------------------------------------------------------

class _Channel:
    """本层真正会请求到的那个模型通道条目。

    `configured=False` 表示"这条通道在本部署里压根没有模型" —— 与
    "有模型但不可用"必须分开：前者是这个 operation 在本节点不存在（不列），
    后者是能力不可用（列出来并标 unhealthy）。
    """

    def __init__(self, *, configured: bool, readiness: str,
                 model: str = "", supports: dict | None = None,
                 limits: dict | None = None):
        self.configured = configured
        self.readiness = readiness
        self.model = model
        self.supports = supports or {}
        self.limits = limits or {}

    def supporting(self, *words: str) -> str:
        """要求这些派生能力都成立时的就绪度。

        **能力缺失不是 unknown 而是 unhealthy**：我们确实观测到了这个模型，
        只是它干不了这活 —— OCR 专用模型被派去遵指令会抽出一堆假的
        `not_found`（系统能力缺失伪装成"文档里没有"）。观测到的否定结论
        必须说成否定，不能降级成"不确定"。
        """
        if self.readiness == "unknown" or not words:
            return self.readiness
        missing = [w for w in words if not self.supports.get(w)]
        return "unhealthy" if missing else self.readiness


#: 本层三条独立可配的通道 ->（独立 URL 的配置项, 本层指定的 model, 网关通道名）
_CHANNELS = {
    "chat": ("chat_url", "chat_model", "chat"),
    "embedding": ("embedding_url", "embedding_model", "embedding"),
    "rerank": ("rerank_url", "rerank_model", "rerank"),
}


def _resolve_channel(kind: str, gateway_channels: list[dict] | None) -> _Channel:
    """解析"本层这条通道实际会打到谁、它现在什么状态"。

    独立 URL 配着时**不继承网关的就绪度**：那时网关报的是它自己的模型。
    没有通用健康路径可探（`chat_url` 指向的是 `/v1/chat/completions`，
    GET 它不代表什么），所以如实 unknown —— 计划 §5.2 的 `configured ≠ ready`。
    """
    url_key, model_key, channel_name = _CHANNELS[kind]
    if getattr(settings, url_key):
        return _Channel(configured=True, readiness="unknown")
    if gateway_channels is None:
        # 网关没给通道清单（旧版本网关 / 取不到）：观测不到，不猜
        return _Channel(configured=True, readiness="unknown")

    wanted = getattr(settings, model_key)
    # 上游的元素形状不对时**跳过它**，不要在这里抛：这条路在 control 的握手
    # 请求里，一个畸形字段不该把能力查询变成 500
    candidates = [c for c in gateway_channels
                  if isinstance(c, dict) and c.get("channel") == channel_name]
    if not candidates:
        # 网关这条通道一个模型都没注册 -> 本层请求必然 404，这条能力不存在
        return _Channel(configured=False, readiness="unhealthy")
    if wanted:
        # 本层显式指定了 model：网关没有这个名字时请求必然 404 model_not_found，
        # **不能退回 default 去报它的健康** —— 那是替真实请求撒谎
        picked = next((c for c in candidates if c.get("model") == wanted
                       or c.get("profile") == wanted), None)
        if picked is None:
            return _Channel(configured=True, readiness="unhealthy", model=wanted)
    else:
        # 留空 = 由网关按注册表选 default，所以必须找那个被标了 default 的
        picked = next((c for c in candidates if c.get("default") is True), None)
        if picked is None:
            return _Channel(configured=True, readiness="unknown")

    readiness = picked.get("readiness")
    if readiness not in CAPABILITY_READINESS_VALUES or readiness == "configured":
        # 不认识的取值 / "只是配置过" 都不是可用证据
        readiness = "unknown"
    supports = picked.get("supports")
    limits = picked.get("limits")
    return _Channel(
        configured=True, readiness=readiness,
        model=str(picked.get("model") or wanted or ""),
        supports=supports if isinstance(supports, dict) else {},
        limits=_sane_limits(limits),
    )


# --------------------------------------------------------------------------
# 上游响应的取用：只取契约字段，且不把过期/未知洗成 ready
# --------------------------------------------------------------------------

def _sane_limits(limits) -> dict:
    """只留契约认得、取值也合法的限额。

    上游给了坏值时**丢掉这一项**，而不是原样透传：control 对整份观测是
    全有全无（一条 profile 不合法就整份判 unknown），一个越界的 limits
    会把整个节点的能力清单连坐成"状态未知"。
    """
    if not isinstance(limits, dict):
        return {}
    out = {}
    for key, floor in (("max_concurrency", 0), ("max_candidates", 1)):
        value = limits.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= floor:
            out[key] = value
    return out


def _sane_versions(versions) -> dict:
    if not isinstance(versions, dict):
        return {}
    return {str(k): v for k, v in versions.items() if isinstance(v, str) and v}


def _fresh(profile: dict, now: datetime) -> bool:
    """上游这条观测**现在**还有效吗。

    过期记录不是当前能力证明（§5.5）。未来时间戳同样不可信（时钟错了或
    上游在编），只容忍 `CLOCK_SKEW_SECONDS`。
    """
    try:
        observed = datetime.fromisoformat(str(profile.get("observed_at")))
        valid_until = datetime.fromisoformat(str(profile.get("valid_until")))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None or valid_until.tzinfo is None:
        return False        # 没有时区的时间戳无法比较，不猜它是哪个时区
    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    return valid_until > now - skew and observed <= now + skew


def _upstream_readiness(profile: dict | None, now: datetime) -> str:
    """网关某条 profile 现在能给出的结论。不认识 / 过期 -> unknown。"""
    if not isinstance(profile, dict):
        return "unknown"
    readiness = profile.get("readiness")
    if readiness not in CAPABILITY_READINESS_VALUES or readiness == "configured":
        return "unknown"
    if not _fresh(profile, now):
        return "unknown"
    return readiness


async def _fetch_gateway(http: httpx.AsyncClient) -> dict | None:
    """GET 网关能力清单；任何失败都返回 None（调用方据此报 unknown）。"""
    try:
        resp = await http.get(
            f"{settings.service_url}/v1/capabilities",
            headers={"Authorization": f"Bearer {settings.service_token}"},
            timeout=5.0,
            follow_redirects=False,     # 禁止重定向：不能拿别处的响应当网关的健康
        )
    except httpx.HTTPError:
        return None
    if not (200 <= resp.status_code < 300):
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("profiles"), list):
        return None
    # **网关自己说没有有效证据时，它的 profiles 不算证据。**
    if body.get("capability_status") != "observed":
        return None
    return body


# --------------------------------------------------------------------------
# 本层 operation 的组合
# --------------------------------------------------------------------------

def _compile_variant(chat: _Channel) -> str:
    """编译能做到哪一档：文本原子照常编，**视觉原子要既看得见图又听得懂指令**。

    编译的视觉步骤发的是一句"只输出 JSON"的指令（`compilation.py`），
    所以 OCR 专用模型顶不上来 —— 它会继续抄字，解析不出 JSON，落成
    `vision_invalid_output`。因此这里要求 vision + instruct 两个词。

    视觉不可用（开关关着 / 没有这种模型 / 模型不健康）时编译**仍然产出**
    （`compilation.py` 打 `vision_unavailable` 降级并继续），所以 readiness
    不能报 unhealthy —— 用 profile 名把这件事说出来，让降级留在契约字段上。
    """
    if not settings.compile_vision_enabled:
        return "text_only"            # 开关关着：不是坏了，是本部署不做这一步
    return "with_vision" if chat.supporting("vision", "instruct") == "ready" else "text_only"


async def collect_capability_profiles(
        http: httpx.AsyncClient, *, now: datetime | None = None) -> tuple[list[dict], str]:
    """返回 `(profiles, capability_status)`。

    网关不可达 / 非 2xx / 格式不符 / 它自己报 unknown -> `([], "unknown")`：
    模型侧唯一的证人不在场时，本节点的能力清单按整份未知处理，与 control-api
    对"上游没接线"的口径一致（宁可说不知道，不把配置当成 ready）。
    """
    now = now or datetime.now(timezone.utc)
    gateway = await _fetch_gateway(http)
    if gateway is None:
        return [], "unknown"

    raw_channels = gateway.get("model_channels")
    channels = raw_channels if isinstance(raw_channels, list) else None
    chat = _resolve_channel("chat", channels)
    embedding = _resolve_channel("embedding", channels)
    rerank = _resolve_channel("rerank", channels)

    upstream = {p["operation"]: p for p in gateway["profiles"]
                if isinstance(p, dict) and isinstance(p.get("operation"), str)}
    store = await observe_store()
    stamp = _stamp(now)

    def profile(operation: str, readiness: str, *, name: str = "",
                versions: dict | None = None, limits: dict | None = None) -> dict:
        out = {
            "schema": "ddp-discovery/1#CapabilityProfile",
            "operation": operation,
            "readiness": readiness,
            "accepting_admissions": _accepting_admissions(operation, readiness, store),
            **stamp,
        }
        if name:
            out["profile"] = name
        if versions:
            out["engine_versions"] = versions
        if limits:
            out["limits"] = limits
        return out

    profiles: list[dict] = []

    # doc.parse —— 唯一真正"转发"的一条：解析由网关执行，本层只记账（要库）
    parse = upstream.get("doc.parse")
    if parse is not None:
        engine = parse.get("profile")
        profiles.append(profile(
            "doc.parse", _worst(_upstream_readiness(parse, now), store),
            name=engine if isinstance(engine, str) else "",
            versions=_sane_versions(parse.get("engine_versions")),
            limits=_sane_limits(parse.get("limits"))))

    # doc.compile —— 本层执行；视觉是可选增强，落在 profile 名字上
    profiles.append(profile("doc.compile", store, name=_compile_variant(chat),
                            versions=_sane_versions({"chat_model": chat.model})))

    # 遵指令的 chat + 检索库：三条生成型操作的硬依赖。
    # **一个 chat 模型都没有时不列它们**（不是 unhealthy）：那是"本节点不做
    # 这件事"，与"做得了但现在不可用"必须分开（control 侧的
    # capability_unsupported vs capability_unknown 就是这条分界）。
    if chat.configured:
        instruct = _worst(chat.supporting("instruct"), store)
        for operation in ("rag.answer.cited", "extract.fields", "wiki.pages"):
            profiles.append(profile(
                operation, instruct,
                name=_verification_variant(operation, chat),
                versions=_sane_versions({"chat_model": chat.model}),
                limits=chat.limits))

    # corpus.retrieve —— 纯本层。向量不可用时关键词路照常工作（可见降级）
    vector_ready = embedding.supporting("dense") == "ready"
    profiles.append(profile("corpus.retrieve", store,
                            name="hybrid" if vector_ready else "keyword_only",
                            versions=_sane_versions(
                                {"embedding_model": embedding.model} if vector_ready else {})))

    # rerank —— 本层开关 + 通道
    if settings.rerank_enabled and rerank.configured:
        # 与其它通道同一判据：段里躺着一个不会重排的条目时，别拿它的健康充数
        profiles.append(profile("rerank", rerank.supporting("rerank"),
                                name=rerank.model or ""))
    return profiles, "observed"


def _verification_variant(operation: str, chat: _Channel) -> str:
    """出处视觉核对开着且真能做时才叫 `verified`。

    核对要把裁图上的字抄出来再比对（`qa.py::verify_parse_consistency`），
    用的是一句中文指令，所以同样要 vision + instruct。做不了时核对结果是
    `None`（"没测出来"），而**不是**"一致" —— 把不能核对说成 verified
    就是发一张假的验证章。

    Wiki 生成没有这道核对（`knowledge.py` 只打文本 chat），所以它没有变体，
    返回空串 = 不写 `profile` 字段，而不是随便给个名字。
    """
    enabled = {"rag.answer.cited": settings.qa_verify_parse,
               "extract.fields": settings.extract_verify}
    if operation not in enabled:
        return ""
    if not enabled[operation]:
        return "unverified"
    return "verified" if chat.supporting("vision", "instruct") == "ready" else "unverified"
