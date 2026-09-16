"""Node credential replay ledger (`ddp-node-credential/1`).

Every node-to-node request now carries a single-use Ed25519 credential issued by the
calling centre's control plane. After the receiving corpus verifies the signature it
inserts the credential's `jti` here; the primary key is the replay arbiter across
replicas (a second insert of the same jti is `credential_replayed`, sequential or
concurrent). Rows live only until the credential's own `expires_at` (at most 120 s
after issue) and the federation sweep deletes them afterwards.

No backfill: before this revision there were no credentials to remember. Downgrade
drops the ledger; a node running the old code does not verify credentials at all.
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "federation_credential_nonces",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("issuer_node_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_federation_credential_nonces_expires_at",
                    "federation_credential_nonces", ["expires_at"])


def downgrade():
    op.drop_table("federation_credential_nonces")
