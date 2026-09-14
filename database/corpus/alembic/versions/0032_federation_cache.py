"""P6 bounded federation cache: one table for projections, negative entries and probes.

`federation_cache_entries` is deliberately one table, not one per projection kind:
the caps (entries / bytes / TTL / per-scope entries) are enforced over a single
population, and eviction must be able to compare a negative entry with a cached
projection by the same rule. A table per kind would move the bound back to
"unbounded number of kinds".

Nothing is backfilled: the cache is rebuildable by definition, so an empty table
after upgrade is the correct state. `hits` is the eviction signal maintained by
`cache.get`; `(scope_key, cache_key)` is the identity the invalidation filters
address. No credentials or source bytes are stored here — values are bounded
projections only.
"""
import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "federation_cache_entries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("scope_key", sa.String(160), nullable=False),
        sa.Column("cache_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value_json", sa.JSON, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("hits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope_key", "cache_key",
                            name="uq_federation_cache_scope_key"),
    )
    op.create_index("ix_federation_cache_entries_scope_key",
                    "federation_cache_entries", ["scope_key"])
    op.create_index("ix_federation_cache_entries_kind",
                    "federation_cache_entries", ["kind"])
    op.create_index("ix_federation_cache_entries_expires_at",
                    "federation_cache_entries", ["expires_at"])
    op.create_index("ix_federation_cache_entries_created_at",
                    "federation_cache_entries", ["created_at"])


def downgrade():
    op.drop_table("federation_cache_entries")
