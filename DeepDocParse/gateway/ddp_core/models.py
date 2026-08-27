"""语料的数据模型 —— **两侧共用的唯一一份**（阶段 2a 从 web 层迁入）。

## 这里放什么，不放什么

按 `plan.md` §2 的划线：**core = 语料服务器**（编译 / 证据 / 检索 / 生成 / 知识），
**web 端 = 账号 / API key / 配额与计量 / 对外 HTTP / 前端**。所以：

    这里（core）   Document · DocumentUpload · ParseJob · Chunk
    留在 web 层    User · ApiKey · UsageRecord（账号层）
                   Conversation · Message · Extraction*（暂留，后续阶段再看）

**`Base` 也在这里**，两侧共用同一份 metadata —— 否则 `create_all` 与 alembic
只看得见一半的表。web 层的模型 import 这个 Base 定义自己的表，
跨包外键按表名字符串解析（SQLAlchemy 在 mapper 配置时才解析），照常工作。

## 除向量列外只用可移植类型

String / JSON / DateTime，不用 PG 专有的 UUID、JSONB：单测因此能在
SQLite in-memory 里跑完，不必为跑测试起一套 PG。向量列见 `ddp_core.types`。

## 向量维度为什么可以读环境变量

`Chunk.embedding` 的维度**只影响 `create_all`**（单测走 SQLite，那里
Vector 退化成 JSON，维度根本不参与），生产的列类型由 alembic 迁移写死。
所以这里读 `EMBEDDING_DIM` 环境变量、默认 1024 是安全的：
它是声明性的文档值，不是运行时的真值来源。

⚠️ **它与 web 层的 `settings.embedding_dim` 不是同一个加载语义**：
这里是裸 `os.environ`，**不读 `.env` 文件**；那边走 pydantic-settings，读。
今天两者行为等价（理由见上一段：这个值不参与任何运行时判断），
但**别把这个常量挪去做真值来源** —— 那一刻两个加载语义就会分叉，
表现是"改了 .env 但维度没变"。真要让 core 知道维度，照 `RerankConfig`
那个办法收成显式入参，别再加第二个全局。
"""
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ddp_core.types import Vector

# 见模块 docstring 最后一段：这个值只用于 create_all 的声明，不是运行时真值
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))


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


class Document(Base):
    """语料里的一份文件。**不属于任何用户。**

    doc_id = 文件内容 sha256：去重键，也作为契约的 doc_id 传给 service，
    使 service 的幂等复用与向量索引分块键稳定（预签名/临时 URL 每次都变）。

    origin 参与去重键：同一份文档从 Web 与对外 API 两个平面提交是两件独立的事，
    混用会重复计费并覆写归档结果（M5 真机 e2e 抓到过）。

    **1b 起：一次部署 = 一份语料 = 一个知识库**（plan.md §2 已定 2）。
    `uploaded_by` 只是**归属署名**，不再是可见性边界 —— 谁都看得见全部语料，
    检索天然跨全语料。全部上传者记在 `document_uploads` 里：同一份文件被
    好几个人先后传过，那仍是**同一份语料**，不是几份。

    **去重因此变成全局的**：唯一约束从 (user_id, doc_id, origin) 收成
    (doc_id, origin)。收益是实打实的钱 —— 以前两个人传同一份手册 =
    两次解析 + 两次索引 + 两套 embedding，而 GPU 按小时租。
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 首个上传者，**仅归属署名** —— 不是可见性边界，也不是授权依据。
    # 全部上传者见 DocumentUpload；删除权限判的是"在不在那张表里，或是不是管理员"
    uploaded_by: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
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
        # **全局去重**：同一份文件在同一个 origin 下，整个部署只存一份
        UniqueConstraint("doc_id", "origin", name="uq_documents_doc_origin"),
        # 列表页按 (未删, 时间倒序) 翻页，不再有 user 维
        Index("ix_documents_deleted_created", "deleted_at", "created_at"),
    )


class DocumentUpload(Base):
    """谁传过这份文件 —— **一份文档可以有多个上传者**。

    全局去重之后，第二个人传同一份文件不会产生第二个 Document，
    但"他也传过"这件事不能丢：删除权限判它，界面上也要说得清这份语料从哪来。
    §11 已定 6「合并并保留全部归属」说的就是这张表。
    """

    __tablename__ = "document_uploads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("document_id", "user_id", name="uq_document_uploads_doc_user"),
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
    # **谁发起的这次解析**，用来记账。语料共享之后（1b）任何人都能对任一文档点
    # 重新解析 / 重建索引，而那是要花钱的（GPU 按小时租）——
    # 按 `documents.uploaded_by` 记账等于"谁传的谁买单"，别人能随意花掉他的额度。
    # 可空：迁移过来的老 job 没有这个信息，那时退回按上传者记（见 archive.py）。
    initiated_by: Mapped[str | None] = mapped_column(String(32), default=None)
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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM),
                                                          default=None)

    __table_args__ = (
        Index("ix_chunks_doc_page_seq", "document_id", "page_idx", "seq"),
        UniqueConstraint("document_id", "parse_job_id", "seq", name="uq_chunks_doc_job_seq"),
    )


# ---------------------------------------------------------------------------
# Evidence 是一等实体（plan.md §5.1，阶段 2b 新建）
# ---------------------------------------------------------------------------
#
# 在它之前，"出处"只是 `messages.citations` / `extraction_items.fields[].citations`
# 里的一段 JSON：查不了、连不了、没法反查"这条证据被谁引过"，更没有地方安放
# 复核状态。三条系统级属性（可追溯 / 可复核 / 可更新）各自缺一个支点。
#
# **阶段 2b 只双写，不改读。** 老的两处 JSON 照常写，这两张表同时写一份；
# 读切换与历史回填是阶段 3。所以这一步随时可以停：drop 掉两张表即可回滚。


def digest_of(text: str) -> str:
    """内容指纹 —— 「可更新」的支点，阶段 3 靠它判断"这个块还是不是当初那段话"。

    归一化只压空白，**不动标点也不改大小写**：这里要的是"内容变没变"，
    不是"读起来像不像"。口径与 `app/qa.py::_same_content` 的前半段一致
    （那边压完空白之后做的是子串包含，因为它手上只有截断过的 snippet）。

    阶段 3 会有**两条路**：新记录有 digest 走 digest（严格），
    老记录只有 snippet 只能走 `_same_content`（宽松）。别以为 digest 是全覆盖的。
    """
    import hashlib

    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()


class Evidence(Base):
    """一条证据：文档里一个可定位、可复核、可追溯的区域。

    与 `Chunk` 的关系：chunk 是**检索单元**（每次 reindex 都重铸，id 是随机 UUID），
    evidence 是**被引用过的那个区域的稳定身份**（跨重建不变）。
    同一个块被引 N 次只有一行 evidence —— 唯一约束 `(parse_job_id, seq)` 钉着，
    用的正是 chunks 那把稳定定位键的后两段。
    """

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    # §5.1 列了这个字段，先建上但**本阶段恒 0**：documents today 没有版本概念。
    # 文档换版（同一份手册的 v2）要到阶段 5 编译层才有意义，那时它才开始有值
    doc_version: Mapped[int] = mapped_column(Integer, default=0)
    parse_job_id: Mapped[str] = mapped_column(String(32), ForeignKey("parse_jobs.id"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    page_idx: Mapped[int] = mapped_column(Integer, default=0)
    bbox: Mapped[list | None] = mapped_column(JSON, default=None)
    # 缺它遇到 CropBox 偏移/旋转页会裁错区域 —— 出处图对不上原文是最恶劣的一种错。
    # **问答侧的 citation dict 里没有这个字段**，所以 evidence 一律从 chunks 行取，
    # 不从 citation dict 取（那样会静默存成 NULL）
    page_size: Mapped[list | None] = mapped_column(JSON, default=None)
    # 取自 `chunks.block_type`（DDP-Layout v1.1 词汇表）。
    # ⚠️ 与 §5.1 写的那张表**不完全一样**：这里有 `title`（真实存在的块类型），
    # 没有 `code`（§5.3 的 code 原子要到阶段 5 编译层才产出）。
    # 如实记录今天真有的东西，别为了对齐一份未来的表而造假
    kind: Mapped[str] = mapped_column(String(16), default="text", index=True)
    # 编译期产出的裁图。阶段 2b 只是把问答/抽取当时顺手裁的那张记下来，
    # 「编译期就产出」是阶段 5 的事
    crop_key: Mapped[str | None] = mapped_column(String(512), default=None)
    content_digest: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 「可追溯」的支点：哪个引擎、哪个模型、什么版本产出的这块版面
    provider: Mapped[dict] = mapped_column(JSON, default=dict)
    # 「可复核」的支点。**默认 unreviewed 而不是 NULL** —— NULL 会让阶段 7 的
    # 复核队列分不清"还没人看过"和"这行是旧数据、字段那时还不存在"
    review_state: Mapped[str] = mapped_column(String(16), default="unreviewed", index=True)
    # 生成物标记：VLM 描述指向它所描述的原子。阶段 5 才开始有值。
    # 不变式 3（生成内容必须与原文可区分）在库上的落点就是这一列非空
    derived_from: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # 同一个块只有一行证据。用的是 chunks 那把稳定定位键的后两段 ——
        # document_id 不进约束是因为它由 parse_job 唯一决定，进去反而允许出现
        # "同一次解析的同一个块挂在两个文档下"这种不可能的行
        UniqueConstraint("parse_job_id", "seq", name="uq_evidence_job_seq"),
        Index("ix_evidence_doc_page", "document_id", "page_idx"),
    )


class Citation(Base):
    """谁引了哪条证据。**有外键，可反查** —— 这是它相对老 JSON 的全部意义。

    ⚠️ 表名 `citations` 与 `messages.citations` 那个 **JSON 列**同名。
    不是笔误：阶段 3 读切换之后那个列会退成只读，阶段 4 删掉。
    在那之前，"citations" 这个词在库里同时指两样东西，写查询时看清楚。
    """

    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    evidence_id: Mapped[str] = mapped_column(String(32), ForeignKey("evidence.id"), index=True)
    # message | extract_field —— 阶段 6 上线断言后加 assertion，阶段 7 加 graph_edge /
    # wiki_sentence。**§5.1 的词汇表里没有 message**：那份表是按"答案是断言序列"
    # （§5.2，阶段 6）写的，而今天问答的出处主体就是 messages 行。
    # 硬套 assertion 是在契约里撒谎 —— 阶段 6 的人会照着字段名去 join 断言表
    source_kind: Mapped[str] = mapped_column(String(16))
    # message -> messages.id；extract_field -> f"{extraction_items.id}:{字段名}"
    source_id: Mapped[str] = mapped_column(String(128))
    # primary | supporting | rejected。
    # **阶段 2b 只写 primary**：`rejected`（"为什么没引这条"）要求保留被丢弃的
    # 检索候选，而今天候选出了 retrieve() 就没了。捕获它是独立的一件事，
    # 混进这一刀会让"老 JSON 与新表逐条相同"这条验收标准失真。
    # ⚠️ 与 `Evidence.review_state` 的 `rejected`（人工驳回）**不是一回事**
    role: Mapped[str] = mapped_column(String(16), default="primary")
    # RRF 融合分（只由名次决定）与余弦相似度（有校准量纲）。两把不同的尺子，
    # 别混用 —— 详见 ddp_core/hits.py 的说明
    score: Mapped[float | None] = mapped_column(Float, default=None)
    similarity: Mapped[float | None] = mapped_column(Float, default=None)
    # 引用当时看到的那段文字。留着它是为了阶段 3 回填老记录时还有东西可比
    snippet: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_citations_source", "source_kind", "source_id"),
        # 同一个来源不该把同一条证据引两次（同一条 message 的 citations 列表里
        # 出现两个相同的 (parse_job_id, seq) 是数据错误，不是"引用了两次"）
        UniqueConstraint("source_kind", "source_id", "evidence_id", "role",
                         name="uq_citations_source_evidence"),
    )
