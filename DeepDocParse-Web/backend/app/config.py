"""配置。

分三组：本层自有资源（PG/MinIO/JWT）、对 service 的调用参数、额度默认值。
service 相关的一切只对应 ../DeepDocParse/openapi.yaml 的契约，不感知其内部实现。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 仓库根的 .env 与 backend/.env 都读（后者优先）——backend 与 alembic 的 cwd 不同
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    # ---- 本层资源 ----
    database_url: str = "postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse"
    jwt_secret: str = "change-me"
    jwt_ttl_minutes: int = 60 * 24 * 7

    # MinIO：service 与浏览器走不同 endpoint —— 预签名 URL 的签名覆盖 host，
    # 两边 host 不同则签名不同，必须分开生成（见 CLAUDE.md 部署陷阱）。
    # 端口 19000/19001：9000 被 gateway 占用，且要避开 Windows 保留段 7964-8063。
    minio_internal_endpoint: str = "127.0.0.1:19000"   # service / 本进程可达
    minio_public_endpoint: str = "127.0.0.1:19000"     # 浏览器可达
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "deepdocparse"

    # ---- 对 service（DeepDocParse）----
    service_url: str = "http://127.0.0.1:9000"
    mcp_url: str = "http://127.0.0.1:9100"
    service_token: str = "change-me"
    # 解析平面必须走 service（那是它的本职）；但 embedding 与 chat 只要求 OpenAI 兼容，
    # 留独立配置以免把本层绑死在 DeepDocParse 的部署形态上（ADR #17）。
    # 留空则回落到 {service_url}/v1/...，dev 下什么都不用配。
    embedding_url: str = ""            # 如直连 TEI：http://127.0.0.1:18080/v1/embeddings
    embedding_token: str = ""          # 留空用 service_token
    embedding_model: str = ""          # 留空由上游注册表选 default
    chat_url: str = ""                 # 如直连 vLLM：http://.../v1/chat/completions
    chat_token: str = ""
    chat_model: str = ""
    # 本服务对 service 可达的外部地址：稳定文件 URL 与解析回调都用它拼。
    # 宿主机混合模式用 127.0.0.1:8080，全容器模式用服务名（http://web-backend:8080）。
    public_base_url: str = "http://127.0.0.1:8080"

    # 多副本部署必须配：限速计数与对账选主都靠它。留空 = 单实例模式（进程内计数）
    redis_url: str = ""

    # ---- 归档与对账 ----
    # 必须 <= service 的 RESULT_TTL(24h)：超过这个窗口结果就被 service 清了，补取不回来
    result_ttl: int = 86400
    reconcile_interval: int = 60

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
    qa_history_turns: int = 6           # 带入的历史消息条数
    qa_crop_count: int = 1              # 做视觉验证的区域数
    qa_rate_per_min: int = 20           # 每用户问答限速
    # 视觉模型在 CPU 上出第一个 token 可能要几分钟（dev 机常态），读超时要留够
    chat_read_timeout: float = 900.0

    # ---- 额度默认值（新建 key 时的初值）----
    default_quota_pages: int | None = 1000   # None = 不限
    default_rate_limit_per_min: int = 60

    @property
    def embeddings_endpoint(self) -> str:
        return self.embedding_url or f"{self.service_url}/v1/embeddings"

    @property
    def chat_endpoint(self) -> str:
        return self.chat_url or f"{self.service_url}/v1/chat/completions"


settings = Settings()
