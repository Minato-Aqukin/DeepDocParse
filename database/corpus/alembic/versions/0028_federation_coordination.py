"""Federation coordination: intent/consent payloads, plan, idempotency, task events.

A4 的 0027 建了执行者面（probe / admission / execution / coverage / request 行）。
这一版只做加法：

- `federation_requests` 补上协调者重放所需的原文（task_spec / 探索许可 /
  执行许可 / scope manifest / plan）、`POST /tasks` 的幂等键与 delegation 代次。
  A4 已有的列一个都没改语义。
- 新建 `federation_task_events`：带序号的可恢复事件流（GET /tasks/{id}/events）。
  唯一 `(root_task_id, seq)` 让"两个写者算出同一个序号"在库里失败，
  而不是静默覆盖。

没有回填：协调者行只会由 B1 的端点新建。
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("federation_requests") as batch:
        # 规划/审批/执行都要按当时批准的内容重放，不能只看摘要。
        batch.add_column(sa.Column("task_spec_json", sa.JSON(), nullable=False,
                                   server_default="{}"))
        batch.add_column(sa.Column("exploration_consent_json", sa.JSON(), nullable=False,
                                   server_default="{}"))
        batch.add_column(sa.Column("execution_consent_json", sa.JSON()))
        # federation_public 由控制面传入；site_public/fixed_resources 本节点构造。
        batch.add_column(sa.Column("scope_manifest_json", sa.JSON()))
        batch.add_column(sa.Column("plan_json", sa.JSON()))
        # 一个 root task 只受理一个幂等键；NULL 不参与唯一性（补做走 resume）。
        batch.add_column(sa.Column("idempotency_key", sa.String(128)))
        batch.add_column(sa.Column("delegation_generation", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.create_unique_constraint(
            "uq_federation_requests_org_idempotency", ["organization_id", "idempotency_key"])

    op.create_table(
        "federation_task_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("root_task_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("root_task_id", "seq", name="uq_federation_task_events_seq"),
    )
    op.create_index("ix_federation_task_events_root_task_id", "federation_task_events",
                    ["root_task_id"])


def downgrade():
    op.drop_index("ix_federation_task_events_root_task_id", table_name="federation_task_events")
    op.drop_table("federation_task_events")
    with op.batch_alter_table("federation_requests") as batch:
        batch.drop_constraint("uq_federation_requests_org_idempotency", type_="unique")
        for name in ("delegation_generation", "idempotency_key", "plan_json",
                     "scope_manifest_json", "execution_consent_json",
                     "exploration_consent_json", "task_spec_json"):
            batch.drop_column(name)
