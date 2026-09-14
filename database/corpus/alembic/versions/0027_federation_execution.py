"""Federation executor persistence: probes, admissions, executions, coverage, deliveries.

One migration for the whole P5 node/executor side (probes, receipts, task rows,
coverage ledgers and coordinator/delivery rows a later slice will use). Nothing is
backfilled: every row is created by a peer request under this node's identity.

"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    # ---- probes -----------------------------------------------------------
    # An evidence/capability/locate probe receipt, scoped to the caller's
    # organization. `query_digest` is the canonical digest of the probed query;
    # replay compares the stored result with the request instead of recomputing.
    op.create_table(
        "federation_probes",
        sa.Column("probe_id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("target_node_id", sa.String(64), nullable=False),
        sa.Column("task_spec_digest", sa.String(71), nullable=False),
        sa.Column("consent_ref", sa.String(128), nullable=False),
        sa.Column("probe_kind", sa.String(24), nullable=False),
        sa.Column("collection_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("query_digest", sa.String(71), nullable=False, server_default=""),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result_json", sa.JSON, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_federation_probes_organization_id", "federation_probes", ["organization_id"])
    op.create_index("ix_federation_probes_expires_at", "federation_probes", ["expires_at"])
    op.create_index("ix_federation_probes_actor_id", "federation_probes", ["actor_id"])

    # ---- admissions -------------------------------------------------------
    # One row per (organization, idempotency key). The receipt is rebuilt from
    # this row on replay; execution never runs a second time for the same key.
    op.create_table(
        "federation_admissions",
        sa.Column("admission_id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("plan_digest", sa.String(71), nullable=False),
        sa.Column("root_task_id", sa.String(64), nullable=False),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("delegation_generation", sa.Integer, nullable=False),
        sa.Column("issuer_node_id", sa.String(64), nullable=False),
        sa.Column("executor_node_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("input_validation", sa.String(24), nullable=False),
        sa.Column("executor_task_id", sa.String(32)),
        sa.Column("verified_input_manifest_digest", sa.String(71)),
        sa.Column("effective_policy_ref", sa.String(160), nullable=False),
        sa.Column("receipt_json", sa.JSON, nullable=False),
        sa.Column("receipt_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "idempotency_key",
                            name="uq_federation_admissions_org_idempotency"),
    )
    op.create_index("ix_federation_admissions_root_task_id", "federation_admissions", ["root_task_id"])
    op.create_index("ix_federation_admissions_executor_task_id", "federation_admissions",
                    ["executor_task_id"])

    # ---- executions -------------------------------------------------------
    # Local execution row for an admitted step. `generation` fences writes: a
    # cancelled/expired attempt may not overwrite a newer final result.
    op.create_table(
        "federation_executions",
        sa.Column("executor_task_id", sa.String(32), primary_key=True),
        sa.Column("admission_id", sa.String(32),
                  sa.ForeignKey("federation_admissions.admission_id"), nullable=False),
        sa.Column("root_task_id", sa.String(64), nullable=False),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("result_ref", sa.String(128)),
        sa.Column("evidence_set_ref", sa.String(128)),
        sa.Column("result_json", sa.JSON, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("admission_id", "root_task_id", "state"):
        op.create_index("ix_federation_executions_" + name, "federation_executions", [name])

    # ---- coordinator task and coverage rows (later slice) -----------------
    op.create_table(
        "federation_requests",
        sa.Column("root_task_id", sa.String(64), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("task_spec_digest", sa.String(71), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("scope_digest", sa.String(71), nullable=False),
        sa.Column("search_mode", sa.String(24), nullable=False),
        sa.Column("planning_state", sa.String(24), nullable=False),
        sa.Column("plan_revision", sa.Integer, nullable=False),
        sa.Column("plan_digest", sa.String(71), nullable=False),
        sa.Column("execution_consent_ref", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("retrieval_completeness", sa.String(24), nullable=False),
        sa.Column("evidence_sufficiency", sa.String(24), nullable=False),
        sa.Column("result_json", sa.JSON, nullable=False),
        sa.Column("coverage_ref", sa.String(64)),
        sa.Column("delivery_id", sa.String(32)),
        sa.Column("delivery_state", sa.String(24), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("organization_id", "actor_id", "status"):
        op.create_index("ix_federation_requests_" + name, "federation_requests", [name])

    op.create_table(
        "coverage_ledgers",
        sa.Column("root_task_id", sa.String(64), primary_key=True),
        sa.Column("scope_ref", sa.String(128), nullable=False),
        sa.Column("search_mode", sa.String(24), nullable=False),
        sa.Column("enumeration_state", sa.String(24), nullable=False),
        sa.Column("retrieval_completeness", sa.String(24), nullable=False),
        sa.Column("evidence_sufficiency", sa.String(24), nullable=False),
        sa.Column("counts_json", sa.JSON, nullable=False),
        sa.Column("manifest_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "coverage_entries",
        sa.Column("root_task_id", sa.String(64),
                  sa.ForeignKey("coverage_ledgers.root_task_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("target_digest", sa.String(64), primary_key=True),
        sa.Column("target_key_json", sa.JSON, nullable=False),
        sa.Column("query_digest", sa.String(71), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("probe_refs_json", sa.JSON, nullable=False),
        sa.Column("actual_index_revision", sa.String(128)),
        sa.Column("search_profile", sa.String(64)),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text),
        sa.Column("evidence_refs_json", sa.JSON, nullable=False),
        sa.Column("used_budget_json", sa.JSON, nullable=False),
        sa.Column("exclusion_basis", sa.String(160)),
    )

    op.create_table(
        "federation_deliveries",
        sa.Column("delivery_id", sa.String(32), primary_key=True),
        sa.Column("root_task_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result_manifest_digest", sa.String(71)),
        sa.Column("retention", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("receipt_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_federation_deliveries_root_task_id", "federation_deliveries",
                    ["root_task_id"])


def downgrade():
    for name in ("federation_deliveries", "coverage_entries", "coverage_ledgers",
                 "federation_requests", "federation_executions", "federation_admissions",
                 "federation_probes"):
        op.drop_table(name)
