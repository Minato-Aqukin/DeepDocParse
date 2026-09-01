"""可移植的向量列类型。

生产是 PostgreSQL + pgvector；单测跑 SQLite in-memory（M5 起就是这样，34 例不用起 PG/MinIO，
这个反馈速度不能因为引入 pgvector 丢掉）。所以向量列按方言分流：
  - PostgreSQL -> pgvector 的 VECTOR(dim)，能建 HNSW 索引、能用 <=> 算子
  - 其它方言   -> JSON 存 float 数组，只保证能存取；检索由 MemoryIndex 在 Python 里做

对应地，检索本身抽在 `SearchIndex` 协议后面，不在模型层写 SQL。

**两侧共用的唯一一份**（阶段 2a 从 DeepDocParse-Web 迁入）。
"""
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator


class Vector(TypeDecorator):
    """float 列表 <-> 向量列。"""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector as PgVector
            except ImportError as exc:      # pragma: no cover - 生产必装
                raise RuntimeError(
                    "PostgreSQL 后端需要 pgvector 包与数据库扩展：pip install pgvector "
                    "且 CREATE EXTENSION vector"
                ) from exc
            return dialect.type_descriptor(PgVector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value                    # pgvector 自己序列化
        return list(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return list(value)
