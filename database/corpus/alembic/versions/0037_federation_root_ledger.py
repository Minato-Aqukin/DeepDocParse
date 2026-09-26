"""Federation independent root cost ledger (0037, after federated wiki 0036).

New table ``federation_root_ledgers`` is the single persistent authority for
the coordinator root cost. One row per logical ``root_task_id`` (no foreign
key to ``federation_requests``: the parent row may be locked by the business
transaction advisory lock + row lock, and a FK KEY SHARE from another session
would deadlock).

- ``caller_budget_json`` freezes the caller-owned slice (TaskIntent.budget,
  TaskPlan.budget shape) for audit; NULL = old row, server-derived default.
- ``max_*``/``deadline`` are the frozen effective caps (server-derived
  intersect caller, min/earliest). All plan revisions, retries, resumes and
  crash-recoveries of the same root share these caps and the monotonic used
  counters; never reset, never refund.
- ``used_*`` only moves forward via a single atomic UPDATE with caps checks
  in the WHERE clause. The spend transaction commits on its own session
  BEFORE the HTTP send, so a parent business rollback/crash cannot revoke it.
- No FK, no parent-row write in the spend path; the parent row is only read
  (or projects the ledger for status output).

No backfill: existing rows get a ledger lazily on first spend by importing
the legacy ``result_json._budget_used`` once (unpublished-DB migration only);
new intents create the ledger with zero use. Downgrade refuses when any
ledger row exists, since that history is the proof of real egress cost.
"""
import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "federation_root_ledgers",
        sa.Column("root_task_id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("caller_budget_json", sa.JSON(), nullable=True),
        sa.Column("max_requests", sa.BigInteger(), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_hops", sa.BigInteger(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_generation_tokens", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("max_probe_requests", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("max_egress_bytes", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("max_discovery_requests", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("used_requests", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_generation_tokens", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("used_hops", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_discovery", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_probes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_egress_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_federation_root_ledgers_organization_id", "federation_root_ledgers",
                    ["organization_id"])


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM federation_root_ledgers")).scalar()
    if count:
        raise RuntimeError(
            "0037 cannot downgrade with federation root ledgers; "
            "export the cost audit first")
    op.drop_index("ix_federation_root_ledgers_organization_id", table_name="federation_root_ledgers")
    op.drop_table("federation_root_ledgers")
