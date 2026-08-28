"""DDP-Compile v1：视觉原子、派生证据与 provider 指纹

Revision ID: 0010
Revises: 0009
"""
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("compile_status", sa.String(16), nullable=False,
                                         server_default="none"))
    op.add_column("documents", sa.Column("compile_degraded", sa.JSON(), nullable=False,
                                         server_default="[]"))
    op.add_column("documents", sa.Column("compile_fingerprint", sa.String(64), nullable=False,
                                         server_default=""))
    op.add_column("documents", sa.Column("layout_version", sa.String(32), nullable=False,
                                         server_default=""))
    op.add_column("documents", sa.Column("code_detection", sa.String(16), nullable=False,
                                         server_default="unavailable"))
    op.add_column("documents", sa.Column("index_generation", sa.Integer(), nullable=False,
                                         server_default="0"))
    op.add_column("documents", sa.Column("index_lease_until",
                                         sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_documents_compile_status", "documents", ["compile_status"])
    op.create_index("ix_documents_compile_fingerprint", "documents", ["compile_fingerprint"])
    op.create_index("ix_documents_code_detection", "documents", ["code_detection"])
    op.create_index("ix_documents_index_lease_until", "documents", ["index_lease_until"])

    op.add_column("parse_jobs", sa.Column("document_version", sa.Integer(), nullable=False,
                                          server_default="1"))

    op.add_column("chunks", sa.Column("search_text", sa.Text(), nullable=False,
                                      server_default=""))
    op.add_column("chunks", sa.Column("derived_text", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("provider", sa.JSON(), nullable=False,
                                      server_default="{}"))
    op.add_column("chunks", sa.Column("provider_fingerprint", sa.String(64), nullable=False,
                                      server_default=""))
    op.add_column("chunks", sa.Column("evidence_id", sa.String(32), nullable=True))
    op.add_column("chunks", sa.Column("derived_evidence_id", sa.String(32), nullable=True))
    op.create_index("ix_chunks_provider_fingerprint", "chunks", ["provider_fingerprint"])
    op.create_foreign_key("fk_chunks_evidence", "chunks", "evidence", ["evidence_id"], ["id"])
    op.create_foreign_key("fk_chunks_derived_evidence", "chunks", "evidence",
                          ["derived_evidence_id"], ["id"])

    op.drop_constraint("uq_evidence_job_seq", "evidence", type_="unique")
    op.add_column("evidence", sa.Column("atom_key", sa.String(64), nullable=False,
                                        server_default=""))
    op.add_column("evidence", sa.Column("content", sa.Text(), nullable=False,
                                        server_default=""))
    op.add_column("evidence", sa.Column("provider_fingerprint", sa.String(64), nullable=False,
                                        server_default=""))
    op.create_foreign_key("fk_evidence_derived_from", "evidence", "evidence",
                          ["derived_from"], ["id"])
    op.create_index("ix_evidence_atom_key", "evidence", ["atom_key"])
    op.create_index("ix_evidence_provider_fingerprint", "evidence", ["provider_fingerprint"])

    conn = op.get_bind()
    jobs = conn.execute(sa.text(
        "SELECT id, document_id FROM parse_jobs ORDER BY document_id, created_at, id")).fetchall()
    versions: dict[str, int] = {}
    for job_id, document_id in jobs:
        versions[document_id] = versions.get(document_id, 0) + 1
        conn.execute(sa.text(
            "UPDATE parse_jobs SET document_version=:v WHERE id=:id"),
            {"v": versions[document_id], "id": job_id})
    op.create_unique_constraint("uq_parse_jobs_doc_version", "parse_jobs",
                                ["document_id", "document_version"])

    chunks = {(row[0], row[1]): row for row in conn.execute(sa.text(
        "SELECT parse_job_id, seq, text FROM chunks")).fetchall()}
    rows = conn.execute(sa.text(
        "SELECT id, parse_job_id, seq, provider FROM evidence ORDER BY id")).fetchall()
    for evidence_id, job_id, seq, provider in rows:
        if isinstance(provider, str):
            try:
                provider = json.loads(provider)
            except ValueError:
                provider = {}
        provider = provider if isinstance(provider, dict) else {}
        fp = hashlib.sha256(json.dumps(provider, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest() if provider else ""
        text_value = chunks.get((job_id, seq), (None, None, ""))[2] or ""
        conn.execute(sa.text(
            "UPDATE evidence SET atom_key=:atom, content=:content, "
            "provider_fingerprint=:fp WHERE id=:id"),
            {"atom": f"source:{seq}", "content": text_value, "fp": fp,
             "id": evidence_id})
    op.create_unique_constraint("uq_evidence_job_atom", "evidence",
                                ["parse_job_id", "atom_key"])

    # 老 chunks 的 search_text 与证据连接补齐；不存在 evidence 的行保持 NULL，
    # 首次阶段 5 reindex 会为每个原子建齐。
    conn.execute(sa.text("UPDATE chunks SET search_text=text WHERE search_text=''"))
    conn.execute(sa.text(
        "UPDATE chunks SET evidence_id=(SELECT e.id FROM evidence e "
        "WHERE e.parse_job_id=chunks.parse_job_id AND e.atom_key="
        "('source:' || CAST(chunks.seq AS TEXT)))"))


def downgrade() -> None:
    # 0009 的唯一键是 (parse_job_id, seq)；0010 起同一 seq 可同时有源/派生
    # Evidence，也会为内容改变的源原子保留老行。直接建老唯一键只会
    # 给一个难懂的 IntegrityError；更不能为了 downgrade 悄悄删掉被引用的证据。
    duplicate = op.get_bind().execute(sa.text(
        "SELECT parse_job_id, seq FROM evidence GROUP BY parse_job_id, seq "
        "HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate:
        raise RuntimeError(
            "0010 cannot downgrade after compilation created multiple evidence atoms for "
            "one seq; preserve the database and revert application code without dropping "
            "DDP-Compile columns")
    op.drop_constraint("uq_parse_jobs_doc_version", "parse_jobs", type_="unique")
    op.drop_constraint("uq_evidence_job_atom", "evidence", type_="unique")
    op.create_unique_constraint("uq_evidence_job_seq", "evidence", ["parse_job_id", "seq"])
    op.drop_index("ix_evidence_provider_fingerprint", table_name="evidence")
    op.drop_index("ix_evidence_atom_key", table_name="evidence")
    op.drop_constraint("fk_evidence_derived_from", "evidence", type_="foreignkey")
    op.drop_column("evidence", "provider_fingerprint")
    op.drop_column("evidence", "content")
    op.drop_column("evidence", "atom_key")

    op.drop_constraint("fk_chunks_derived_evidence", "chunks", type_="foreignkey")
    op.drop_constraint("fk_chunks_evidence", "chunks", type_="foreignkey")
    op.drop_index("ix_chunks_provider_fingerprint", table_name="chunks")
    for name in ("derived_evidence_id", "evidence_id", "provider_fingerprint", "provider",
                 "derived_text", "search_text"):
        op.drop_column("chunks", name)
    op.drop_column("parse_jobs", "document_version")
    for name in ("ix_documents_index_lease_until", "ix_documents_code_detection",
                 "ix_documents_compile_fingerprint",
                 "ix_documents_compile_status"):
        op.drop_index(name, table_name="documents")
    for name in ("index_lease_until", "index_generation", "code_detection",
                 "layout_version", "compile_fingerprint",
                 "compile_degraded", "compile_status"):
        op.drop_column("documents", name)
