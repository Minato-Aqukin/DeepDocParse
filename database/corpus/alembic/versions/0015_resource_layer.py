"""Independent logical assets over shared source bytes.

Historical additional uploaders have no recorded organization or filename. They
are retained under the reserved migration organization until an operator supplies
an authoritative mapping; never assign them the first uploader's organization.

Revision ID: 0015
Revises: 0014
"""
import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------ resources
    op.create_table(
        "resources",
        sa.Column("id", sa.String(32), primary_key=True),
        # 组织边界。单组织部署也必须带（企业边界 8）
        sa.Column("organization_id", sa.String(32), nullable=False, server_default=""),
        # 资产归属人。**无外键**：用户住在 control schema，由 Go 拥有 ——
        # 跨 schema 硬外键会把两个服务的发布顺序绑死（与 documents.uploaded_by 同理）
        sa.Column("owner_id", sa.String(32), nullable=False),
        # 首次提交人。通常 == owner_id，"另存为我的资源"时两者不同
        sa.Column("uploaded_by", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        # 契约 publishing_state：private | draft | published | withdrawn。
        # **私有来源的派生内容不能靠切 published 绕过原许可**（计划 §4.4）——
        # 那条检查在应用层，这里只存状态
        sa.Column("publication", sa.String(16), nullable=False, server_default="private"),
        # "另存为我的资源"时指向来源资产。**副本保留来源身份**（不变量 I02）
        sa.Column("copied_from", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_resources_owner_id", "resources", ["owner_id"])
    op.create_index("ix_resources_organization_id", "resources", ["organization_id"])
    op.create_index("ix_resources_publication", "resources", ["publication"])
    # 列表页按 (未删, 时间倒序) 翻页
    op.create_index("ix_resources_deleted_created", "resources", ["deleted_at", "created_at"])

    # ---------------------------------------------------- resource_versions
    op.create_table(
        "resource_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("resource_id", sa.String(32),
                  sa.ForeignKey("resources.id", ondelete="CASCADE"), nullable=False),
        # 从 1 起。同一资产换版 = 加一行，**不是原地改** —— T04
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        # 绑定到内容层。**多个 resource_version 可以指同一个 document**，
        # 那正是"内容去重仍然生效"的形状
        sa.Column("document_id", sa.String(32),
                  sa.ForeignKey("documents.id"), nullable=False),
        # 冗余一份内容摘要：验证绑定没被换掉，且联邦侧 DDP-EVIDENCE 的
        # source_digest 直接取它，不必回表 join documents
        sa.Column("source_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("resource_id", "version_no", name="uq_resource_versions_no"),
    )
    op.create_index("ix_resource_versions_resource_id", "resource_versions", ["resource_id"])
    # GC 要反查"还有没有活的资源版本引用这个 document"，这个索引是那条查询的主力
    op.create_index("ix_resource_versions_document_id", "resource_versions", ["document_id"])

    # -------------------------------------------------------- upload_events
    op.create_table(
        "upload_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("resource_version_id", sa.String(32),
                  sa.ForeignKey("resource_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        # **网络重试不是新上传**（I01 的后半句 / T02）。同一个幂等键只留一条；
        # 想真的再传一次要换键，那时是一次显式的新上传
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        # 同键不同正文要能判冲突（对齐联邦侧的 idempotency_conflict）
        sa.Column("request_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # **键域按提交人隔离**（计划 T80：不同用户的键域不能串用）。
        # 只按 idempotency_key 全局唯一的话，A 用过的键 B 再用会被判成重试，
        # 于是 B 的上传静默变成"已存在"，拿到的是 A 的资产
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_upload_events_actor_key"),
    )
    op.create_index("ix_upload_events_resource_version_id", "upload_events",
                    ["resource_version_id"])
    op.create_index("ix_upload_events_actor_id", "upload_events", ["actor_id"])

    # ------------------------------------------------------------ 唯一约束
    # 回填与日常写入的幂等靠它：一个 (document, owner) 只生成一份原始资产。
    # **不含 version_no** —— 它约束的是"同一个人对同一份内容只有一条原始资产"，
    # 而不是"只有一个版本"
    op.create_index("uq_resources_origin_binding", "resource_versions",
                    ["document_id", "resource_id"], unique=True)

    backfill_assets(bind)


def backfill_assets(bind) -> None:
    """Portable deterministic IDs include the entire document and owner identity."""
    metadata = sa.MetaData()
    documents = sa.Table("documents", metadata, autoload_with=bind)
    uploads = sa.Table("document_uploads", metadata, autoload_with=bind)
    resources = sa.Table("resources", metadata, autoload_with=bind)
    versions = sa.Table("resource_versions", metadata, autoload_with=bind)
    owners = sa.union(sa.select(uploads.c.document_id, uploads.c.user_id),
                      sa.select(documents.c.id, documents.c.uploaded_by)).subquery()
    stmt = sa.select(documents, owners.c.user_id, uploads.c.created_at.label("upload_time")).join(
        owners, owners.c.document_id == documents.c.id).outerjoin(uploads, sa.and_(
            uploads.c.document_id == documents.c.id, uploads.c.user_id == owners.c.user_id))
    for row in bind.execute(stmt).mappings():
        identity = f"{row['id']}:{row['user_id']}"
        rid = hashlib.sha256(f"resource:{identity}".encode()).hexdigest()[:32]
        vid = hashlib.sha256(f"version:{identity}".encode()).hexdigest()[:32]
        first = row["user_id"] == row["uploaded_by"]
        filename = row["filename"] if first else "Recovered document.pdf"
        created = row["upload_time"] or row["created_at"]
        if not bind.scalar(sa.select(resources.c.id).where(resources.c.id == rid)):
            bind.execute(resources.insert().values(id=rid,
                organization_id=(row["organization_id"] if first and row["organization_id"]
                                 else "migration:unresolved"),
                owner_id=row["user_id"], uploaded_by=row["user_id"], display_name=filename,
                publication="private", created_at=created, updated_at=created,
                deleted_at=row["deleted_at"]))
        if not bind.scalar(sa.select(versions.c.id).where(versions.c.id == vid)):
            bind.execute(versions.insert().values(id=vid, resource_id=rid, version_no=1,
                document_id=row["id"], source_digest=row["doc_id"], filename=filename,
                size_bytes=row["size_bytes"], created_at=created, deleted_at=row["deleted_at"],
                **({"bundle_prefix": ""} if "bundle_prefix" in versions.c else {})))


def downgrade() -> None:
    # 纯新增，内容层没动过 —— 这次是**真的**数据回滚（0006 不是）
    op.drop_index("uq_resources_origin_binding", table_name="resource_versions")
    op.drop_table("upload_events")
    op.drop_table("resource_versions")
    op.drop_table("resources")
