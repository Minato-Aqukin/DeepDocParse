"""配置。

分三组：本层自有资源（PG/MinIO/JWT）、对 service 的调用参数、额度默认值。
service 相关的一切只对应 ../DeepDocParse/openapi.yaml 的契约，不感知其内部实现。
"""
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 仓库根的 .env 与 backend/.env 都读（后者优先）——backend 与 alembic 的 cwd 不同
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    # ---- 本层资源 ----
    # PostgreSQL（必须带 pgvector 扩展；单测走 SQLite in-memory，不读这一项）。
    # 端口 15432 是为了不和别处的 PG 撞车
    database_url: str = "postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse"
    # 签发/校验用户会话的密钥。**占位值会被拒绝启动**：它是 change-me
    # 等于任何人都能给任意 user_id 伪造一个有效会话，且运行时不报任何错
    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 24 * 7          # 登录态有效期（分钟），默认 7 天
    # bcrypt 成本因子。**生产不要调低** —— 它就是抗离线爆破的全部本钱。
    # 单测把它降到 4：默认 12 时一次 hash+verify 要 0.37s，而几乎每个用例都要注册一个
    # 用户，光这一项就占掉整个套件大半时间（见 tests/conftest.py）
    bcrypt_rounds: int = 12

    # MinIO：service 与浏览器走不同 endpoint —— 预签名 URL 的签名覆盖 host，
    # 两边 host 不同则签名不同，必须分开生成（见 CLAUDE.md 部署陷阱）。
    # 端口 19000/19001：9000 被 gateway 占用，且要避开 Windows 保留段 7964-8063。
    minio_internal_endpoint: str = "127.0.0.1:19000"   # service / 本进程可达
    minio_public_endpoint: str = "127.0.0.1:19000"     # 浏览器可达
    minio_access_key: str = "minioadmin"                # 对象存储凭据（生产必须改）
    minio_secret_key: str = "minioadmin"                # 同上
    minio_secure: bool = False                          # 走 https 则置 true
    # 桶名。原件、解析结果、出处裁剪图都在这一个桶里，靠对象键前缀分区
    minio_bucket: str = "deepdocparse"

    # ---- 对 service（DeepDocParse）----
    # DeepDocParse gateway 的地址。解析平面必须走它，embedding/chat 缺省也回落到它
    service_url: str = "http://127.0.0.1:9000"
    mcp_url: str = "http://127.0.0.1:9100"      # service 的 MCP 平面，/mcp 反代的上游
    # 与 service 之间的内网令牌，**必须与 DeepDocParse/.env 的 SERVICE_TOKEN 一致**。
    # 它同时也是 /internal/* 回调端点的凭据 —— 占位值会被拒绝启动
    service_token: str = "change-me"
    # 上传/重解析没有显式指定引擎时用哪个。**名字必须在 service 的 models.yaml 里存在**，
    # 否则 service 返回 404 unknown_engine —— 这正是无 GPU 环境踩到的：
    # models.cpu.yaml 只注册了 borndigital，本层却按名字写死 mineru，第一步就断。
    # 与注册表驱动一致：换引擎 = 改这一行配置，不改代码
    default_parse_engine: str = "mineru"
    # 解析平面必须走 service（那是它的本职）；但 embedding 与 chat 只要求 OpenAI 兼容，
    # 留独立配置以免把本层绑死在 DeepDocParse 的部署形态上（ADR #17）。
    # 留空则回落到 {service_url}/v1/...，dev 下什么都不用配。
    embedding_url: str = ""            # 如直连 TEI：http://127.0.0.1:18080/v1/embeddings
    embedding_token: str = ""          # 留空用 service_token
    embedding_model: str = ""          # 留空由上游注册表选 default
    chat_url: str = ""                 # 如直连 vLLM：http://.../v1/chat/completions
    chat_token: str = ""               # 留空用 service_token
    chat_model: str = ""               # 留空由上游注册表选 default
    # 本服务对 service 可达的外部地址：稳定文件 URL 与解析回调都用它拼。
    # 宿主机混合模式用 127.0.0.1:8080，全容器模式用服务名（http://web-backend:8080）。
    public_base_url: str = "http://127.0.0.1:8080"

    # 多副本部署必须配：限速计数与对账选主都靠它。留空 = 单实例模式（进程内计数）
    redis_url: str = ""

    # 单次上传的字节上限。**必须有**：上传体要整个进内存（算内容 sha256 当 doc_id，
    # 再原样 put 进 MinIO），没有上限时任意登录用户传个大文件就能把进程打爆
    # ——dev 机 WSL 只有 ~7.7GB，门槛极低。超限返回 413。
    max_upload_bytes: int = 200 * 1024 * 1024
    # 分片读取的粒度：边读边累计，超限立刻中断，不等整个文件落地
    upload_chunk_bytes: int = 1024 * 1024

    # ---- 归档与对账 ----
    # 必须 <= service 的 RESULT_TTL(24h)：超过这个窗口结果就被 service 清了，补取不回来
    result_ttl: int = 86400
    # 对账扫描间隔（秒）。回调是尽力而为的，恰好在重启时丢掉就得靠这条自愈路径
    reconcile_interval: int = 60
    # 软删除后多久才允许真正回收对象。删对象不可逆，而"删了又重新上传"会复活同一行
    # （documents.upload 的复活分支）—— 宽限期把"回收与复活撞车"从一个真实的竞态
    # 变成实际不可达，剩下的由 gc 里的 claim 兜住
    gc_grace_seconds: int = 3600

    # ---- 检索与问答 ----
    # bge-m3 是 1024 维。换模型维度变了必须整体 reindex（关系库不能像 Redis 那样按维度分索引名）
    embedding_dim: int = 1024
    # 单次 embeddings 请求的最大条数。必须低于运行时的 max-client-batch-size（TEI 默认 32），
    # 否则长文档整批被拒 413（service 侧 worker 已经踩过）
    embedding_batch_size: int = 16
    chunk_max_chars: int = 800          # 分块上限，影响出处粒度
    # DDP-Compile v1：视觉原子在入库时裁图并由视觉模型生成派生理解。
    # 关掉不是“等价的轻量模式”：文档仍可索引，但 compile_degraded 会明确记录
    # vision_unavailable，图表类问题不会假装已获得视觉理解。
    compile_vision_enabled: bool = True
    compile_vision_concurrency: int = 2       # 单文档同时调用 VLM 的原子数
    compile_vision_timeout: float = 120.0      # 每个原子的 VLM 超时（秒）
    # 索引 worker 租约：heartbeat 续租，进程崩溃后 reconcile 才能安全接管。
    # heartbeat 必须显著短于 lease；generation fencing 保证旧 worker 复活也写不进去。
    index_lease_seconds: int = 300          # worker 无 heartbeat 后多久允许接管
    index_heartbeat_seconds: int = 30       # 活 worker 的续租周期
    qa_top_k: int = 4                   # 进 prompt 的 chunk 数
    qa_candidates: int = 8              # 每路检索的候选数（融合前）
    qa_context_chars: int = 6000        # 资料段总字符上限
    # 余弦相似度下限。没有它的话 top-k 永远返回东西：问一个与文档无关的问题
    # 也会拿到几条不相干的"出处"，还带着"已做视觉验证"的标记——出处就成了假证据。
    #
    # 0.45 来自真 bge-m3(TEI, CLS pooling) 的实测分布：
    #   相关问题的最佳命中 0.725~0.786，无关问题 0.246~0.381，分离点约 0.55。
    # 取 0.45 两侧都有余量。**往上调是安全的（真实命中还有 0.27 余量），往下调才危险**
    # ——0.35 会放过一部分无关问题（实测有无关提问以 0.352/0.381 越线）。
    qa_min_similarity: float = 0.45
    # "低相关"提示线：命中过了下限、但离典型真实命中还差得远时，界面主动提醒用户
    # "这个回答的依据不太牢靠"，而不是替他判断。取 0.60 = 下限 0.45 与实测真实命中
    # 0.725~0.786 之间偏下的位置：宁可多提醒，也不要让勉强及格的出处看起来同样可信。
    # **必须 > qa_min_similarity**，否则永远不触发。
    qa_low_similarity: float = 0.60
    qa_history_turns: int = 6           # 带入的历史消息条数
    qa_crop_count: int = 1              # 做视觉验证的区域数
    # 出处一致性核对（A4）：把裁出来的区域图让视觉模型原样抄一遍，与 chunk 文本比对。
    # 补的是七种降级里唯一的洞 ——「解析本身错了」。那时 chunk 文本是错的，
    # 但语义相似度照样过阈值、照样裁图、照样标 verified，产出这个类别最恶劣的错误：
    # **带着"已做视觉验证"标记的假出处**。核对与回答并发跑，不增加首字延迟。
    qa_verify_parse: bool = True
    # 相似度低于它就判定解析与原图对不上（difflib 比值，0~1）。
    #
    # **2026-08-25 在 4090D + DeepSeek-OCR-2 上标定过**（此前是没有依据的 0.35）：
    #   一致组（块图 vs 自己的文本）  n=10，全部 1.000
    #   不一致组（块图 vs 别人的文本）n=90，p95=0.382，max=0.643
    # 旧的 0.35 会放过 5/90 个该报的不一致。标定脚本在 service 仓库：
    # `DeepDocParse/scripts/calibrate_verify_threshold.py`。
    #
    # 取 0.55 而不是脚本建议的中点 0.69：标定样本是 born-digital 英文单栏，
    # 是最容易的一类；扫描件/中文上抄写保真度会掉。仍然**宁可漏报不要误报**：
    # 误报会把好出处打成"存疑"，比不报更伤信任。
    #
    # **这个数字的来源与本层的实际用法并不完全对得上，用之前先读这段。**
    # 标定跑的是 service 侧：DeepSeek-OCR-2 + 它的原生 prompt `Free OCR.`。
    # 而本层 qa.TRANSCRIBE_PROMPT 写死的是一句**中文指令**，模型由 CHAT_MODEL 决定
    # （quickstart 缺省引导用户填一个通用文本/视觉模型）—— service 侧那套
    # "按注册表 options.transcribe_prompt 换成模型听得懂的话"**没有移植到本层**。
    # 也就是说：把阈值从 0.35 提到 0.55 在本层是朝着**误报**方向动的，
    # 而上面刚说过本层的既定取向是宁可漏报。之所以仍然跟着提，是因为两处同源、
    # 分叉会更难解释；但**本层至今没有自己的标定数据**。
    # 拿到 GPU 机器后要做的是：用本层真实的 CHAT_MODEL + 中文 prompt 重跑一次
    # calibrate_verify_threshold.py，按那个分布定本层自己的值
    # （或者把 transcribe_prompt 那套移植过来，让两层真的同源）。
    qa_parse_mismatch_threshold: float = 0.55
    # 等核对结果的上限（秒）。核对与回答并发跑，正常情况下回答先结束、这里几乎不等；
    # 但视觉模型在 CPU 上抄一段文字可能要几分钟（read 超时是 900s），
    # **没有上限的话 done 帧会被硬生生拖后几分钟**，用户看着答案已经出完却迟迟不落定。
    # 超时就当"没测出来"——宁可不打标，也不能让核对拖垮体验
    qa_verify_timeout: float = 20.0
    qa_rate_per_min: int = 20           # 每用户问答限速

    # ---- 重排序（D1）----
    # 交叉编码器精排。留空 = 回落到 {service_url}/v1/rerank；
    # **service 侧没注册 rerank_models 时那个端点返回 404**，本层据此打
    # degraded="rerank_unavailable" 并照常返回融合名次 —— 可见降级，不是静默跳过
    rerank_url: str = ""
    rerank_token: str = ""              # 留空用 service_token
    rerank_model: str = ""              # 留空由上游注册表选 default
    # 关掉就完全不调 rerank。默认关：没部署 rerank 容器的人不该每次问答都吃一个 404 往返
    rerank_enabled: bool = False
    # 送进重排的候选数。**必须显著大于 qa_top_k**，否则无米下锅 ——
    # 精排的价值全在"从更大的候选池里挑"，候选=top_k 时它只是把 4 条重新排了个序
    rerank_candidates: int = 24
    # 等重排结果的上限（秒）。交叉编码器每个候选一次前向，CPU 上 24 条要几秒；
    # 超时就当"没重排"并打 rerank_unavailable —— 宁可不精排也不能把问答拖死
    rerank_timeout: float = 20.0

    # ---- 结构化抽取（v1.1）----
    # 每个字段进模型的候选块数。抽取是"一个字段一次定位"，候选给多了纯属烧钱：
    # 模型要在 N 个块里挑一个，块越多挑错的机会越大
    extract_candidates: int = 4
    # 一次抽取最多处理多少字段。schema 由用户给，没有上限时一个 200 字段的 schema
    # 就是 200 次检索 + 200 次模型调用
    extract_max_fields: int = 64
    # 多记录（表格）抽取时最多看多少个候选块
    extract_max_record_blocks: int = 8
    # 模型输出不合 schema 时的重试次数。用尽仍不合规打 schema_violation，
    # **绝不静默把该字段当成"文档里没有"** —— 那会让系统故障伪装成事实
    extract_max_retries: int = 2
    # 一次抽取里并发跑多少个字段。字段互不依赖，串行跑 30 个字段要等半分钟；
    # 但也不能敞开 —— 上游是同一个 chat 端点，打满只会一起变慢
    extract_concurrency: int = 4
    # 批量抽取里同时处理多少份文档。乘以 extract_concurrency 才是真实并发，
    # 两个都调大很容易把上游打挂
    extract_doc_concurrency: int = 2
    # 一次批量最多多少份文档。没有上限时"全选"就能提交几千份
    extract_max_documents: int = 200
    # 出处一致性核对：裁出区域图让视觉模型原样抄一遍（沿用问答平面 A4 的做法）。
    # 抽取默认**开**（与 service 侧默认关相反）：产品层有原件、有裁剪管线，
    # 而抽取结果是要被当数据用的，核对的价值比问答那边更高
    extract_verify: bool = True
    # 核对的字段数上限。每个字段核对一次 = 一次渲染 + 一次视觉模型调用，
    # 全量核对会让一次 30 字段的抽取变成 60 次模型调用
    extract_verify_fields: int = 3
    extract_rate_per_min: int = 6       # 每用户批量抽取限速（次/分钟）
    # 视觉模型在 CPU 上出第一个 token 可能要几分钟（dev 机常态），读超时要留够
    chat_read_timeout: float = 900.0

    # ---- 额度默认值（新建 key 时的初值）----
    default_quota_pages: int | None = 1000   # None = 不限
    default_rate_limit_per_min: int = 60     # 新建 key 的默认限速（次/分钟）

    # 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权
    allow_insecure_defaults: bool = False

    # 允许跨源访问的前端地址，逗号分隔。默认是 dev 的 Vite（5173）——
    # 换部署形态时必须能改配置而不是改代码
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @model_validator(mode="after")
    def _check_default_parse_engine(self):
        """缺省引擎不能是空串。

        空串现在是"走 service 注册表缺省"的约定值，解析本身会成功，但本层会把
        ParseJob.engine 存成空串、options_hash 也按空串算 —— 历史版本从此对不上号。
        要用注册表缺省就让 service 去决定，不要把空串配进本层。
        """
        if not self.default_parse_engine.strip():
            raise ValueError(
                "DEFAULT_PARSE_ENGINE 不能为空：它要与 service 的 models.yaml 里的"
                " 引擎名一致（无 GPU 部署填 borndigital）")
        return self

    @model_validator(mode="after")
    def _check_rerank_candidates(self):
        """送进重排的候选必须显著多于最终 top_k，否则精排无米下锅。

        候选 == top_k 时 rerank 只是把已经选定的那几条重新排了个序，
        召回一点没变 —— 但它照常消耗一次模型调用，还会让人以为"上了精排"。
        这正是这个项目最讨厌的那种：**功能在，效果不在，且看不出来**。
        """
        if self.rerank_enabled and self.rerank_candidates <= self.qa_top_k:
            raise ValueError(
                f"RERANK_CANDIDATES({self.rerank_candidates}) 必须大于 "
                f"QA_TOP_K({self.qa_top_k})，否则重排没有可挑的候选（见 config 注释）")
        return self

    @model_validator(mode="after")
    def _check_similarity_thresholds(self):
        """低相关提示线必须高于相似度下限。

        配反了不会报任何错，只会让"相关度偏低"这个提示**永远不出现** ——
        又一个静默失效的功能。这个项目吃够这种亏了，启动时就拦下来。
        """
        if self.qa_low_similarity <= self.qa_min_similarity:
            raise ValueError(
                f"QA_LOW_SIMILARITY({self.qa_low_similarity}) 必须大于 "
                f"QA_MIN_SIMILARITY({self.qa_min_similarity})，否则低相关提示永远不会触发")
        return self

    @model_validator(mode="after")
    def _check_index_lease(self):
        if self.index_lease_seconds < 3 or not (
                0 < self.index_heartbeat_seconds < self.index_lease_seconds / 2):
            raise ValueError(
                "INDEX_HEARTBEAT_SECONDS 必须 > 0 且小于 INDEX_LEASE_SECONDS 的一半，"
                "INDEX_LEASE_SECONDS 至少为 3")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def embeddings_endpoint(self) -> str:
        return self.embedding_url or f"{self.service_url}/v1/embeddings"

    @property
    def chat_endpoint(self) -> str:
        return self.chat_url or f"{self.service_url}/v1/chat/completions"

    @property
    def rerank_endpoint(self) -> str:
        return self.rerank_url or f"{self.service_url}/v1/rerank"


settings = Settings()

# 占位值集合。`.env.example` 里写的是 change-me / change-me-please，
# 复制过去忘了改是最常见的部署事故
_PLACEHOLDER_SECRETS = {"", "change-me", "change-me-please", "changeme", "secret"}


def rerank_config() -> "RerankConfig":
    """把本层的 settings 装配成 core 认识的形状。

    `ddp_core.rerank` 是两个仓库共用的叶子模块，**不能 import 任何一侧的
    `app.config`**（两边各有一个 `app` 顶层包）。所以配置由调用方装配后传进去。
    """
    from ddp_core.rerank import RerankConfig

    return RerankConfig(
        enabled=settings.rerank_enabled,
        endpoint=settings.rerank_endpoint,
        # 留空用 service_token —— 这条口径留在本层，core 不该知道 service_token 是什么
        token=settings.rerank_token or settings.service_token,
        model=settings.rerank_model,
        timeout=settings.rerank_timeout,
    )


def assert_secrets_configured() -> None:
    """启动即失败，而不是带着占位密钥安静地跑起来。

    - jwt_secret 是占位值 = 任何人都能给任意 user_id 伪造一个有效会话
    - service_token 是占位值 = 内网回调端点（/internal/*）对全世界敞开

    这两条都不会在运行时报任何错，只会安静地把整套鉴权变成摆设 —— 正是
    必须在启动时拦下来的那类问题。
    """
    if settings.allow_insecure_defaults:
        print("[config] WARNING: ALLOW_INSECURE_DEFAULTS 已开启，占位密钥检查被跳过")
        return
    bad = [name for name in ("jwt_secret", "service_token")
           if getattr(settings, name).strip().lower() in _PLACEHOLDER_SECRETS]
    if bad:
        raise RuntimeError(
            f"拒绝启动：{', '.join(n.upper() for n in bad)} 还是占位值。"
            " 请在 .env 里设置真实随机值（例如 python -c \"import secrets;"
            " print(secrets.token_urlsafe(32))\"）。"
            " 确实需要用占位值启动请显式设置 ALLOW_INSECURE_DEFAULTS=true。"
        )
