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
    # **这个默认值还没有在真视觉模型上标定过**（本机无 GPU），拿到 GPU 机器后
    # 按实测分布重定 —— 与 qa_min_similarity 当年的定法一样。宁可漏报不要误报：
    # 误报会把好出处打成"存疑"，比不报更伤信任
    qa_parse_mismatch_threshold: float = 0.35
    # 等核对结果的上限（秒）。核对与回答并发跑，正常情况下回答先结束、这里几乎不等；
    # 但视觉模型在 CPU 上抄一段文字可能要几分钟（read 超时是 900s），
    # **没有上限的话 done 帧会被硬生生拖后几分钟**，用户看着答案已经出完却迟迟不落定。
    # 超时就当"没测出来"——宁可不打标，也不能让核对拖垮体验
    qa_verify_timeout: float = 20.0
    qa_rate_per_min: int = 20           # 每用户问答限速
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def embeddings_endpoint(self) -> str:
        return self.embedding_url or f"{self.service_url}/v1/embeddings"

    @property
    def chat_endpoint(self) -> str:
        return self.chat_url or f"{self.service_url}/v1/chat/completions"


settings = Settings()

# 占位值集合。`.env.example` 里写的是 change-me / change-me-please，
# 复制过去忘了改是最常见的部署事故
_PLACEHOLDER_SECRETS = {"", "change-me", "change-me-please", "changeme", "secret"}


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
