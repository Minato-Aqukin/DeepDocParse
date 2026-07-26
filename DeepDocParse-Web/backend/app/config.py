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
    # 本服务对 service 可达的外部地址：稳定文件 URL 与解析回调都用它拼。
    # 宿主机混合模式用 127.0.0.1:8080，全容器模式用服务名（http://web-backend:8080）。
    public_base_url: str = "http://127.0.0.1:8080"

    # ---- 归档与对账 ----
    # 必须 <= service 的 RESULT_TTL(24h)：超过这个窗口结果就被 service 清了，补取不回来
    result_ttl: int = 86400
    reconcile_interval: int = 60

    # ---- 额度默认值（新建 key 时的初值）----
    default_quota_pages: int | None = 1000   # None = 不限
    default_rate_limit_per_min: int = 60


settings = Settings()
