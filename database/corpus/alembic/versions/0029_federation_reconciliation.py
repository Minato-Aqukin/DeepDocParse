"""Federation reconciliation keys, intent idempotency and honest exclusion basis.

P5 复核（T80/T81/T85）补的三处加法，理由都在代码注释与契约里：

- `federation_requests.intent_idempotency_key`：`POST /api/v1/task-intents` 的
  幂等锚。与 `/tasks` 的 `idempotency_key` **不是同一个键**（一个是需求受理、
  一个是执行受理），所以单开一列；`intent_request_digest` 让"同键异实体"
  能当场判 409，而不是靠重新生成 scope manifest 去比（本地枚举每次都不同）。
- `coverage_entries.exclusion_basis` 从 String(160) 放宽到 Text：内核按
  `missing_requirements` 拼接，最多允许 4096 字符；旧列宽在 PostgreSQL 上是
  DataError 500 且任务停在 running。放宽之后由内核显式校验上限，绝不静默截断。
  **降级方向明确有损**：超长依据先截到 160 再收窄列，否则 ALTER 在填充过的
  库上直接失败（见 `downgrade()` 的注释）。

没有回填：这些列只由新端点写。
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("federation_requests") as batch:
        batch.add_column(sa.Column("intent_idempotency_key", sa.String(128)))
        batch.add_column(sa.Column("intent_request_digest", sa.String(71)))
        batch.create_unique_constraint(
            "uq_federation_requests_org_intent_idempotency",
            ["organization_id", "intent_idempotency_key"])
    with op.batch_alter_table("coverage_entries") as batch:
        batch.alter_column("exclusion_basis", existing_type=sa.String(160),
                           type_=sa.Text(), existing_nullable=True)


def downgrade():
    # 这个方向明确有损：先把超过旧列宽的依据截到 160 字符（保留前缀），再收窄
    # 列。不先截断的话，PostgreSQL 的 ALTER 会在存在超长行时直接失败，降级在
    # 已填充的库上根本跑不完 —— docstring 说的"截断保留前缀"必须真的做出来。
    op.execute(sa.text(
        "UPDATE coverage_entries SET exclusion_basis = substr(exclusion_basis, 1, 160) "
        "WHERE exclusion_basis IS NOT NULL AND length(exclusion_basis) > 160"))
    with op.batch_alter_table("coverage_entries") as batch:
        batch.alter_column("exclusion_basis", existing_type=sa.Text(),
                           type_=sa.String(160), existing_nullable=True)
    with op.batch_alter_table("federation_requests") as batch:
        batch.drop_constraint("uq_federation_requests_org_intent_idempotency", type_="unique")
        batch.drop_column("intent_request_digest")
        batch.drop_column("intent_idempotency_key")
