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
    # 不给默认值：漏传就该在插入时炸，而不是悄悄记成 mineru —— 无 GPU 部署上
    # 那个名字在 service 注册表里根本不存在（三处构造点都显式传 engine）。
    # **光去掉这里的 default= 只在 SQLite 上生效**：PG 上 SQLAlchemy 会把整列从
    # INSERT 里省掉，0002 建表时写的 server_default 照样兜底 —— 迁移 0004 把它摘了
    engine: Mapped[str] = mapped_column(String(32))
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
    # DDP-Layout v1.1 的块类型（text/title/table/figure/equation/list/other）。
    # 表格块靠它被检索侧优先看到 —— 在它之前，表格与正文在索引里完全无法区分
    block_type: Mapped[str] = mapped_column(String(16), default="text", index=True)
    # 表格结构的唯一载体。block_text 拼出来的是拍平的单元格文字，行列关系已经没了；
    # 抽取平面把表格映射成记录数组靠的就是它。非表格块为 None
    table_html: Mapped[str | None] = mapped_column(Text, default=None)
    # D2：jieba 切好的文本，空格分隔。**关键词检索路直接查这一列**——
    # `to_tsvector('simple', text)` 会把整段中文当成一个 token，
    # 于是"混合检索"在中文文档上实际只有向量一条腿（A1 量到关键词路命中率 25%）
    text_tokenized: Mapped[str] = mapped_column(Text, default="")
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
      {"字段名": {status, value, citations, verified, degraded, confidence}}
    **citations 里存的是稳定定位键 (parse_job_id, seq)**，不是 chunk_id ——
    chunk_id 每次 reindex 都会重铸，只存它等于历史抽取结果一次重建就永久失去依据（P0）。
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
