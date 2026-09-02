"""语料 API 自己的表（PostgreSQL 生产 / SQLite 单测）。

除向量列外只用可移植类型（String / JSON / DateTime），不用 PG 专有的 UUID、JSONB：
单测因此能在 SQLite in-memory 里跑完，不必为跑测试起一套 PG。向量列见 `ddp_core.types`。

## 这里放什么，不放什么

    ddp_core       Document · ParseJob · Chunk · Evidence · Citation · 知识层
                   （两侧共用的语料模型与 Base）
    这里           Conversation · Message · Extraction*（HTTP 产品壳）
                   CorpusOutbox · ProcessedEvent（跨边界事件）
    control（Go）  Organization · User · Membership · ApiKey · UsageLedger · AuditEvent

**账号层已整体迁出。** 合仓前这里有 `User` / `ApiKey` / `UsageRecord` / `FileToken`
四张表，现在它们住在 control schema，由 Go 独占写入（企业边界 5）。
本文件里因此**一个指向 `users.id` 的外键都没有** —— 只有裸的 `actor_id`。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 语料模型与 Base 都在 core（两侧共用同一份 metadata）。
# **这里原样再导出**，让既有的 `from ddp_corpus.models import Document` 一字不用改。
from ddp_core.models import (  # noqa: F401
    AgentTurn, Assertion, Base, Chunk, Citation, Document, DocumentUpload, Evidence,
    EvidenceVerification, GraphEdge, KnowledgeEntity, KnowledgeReview, ParseJob,
    RetrievalCandidate, WikiEntry, WikiSection, WikiSentence,
    as_aware, new_id, utcnow,
)


class Conversation(Base):
    """问答会话，绑 Document 而不是某次解析——换解析版本不该丢历史对话。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 发起人。**无外键**：用户在 control schema（见模块 docstring）
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    organization_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class Message(Base):
    """一轮问或答。

    verified / degraded 是**降级可见性**的落点：静默降级是这个项目吃过大亏的地方
    （M4a 的向量检索静默退回 BM25），回答没做视觉验证必须让用户看得见。
    取值由 `packages/contracts/enums.yaml` 的 `degraded` 生成，
    `scripts/check_enum_usage.py` 反向扫描代码里出现的字面量。

    model_meta 是**可比较性**的落点：不记下这一轮用了哪个 chat / embedding 模型、
    哪套检索参数，换模型之后历史数据就无法分组对比——而那正是判断
    "新配置有没有变好"的唯一依据。见 ddp_corpus/qa.py::answer_model_meta。
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id"),
                                                  index=True)
    role: Mapped[str] = mapped_column(String(16))                  # user | assistant
    content: Mapped[str] = mapped_column(Text, default="")
    # 出处**不在这里**：它住在 evidence / citations 两张表，
    # 由 `ddp_corpus.evidence.load_citations` 接回当前索引。
    # 这里曾经有一个 JSON 列，与新表并存了两个阶段——
    # 留着两个真相的代价是：其中一份没人维护、没人读，却长得跟真的一模一样
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    degraded: Mapped[str | None] = mapped_column(String(32), default=None)
    # {chat_model, embedding_model, embedding_dim, retrieval:{...}}
    model_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ExtractionTemplate(Base):
    """抽取模板：一份可复用的受限 JSON Schema。

    存在的理由是**批量**：抽取的真实用法是"一批同类文档 -> 一张表"，
    而 schema 写起来有成本（每个字段都要写 description，那是它的检索 query）。
    写一次、跑很多批，模板才让这件事成立。

    schema 只做**受限子集**（顶层 object 或 array，叶子必须带 description，
    不支持嵌套/oneOf/$ref）—— 边界与理由见
    `packages/contracts/ddp/extract-format.md`。
    """

    __tablename__ = "extraction_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    organization_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    schema_json: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("actor_id", "name", name="uq_extraction_templates_actor_name"),
    )


class ExtractionRun(Base):
    """一次抽取执行。一个 run 可以横跨多个文档（批量），这是与问答最大的结构差别。

    **schema_json 是快照，不是外键取值。** 模板改了之后，历史 run 的结果必须还能
    解释得通 —— 用模板当前的 schema 去渲染一份三个月前的结果，列会对不上号。
    同一条教训在 Message.model_meta 上已经吃过一次（换模型后历史问答无法分组对比）。
    """

    __tablename__ = "extraction_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    organization_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 模板可以被删，run 不该跟着消失 —— 所以是可空的弱引用，真正的依据是 schema_json
    template_id: Mapped[str | None] = mapped_column(String(32), default=None)
    name: Mapped[str] = mapped_column(String(128), default="")
    schema_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(8), default="object")   # object | array
    # pending | running | succeeded | partial | failed（契约 run_status）
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
    **出处不在这份 JSON 里**：它住在 evidence / citations 两张表，
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


class CorpusOutbox(Base):
    """出站事件。**跨服务边界的唯一正确姿势。**

    业务数据与事件在同一个本地事务里提交，再由投递器发给 control-api。
    分两次写（先改状态、再发请求）的话，进程在中间崩溃会留下一个
    "状态已变但没人知道"的洞 —— 用量记不上、配额扣不掉。

    投递是**至少一次**的，所以消费端必须按 id 幂等（control 侧
    `usage_ledger.event_id` 上有唯一约束）。
    """

    __tablename__ = "corpus_outbox"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 失败原因必须持久化：只写日志的话，运维看到的是"账目不对"
    # 而不是"事件投了 7 次都是 502"
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                      index=True)


class UsageClaim(Base):
    """"这个人已经为这个 job 付过费了" —— **按 (actor, job) 判重的锚点。**

    ## 为什么需要一张表

    计量的真相在 control schema（Go 出账单），语料侧查不到它。而"同一个
    解析任务被多个用户共享时，每个人各记一次"这条计费语义**必须在发事件
    之前判**，否则会重复记账。

    ## 为什么是按 (用户, job) 而不是按 job

    全局去重之前，每个用户各有一份 Document 与 ParseJob，`job.page_count == 0`
    就是"这次任务还没记过账"的锚点。全局去重之后一个 job 被多个用户共享，
    那个锚点从**按任务**变成了**按语料**：第二个用户拿到的 job 早就
    `page_count != 0`，于是**完全不计费**，可以无限白嫖解析并绕过配额
    （实测：B used_pages=0）。

    按 (用户, job) 判重**恰好还原去重之前的计费行为**。不是新政策，
    是在新数据模型下把老语义保住。

    ## 为什么不是"算力只花了一次所以第二个人免费"

    三条理由，第二条最硬：

    1. 这里的计量是**产品配额**不是成本核算。配额是对单个客户的授权额度；
       去重让页数免费的话，客户的额度就取决于**别的客户碰巧传没传过同一份
       文件** —— 不可预测、不可对账。
    2. **它会变成一条侧信道。** "这次收费了没有"直接泄露"这份文档在本部署里
       是不是已经有人传过"。拿一批候选文件挨个提交、看哪些不扣费，就能反推出
       别人的语料构成 —— 等于把计费口径做成了探测接口。
    3. 省下来的算力钱本来就归运营方（GPU 只跑一次，边际成本真降了）。
       这份收益不必靠给用户打折来兑现。
    """

    __tablename__ = "usage_claims"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    parse_job_id: Mapped[str] = mapped_column(String(32), ForeignKey("parse_jobs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("actor_id", "parse_job_id", name="uq_usage_claims_actor_job"),
    )


class ProcessedEvent(Base):
    """已消费的入站事件 —— **幂等消费的落点**。

    control 侧的投递器是"至少一次"的，所以同一个 `DocumentSubmitted`
    可能到达好几次。没有这张表的话，一次网络抖动就会让同一份上传
    变成两个 Document、两次解析、两次计费。
    """

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    organization_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 处理结果的引用（如新建的 document_id），便于重投时直接返回同一个结果
    result_id: Mapped[str | None] = mapped_column(String(64), default=None)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
