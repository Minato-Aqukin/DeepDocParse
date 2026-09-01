"""去掉 parse_jobs.engine 的库级默认值 'mineru'

0002 建表时写了 `server_default="mineru"`。当时所有部署都跑 mineru，这个默认值
看着无害；无 GPU 部署（service 侧 models.cpu.yaml 只注册 borndigital）出现之后
它就变成了陷阱：漏传 engine 时库会**静默**填一个在目标注册表里根本不存在的引擎名，
真正的报错要等到解析提交时才以 404 unknown_engine 的形式出现，且指不回源头。

models.py 已经去掉客户端侧的 `default=`，但那只在 SQLite（单测 create_all 走模型）
上生效；PG 上 SQLAlchemy 会把整列从 INSERT 里省掉，于是库级默认值照样兜底 ——
"写着有防护、实际没有"。这条迁移把两边对齐，顺带消掉 models.py 与库结构的漂移
（否则下次 `alembic revision --autogenerate` 会凭空多出一条 alter_column）。

不动 nullable=False：漏传就该在插入时炸，这正是想要的行为。
不回填、不改任何现存行——只摘掉默认值。

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("parse_jobs") as batch:
        batch.alter_column("engine", existing_type=sa.String(32),
                           existing_nullable=False, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("parse_jobs") as batch:
        batch.alter_column("engine", existing_type=sa.String(32),
                           existing_nullable=False, server_default="mineru")
