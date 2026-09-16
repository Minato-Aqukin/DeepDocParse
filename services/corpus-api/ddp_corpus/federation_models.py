"""Federation executor tables (probe receipts, admissions, executions, coverage).

All five node-side tables (plus the coordinator/delivery rows a later slice uses)
live here, not in `models.py`: the P5 executor is one bounded feature and keeping
its schema next to its logic makes the ownership obvious.

Registered with the shared `Base` like every other corpus model, so
`Base.metadata.create_all` in tests creates them. `federation.py` imports this
module, and `main.py` mounts that module's router — that import chain is what
registers the metadata; **do not** add these to `models.py`, whose import graph
is deliberately the generic product surface.

Nothing here stores credentials or file bytes. `result_json` holds probe/execution
payloads (evidence excerpts, locators) and is read back through the HTTP boundary
after an authorization check — never dumped whole into a response by the routes.
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ddp_core.models import Base, new_id, utcnow


class FederationProbe(Base):
    """A persisted ProbeResult/locate receipt, scoped to one organization.

    `probe_id` is derived from `(organization, actor, idempotency key)`, so a
    retried probe with the same key addresses the same row; the stored result
    decides whether the retry is a replay or a conflict.
    """

    __tablename__ = "federation_probes"

    probe_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    target_node_id: Mapped[str] = mapped_column(String(64))
    task_spec_digest: Mapped[str] = mapped_column(String(71))
    consent_ref: Mapped[str] = mapped_column(String(128))
    probe_kind: Mapped[str] = mapped_column(String(24))
    collection_id: Mapped[str] = mapped_column(String(32), default="")
    query_digest: Mapped[str] = mapped_column(String(71), default="")
    # coverage_target_state 的探测侧取值：succeeded / partial / denied / ...
    state: Mapped[str] = mapped_column(String(24), index=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FederationAdmission(Base):
    """The persistent admission receipt for one executor step.

    Unique `(organization_id, idempotency_key)` is the reconciliation anchor:
    same key + same `request_digest` replays the stored receipt, same key +
    different digest is `idempotency_conflict` (never reuse an unrelated result).
    """

    __tablename__ = "federation_admissions"

    admission_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(71))
    plan_digest: Mapped[str] = mapped_column(String(71))
    root_task_id: Mapped[str] = mapped_column(String(64), index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    delegation_generation: Mapped[int] = mapped_column(Integer, default=0)
    issuer_node_id: Mapped[str] = mapped_column(String(64))
    executor_node_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), index=True)          # admission_state
    input_validation: Mapped[str] = mapped_column(String(24))           # input_validation
    executor_task_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    verified_input_manifest_digest: Mapped[str | None] = mapped_column(String(71), default=None)
    effective_policy_ref: Mapped[str] = mapped_column(String(160))
    receipt_json: Mapped[dict] = mapped_column(JSON, default=dict)
    receipt_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key",
                         name="uq_federation_admissions_org_idempotency"),
    )


class FederationExecution(Base):
    """A locally executed admitted step.

    `generation` is the fencing token, same idea as `Task.generation`: a lease
    decides who *may* take over, but the final write must still match the
    generation it started with. `cancel` bumps it so a late result is dropped
    instead of overwriting the cancellation.
    """

    __tablename__ = "federation_executions"

    executor_task_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    admission_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("federation_admissions.admission_id"), index=True)
    root_task_id: Mapped[str] = mapped_column(String(64), index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # task_status
    # 契约 ExecutionStatus 要求 generation >= 1：从 1 起算，每次领取/取消 +1。
    generation: Mapped[int] = mapped_column(Integer, default=1)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    evidence_set_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    # {"spec": {...}, "result": {...}} — internal storage; GET returns the status
    # envelope only and never serves this JSON wholesale.
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class FederationRequest(Base):
    """Coordinator-side task row (written by the later tasks slice)."""

    __tablename__ = "federation_requests"

    root_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    task_spec_digest: Mapped[str] = mapped_column(String(71))
    scope_id: Mapped[str] = mapped_column(String(128), default="")
    scope_digest: Mapped[str] = mapped_column(String(71), default="")
    search_mode: Mapped[str] = mapped_column(String(24), default="fast")
    planning_state: Mapped[str] = mapped_column(String(24), default="draft")
    plan_revision: Mapped[int] = mapped_column(Integer, default=0)
    plan_digest: Mapped[str] = mapped_column(String(71), default="")
    execution_consent_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    retrieval_completeness: Mapped[str] = mapped_column(String(24), default="not_started")
    evidence_sufficiency: Mapped[str] = mapped_column(String(24), default="unknown")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    delivery_id: Mapped[str | None] = mapped_column(String(32), default=None)
    delivery_state: Mapped[str] = mapped_column(String(24), default="not_requested")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # ---- 协调者（B1）追加：A4 的列一个都没动，这些是新语义需要的载体 ----
    # 需求/许可原文：规划、审批、执行都要按当时批准的内容重放，不能只看摘要。
    task_spec_json: Mapped[dict] = mapped_column(JSON, default=dict)
    exploration_consent_json: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_consent_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    # federation_public 由控制面传入；site_public/fixed_resources 由本节点枚举或构造。
    scope_manifest_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    # planning_state=ready 之后持久化；审批只改 planning_state/execution_consent_ref，
    # 两处都在 plan_digest 之外，因此计划摘要保持不变。
    plan_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    # POST /tasks 的幂等锚：一个 root task 只受理一个键，补做走 resume。
    idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    # POST /task-intents 的幂等锚。与上面的执行受理键分开：丢响应后的显式重试
    # 必须落回同一个 intent，而不是造第二个 root task（T80/T81）。
    intent_idempotency_key: Mapped[str | None] = mapped_column(String(128), default=None)
    # 首次请求实体的摘要（task_spec + 探索许可 + 显式 scope manifest）。本地枚举
    # 生成的 manifest 每次都不同，所以只能用入参摘要判"同键异实体"。
    intent_request_digest: Mapped[str | None] = mapped_column(String(71), default=None)
    # 每次受理/恢复 +1，进 admission 的 idempotency key，让 resume 能重新执行
    # 而不是重放旧回执。
    delegation_generation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key",
                         name="uq_federation_requests_org_idempotency"),
        UniqueConstraint("organization_id", "intent_idempotency_key",
                         name="uq_federation_requests_org_intent_idempotency"),
    )


class FederationTaskEvent(Base):
    """协调任务的带序号事件（GET /tasks/{id}/events 的持久载体）。

    `seq` 由追加方在事务内取 `max+1`；同任务唯一约束让"两个并发写者算出同一个
    序号"在数据库层失败，而不是静默覆盖。断开重连只是从 `after` 继续读 ——
    事件流本身不承载取消语义。
    """

    __tablename__ = "federation_task_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    root_task_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("root_task_id", "seq", name="uq_federation_task_events_seq"),
    )


class CoverageLedger(Base):
    """One coverage ledger per root task (`ddp-scope-coverage/1#CoverageLedger`)."""

    __tablename__ = "coverage_ledgers"

    root_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_ref: Mapped[str] = mapped_column(String(128))
    search_mode: Mapped[str] = mapped_column(String(24))
    enumeration_state: Mapped[str] = mapped_column(String(24))
    retrieval_completeness: Mapped[str] = mapped_column(String(24))
    evidence_sufficiency: Mapped[str] = mapped_column(String(24))
    counts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_digest: Mapped[str] = mapped_column(String(71))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class CoverageEntry(Base):
    """One `(root task, target)` row: the honest per-target coverage record."""

    __tablename__ = "coverage_entries"

    root_task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("coverage_ledgers.root_task_id", ondelete="CASCADE"),
        primary_key=True)
    target_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_key_json: Mapped[dict] = mapped_column(JSON, default=dict)
    query_digest: Mapped[str] = mapped_column(String(71))
    state: Mapped[str] = mapped_column(String(24), index=True)
    probe_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    actual_index_revision: Mapped[str | None] = mapped_column(String(128), default=None)
    search_profile: Mapped[str | None] = mapped_column(String(64), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    evidence_refs_json: Mapped[list] = mapped_column(JSON, default=list)
    used_budget_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 内核把 missing_requirements 拼成依据（上限见 coverage.MAX_EXCLUSION_BASIS_CHARS）；
    # Text 与内核上限一起保证它不会在 PostgreSQL 上撞列宽 500。
    exclusion_basis: Mapped[str | None] = mapped_column(Text, default=None)


class FederationDelivery(Base):
    """Delivery receipt row (`ddp-evidence/1#DeliveryReceipt`) for the later slice."""

    __tablename__ = "federation_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    root_task_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(24), default="not_requested")
    result_manifest_digest: Mapped[str | None] = mapped_column(String(71), default=None)
    # 可交付结果文档（有界规范 JSON，无正文摘录）。读取端点直接返回它，
    # 客户端用 content_digest(canonical result) 与上面这个摘要对账。
    # 超界不持久化（None），绝不存截断版。
    result_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    retention: Mapped[str] = mapped_column(String(24), default="temporary")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    receipt_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class FederationCredentialNonce(Base):
    """One consumed node credential (`ddp-node-credential/1`), kept until it expires.

    The primary key is the credential's `jti`: a second use of the same credential —
    sequential or concurrent — collides here and is refused as `credential_replayed`.
    The row is written only **after** the signature verified, so unauthenticated
    traffic cannot fill the table. Nothing secret is stored: the jti alone cannot
    be presented (the verifier needs the signed payload), and the credential token
    itself is never persisted. The federation sweep deletes rows past `expires_at`.
    """

    __tablename__ = "federation_credential_nonces"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer_node_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
