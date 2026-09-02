"""把仍然指向遗留 public.users 的外键删干净。

0013 想删这些外键，但**它按改名之后的列名去猜约束名**：

    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_fkey')
    #  column 已经是 "actor_id"  ->  conversations_actor_id_fkey

而 PostgreSQL **改列名不会改约束名** —— 真实约束仍叫
`conversations_user_id_fkey`。`IF EXISTS` 于是静默匹配不到、静默成功。
同一个循环里那些没改过名的（`documents.uploaded_by`、`parse_jobs.api_key_id`…）
名字对得上，全删掉了；**只有三张改过名的没删掉**。

## 后果不是不整洁，是主链路不通

`conversations` / `extraction_templates` / `extraction_runs` 三张**活的语料表**
仍然要求 `actor_id` 存在于遗留的 `public.users` 里。而账号已经搬去
`control.users` —— 迁移之后经 control-api 注册的用户，他的 id 根本不在旧表里：

    ERROR:  insert or update on table "conversations"
            violates foreign key constraint "conversations_user_id_fkey"
    DETAIL: Key (actor_id)=(...) is not present in table "users".

**开不了会话、建不了抽取模板、跑不了抽取批次** —— 而那正是新架构的常态用户。

## 为什么一路没被发现

- 单测走 `Base.metadata.create_all`，ORM 里没有这个外键
  （`models.py` 说"一个指向 users.id 的外键都没有"—— 那是**模型**的实情，
  不是**库**的实情）；
- 0013 那段带 `if not is_sqlite`，单测连碰都不碰；
- `check_data_ownership.py` 扫源码，源码是干净的；
- `check_db_boundary.sh` 验的是权限，不是约束；
- `e2e_stack.py` 因为没有 embedding 端点跳过了问答，一条会话都没插过。

只有**对着真库跑完迁移再查 `pg_constraint`** 才看得见 ——
`python.yml` 那条真库迁移作业后面已经加了这条断言。

## 这一版怎么做

**不猜名字，从 `pg_constraint` 查出来。** 名字是可以被改过的
（手工建的库、历史迁移、pg_dump 恢复都可能不一样），而"指向 public.users
的外键"这个性质不会变。
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

#: 只清理这几张**语料**表上的外键。
#: `api_keys` / `usage_records` 是待删的旧账号表，它们指向 users 是应该的 ——
#: 那两张由 `database/migrator/drop_legacy_account_tables.py` 整张删掉。
CORPUS_TABLES = ("conversations", "extraction_templates", "extraction_runs")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite 不支持 DROP CONSTRAINT，而单测用 create_all 建表，
        # 那条路径上本来就没有这些外键
        return

    rows = bind.execute(sa.text("""
        SELECT c.conname, c.conrelid::regclass::text AS tbl
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.confrelid = to_regclass('public.users')
          AND c.conrelid::regclass::text = ANY(:tables)
    """), {"tables": list(CORPUS_TABLES)}).fetchall()

    for conname, table in rows:
        # 名字从库里查出来的，不是拼的 —— 所以这里不需要 IF EXISTS，
        # 而不需要 IF EXISTS 意味着**删不掉会报错而不是静默跳过**
        op.execute(sa.text(f'ALTER TABLE {table} DROP CONSTRAINT "{conname}"'))


def downgrade() -> None:
    # **不重建。** 这些外键指向的是一张即将被删掉的遗留表，
    # 重建它们等于把刚修好的缺陷再造一遍。
    # downgrade 到 0013 的语义是"回到那个版本的 schema"，而那个版本的
    # 本意就是没有这些外键 —— 0013 只是没做到。
    pass
