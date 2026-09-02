"""持久任务队列、outbox、事件去重，以及账号层剥离后的 actor 边界。

Revision ID: 0013
Revises: 0012

## 这一版做了两件事

### 1. 账号层迁出 corpus schema

`users` / `api_keys` / `usage_records` / `file_tokens` 四张表整体迁去
control schema（Go 拥有）。语料侧因此：

- 9 处指向 `users.id` 的外键**全部去掉**，列改名 `actor_id`
  （跨 schema 硬外键会把两个服务的发布顺序绑死，也让"Python 不得修改
  组织成员"失去数据库层保障）
- 所有语料表加 `organization_id`。**单组织部署也带** —— 将来上多组织
  SaaS 时要补的是隔离与 RLS，而不是给几十张表加列

**本迁移不删那四张表。** 数据要先由一次性迁移器搬进 control schema，
搬完并对账通过之后再另起一个迁移删除 —— 在同一个迁移里又搬又删，
失败时既没法回滚也说不清搬到哪了。

### 2. 持久任务队列

`tasks` / `corpus_outbox` / `processed_events` / `usage_claims` 四张新表。
理由分别写在各自模型的 docstring 里，最要紧的两条：

- `tasks.generation` 是 fencing token。**只有 lease 没有 fencing 的队列
  是不安全的**：被判死的旧 worker 醒过来照样会写结果，把新结果覆盖掉，
  而那不会报任何错
- `processed_events` 让事件消费幂等。投递是"至少一次"的，没有它，
  一次网络抖动就会让同一份上传变成两个 Document、两次解析、两次计费
"""
import sqlalchemy as sa
from alembic import op

# 编号与既有迁移保持同一形状（纯数字字符串）——
# 混用两种命名的表现是 alembic 直接 KeyError，第一次演练就撞到了
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_organization(table: str) -> None:
    """给一张语料表补 organization_id。

    `server_default=''` 而不是 NULL：存量行必须有一个确定的值，否则
    "组织边界"这条约束从第一天起就有例外，而例外会被 WHERE 子句漏掉。
    一次性迁移器会把它改写成真实的组织 id。
    """
    if _has_column(table, "organization_id"):
        return
    op.add_column(table, sa.Column("organization_id", sa.String(32),
                                   nullable=False, server_default=""))
    op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ---- 组织边界 ----
    for table in ("documents", "conversations", "extraction_templates", "extraction_runs"):
        _add_organization(table)

    # ---- actor 改名（去外键）----
    # SQLite 不支持 DROP CONSTRAINT，单测本来就不跑迁移（用 create_all），
    # 所以这一段只在真库上执行
    if not is_sqlite:
        for table, old, new in (
            ("conversations", "user_id", "actor_id"),
            ("extraction_templates", "user_id", "actor_id"),
            ("extraction_runs", "user_id", "actor_id"),
        ):
            if _has_column(table, old) and not _has_column(table, new):
                op.alter_column(table, old, new_column_name=new)

        # 跨 schema 外键一律去掉。**约束名按 PG 的默认命名规则推**，
        # 存在才删 —— 手工建过的库可能叫别的名字，那种情况留给迁移器报告
        for table, column in (
            ("documents", "uploaded_by"),
            ("document_uploads", "user_id"),
            ("evidence_verifications", "reviewer_id"),
            ("knowledge_reviews", "reviewer_id"),
            ("conversations", "actor_id"),
            ("extraction_templates", "actor_id"),
            ("extraction_runs", "actor_id"),
            ("parse_jobs", "api_key_id"),
        ):
            op.execute(sa.text(
                f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS '
                f'{table}_{column}_fkey'))

        # 抽取模板的唯一约束跟着改名
        op.execute(sa.text(
            "ALTER TABLE extraction_templates "
            "DROP CONSTRAINT IF EXISTS uq_extraction_templates_user_name"))
        op.create_unique_constraint("uq_extraction_templates_actor_name",
                                    "extraction_templates", ["actor_id", "name"])

    # ---- 持久任务队列 ----
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("dedupe_key", sa.String(128)),
        sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
        # fencing token —— 见模块 docstring
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claimed_by", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("error", sa.Text),
        sa.Column("degraded", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("kind", "dedupe_key", name="uq_tasks_kind_dedupe"),
    )
    # 领取路径的索引。**没有它，每次领取都是全表扫**，而领取是最高频的查询
    op.create_index("ix_tasks_pickup", "tasks", ["status", "run_after"])
    op.create_index("ix_tasks_kind", "tasks", ["kind"])
    op.create_index("ix_tasks_lease_until", "tasks", ["lease_until"])
    op.create_index("ix_tasks_organization_id", "tasks", ["organization_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_run_after", "tasks", ["run_after"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "corpus_outbox",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_corpus_outbox_type", "corpus_outbox", ["type"])
    op.create_index("ix_corpus_outbox_created_at", "corpus_outbox", ["created_at"])
    op.create_index("ix_corpus_outbox_next_attempt_at", "corpus_outbox", ["next_attempt_at"])
    op.create_index("ix_corpus_outbox_organization_id", "corpus_outbox", ["organization_id"])

    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("organization_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("result_id", sa.String(64)),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_processed_events_type", "processed_events", ["type"])
    op.create_index("ix_processed_events_organization_id", "processed_events",
                    ["organization_id"])

    op.create_table(
        "usage_claims",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("parse_job_id", sa.String(32),
                  sa.ForeignKey("parse_jobs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("actor_id", "parse_job_id", name="uq_usage_claims_actor_job"),
    )
    op.create_index("ix_usage_claims_actor_id", "usage_claims", ["actor_id"])
    op.create_index("ix_usage_claims_parse_job_id", "usage_claims", ["parse_job_id"])


def downgrade() -> None:
    """回滚。

    **有在途任务或未投递事件时拒绝回滚** —— 那些是"已经答应用户要做的事"，
    删掉它们等于静默毁掉用户的请求。先把队列跑干净再回滚。
    """
    bind = op.get_bind()
    pending = bind.execute(sa.text(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','claimed','running')"
    )).scalar() or 0
    undelivered = bind.execute(sa.text(
        "SELECT COUNT(*) FROM corpus_outbox WHERE delivered_at IS NULL"
    )).scalar() or 0
    if pending or undelivered:
        raise RuntimeError(
            f"0013 拒绝回滚：还有 {pending} 个在途任务、{undelivered} 条未投递事件。"
            " 它们是已经答应用户要做的事，先把队列跑干净再回滚。")

    op.drop_table("usage_claims")
    op.drop_table("processed_events")
    op.drop_table("corpus_outbox")
    op.drop_table("tasks")

    if bind.dialect.name != "sqlite":
        op.execute(sa.text("ALTER TABLE extraction_templates "
                           "DROP CONSTRAINT IF EXISTS uq_extraction_templates_actor_name"))
        for table, new, old in (
            ("conversations", "actor_id", "user_id"),
            ("extraction_templates", "actor_id", "user_id"),
            ("extraction_runs", "actor_id", "user_id"),
        ):
            if _has_column(table, new):
                op.alter_column(table, new, new_column_name=old)

    for table in ("extraction_runs", "extraction_templates", "conversations", "documents"):
        if _has_column(table, "organization_id"):
            op.drop_index(f"ix_{table}_organization_id", table_name=table)
            op.drop_column(table, "organization_id")
