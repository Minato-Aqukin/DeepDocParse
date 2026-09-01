"""evidence / citation 两张表（plan.md 阶段 2b）

出处此前只是两处 JSON 列里的字典（`messages.citations` 与
`extraction_items.fields[].citations`）：查不了、连不了、没法反查"这条证据
被谁引过"，也没有地方安放复核状态。§5.1 把 evidence 升成一等实体，
三条系统级属性各自拿到一个支点：

    content_digest  -> 可更新（阶段 3 靠它判断块内容变没变）
    provider        -> 可追溯（哪个引擎/模型/版本产出的这块版面）
    review_state    -> 可复核（阶段 7 的复核队列按它排）

**这一版只建表，不回填。** 双写从这一刻开始，读仍然走老路。
历史回填（按 (parse_job_id, seq) 找块算 digest，对不上就标失效）是阶段 3 ——
那一步才是最容易出假出处的地方，不能和建表混在一起。

## 回滚

`downgrade` 直接 drop 两张表。老路径从头到尾没被碰过，所以**回滚零风险**
—— 这也是阶段 2b 被设计成"只双写"的全部理由。

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        # 本阶段恒 0：documents 今天没有版本概念，文档换版要到阶段 5 才有意义
        sa.Column("doc_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parse_job_id", sa.String(32), sa.ForeignKey("parse_jobs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("page_idx", sa.Integer(), nullable=False),
        sa.Column("bbox", sa.JSON(), nullable=True),
        # 缺它遇到 CropBox 偏移/旋转页会裁错区域 —— 出处图对不上原文是最恶劣的错
        sa.Column("page_size", sa.JSON(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="text"),
        sa.Column("crop_key", sa.String(512), nullable=True),
        sa.Column("content_digest", sa.String(64), nullable=False, server_default=""),
        sa.Column("provider", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("review_state", sa.String(16), nullable=False, server_default="unreviewed"),
        sa.Column("derived_from", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # 同一个块只有一行证据。用的是 chunks 那把稳定定位键的后两段
        sa.UniqueConstraint("parse_job_id", "seq", name="uq_evidence_job_seq"),
    )
    op.create_index("ix_evidence_document_id", "evidence", ["document_id"])
    op.create_index("ix_evidence_kind", "evidence", ["kind"])
    op.create_index("ix_evidence_content_digest", "evidence", ["content_digest"])
    op.create_index("ix_evidence_review_state", "evidence", ["review_state"])
    op.create_index("ix_evidence_doc_page", "evidence", ["document_id", "page_idx"])

    op.create_table(
        "citations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("evidence_id", sa.String(32), sa.ForeignKey("evidence.id"), nullable=False),
        # message | extract_field（阶段 6 加 assertion，阶段 7 加 graph_edge / wiki_sentence）
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        # 本阶段只写 primary；rejected 需要保留被丢弃的检索候选，是独立的一件事
        sa.Column("role", sa.String(16), nullable=False, server_default="primary"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source_kind", "source_id", "evidence_id", "role",
                            name="uq_citations_source_evidence"),
    )
    op.create_index("ix_citations_evidence_id", "citations", ["evidence_id"])
    op.create_index("ix_citations_source", "citations", ["source_kind", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_citations_source", table_name="citations")
    op.drop_index("ix_citations_evidence_id", table_name="citations")
    op.drop_table("citations")
    for name in ("ix_evidence_doc_page", "ix_evidence_review_state",
                 "ix_evidence_content_digest", "ix_evidence_kind", "ix_evidence_document_id"):
        op.drop_index(name, table_name="evidence")
    op.drop_table("evidence")
