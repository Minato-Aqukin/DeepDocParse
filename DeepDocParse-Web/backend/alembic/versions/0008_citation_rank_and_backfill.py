"""citations.rank + 历史出处回填（plan.md 阶段 3）

## 两件事

**1. 补 `citations.rank`。** 阶段 2b 漏了：老的 `messages.citations` 是一个
**有序列表**，顺序就是检索名次；而表是无序的行集合。靠 `score` 排替代不了 ——
RRF 分只由名次决定，两路都排第一的块分数完全相同，于是并列的出处在界面上
每次刷新都可能换位置。

**2. 把历史出处搬进 evidence / citations。** 阶段 3 的读已经切到新表，
不回填的话，**所有 2b 之前的老会话与老抽取结果会集体变成"没有出处"** ——
比显示失效严重得多：失效至少还看得见有过一条出处。

回填逻辑在 `app/backfill.py`（可单测、可重复跑）。三个去处加起来必须等于
老记录总数，对不上就抛 —— 悄悄少搬几条正是最难发现的那种错。

## 回滚

`downgrade` 删掉回填进来的行（`provider->>'backfilled'` 打了标）并去掉 rank 列。
**双写写下的行不动** —— 它们不是这次迁移造的。
读切回老列的话把 conversations/extractions 的读改回 `attach_resolution` 即可，
老列全程没被碰过。

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # rank 的 server_default 只为存量行；模型侧不带 server_default，
    # 新行的值一律由应用显式给（漏给就该在插入时暴露，而不是默默记成 0）
    op.add_column("citations",
                  sa.Column("rank", sa.Integer(), nullable=False, server_default="0"))
    # 每条引用各记各的指纹。与 evidence.content_digest 是两回事：
    # 那份是证据首次锚定时的内容，这份是这一次引用当时的内容 ——
    # 只留前者的话，同一个块跨重建被引两次，后一次刚问完就显示出处失效
    op.add_column("citations",
                  sa.Column("content_digest", sa.String(64), nullable=False,
                            server_default=""))

    from app.backfill import backfill

    report = backfill(op.get_bind())
    # 迁移的输出会进 alembic 日志。**这几个数字要留痕** ——
    # 尤其 skipped_no_locator / skipped_no_job：那些出处阶段 4 删老列时会随之消失
    print(f"[0008] {report}")
    for note in report.notes:
        print(f"[0008] {note}")


def downgrade() -> None:
    # 只删这次迁移造出来的行。双写写下的证据没有 backfilled 标记，不动它们
    op.execute(sa.text(
        "DELETE FROM citations WHERE evidence_id IN "
        "(SELECT id FROM evidence WHERE CAST(provider AS TEXT) LIKE '%\"backfilled\": true%')"))
    op.execute(sa.text(
        "DELETE FROM evidence WHERE CAST(provider AS TEXT) LIKE '%\"backfilled\": true%'"))
    op.drop_column("citations", "content_digest")
    op.drop_column("citations", "rank")
