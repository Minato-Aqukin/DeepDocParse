"""出处稳定定位键 + 回答的模型戳（P0 数据留存修复）

两件事，都是**拖一天就多丢一天可追溯性**的那类：

1. `chunks.id` 是随机 UUID，而 indexing.py 每次重建索引都先
   `DELETE FROM chunks WHERE document_id=...` 再重新 add_all —— 于是
   `messages.citations[].chunk_id` 在每次 reindex 后全部悬空，
   "这个回答当时基于哪段原文"就此还原不回来（citations 里只剩 160 字的 snippet）。
   修法：citations 补存 `(parse_job_id, seq)` 这个不随重建变化的定位键，
   并在库上加唯一约束把这条不变式钉死。**本迁移顺带回填一次**：
   chunk_id 当前还指得到的老记录，趁现在把定位键补上，过了这村就没了。

2. messages 没记是哪个模型、哪套检索参数产出的。一旦开始换模型，
   历史数据就无法分组对比 —— 而那是判断"新配置有没有变好"的唯一依据。

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-18
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("model_meta", sa.JSON(), nullable=False,
                                   server_default="{}"))

    _backfill_citation_locators(op.get_bind())

    with op.batch_alter_table("chunks") as batch:
        batch.create_unique_constraint("uq_chunks_doc_job_seq",
                                       ["document_id", "parse_job_id", "seq"])


def _backfill_citation_locators(bind) -> None:
    """给还指得到 chunk 的老 citations 补上 (parse_job_id, seq)。

    指不到的（已经重建过索引）无法恢复 —— 那正是这次修复要止住的损失。
    """
    chunks = {
        row["id"]: (row["parse_job_id"], row["seq"])
        for row in bind.execute(sa.text("SELECT id, parse_job_id, seq FROM chunks")).mappings()
    }
    if not chunks:
        return

    rows = bind.execute(sa.text(
        "SELECT id, citations FROM messages WHERE citations IS NOT NULL")).mappings().all()
    for row in rows:
        citations = row["citations"]
        if isinstance(citations, str):
            try:
                citations = json.loads(citations)
            except ValueError:
                continue
        if not isinstance(citations, list) or not citations:
            continue

        changed = False
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("parse_job_id"):
                continue
            located = chunks.get(citation.get("chunk_id"))
            if located is None:
                continue
            citation["parse_job_id"], citation["seq"] = located
            changed = True
        if changed:
            bind.execute(sa.text("UPDATE messages SET citations = :c WHERE id = :id"),
                         {"c": json.dumps(citations, ensure_ascii=False), "id": row["id"]})


def downgrade() -> None:
    with op.batch_alter_table("chunks") as batch:
        batch.drop_constraint("uq_chunks_doc_job_seq", type_="unique")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("model_meta")
