"""数据模型（PostgreSQL 生产 / SQLite 单测）。

除向量列外只用可移植类型（String / JSON / DateTime），不用 PG 专有的 UUID、JSONB：
单测因此能在 SQLite in-memory 里跑完，不必为跑测试起一套 PG。向量列见 app/types.py。

模型的核心是 Document 与 ParseJob 分离（ADR #15）：
  Document = 用户的一份文件（内容 sha256 唯一）
  ParseJob = 对它的一次解析（换引擎/参数就是一条新 job）
问答、检索、分享都绑 Document；换参数重解析、版本对比才有地方安放。
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.types import Vector


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


class Document(Base):
    """用户的一份文件。

    doc_id = 文件内容 sha256：本层去重键，也作为契约的 doc_id 传给 service，
    使 service 的幂等复用与向量索引分块键稳定（预签名/临时 URL 每次都变）。

    origin 参与去重键：同一份文档从 Web 与对外 API 两个平面提交是两件独立的事，
    混用会重复计费并覆写归档结果（M5 真机 e2e 抓到过）。
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(8), default="web")   # web | external
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 空串 = 外部提交：文件在调用方那儿，本层不下载也不归档
    object_key: Mapped[str] = mapped_column(String(512), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    # 当前生效的解析版本。无 FK 约束：与 parse_jobs 互相引用，加 FK 会形成建表循环
    current_job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # none | pending | indexing | ready | failed —— 索引失败必须能在 UI 上看到，不许静默
    index_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    index_error: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "doc_id", "origin", name="uq_documents_user_doc_origin"),
        Index("ix_documents_user_deleted_created", "user_id", "deleted_at", "created_at"),
    )


class ParseJob(Base):
    """对一份文件的一次解析。status 是本层状态机，比契约四态多一个 archiving。

    options_hash 让"同参数重解析"幂等命中已有 job，换参数才建新行。
    """

    __tablename__ = "parse_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("api_keys.id"),
                                                   default=None)
    engine: Mapped[str] = mapped_column(String(32), default="mineru")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    options_hash: Mapped[str] = mapped_column(String(64))
    service_task_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    result_prefix: Mapped[str | None] = mapped_column(String(512), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("document_id", "options_hash", name="uq_parse_jobs_doc_options"),
    )


class Chunk(Base):
    """检索单元。页码 + bbox + page_size 是出处三件套，缺一不可：

    - page_idx  出处落到唯一页（chunk 永不跨页）
    - bbox      高亮与裁剪的区域
    - page_size 坐标换算的基准，缺它遇到 CropBox 偏移/旋转页会裁错区域

    **id 不是出处的定位键**：它是随机 UUID，每次 reindex 都会重铸
    （indexing.py 先 DELETE 再 add_all）。稳定定位键是
    `(document_id, parse_job_id, seq)` —— 同一份解析重建索引时它保持不变，
    历史 citations 靠它接回原文。唯一约束把这条不变式钉死在库上。
    """

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    parse_job_id: Mapped[str] = mapped_column(String(32), ForeignKey("parse_jobs.id"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    page_idx: Mapped[int] = mapped_column(Integer, default=0)
    bbox: Mapped[list | None] = mapped_column(JSON, default=None)
    page_size: Mapped[list | None] = mapped_column(JSON, default=None)
    text: Mapped[str] = mapped_column(Text)
    char_len: Mapped[int] = mapped_column(Integer, default=0)      # prompt 预算用
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim),
                                                          default=None)

    __table_args__ = (
        Index("ix_chunks_doc_page_seq", "document_id", "page_idx", "seq"),
        UniqueConstraint("document_id", "parse_job_id", "seq", name="uq_chunks_doc_job_seq"),
    )


class Conversation(Base):
    """问答会话，绑 Document 而不是某次解析——换解析版本不该丢历史对话。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class Message(Base):
    """一轮问或答。

    verified / degraded 是**降级可见性**的落点：静默降级是这个项目吃过大亏的地方
    （M4a 的向量检索静默退回 BM25），回答没做视觉验证必须让用户看得见。

    model_meta 是**可比较性**的落点：不记下这一轮用了哪个 chat / embedding 模型、
    哪套检索参数，换模型之后历史数据就无法分组对比——而那正是判断
    "新配置有没有变好"的唯一依据。见 app/qa.py::answer_model_meta。
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id"),
                                                  index=True)
    role: Mapped[str] = mapped_column(String(16))                  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # [{chunk_id, parse_job_id, seq, page_idx, bbox, crop_key, snippet, score}]
    # chunk_id 会随 reindex 失效，(parse_job_id, seq) 才是能一直接回原文的定位键
    citations: Mapped[list] = mapped_column(JSON, default=list)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # no_hits | embedding_unavailable | vision_unavailable | crop_unsupported | crop_failed
    # | parse_mismatch | client_aborted | upstream_error | upstream_interrupted
    degraded: Mapped[str | None] = mapped_column(String(32), default=None)
    # {chat_model, embedding_model, embedding_dim, retrieval:{...}}，见 qa.answer_model_meta
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FileToken(Base):
    """稳定文件 URL 的凭证：/files/{token} -> 原件。

    存在的理由：service 要能下载文件，而 MinIO 预签名 URL 会过期且每次签名不同
    （MCP 平面的 ask_document 只有 file_url、传不了 doc_id，URL 一变检索缓存就失效）。
    token 本身即凭证（32 字节随机），可撤销、可设过期。
    """

    __tablename__ = "file_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    scope: Mapped[str] = mapped_column(String(8), default="source")   # source | share
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageRecord(Base):
    """计量流水：按页（解析）与按次（所有平面）。用量图表与额度扣减的唯一数据源。"""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("api_keys.id"),
                                                    default=None)
    parse_job_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("parse_jobs.id"),
                                                      default=None)
    kind: Mapped[str] = mapped_column(String(16))   # parse | chat | embeddings | mcp | qa | embed
    pages: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


Index("ix_usage_user_created", UsageRecord.user_id, UsageRecord.created_at)
