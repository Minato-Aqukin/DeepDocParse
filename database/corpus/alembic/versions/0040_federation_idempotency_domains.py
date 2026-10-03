"""Scope coordinator keys to acting principals and executor keys to issuers.

Existing business keys and receipts remain unchanged. The expanded domains
allow independent callers to use the same key without replaying or conflicting
with another caller's request. Downgrade refuses to collapse occupied domains.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_federation_requests_org_idempotency", "federation_requests",
                       type_="unique")
    op.drop_constraint("uq_federation_requests_org_intent_idempotency", "federation_requests",
                       type_="unique")
    op.create_unique_constraint(
        "uq_federation_requests_org_actor_idempotency", "federation_requests",
        ["organization_id", "actor_id", "idempotency_key"])
    op.create_unique_constraint(
        "uq_federation_requests_org_actor_intent_idempotency", "federation_requests",
        ["organization_id", "actor_id", "intent_idempotency_key"])
    op.drop_constraint("uq_federation_admissions_org_idempotency", "federation_admissions",
                       type_="unique")
    op.create_unique_constraint(
        "uq_federation_admissions_org_issuer_idempotency", "federation_admissions",
        ["organization_id", "issuer_node_id", "idempotency_key"])


def downgrade():
    connection = op.get_bind()
    for table, column in (
        ("federation_requests", "idempotency_key"),
        ("federation_requests", "intent_idempotency_key"),
        ("federation_admissions", "idempotency_key"),
    ):
        collision = connection.execute(sa.text(
            f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL "
            f"GROUP BY organization_id, {column} HAVING COUNT(*) > 1 LIMIT 1"
        )).scalar()
        if collision is not None:
            raise RuntimeError(
                "0040 cannot downgrade occupied idempotency domains; "
                "export and reconcile the caller-scoped receipts first")
    op.drop_constraint("uq_federation_requests_org_actor_idempotency", "federation_requests",
                       type_="unique")
    op.drop_constraint("uq_federation_requests_org_actor_intent_idempotency", "federation_requests",
                       type_="unique")
    op.create_unique_constraint(
        "uq_federation_requests_org_idempotency", "federation_requests",
        ["organization_id", "idempotency_key"])
    op.create_unique_constraint(
        "uq_federation_requests_org_intent_idempotency", "federation_requests",
        ["organization_id", "intent_idempotency_key"])
    op.drop_constraint("uq_federation_admissions_org_issuer_idempotency", "federation_admissions",
                       type_="unique")
    op.create_unique_constraint(
        "uq_federation_admissions_org_idempotency", "federation_admissions",
        ["organization_id", "idempotency_key"])
