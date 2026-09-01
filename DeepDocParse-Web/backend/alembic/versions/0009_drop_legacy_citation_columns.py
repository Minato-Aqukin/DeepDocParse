"""删掉出处的第二份存储（plan.md 阶段 4）

## ⚠️ 这一步不可逆

`messages.citations` 整列删除，`extraction_items.fields[].citations` 从 JSON
里摘掉。**downgrade 只能把列/键的形状加回来，数据回不来** ——
出处此时的唯一真相是 `evidence` / `citations` 两张表（阶段 2b 建、阶段 3 切读）。
按 plan.md：**跑这一步之前先打数据库快照。**

## 为什么非删不可

留着第二份的代价不是磁盘，是**两个真相**。那一份没人维护、没人读，
却长得跟真的一模一样：重建索引之后它仍然是当年的旧快照，
下一个人照着它排查就会得出错误结论。阶段 3 已经现场抓到过这个形状 ——
`extractions.py` 里 `load_citation_targets` 导入了却从没被调用，
于是抽取的出处被无条件标成 `resolved=True`，而那个函数的注释正写着
"不能无脑 resolved=True"。参数是死的，守卫就是装饰。

## 前置

`evidence` / `citations` 必须已经有数据（0007 建表 + 双写，0008 回填历史）。
**这里主动查一次**：新表是空的而老列有数据，说明回填没跑或跑失败了，
此时删列 = 永久丢掉全部出处。宁可让迁移停下。

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 闸门：老列还有出处、新表却是空的 —— 那就是回填没生效，绝不能往下走
    old_rows = conn.execute(sa.text(
        "SELECT COUNT(*) FROM messages WHERE citations IS NOT NULL "
        "AND CAST(citations AS TEXT) NOT IN ('[]', 'null', '')")).scalar() or 0
    new_rows = conn.execute(sa.text("SELECT COUNT(*) FROM citations")).scalar() or 0
    if old_rows and not new_rows:
        raise RuntimeError(
            f"拒绝删列：messages.citations 还有 {old_rows} 行有出处，而 citations 表是空的。"
            f"这说明 0008 的回填没跑成 —— 现在删列会永久丢掉全部出处。"
            f"先确认 0008 的输出（那几个计数），必要时 downgrade 到 0007 重来。")

    op.drop_column("messages", "citations")

    # extraction_items.fields 是 JSON 列，出处是里面的一个键。整列不能删
    # （status/value/verified/degraded/confidence 还在用），只摘 citations。
    # **用 Python 改而不是 SQL JSON 函数**：两个方言的 JSON 函数不一样，
    # 而这段只在升级时跑一次，可读性比性能重要得多
    import json

    rows = conn.execute(sa.text(
        "SELECT id, fields FROM extraction_items ORDER BY id")).fetchall()
    stripped = 0
    for row_id, fields in rows:
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except (TypeError, ValueError):
                continue
        if not isinstance(fields, dict):
            continue
        cleaned = {name: ({k: v for k, v in cell.items() if k != "citations"}
                          if isinstance(cell, dict) else cell)
                   for name, cell in fields.items()}
        if cleaned != fields:
            conn.execute(sa.text("UPDATE extraction_items SET fields = :f WHERE id = :id"),
                         {"f": json.dumps(cleaned, ensure_ascii=False), "id": row_id})
            stripped += 1
    print(f"[0009] 删掉 messages.citations（{old_rows} 行曾有出处）；"
          f"从 {stripped}/{len(rows)} 条抽取结果里摘掉 fields[].citations")


def downgrade() -> None:
    """只把列加回来，**数据回不来**。

    加回来是为了让 0008 那类还会读这一列的迁移能重跑，不是为了恢复出处。
    真要恢复出处只有一条路：从快照还原。
    """
    op.add_column("messages", sa.Column("citations", sa.JSON(), nullable=True))
