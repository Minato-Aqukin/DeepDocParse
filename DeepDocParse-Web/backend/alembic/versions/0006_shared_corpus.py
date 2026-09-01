"""语料共享化：文档不再属于某个用户，去重变成全局的

plan.md §2 已定 2：**一次部署 = 一份语料 = 一个知识库**。
core 不做租户隔离，账号层只管认证 / 计量 / 限速，**不管授权**。

三件事：

1. `documents.user_id` -> `uploaded_by`（**仅归属署名**，不再是可见性边界）
2. 唯一约束 `(user_id, doc_id, origin)` -> **`(doc_id, origin)`**，去重变全局；
   索引 `(user_id, deleted_at, created_at)` -> `(deleted_at, created_at)`
3. 新表 `document_uploads` 记**全部**上传者，`users` 加 `is_admin`

## 合并（这一步不可逆，跑之前请备份）

收紧唯一约束意味着"同一份文件被 N 个用户传过"的 N 条记录必须并成 1 条。
§11 已定 6：**合并并保留全部归属**，不丢历史。做法：

- 每组 (doc_id, origin) 留 `created_at` 最早的那条（它的 id 成为幸存者）
- 其余记录的 **chunks / conversations / file_tokens / extraction_items
  改指幸存者**，parse_jobs 单独处理（见下）
- **两处是真的会丢，别当成"什么都不丢"**：
  ① 冗余的 parse_jobs 连同它们的 chunks 是**删掉**的（同一文件同一参数的重复解析）；
  ② `extraction_items.parse_job_id` **不会被改指** —— 那些指向已删 job 的抽取
  结果，出处会静默失效（`load_citation_targets` 查不到 → `resolved=False`）。
  行为本身是**安全**的（符合不变式 1：接不回就说接不回，不指错地方），
  但被合并掉的那些文档的历史抽取出处会整批变成"接不回去"
- 每个原记录的 user_id 都写进 `document_uploads`，一个不落
- **软删除状态取最宽松的**：只要有人还没删，合并后就是未删。
  反过来（有人删了就整份消失）会让 A 删自己的副本连带 B 的一起没了

`downgrade` 能把**结构**退回去（`uploaded_by` 改回 `user_id`、约束与索引复原、
删掉新表与新列），但**数据不对称，两处**：

1. **合并掉的文档行拆不回来** —— 它们在 upgrade 时已经删了
2. **归属也会丢**：downgrade 删掉 `document_uploads` 整张表，再 upgrade 时
   只能从幸存文档的 `uploaded_by` 重新记一遍。实测 11 条归属经过
   downgrade→upgrade 一轮之后只剩 6 条 —— 那 5 个"也传过这份文件的人"
   永久消失了

所以 downgrade 是**结构回滚，不是数据回滚**。真要退回去用备份，别指望它。
（本次在真 PG 上验过 upgrade → downgrade → 再 upgrade 全通，
计量流水 30 条一条不少。）

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# 指向 documents 的五张表。合并时它们都要改指幸存者 ——
# 漏掉任何一张，那张表的行会在删除原文档时撞外键（PG 上直接炸），
# 或者更糟：留下一堆指向已消失文档的孤儿行
# 指向 documents 的引用表。**parse_jobs 不在这里** —— 它有
# uq_parse_jobs_doc_options，合并会撞车，单独处理（见 upgrade 里那一段）。
REFERRING = ("chunks", "conversations", "file_tokens", "extraction_items")


def _rename_index(conn, old: str, new: str) -> None:
    """改索引名，**不存在就跳过**。

    不用裸 `ALTER INDEX`：这个迁移在开发期被反复 up/down 过，中途版本没有这一步，
    于是 downgrade 时旧名可能压根不存在 —— 硬改会让整条迁移炸在一个纯粹
    装饰性的步骤上。判存在再改，两个方向都安全。
    """
    exists = conn.execute(sa.text(
        "SELECT 1 FROM pg_indexes WHERE tablename='documents' AND indexname=:n"),
        {"n": old}).scalar()
    if exists:
        conn.execute(sa.text(f'ALTER INDEX {old} RENAME TO {new}'))


def _rename_constraint(conn, old: str, new: str) -> None:
    """改约束名，**不存在就跳过**。理由同 `_rename_index`。"""
    exists = conn.execute(sa.text(
        "SELECT 1 FROM pg_constraint WHERE conname=:n "
        "AND conrelid='documents'::regclass"), {"n": old}).scalar()
    if exists:
        conn.execute(sa.text(f'ALTER TABLE documents RENAME CONSTRAINT {old} TO {new}'))


def upgrade() -> None:
    conn = op.get_bind()

    # ---- 1. users.is_admin ----
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    op.alter_column("users", "is_admin", server_default=None)

    # ---- 1b. parse_jobs.initiated_by ----
    # 谁发起的这次解析，用来记账。语料共享之后任何人都能对任一文档点重新解析，
    # 按 documents.uploaded_by 记账等于"谁传的谁买单"。
    # 可空：存量 job 没有这个信息，代码里退回按上传者记
    op.add_column("parse_jobs", sa.Column("initiated_by", sa.String(32), nullable=True))

    # ---- 2. document_uploads ----
    op.create_table(
        "document_uploads",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "user_id", name="uq_document_uploads_doc_user"),
    )
    op.create_index("ix_document_uploads_document_id", "document_uploads", ["document_id"])
    op.create_index("ix_document_uploads_user_id", "document_uploads", ["user_id"])

    # ---- 3. 先把归属全部记下来，再动 documents ----
    # 顺序很重要：合并会删行，删之前必须把每一条的 user_id 落到新表里
    conn.execute(sa.text("""
        INSERT INTO document_uploads (id, document_id, user_id, created_at)
        SELECT md5(random()::text || clock_timestamp()::text), id, user_id, created_at
        FROM documents
    """))

    # ---- 4. 合并重复组 ----
    # 幸存者 = 每组 (doc_id, origin) 里 created_at 最早的（并列时取 id 最小，保证确定性）
    conn.execute(sa.text("""
        CREATE TEMP TABLE _merge_map AS
        SELECT d.id AS loser_id, s.id AS keeper_id
        FROM documents d
        JOIN (
            SELECT DISTINCT ON (doc_id, origin) doc_id, origin, id
            FROM documents ORDER BY doc_id, origin, created_at, id
        ) s ON s.doc_id = d.doc_id AND s.origin = d.origin
        WHERE d.id <> s.id
    """))

    # 归属改指幸存者（去重：同一个人可能在两条记录上都出现过）
    conn.execute(sa.text("""
        UPDATE document_uploads u SET document_id = m.keeper_id
        FROM _merge_map m WHERE u.document_id = m.loser_id
          AND NOT EXISTS (SELECT 1 FROM document_uploads x
                          WHERE x.document_id = m.keeper_id AND x.user_id = u.user_id)
    """))
    # 上面那条 UPDATE 跳过的是"幸存者已经有这个人了"的行，它们现在是多余的，删掉
    conn.execute(sa.text("""
        DELETE FROM document_uploads u USING _merge_map m WHERE u.document_id = m.loser_id
    """))

    # ---- parse_jobs 要单独处理：它有 uq_parse_jobs_doc_options ----
    # 6 份重复文档各自解析过，合并后这些 job 会在 (document_id, options_hash)
    # 上撞车（真实数据上第一次跑就撞了）。**同一个文件、同一套参数的 N 次解析
    # 本来就是冗余的** —— 那个约束存在的理由正是"同参数重解析要幂等命中已有 job"。
    #
    # 所以每组 (keeper, options_hash) 只留一个：
    #   优先 keeper 自己当前指着的那个 -> 再优先 succeeded -> 再取最早 -> id 兜底
    conn.execute(sa.text("""
        CREATE TEMP TABLE _job_map AS
        WITH grouped AS (
            SELECT j.id AS job_id, COALESCE(m.keeper_id, j.document_id) AS keeper_id,
                   j.options_hash,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(m.keeper_id, j.document_id), j.options_hash
                       -- `d` 是按分区键 join 的，所以 `d.current_job_id` 在同一分区内
                       -- 是常量：它为 NULL 时整个分区并列，NULLS FIRST/LAST 都一样，
                       -- 排序**照旧退化到 created_at**。写 NULLS LAST 只是让意图明确，
                       -- 别指望它能修好这件事。
                       --
                       -- **幸存者选谁其实不影响正确性** —— 合并过的 keeper
                       -- 在下面一律清 chunks + index_status='none' 重建索引。
                       -- 早先这里想靠排序保证"选中 keeper 当前那个 job"，
                       -- 而真正堵住"看着 ready 其实没索引"的是那两条兜底清理。
                       ORDER BY (d.current_job_id = j.id) DESC NULLS LAST,
                                (j.status = 'succeeded') DESC NULLS LAST,
                                j.created_at, j.id
                   ) AS rn
            FROM parse_jobs j
            LEFT JOIN _merge_map m ON m.loser_id = j.document_id
            LEFT JOIN documents d ON d.id = COALESCE(m.keeper_id, j.document_id)
            WHERE j.document_id IN (SELECT loser_id FROM _merge_map)
               OR j.document_id IN (SELECT keeper_id FROM _merge_map)
        )
        SELECT g.job_id, g.keeper_id,
               FIRST_VALUE(g.job_id) OVER (
                   PARTITION BY g.keeper_id, g.options_hash ORDER BY g.rn
               ) AS survivor_id
        FROM grouped g
    """))

    # 冗余 job 的计量流水**改指幸存 job，不删也不置空**：
    # 账单不能因为合并就消失（"计量流水不跟着删"是删除端点里就写着的规矩），
    # 而幸存 job 与它是同一个文件、同一套参数的同一件事，指过去是诚实的
    conn.execute(sa.text("""
        UPDATE usage_records u SET parse_job_id = jm.survivor_id
        FROM _job_map jm WHERE u.parse_job_id = jm.job_id AND jm.job_id <> jm.survivor_id
    """))
    # 冗余 job 的 chunks 直接删：它们是**可重建缓存**（架构上就是这么定位的），
    # 而按 (job, seq) 定位的出处在两个等价 job 之间硬接会指错块 —— 宁可重建
    conn.execute(sa.text("""
        DELETE FROM chunks WHERE parse_job_id IN (
            SELECT job_id FROM _job_map WHERE job_id <> survivor_id)
    """))
    conn.execute(sa.text("""
        DELETE FROM parse_jobs WHERE id IN (
            SELECT job_id FROM _job_map WHERE job_id <> survivor_id)
    """))
    # 活下来的 job 改指幸存文档
    conn.execute(sa.text("""
        UPDATE parse_jobs j SET document_id = jm.keeper_id
        FROM _job_map jm WHERE j.id = jm.job_id AND jm.job_id = jm.survivor_id
    """))
    # keeper 的 current_job_id 若指向已删的 job，改指幸存者
    conn.execute(sa.text("""
        UPDATE documents d SET current_job_id = jm.survivor_id
        FROM _job_map jm WHERE d.current_job_id = jm.job_id AND jm.job_id <> jm.survivor_id
    """))
    conn.execute(sa.text("DROP TABLE _job_map"))

    # 其余四张引用表直接改指幸存者
    for table in REFERRING:
        conn.execute(sa.text(f"""
            UPDATE {table} t SET document_id = m.keeper_id
            FROM _merge_map m WHERE t.document_id = m.loser_id
        """))

    # 软删除取最宽松：只要有人没删，合并后就是未删
    conn.execute(sa.text("""
        UPDATE documents k SET deleted_at = NULL
        WHERE EXISTS (SELECT 1 FROM _merge_map m JOIN documents l ON l.id = m.loser_id
                      WHERE m.keeper_id = k.id AND l.deleted_at IS NULL)
    """))

    # ---- 合并过的文档一律清索引重建 ----
    # **两个洞，都不报错，都在这里堵。**
    #
    # ① 存活的 loser job 会把自己的 chunks 一起带到 keeper 上，于是**一份文档
    #    挂着两个 parse_job 的块**。而应用层的不变量是"一份文档 = 一个 job 的
    #    chunks"（indexing.py 每次建索引都先按 document_id 全删），
    #    `search.py` 的作用域也只有 document_id、**从不按 parse_job_id 过滤**。
    #    结果：检索会把两个解析版本的块混着返回，命中非当前版本的块时
    #    citation 带的是那个版本的 page_idx/bbox，而前端按当前版本的页去画框 ——
    #    **带着已验证标记的假出处**，本项目定义的最恶劣错误。
    #
    # ② 光删 chunks 而不动 index_status，会留下"看着是 ready 其实没有索引"：
    #    stats 把它算进可问答、界面允许提问，而每次提问都返回 `no_hits` ——
    #    **而 no_hits 的语义是"文档里没有"**，于是系统状态故障伪装成了事实。
    #    与 no_instruct_model 那个坑同一类。
    conn.execute(sa.text("""
        DELETE FROM chunks WHERE document_id IN (SELECT keeper_id FROM _merge_map)
    """))
    conn.execute(sa.text("""
        UPDATE documents SET index_status = 'none', index_error = NULL
        WHERE id IN (SELECT keeper_id FROM _merge_map)
    """))

    conn.execute(sa.text("DELETE FROM documents WHERE id IN (SELECT loser_id FROM _merge_map)"))
    conn.execute(sa.text("DROP TABLE _merge_map"))

    # ---- 5. 结构收紧 ----
    op.drop_constraint("uq_documents_user_doc_origin", "documents", type_="unique")
    op.drop_index("ix_documents_user_deleted_created", table_name="documents")
    op.alter_column("documents", "user_id", new_column_name="uploaded_by")
    # 列改名之后 PG 会**留着索引的旧名字**（`ix_documents_user_id` 仍然指着
    # `uploaded_by`）—— 功能没错，但与 models.py 的 `index=True` 推出来的名字对不上，
    # 全新装出来的库和迁移出来的库会长得不一样。这种不一致以后排查 schema
    # 漂移时最费时间，顺手改掉。
    _rename_index(conn, "ix_documents_user_id", "ix_documents_uploaded_by")
    # 外键约束名同理会留旧名（`documents_user_id_fkey`），而全新 create_all
    # 出来的是 `documents_uploaded_by_fkey`。同一个理由：迁移出来的库与全新装的
    # 库长得不一样，以后排查 schema 漂移最费时间
    _rename_constraint(conn, "documents_user_id_fkey", "documents_uploaded_by_fkey")
    op.create_unique_constraint("uq_documents_doc_origin", "documents", ["doc_id", "origin"])
    op.create_index("ix_documents_deleted_created", "documents", ["deleted_at", "created_at"])


def downgrade() -> None:
    """结构可逆，**合并掉的行不可逆**（见模块 docstring）。"""
    op.drop_index("ix_documents_deleted_created", table_name="documents")
    op.drop_constraint("uq_documents_doc_origin", "documents", type_="unique")
    _rename_constraint(op.get_bind(), "documents_uploaded_by_fkey", "documents_user_id_fkey")
    _rename_index(op.get_bind(), "ix_documents_uploaded_by", "ix_documents_user_id")
    op.alter_column("documents", "uploaded_by", new_column_name="user_id")
    op.create_unique_constraint("uq_documents_user_doc_origin", "documents",
                                ["user_id", "doc_id", "origin"])
    op.create_index("ix_documents_user_deleted_created", "documents",
                    ["user_id", "deleted_at", "created_at"])
    op.drop_index("ix_document_uploads_user_id", table_name="document_uploads")
    op.drop_index("ix_document_uploads_document_id", table_name="document_uploads")
    op.drop_table("document_uploads")
    op.drop_column("users", "is_admin")
    op.drop_column("parse_jobs", "initiated_by")
