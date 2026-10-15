"""配置与模型注册表加载。

gateway 不 import 任何模型代码——只认 models.yaml 里的 endpoint。
"""
import json
import os
import re
from pathlib import Path

import yaml
from ddp_core.fetch_policy import FetchPolicyConfig, is_trusted
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    # 唯一鉴权凭据：所有 /v1/* 都验它，且必须与 DeepDocParse-Web 的 SERVICE_TOKEN 一致。
    # 用户 API key 在 Web 层校验，service 完全不感知用户
    service_token: str = "change-me"
    # ARQ 队列 + 任务暂存 + 向量索引都用它。**不是缓存**：结果丢了就得重新解析
    redis_url: str = "redis://localhost:6379/0"
    # 模型注册表路径。加模型 = 加容器 + 在这个文件里加一行（铁律 3）
    models_config: str = "models.yaml"
    # 在途解析任务的水位上限，超了 /v1/parse 直接 429，让调用方退避而不是把 mineru 压垮
    parse_queue_max: int = 200
    # VQA 同步通道的并发闸。视觉模型显存吃紧，超了返回 429 而不是排队等到超时
    vqa_max_concurrency: int = 8
    # 解析结果在 service 侧的暂存时长（秒）。永久归档是 Web 层的事，
    # 这个窗口只保证"backend 有足够时间来取"。调小会让对账补取不回来
    result_ttl: int = 86400
    # ARQ 轮询 mineru 的退避起点（秒）
    poll_initial_delay: float = 1.0
    # 轮询退避上限（秒）：指数退避到这里就不再变长
    poll_max_delay: float = 10.0
    # 单个解析任务的轮询总时限（秒），超时判失败并释放水位。
    # 必须 < QUEUE_INFLIGHT_TTL，否则水位先被淘汰、任务还在跑
    poll_timeout: float = 1800.0
    # 在途任务在水位集合里的存活上限。worker 最迟在 poll_timeout 把任务判失败并释放，
    # 所以超过 poll_timeout + 余量还挂着的一定是"释放丢了"（worker 被杀/Redis 重启），
    # 淘汰它才能让水位自愈 —— 否则 /v1/parse 会永久 429（见 task_store.QUEUE_INFLIGHT_KEY）
    queue_inflight_ttl: float = 2400.0
    # v2 分块索引：单次 embeddings 请求的最大 chunk 数。
    # 必须留在运行时的 max-client-batch-size 之下（TEI 默认 32），否则长文档整批被拒 413
    embedding_batch_size: int = 16
    # ---- 抽取平面（v1.1）----
    # 每个字段进模型的候选块数。抽取是"一个字段一次定位"，候选给多了纯属烧钱：
    # 模型要在 N 个块里挑一个，块越多挑错的机会越大
    extract_candidates: int = 4
    # 余弦相似度下限。**与问答平面同一把尺子**：Web 层实测无关问题 0.246~0.381、
    # 真实命中 0.725~0.786。没有它的话每个"文档里其实没有"的字段都会被硬塞一个
    # 最相似的噪声块当出处 —— 而带着出处的假值比空值危险得多
    extract_min_similarity: float = 0.45
    # 低相关提示线：过了下限但没到这里的，界面要提醒"这条依据不牢"。**必须 > 下限**
    extract_low_similarity: float = 0.60
    # 模型输出不合 schema 时的重试次数。用尽仍不合规打 schema_violation，
    # **绝不静默把该字段当成"文档里没有"** —— 那会让系统故障伪装成事实
    extract_max_retries: int = 2
    # 一次抽取最多处理多少字段。schema 是调用方给的，没有上限时一个 200 字段的
    # schema 就是 200 次检索 + 200 次模型调用
    extract_max_fields: int = 64
    # 多记录（表格）抽取时最多看多少个候选块
    extract_max_record_blocks: int = 8
    # 一次抽取里并发跑多少个字段。字段之间互不依赖，串行跑一个 30 字段的 schema
    # 要等半分钟；但也不能敞开 —— 上游是同一个模型运行时，打满只会一起变慢
    extract_concurrency: int = 4
    # 出处一致性核对：裁出区域图让视觉模型原样抄一遍，与块文本比对（沿用问答平面 A4）。
    # 请求方可用 options.verify 覆盖。没有 file_url 或未注册 VQA 模型时打 vision_unavailable
    extract_verify: bool = False
    # 抄写相似度低于它判定解析与原图对不上。与 Web 层 qa_parse_mismatch_threshold 同源。
    #
    # **2026-08-25 在 4090D + DeepSeek-OCR-2 上标定过**（此前是没有依据的 0.35）：
    #   一致组（块图 vs 自己的文本）  n=10，全部 1.000
    #   不一致组（块图 vs 别人的文本）n=90，p95=0.382，max=0.643
    # 用旧的 0.35 会放过 5/90 个**该报的不一致**；脚本建议取两组中点 0.69。
    #
    # 这里取 **0.55** 而不是 0.69：标定用的是 tests/fixtures/contract.pdf ——
    # born-digital、英文、单栏，是最容易的一类，一致组才会齐刷刷 1.000。
    # 扫描件/中文/多栏上抄写保真度一定往下掉，阈值定太高会把好出处打成"存疑"，
    # 而这两处的既定取向是**宁可漏报不要误报**（误报比不报更伤信任）。
    #
    # 换文档类型前重标一次：
    #   python scripts/calibrate_verify_threshold.py --pdf <你的文档> \
    #       --endpoint http://127.0.0.1:18001 --model deepseek-ocr-2 \
    #       --models-config models.autodl.yaml
    extract_mismatch_threshold: float = 0.55
    # ---- 回调与出站抓取 ----
    # 回调只发往这个基座（回调带服务凭据）。空 = 拒绝一切 callback_url（fail closed）；
    # 根路径基座同样拒绝 —— 它会把整台主机的所有端点都交给带凭据的回调。
    # corpus-api 内部 ingest 传 {public_base_url}/internal/parse-callback，
    # compose/autodl 把它配成 corpus-api 的内网基座（如 http://corpus-api:8081/internal/）。
    callback_allowed_base: str = ""
    # mineru 转传的文件字节上限（Content-Length 见超即 413，一字节都不下载）。borndigital 的上限见 engines。
    mineru_max_bytes: int = 200 * 1024 * 1024
    # 出站抓取缺省不跟随重定向（防 SSRF 跳转到内网）。True = 手工逐跳、每跳重验
    # 目的地策略、至多 3 跳，且永不转发 Authorization（gateway 的出站抓取本来就不带它）。
    # 判据见 `ddp_core.fetch_policy`，这里只装配配置快照。
    fetch_allow_redirects: bool = False
    # 受信内部回源基座（防 SSRF，逗号分隔可配多个）。url 受信当且仅当
    # scheme+host+port 一致、路径按段对齐落在基座路径之下；根路径基座不算受信。
    # 受信不传染：重定向的每一跳也要落在登记的基座之下。稳定文件 URL 的链是
    # /files/{token} -> 对象存储内网预签名，两站都要登记，例如
    # http://control-api:8080/files/,http://minio:9000/deepdocparse/。
    # 判据见 `ddp_core.fetch_policy`。
    fetch_trusted_base: str = ""

    # 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权
    allow_insecure_defaults: bool = False

    def callback_permitted(self, callback_url: str | None) -> bool:
        """callback_url 是否落在 CALLBACK_ALLOWED_BASE 之下。

        没传回调本来就不用发；空基座 = 全部拒绝；其余按 `is_trusted` 的同一判据：
        带 userinfo、scheme+host+port 对不上、路径不在基座路径之下的一律拒绝。
        """
        if not callback_url:
            return True
        base = (self.callback_allowed_base or "").strip()
        return bool(base) and is_trusted(callback_url, base)

    def fetch_policy_config(self) -> FetchPolicyConfig:
        """本服务的出站抓取配置快照：判据住在 `ddp_core.fetch_policy`，这里只装配。

        直接读环境（`settings` 在 import 时建好，而测试会 monkeypatch 属性或
        环境变量，两边都要当场生效）。
        """
        if "FETCH_TRUSTED_BASE" in os.environ:
            bases = os.environ.get("FETCH_TRUSTED_BASE", "")
        else:
            bases = self.fetch_trusted_base or ""
        if "FETCH_ALLOW_REDIRECTS" in os.environ:
            allow = os.environ.get(
                "FETCH_ALLOW_REDIRECTS", "").strip().lower() in {"1", "true", "yes", "on"}
        else:
            allow = self.fetch_allow_redirects
        return FetchPolicyConfig(
            trusted_bases=tuple(
                base.strip() for base in bases.split(",") if base.strip()),
            allow_redirects=allow,
        )


#: `options.structured_output` 的取值：上游认哪种 response_format。
#: json_schema（缺省）= 原样透传；json_object = 上游只有 JSON mode（DeepSeek 实测：
#: json_schema 回 400 "This response_format type is unavailable now"），见 adapt_structured_output
STRUCTURED_OUTPUT_MODES = frozenset({"json_schema", "json_object"})


class ModelEntry(BaseModel):
    """注册表里的一行。

    **能力曾经是靠段名隐含的**（vqa_models / parse_engines / embedding_models），
    这卡住两类未来：一个模型多种能力（bge-m3 的 dense/sparse/colbert 三个头）、
    一个 endpoint 承载多个逻辑模型（LoRA adapter）。capabilities 把能力显式化 ——
    留空按段名补缺省；runtime 把协议显式化 —— 解析引擎留空是配置错，
    resolve 直接失败（此前缺省按 mineru 协议说话，错过一次，不再猜）。
    """

    endpoint: str
    default: bool = False
    # 引擎级默认透传选项（如 mineru 的 backend=pipeline|hybrid-engine|vlm-engine），请求方 options 可覆盖。
    # 放注册表而非代码：dev/prod 换后端 = 改一行配置（铁律 3）
    options: dict = {}
    # 用哪个适配器/协议说话。解析引擎见 services/engines.py（mineru-api | borndigital | vlm-ocr），
    # 留空 = 配置错，resolve 直接 LookupError；vqa/embedding 段本轮不读它（探针只拿它纠正路径）
    runtime: str = ""
    # 显式声明能力。留空按段名推断（parse / vision / dense）。
    # 例：不支持流式的 VQA 写 [vision, no_stream]，取得到稀疏头的 embedding 写 [dense, sparse]。
    # **`no_instruct`**：OCR 专用模型（DeepSeek-OCR 系）只会把图上的字抄出来，
    # 不会遵循抽取指令。抽取平面挑模型时会跳过带这个词的条目，
    # 一个都挑不到就报 no_instruct_model —— 而不是拿它硬抽出一堆假的 not_found。
    # 视觉核对（原样抄写）不受影响，那正是它们的本行。见 services/extraction.py。
    capabilities: list[str] = []
    # 预留接缝：LoRA / 缝合模型在同一个 endpoint 上靠这个字段区分，
    # 请求时作为 model 字段传给运行时。现在也是外部 OpenAI 兼容服务的
    # provider 侧模型 id（"request model field"）：条目名是我们的稳定 id
    # （语料/界面看到的那个），请求体里的 model 填 `adapter or 条目名`
    # （见 request_model_id）。自建 vLLM 的 served-model-name 与条目名一致，
    # adapter 留空照跑；外部服务两边不一样，不写 adapter 每次请求都是 404。
    adapter: str | None = None
    # 外部 OpenAI 兼容服务（DeepSeek / Kimi / OpenAI / Claude 中继）的密钥来源：
    # **环境变量的名字，不是密钥本身**。密钥永不进 YAML、日志、错误体或响应。
    # 本字段非空时，该条目的每一次出站调用都带
    # `Authorization: Bearer <os.environ[<本字段>]>`（见 upstream_auth_headers）。
    # 调用时环境里没有这个变量 = 可见失败：同步平面 503 provider_key_missing，
    # 探针报 unhealthy，异步链落对应降级 —— 绝不无鉴权裸调（那会把 401 伪装成上游故障）。
    api_key_env: str = ""

    @field_validator("api_key_env")
    @classmethod
    def _env_name_must_look_like_one(cls, value: str) -> str:
        """api_key_env 必须是合法的环境变量名 —— 它会被拿去查 os.environ。

        配错的后果不是报错而是**永远查不到**：比如写成带横线的名字，
        环境里永远不会有同名变量，每一次调用都是 provider_key_missing，
        排查时还以为是密钥没配。注册表加载时直接拦住。
        """
        if value and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError(
                f"api_key_env 必须是环境变量名（^[A-Z_][A-Z0-9_]*$），实得 {value!r}")
        return value

    @field_validator("options")
    @classmethod
    def _request_extras_must_not_shadow_envelope(cls, options: dict) -> dict:
        """`options.request_extras` 不许出现 model/messages/stream。

        这三个是请求信封（选路与协议决定），不是可声明的扩展：允许覆盖的话，
        一行注册表就能让"请求 A 模型"变成"请求 B 模型"（探针照样核对 A，
        于是又是假绿），或把 messages 换掉让抽取平面问出别的东西。
        放这里而不是 chat_request_defaults 里拦：**部署时就炸**，
        不等到第一次调用才发现。
        """
        extras = (options or {}).get("request_extras")
        if extras is None:
            return options
        if not isinstance(extras, dict):
            raise ValueError("options.request_extras 必须是 dict（声明式请求扩展）")
        shadowed = {"model", "messages", "stream"} & set(extras)
        if shadowed:
            raise ValueError(
                "options.request_extras 不许声明 "
                f"{sorted(shadowed)}（请求信封由选路决定，不可覆盖）")
        return options

    @field_validator("options")
    @classmethod
    def _structured_output_must_be_known(cls, options: dict) -> dict:
        """`options.structured_output`：上游认哪种 response_format（缺省 json_schema）。

        写错（比如 `json-object`）不能被当成缺省悄悄放过 —— 那会把 json_schema 原样发给
        只认 json_object 的托管 API，每个问答都 400。加载时就拒。
        """
        mode = (options or {}).get("structured_output")
        if mode is not None and mode not in STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                f"options.structured_output 只认 {sorted(STRUCTURED_OUTPUT_MODES)}，实得 {mode!r}")
        return options


def request_model_id(name: str, entry: ModelEntry) -> str:
    """真实请求里填在 `model` 字段上的那个 id：`adapter or 条目名`。

    条目名是我们的稳定 id（语料/界面看到的那个），adapter 是对端认识的 id。
    自建 vLLM 的 served-model-name 与条目名一致，adapter 留空照跑；
    外部服务两边不一样，必须在注册表里写 adapter，否则每一次请求都是 404。
    **唯一的例外是 vlm-ocr**：历史原因它用 `options.model`，
    见 capabilities._requested_model（探针与调用路径必须同源）。
    """
    return entry.adapter or name


#: 上游鉴权缺失时的可见失败码。同步平面 503，探针 unhealthy，异步链 upstream_error。
PROVIDER_KEY_MISSING = "provider_key_missing"


def upstream_auth_headers(entry: ModelEntry) -> dict[str, str]:
    """该条目出站调用要带的鉴权头。没有 api_key_env 的条目返回空 dict（行为不变）。

    密钥只在调用瞬间从环境里取，**永不进日志/异常/响应**：抛出的 LookupError
    只点环境变量的**名**。调用方（路由/服务/探针）各自把它翻译成自己平面的
    可见失败 —— 401/404/5xx 照旧是上游的问题，绝不因为没带上 key 而伪装成它们。
    """
    env_name = entry.api_key_env or ""
    if not env_name:
        return {}
    key = os.environ.get(env_name, "")
    if not key:
        raise LookupError(
            f"provider key missing: environment variable {env_name} is not set "
            f"(declared by registry entry api_key_env)")
    return {"Authorization": f"Bearer {key}"}


def chat_request_defaults(options: dict) -> dict:
    """Select declared chat extensions only; engine metadata is not a chat payload.

    边界（两条，缺一不可）：
    - `chat_template_kwargs`：vLLM 系的模板扩展，只在注册表显式声明时透传；
    - `request_extras`：外部 OpenAI 兼容服务的声明式扩展（如 DeepSeek 的
      `{"thinking": {"type": "disabled"}}`、Kimi 的 `{"reasoning_effort": "low"}`），
      原样并进 chat 请求体。model/messages/stream 不可能出现在这里 ——
      ModelEntry 的 validator 在注册表加载时已经拦下。
    transcribe_prompt、凭据等其它 options 永不进入 chat 请求体。
    """
    out: dict = {}
    if "chat_template_kwargs" in options:
        out["chat_template_kwargs"] = options["chat_template_kwargs"]
    extras = options.get("request_extras")
    if isinstance(extras, dict):
        out.update(extras)
    return out


def adapt_structured_output(body: dict, options: dict) -> dict:
    """把 json_schema 请求改写成上游认得的 JSON mode（只对声明了 json_object 的条目）。

    语料层的问答 / 联邦 / wiki 一律发 `response_format: json_schema`（strict）。只有 JSON mode
    的托管 API 收到它直接 400，问答整条线不可用。改写成 `{"type": "json_object"}`，并把 schema
    原文写进 system 消息 —— JSON mode 只保证"是 JSON"，不保证形状，形状要靠模型照着 schema 写；
    DeepSeek 还要求 prompt 里出现 "json" 字样。**约束从解码期退到了提示词**：输出仍由语料层
    按 schema 校验，不合规是可见失败（schema_violation），不会被当成合法答案。
    """
    if options.get("structured_output") != "json_object":
        return body
    fmt = body.get("response_format")
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        return body
    schema = (fmt.get("json_schema") or {}).get("schema")
    instruction = ("Reply with exactly one JSON object (json) that conforms to this JSON Schema. "
                   "No prose, no code fences.\nJSON Schema:\n"
                   + json.dumps(schema, ensure_ascii=False))
    messages = list(body.get("messages") or [])
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system" and isinstance(first.get("content"), str):
        messages[0] = {**first, "content": f"{first['content']}\n\n{instruction}"}
    else:
        messages.insert(0, {"role": "system", "content": instruction})
    return {**body, "messages": messages, "response_format": {"type": "json_object"}}


# 段名 -> 该段条目缺省具备的能力。**只是缺省值**：条目自己写了 capabilities 就以它为准
SECTION_CAPABILITIES = {
    "vqa_models": ["vision"],
    "parse_engines": ["parse"],
    "embedding_models": ["dense"],
    "rerank_models": ["rerank"],
}


class Registry(BaseModel):
    """models.yaml 的内存形态。"""

    vqa_models: dict[str, ModelEntry] = {}
    parse_engines: dict[str, ModelEntry] = {}
    embedding_models: dict[str, ModelEntry] = {}  # v2 (M4) 启用
    # v1.1：交叉编码器重排。**未注册时 /v1/rerank 返回 404，调用方退回不重排** ——
    # 注册表驱动的开关，与 embedding 段的处理方式一致（不改代码，只改一行配置）
    rerank_models: dict[str, ModelEntry] = {}

    def model_post_init(self, _context) -> None:
        """把段名隐含的能力补进条目，让下游只读 capabilities 一个地方。"""
        for section, defaults in SECTION_CAPABILITIES.items():
            for entry in getattr(self, section).values():
                if not entry.capabilities:
                    entry.capabilities = list(defaults)

    def default_of(self, section: dict[str, ModelEntry]) -> tuple[str, ModelEntry]:
        """取该类目的默认模型。空类目抛 LookupError 由调用方转成 404 ——
        直接 next(iter({})) 会抛 StopIteration，在协程里会变成一个语焉不详的 500。"""
        for name, entry in section.items():
            if entry.default:
                return name, entry
        # 没标 default 就取第一个
        for name, entry in section.items():
            return name, entry
        raise LookupError("no model registered in this section")


def assert_thresholds_sane() -> None:
    """低相关提示线必须高于相似度下限。

    配反了不会报任何错，只会让「相关度偏低」这个提示**永远不出现** ——
    Web 层为同一件事已经加过一次校验（config._check_similarity_thresholds），
    抽取平面复用了同一套阈值语义，就得复用同一道拦截。
    """
    if settings.extract_low_similarity <= settings.extract_min_similarity:
        raise RuntimeError(
            f"拒绝启动：EXTRACT_LOW_SIMILARITY({settings.extract_low_similarity}) 必须大于 "
            f"EXTRACT_MIN_SIMILARITY({settings.extract_min_similarity})，"
            f"否则抽取结果的低相关提示永远不会触发")


def load_registry(path: str | Path) -> Registry:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Registry(**{k: v for k, v in data.items() if v})


settings = Settings()

# `.env.example` 里写的就是 change-me，复制过去忘了改是最常见的部署事故
_PLACEHOLDER_SECRETS = {"", "change-me", "change-me-please", "changeme", "secret"}


def assert_secrets_configured() -> None:
    """启动即失败，而不是带着占位 token 安静地跑起来。

    service_token 是 gateway 的**唯一**鉴权凭据（用户 key 在 Web 层校验）。
    它是占位值就意味着 /v1/parse、/v1/chat/completions、/v1/embeddings
    对任何能连上这个端口的人开放，而运行时不会有任何异常。
    """
    if settings.allow_insecure_defaults:
        print("[config] WARNING: ALLOW_INSECURE_DEFAULTS 已开启，占位 token 检查被跳过")
        return
    if settings.service_token.strip().lower() in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "拒绝启动：SERVICE_TOKEN 还是占位值，gateway 的所有 /v1/* 将无鉴权开放。"
            " 请在 .env 里设置真实随机值（python -c \"import secrets;"
            " print(secrets.token_urlsafe(32))\"），"
            " 确需占位值启动请显式设置 ALLOW_INSECURE_DEFAULTS=true。"
        )
