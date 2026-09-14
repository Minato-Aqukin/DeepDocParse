"""Federation delivery bytes: persist the bounded deliverable result document.

P5 的交付以前只到"清单摘要确认"：`GET /api/v1/deliveries/{id}` 没有可读的
结果字节，"本地已校验持久提交"只覆盖摘要层面。0030 给
`federation_deliveries` 加一列可交付文档（服务端规范 JSON，**不含源文件
字节/正文摘录**），由读取端点返回，客户端重算 `content_digest` 与
`result_manifest_digest` 对账后才允许 ack。

为什么单开一列而不是复用 `federation_requests.result_json`：
交付有自己的生命周期（TTL/确认/过期），确认是**交付对象**的状态，而不是
任务结果的状态；两者混用会让"结果还在但交付已过期"无法表达。

没有回填：这一列只由新执行路径写；历史交付行按"结果文档缺失"处理
（读取端点返回 result=null，客户端拒绝确认）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("federation_deliveries") as batch:
        batch.add_column(sa.Column("result_json", sa.JSON, nullable=True))


def downgrade():
    with op.batch_alter_table("federation_deliveries") as batch:
        batch.drop_column("result_json")
