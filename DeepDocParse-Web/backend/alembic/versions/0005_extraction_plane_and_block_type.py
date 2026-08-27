"""抽取平面的三张表 + 分块的块类型与分词列

两组改动，都服务于「结构化信息提取」这条线：

1. **chunks 加三列**（block_type / table_html / text_tokenized）
   - block_type：DDP-Layout v1.1 把 para_blocks[].type 升进了承诺字段。
     在它之前表格与正文在索引里完全无法区分，抽取平面找不到记录数组
   - table_html：表格结构的唯一载体。拼出来的单元格文字已经丢了行列关系
   - text_tokenized：D2 中文分词。`to_tsvector('simple', text)` 会把整段中文
     当成**一个 token**，于是"混合检索"在中文文档上实际只有向量一条腿
     （A1 量到关键词路单独工作时页码命中率 25%，正是这条腿瘸着的样子）

   **回填策略**：block_type 与 table_html 需要版面数据才能算，这里不回填 ——
   老 chunk 的 block_type 留 'text'（保守且无害），要精确值就重建索引。
   text_tokenized 则**必须回填**：不回填的话老文档的关键词路会从"整段一个 token"
   变成"空字符串"，召回不是变差而是归零，比改之前更糟。

2. **抽取平面三张表**：extraction_templates / extraction_runs / extraction_items。
   形状说明见 app/models.py 的类 docstring，关键一条：
   ExtractionRun.schema_json 是**快照**而非取模板当前值 ——
   模板改了之后历史 run 的列会对不上号（同一条教训在 Message.model_meta 上吃过一次）。

downgrade 完整可逆：删表 + 删列。已经写进 items 的抽取结果会丢，
但那是降级本身的语义，不做取巧的保留。

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 回填时一次取多少行。整表读进内存对长文档库不现实（一份 200 页 PDF 就是几百个 chunk）
_BATCH = 500


def upgrade() -> None:
    with op.batch_alter_table("chunks") as batch:
        batch.add_column(sa.Column("block_type", sa.String(16), nullable=False,
                                   server_default="text"))
        batch.add_column(sa.Column("table_html", sa.Text(), nullable=True))
        batch.add_column(sa.Column("text_tokenized", sa.Text(), nullable=False,
                                   server_default=""))
    op.create_index("ix_chunks_block_type", "chunks", ["block_type"])

    _backfill_tokenized(op.get_bind())

    op.create_table(
        "extraction_templates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_extraction_templates_user_name"),
    )
    op.create_index("ix_extraction_templates_user_id", "extraction_templates", ["user_id"])

    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        # 弱引用，无 FK：模板可以被删，run 不该跟着消失（真正的依据是 schema_json 快照）
        sa.Column("template_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("kind", sa.String(8), nullable=False, server_default="object"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model_meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extraction_runs_user_id", "extraction_runs", ["user_id"])
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])
    op.create_index("ix_extraction_runs_created_at", "extraction_runs", ["created_at"])

    op.create_table(
        "extraction_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("extraction_runs.id"), nullable=False),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("parse_job_id", sa.String(32), nullable=True),
        sa.Column("record_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("degraded", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "document_id", "record_index",
                            name="uq_extraction_items_run_doc_record"),
    )
    op.create_index("ix_extraction_items_run_id", "extraction_items", ["run_id"])
    op.create_index("ix_extraction_items_document_id", "extraction_items", ["document_id"])
    op.create_index("ix_extraction_items_run_created", "extraction_items",
                    ["run_id", "created_at"])


def _backfill_tokenized(bind) -> None:
    """给已有 chunk 补上分词列。

    **不做这一步的后果比不加这个功能更糟**：search.py 改成查 text_tokenized 之后，
    老 chunk 那一列是空串 -> `to_tsvector('simple', '')` 匹配不上任何东西 ->
    关键词路对所有历史文档**召回归零**。而向量路照常工作，所以表现是
    "检索悄悄变差了"，不会有任何报错 —— 又一个静默降级。

    分词器装不上时（jieba 是软依赖）退回二元组，与运行时同一个实现，
    保证回填与后续写入切法一致（两边切法不同 = 永远匹配不上）。
    """
    import sys
    from pathlib import Path

    # alembic 的 cwd 是 backend/，但迁移文件在 alembic/versions/ 下，
    # 直接 import app 在某些调用方式下会失败 —— 显式补一次路径
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # 分词已搬进 ddp_core（阶段 1）。**迁移里的 import 必须跟着改** ——
    # 这一段是无条件执行的，空库也会走到，所以一个全新部署会在这里
    # ModuleNotFoundError 然后停在 0004。已有库早就过了 0005，看不见这个洞，
    # 那正是它到阶段 2a 验收才被发现的原因。
    from ddp_core.tokenize import tokenized

    chunks = sa.table("chunks", sa.column("id", sa.String), sa.column("text", sa.Text),
                      sa.column("text_tokenized", sa.Text))
    # **keyset 分页，不用 OFFSET。** OFFSET 在大表上是 O(n²) 扫描，而且一旦有并发写入
    # 就会漂移**漏行** —— 漏掉的行 text_tokenized 是空串，
    # `to_tsvector('simple','') @@ tsq` 恒为 false，那一行从此在关键词路上消失，
    # 且没有任何报错。按主键游标推进则天然不漏。
    last_id = ""
    while True:
        rows = bind.execute(
            sa.select(chunks.c.id, chunks.c.text)
            .where(chunks.c.id > last_id).order_by(chunks.c.id).limit(_BATCH)
        ).fetchall()
        if not rows:
            break
        # 一次 executemany 提交一批，不是一行一条往返：百万级 chunk 上
        # 逐行往返会让整个迁移跑到天荒地老，而迁移是在一个事务里的
        bind.execute(
            chunks.update()
            .where(chunks.c.id == sa.bindparam("_id"))
            .values(text_tokenized=sa.bindparam("_tok")),
            [{"_id": cid, "_tok": tokenized(text or "")} for cid, text in rows],
        )
        last_id = rows[-1][0]


def downgrade() -> None:
    op.drop_table("extraction_items")
    op.drop_table("extraction_runs")
    op.drop_table("extraction_templates")
    op.drop_index("ix_chunks_block_type", table_name="chunks")
    with op.batch_alter_table("chunks") as batch:
        batch.drop_column("text_tokenized")
        batch.drop_column("table_html")
        batch.drop_column("block_type")
