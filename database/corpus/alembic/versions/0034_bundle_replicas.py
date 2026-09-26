"""Authorized Bundle replica ledger (0034, after credential nonces 0033).

`bundle_replicas` records one authorized copy per import: the source
authority/origin identity, the fixed local version/digest it snapshots, the
owning actor, the source policy revision and the term/revocation state.
Reading the licensed source reuses the fixed version authorization and the
stored snapshot bytes — it never invents a permission of its own. A revoked
or expired replica blocks reads; an offline original only stays readable
through the fixed snapshot of a still-valid replica.

`bundle_replica_revoke_keys` makes `POST .../revoke` idempotent per
(organization, actor, Idempotency-Key): the same key replays the original
revocation, a different request on the same key is a 409 conflict.

No backfill: rows are only written by the new bundle-replica endpoints.
Downgrade refuses when any replica (or revoke key) exists, since that
history is the proof a revoked copy was once authorized.
"""
import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bundle_replicas",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(32),
                  sa.ForeignKey("resources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_version_id", sa.String(32),
                  sa.ForeignKey("resource_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("origin_node_id", sa.String(64), nullable=False),
        sa.Column("authority_node_id", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("policy_revision", sa.String(512), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "source_version_id", "owner_id",
                            name="uq_bundle_replicas_version_owner"),
    )
    op.create_index("ix_bundle_replicas_org", "bundle_replicas", ["organization_id"])
    op.create_index("ix_bundle_replicas_resource", "bundle_replicas", ["resource_id"])
    op.create_index("ix_bundle_replicas_version", "bundle_replicas", ["source_version_id"])
    op.create_index("ix_bundle_replicas_owner", "bundle_replicas", ["owner_id"])
    op.create_table(
        "bundle_replica_revoke_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("replica_id", sa.String(32),
                  sa.ForeignKey("bundle_replicas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "actor_id", "idempotency_key",
                            name="uq_bundle_revoke_actor_key"),
    )
    op.create_index("ix_bundle_revoke_keys_replica", "bundle_replica_revoke_keys",
                    ["replica_id"])


def downgrade():
    bind = op.get_bind()
    replicas = bind.execute(sa.text("SELECT COUNT(*) FROM bundle_replicas")).scalar()
    if replicas:
        raise RuntimeError(
            "0034 cannot downgrade with authorized Bundle replicas; "
            "export the replica/revocation audit first")
    revoke_keys = bind.execute(sa.text("SELECT COUNT(*) FROM bundle_replica_revoke_keys")).scalar()
    if revoke_keys:
        raise RuntimeError(
            "0034 cannot downgrade with Bundle revocation keys; "
            "export the replica/revocation audit first")
    op.drop_index("ix_bundle_revoke_keys_replica", table_name="bundle_replica_revoke_keys")
    op.drop_table("bundle_replica_revoke_keys")
    op.drop_index("ix_bundle_replicas_owner", table_name="bundle_replicas")
    op.drop_index("ix_bundle_replicas_version", table_name="bundle_replicas")
    op.drop_index("ix_bundle_replicas_resource", table_name="bundle_replicas")
    op.drop_index("ix_bundle_replicas_org", table_name="bundle_replicas")
    op.drop_table("bundle_replicas")
