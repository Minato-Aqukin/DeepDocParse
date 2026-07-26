"""数据模型（PostgreSQL 生产 / SQLite 单测）。

刻意只用可移植类型（String / JSON / DateTime），不用 PG 专有的 UUID、JSONB：
单测因此能在 SQLite in-memory 里跑完，不必为跑测试起一套 PG。
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(dt: datetime | None) -> datetime | None:
    """把库里读出的时间统一成 aware。

    PostgreSQL(timestamptz) 读出来带时区，SQLite 读出来是 naive —— 直接和 utcnow()
    比较会抛 TypeError。所有"库里的时间 vs 现在"的比较都要过这里。
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """sk- 开头的对外 key。明文只在创建时返回一次，库里只有 sha256。

    用 sha256 而非 bcrypt：每个对外请求都要验一次 key，bcrypt 的成本函数
    会直接压垮代理路径；key 本身是 32 字节随机串，不存在弱口令问题。
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="default")
    key_prefix: Mapped[str] = mapped_column(String(16))            # 展示用，如 sk-AbCdEfGh
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    quota_pages: Mapped[int | None] = mapped_column(Integer, default=None)  # None = 不限
    used_pages: Mapped[int] = mapped_column(Integer, default=0)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=60)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    """一次文档解析。status 是本层状态机，比契约的四态多一个 archiving。

    doc_id = 文件内容 sha256：既是本层去重键，也作为契约的 doc_id 传给 service，
    使 service 的幂等复用与向量索引分块键稳定（预签名/临时 URL 每次都变）。
    """

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("api_keys.id"), default=None)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    # web = 本层上传（文件在 MinIO，要归档）；external = 经 /v1/* 代理提交（文件在调用方那儿）
    # 参与去重键：同一份文档从两个平面提交是两件独立的事，混用会重复计费并覆写归档结果
    origin: Mapped[str] = mapped_column(String(8), default="web")
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    object_key: Mapped[str] = mapped_column(String(512))            # MinIO 里的原件
    service_task_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # pending | running | archiving | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    engine: Mapped[str] = mapped_column(String(32), default="mineru")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    result_prefix: Mapped[str | None] = mapped_column(String(512), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        # 同一用户、同一平面重复提交同一文件直接复用已归档结果（不再打 service）
        UniqueConstraint("user_id", "doc_id", "origin", name="uq_tasks_user_doc_origin"),
    )


class FileToken(Base):
    """稳定文件 URL 的凭证：/files/{token} -> 原件。

    存在的理由：service 要能下载文件，而 MinIO 预签名 URL 会过期且每次签名不同
    （MCP 平面的 ask_document 只有 file_url、传不了 doc_id，URL 一变检索缓存就失效）。
    token 本身即凭证（32 字节随机），可撤销、可设过期。
    """

    __tablename__ = "file_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.id"), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageRecord(Base):
    """计量流水：按页（解析）与按次（所有平面）。用量图表与额度扣减的唯一数据源。"""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("api_keys.id"), default=None)
    task_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tasks.id"), default=None)
    kind: Mapped[str] = mapped_column(String(16))          # parse | chat | embeddings | mcp
    pages: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


Index("ix_usage_user_created", UsageRecord.user_id, UsageRecord.created_at)
