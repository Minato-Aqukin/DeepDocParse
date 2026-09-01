"""配置与模型注册表加载。

gateway 不 import 任何模型代码——只认 models.yaml 里的 endpoint。
"""
from pathlib import Path

import yaml
from pydantic import BaseModel
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

    # 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权
    allow_insecure_defaults: bool = False


class ModelEntry(BaseModel):
    """注册表里的一行。

    **能力曾经是靠段名隐含的**（vqa_models / parse_engines / embedding_models），
    这卡住两类未来：一个模型多种能力（bge-m3 的 dense/sparse/colbert 三个头）、
    一个 endpoint 承载多个逻辑模型（LoRA adapter）。runtime / capabilities / adapter
    把这些显式化 —— 全部可选，不填就按段名推断，老 models.yaml 一字不改照跑。
    """

    endpoint: str
    default: bool = False
    # 引擎级默认透传选项（如 mineru 的 backend=pipeline|vlm），请求方 options 可覆盖。
    # 放注册表而非代码：dev/prod 换后端 = 改一行配置（铁律 3）
    options: dict = {}
    # 用哪个适配器/协议说话。解析引擎见 services/engines.py（mineru-api | borndigital）；
    # 留空则按所在段推断，这就是"加引擎 = 加容器 + 一行配置"真正兑现的地方
    runtime: str = ""
    # 显式声明能力。留空按段名推断（parse / vision / dense）。
    # 例：不支持流式的 VQA 写 [vision, no_stream]，取得到稀疏头的 embedding 写 [dense, sparse]。
    # **`no_instruct`**：OCR 专用模型（DeepSeek-OCR 系）只会把图上的字抄出来，
    # 不会遵循抽取指令。抽取平面挑模型时会跳过带这个词的条目，
    # 一个都挑不到就报 no_instruct_model —— 而不是拿它硬抽出一堆假的 not_found。
    # 视觉核对（原样抄写）不受影响，那正是它们的本行。见 services/extraction.py。
    capabilities: list[str] = []
    # 预留接缝：LoRA / 缝合模型在同一个 endpoint 上靠这个字段区分，
    # 请求时作为 model 字段传给运行时。**只是接缝，本轮不实现**
    adapter: str | None = None


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
