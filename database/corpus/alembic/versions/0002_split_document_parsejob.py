"""拆分 tasks -> documents + parse_jobs，新增 chunks / conversations / messages

把"一份文件"与"对它的一次解析"分开（ADR #15）：换参数重解析、版本对比、
问答绑定、跨文档检索都需要这个拆分。

回填策略：
- document.id 沿用 task.id —— 外键回填与 MinIO 里已有的对象前缀都不用动
- 每个 task 变成一个 document + 一个 parse_job，且该 job 即 current_job
- parse_job.result_prefix 保持指向 results/{原 task_id}/，**不搬对象**
  （搬对象是不可逆操作，不值得为命名一致性冒险）；新建的 job 才用 results/{job_id}/

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-27
"""
import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024        # bge-m3。换模型维度变了要另写迁移 + 全量 reindex


def _vector_type():
    """PG 上用 pgvector，其它方言（单测的 SQLite）退回 JSON。"""
    if op.get_bind().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector
        return Vector(EMBEDDING_DIM)
    return sa.JSON()


def _options_hash(engine: str, options) -> str:
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            options = {}
    canonical = json.dumps(options or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{engine}|{canonical}".encode()).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("doc_id", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False, server_default="web"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False,
                  server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("object_key", sa.String(512), nullable=False, server_default=""),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        # 无 FK：与 parse_jobs 互相引用，加 FK 会形成建表循环
        sa.Column("current_job_id", sa.String(32), nullable=True),
        sa.Column("index_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("index_error", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "doc_id", "origin", name="uq_documents_user_doc_origin"),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_doc_id", "documents", ["doc_id"])
    op.create_index("ix_documents_index_status", "documents", ["index_status"])
    op.create_index("ix_documents_created_at", "documents", ["created_at"])
    op.create_index("ix_documents_user_deleted_created", "documents",
                    ["user_id", "deleted_at", "created_at"])

    op.create_table(
        "parse_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("api_key_id", sa.String(32), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("engine", sa.String(32), nullable=False, server_default="mineru"),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("options_hash", sa.String(64), nullable=False),
        sa.Column("service_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_prefix", sa.String(512), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "options_hash", name="uq_parse_jobs_doc_options"),
    )
    op.create_index("ix_parse_jobs_document_id", "parse_jobs", ["document_id"])
    op.create_index("ix_parse_jobs_service_task_id", "parse_jobs", ["service_task_id"])
    op.create_index("ix_parse_jobs_status", "parse_jobs", ["status"])
    op.create_index("ix_parse_jobs_created_at", "parse_jobs", ["created_at"])

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("parse_job_id", sa.String(32), sa.ForeignKey("parse_jobs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column("page_size", sa.JSON(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_len", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", _vector_type(), nullable=True),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_doc_page_seq", "chunks", ["document_id", "page_idx", "seq"])
    if bind.dialect.name == "postgresql":
        # HNSW：查询快、写入慢，文档索引是一次性写入多次查询，正合适
        op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
                   "USING hnsw (embedding vector_cosine_ops)")
        # 关键词路（混合检索的另一半）。中文用 simple 配置，不装分词插件
        op.execute("CREATE INDEX ix_chunks_text_tsv ON chunks "
                   "USING gin (to_tsvector('simple', text))")

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default="新会话"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_document_id", "conversations", ["document_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("conversation_id", sa.String(32), sa.ForeignKey("conversations.id"),
                  nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("degraded", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    _backfill(bind)

    # file_tokens: task_id -> document_id（值相同，因为 document.id 沿用了 task.id）
    with op.batch_alter_table("file_tokens") as batch:
        batch.add_column(sa.Column("document_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("scope", sa.String(8), nullable=False, server_default="source"))
    op.execute("UPDATE file_tokens SET document_id = task_id")
    with op.batch_alter_table("file_tokens") as batch:
        batch.alter_column("document_id", nullable=False)
        batch.drop_index("ix_file_tokens_task_id")
        batch.drop_column("task_id")
        batch.create_foreign_key("fk_file_tokens_document", "documents",
                                 ["document_id"], ["id"])
    op.create_index("ix_file_tokens_document_id", "file_tokens", ["document_id"])

    # usage_records: task_id -> parse_job_id
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("parse_job_id", sa.String(32), nullable=True))
    op.execute("""
        UPDATE usage_records SET parse_job_id =
            (SELECT id FROM parse_jobs WHERE parse_jobs.document_id = usage_records.task_id)
        WHERE task_id IS NOT NULL
    """)
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_column("task_id")
        batch.create_foreign_key("fk_usage_records_parse_job", "parse_jobs",
                                 ["parse_job_id"], ["id"])

    op.drop_table("tasks")


def _backfill(bind) -> None:
    rows = bind.execute(sa.text("""
        SELECT id, user_id, api_key_id, doc_id, origin, filename, size_bytes, mime, object_key,
               service_task_id, status, error, page_count, engine, options, result_prefix,
               archived_at, created_at, updated_at
        FROM tasks
    """)).mappings().all()

    for row in rows:
        bind.execute(sa.text("""
            INSERT INTO documents (id, user_id, doc_id, origin, filename, mime, size_bytes,
                                   object_key, page_count, index_status, created_at, updated_at)
            VALUES (:id, :user_id, :doc_id, :origin, :filename, :mime, :size_bytes,
                    :object_key, :page_count, 'none', :created_at, :updated_at)
        """), {k: row[k] for k in ("id", "user_id", "doc_id", "origin", "filename", "mime",
                                   "size_bytes", "object_key", "page_count", "created_at",
                                   "updated_at")})

        job_id = uuid.uuid4().hex
        options = row["options"] if row["options"] is not None else {}
        bind.execute(sa.text("""
            INSERT INTO parse_jobs (id, document_id, api_key_id, engine, options, options_hash,
                                    service_task_id, status, error, page_count, result_prefix,
                                    archived_at, created_at, updated_at)
            VALUES (:id, :document_id, :api_key_id, :engine, :options, :options_hash,
                    :service_task_id, :status, :error, :page_count, :result_prefix,
                    :archived_at, :created_at, :updated_at)
        """), {
            "id": job_id, "document_id": row["id"], "api_key_id": row["api_key_id"],
            "engine": row["engine"] or "mineru",
            "options": json.dumps(options if isinstance(options, dict) else {}),
            "options_hash": _options_hash(row["engine"] or "mineru", options),
            "service_task_id": row["service_task_id"], "status": row["status"],
            "error": row["error"], "page_count": row["page_count"],
            "result_prefix": row["result_prefix"], "archived_at": row["archived_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
        bind.execute(sa.text("UPDATE documents SET current_job_id = :job WHERE id = :doc"),
                     {"job": job_id, "doc": row["id"]})


def downgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("api_key_id", sa.String(32), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("doc_id", sa.String(64), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False, server_default="web"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("service_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engine", sa.String(32), nullable=False, server_default="mineru"),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("result_prefix", sa.String(512), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "doc_id", "origin", name="uq_tasks_user_doc_origin"),
    )
    op.execute("""
        INSERT INTO tasks (id, user_id, api_key_id, doc_id, origin, filename, size_bytes, mime,
                           object_key, service_task_id, status, error, page_count, engine,
                           options, result_prefix, archived_at, created_at, updated_at)
        SELECT d.id, d.user_id, j.api_key_id, d.doc_id, d.origin, d.filename, d.size_bytes,
               d.mime, d.object_key, j.service_task_id, j.status, j.error, j.page_count,
               j.engine, j.options, j.result_prefix, j.archived_at, d.created_at, d.updated_at
        FROM documents d LEFT JOIN parse_jobs j ON j.id = d.current_job_id
    """)

    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("task_id", sa.String(32), nullable=True))
    op.execute("""
        UPDATE usage_records SET task_id =
            (SELECT document_id FROM parse_jobs WHERE parse_jobs.id = usage_records.parse_job_id)
        WHERE parse_job_id IS NOT NULL
    """)
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_column("parse_job_id")

    with op.batch_alter_table("file_tokens") as batch:
        batch.add_column(sa.Column("task_id", sa.String(32), nullable=True))
    op.execute("UPDATE file_tokens SET task_id = document_id")
    with op.batch_alter_table("file_tokens") as batch:
        batch.alter_column("task_id", nullable=False)
        batch.drop_index("ix_file_tokens_document_id")
        batch.drop_column("document_id")
        batch.drop_column("scope")
    op.create_index("ix_file_tokens_task_id", "file_tokens", ["task_id"])

    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("chunks")
    op.drop_table("parse_jobs")
    op.drop_table("documents")
