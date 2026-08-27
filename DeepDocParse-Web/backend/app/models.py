"""数据模型（PostgreSQL 生产 / SQLite 单测）。

除向量列外只用可移植类型（String / JSON / DateTime），不用 PG 专有的 UUID、JSONB：
单测因此能在 SQLite in-memory 里跑完，不必为跑测试起一套 PG。向量列见 `ddp_core/types.py`。

模型的核心是 Document 与 ParseJob 分离（ADR #15）：
  Document = 用户的一份文件（内容 sha256 唯一）
  ParseJob = 对它的一次解析（换引擎/参数就是一条新 job）
问答、检索、分享都绑 Document；换参数重解析、版本对比才有地方安放。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 语料模型与 Base 都在 core（两侧共用同一份 metadata）。
# **这里原样再导出**，让既有的 `from app.models import Document` 一字不用改。
from ddp_core.models import (  # noqa: F401
    Base, Chunk, Document, DocumentUpload, ParseJob, as_aware, new_id, utcnow,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 语料不隔离之后，**全站只剩一处授权判断**：谁能删文档（上传者或管理员）。
    # 除此之外账号层只管认证、计量、限速，不管授权（plan.md §2 已定 2）。
    # 目前没有提升管理员的界面 —— 需要时直接改库。这是有意的：
    # 加一套权限管理 UI 远超本阶段范围，而唯一用途只有"删别人传的文档"。
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # 出处**不在这里**（阶段 4 起）：它住在 evidence / citations 两张表，
    # 由 `app.evidence.load_citations` 接回当前索引。
    # 这里曾经有一个 JSON 列，与新表并存了两个阶段（2b 双写 / 3 读切换）——
    # 留着两个真相的代价是：其中一份没人维护、没人读，却长得跟真的一模一样
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # no_hits | embedding_unavailable | vision_unavailable | crop_unsupported | crop_failed
    # | parse_mismatch | client_aborted | upstream_error | upstream_interrupted
    # | schema_violation（抽取平面）| rerank_unavailable（配了精排但上游没注册）
    # | no_instruct_model（上游只有 OCR 专用模型，抽值无处可调）
    degraded: Mapped[str | None] = mapped_column(String(32), default=None)
    # {chat_model, embedding_model, embedding_dim, retrieval:{...}}，见 qa.answer_model_meta
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ExtractionTemplate(Base):
    """抽取模板：一份可复用的受限 JSON Schema。

    存在的理由是**批量**：抽取的真实用法是"一批同类文档 -> 一张表"，
    而 schema 写起来有成本（每个字段都要写 description，那是它的检索 query）。
    写一次、跑很多批，模板才让这件事成立。

    schema 只做**受限子集**（顶层 object 或 array，叶子必须带 description，
    不支持嵌套/oneOf/$ref）—— 边界与理由见 ../DeepDocParse/docs/extract-format.md。
    """

    __tablename__ = "extraction_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    schema_json: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_extraction_templates_user_name"),
    )


class ExtractionRun(Base):
    """一次抽取执行。一个 run 可以横跨多个文档（批量），这是与问答最大的结构差别。

    **schema_json 是快照，不是外键取值。** 模板改了之后，历史 run 的结果必须还能
    解释得通 —— 用模板当前的 schema 去渲染一份三个月前的结果，列会对不上号。
    同一条教训在 Message.model_meta 上已经吃过一次（换模型后历史问答无法分组对比）。
    """

    __tablename__ = "extraction_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    # 模板可以被删，run 不该跟着消失 —— 所以是可空的弱引用，真正的依据是 schema_json
    template_id: Mapped[str | None] = mapped_column(String(32), default=None)
    name: Mapped[str] = mapped_column(String(128), default="")
    schema_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(8), default="object")   # object | array
    # pending | running | succeeded | partial | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 与 Message.model_meta 同一个作用：不记下这一轮用了什么模型与检索参数，
    # 换配置后就无法判断"新配置有没有变好"
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class ExtractionItem(Base):
    """抽取结果的一行。**行 = (文档, 记录序号)**，这一个形状同时覆盖两种 schema：

      顶层 object -> 一份文档一行（record_index 恒为 0）
      顶层 array  -> 一份文档 N 行（表格的 N 条记录）

    前端的结果表格就是它：行是 item，列是 schema 字段，点单元格跳原件 bbox。

    fields 的形状是 DDP-Extract v1 的字段表：
      {"字段名": {status, value, verified, degraded, confidence}}
    **出处不在这份 JSON 里**（阶段 4 起）：它住在 evidence / citations 两张表，
    来源键是 `{item_id}:{字段名}` —— 抽取的出处是字段级的，
    "这个字段的值是从哪一块抽出来的"正是本产品相对"字段 + 置信度"那类
    抽取产品的差异点。对外响应仍然带 citations，由 `_fields_out` 现拼。
    """

    __tablename__ = "extraction_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("extraction_runs.id"), index=True)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    parse_job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    record_index: Mapped[int] = mapped_column(Integer, default=0)
    # ok | partial | failed —— 与 DDP-Extract 的整体 status 同义
    status: Mapped[str] = mapped_column(String(16), default="ok")
    degraded: Mapped[str | None] = mapped_column(String(32), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "document_id", "record_index",
                         name="uq_extraction_items_run_doc_record"),
        Index("ix_extraction_items_run_created", "run_id", "created_at"),
    )


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
    # parse | chat | embeddings | mcp | qa | embed | extract
    # extract 按**字段数**计 requests：一次抽取 = N 次检索 + N 次模型调用，
    # 按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜
    kind: Mapped[str] = mapped_column(String(16))
    pages: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


Index("ix_usage_user_created", UsageRecord.user_id, UsageRecord.created_at)
