"""向量缓存索引的命名规则 —— **写入侧与检索侧共用的唯一一份。**

合仓前这个函数在两处各写了一遍（`gateway/app/services/task_store.py` 与
`mcp_server/server.py`），靠注释互相叮嘱"必须保持同一命名规则"。
两边漂开的后果**不报错**：写入侧往 A 索引写、检索侧查 B 索引，
结果是永久零命中、永久静默退回 BM25 —— 而 `index_status` 一直是 ready。

同一类"两份复制品靠注释同步"在这个项目里已经静默出错过三次
（关键词路 AND/OR 语义、重建索引指错块、抽取平面从不打 vision_unavailable），
所以这里只留一份。
"""


def chunk_index_name(dim: int) -> str:
    """索引名带维度。

    换 embedding 模型（维度变化）时会走一个新索引，而不是往旧索引里写
    维度不符的向量 —— 那些向量会被 RediSearch **静默丢弃**，表现为永久零命中
    （M4 验收发现）。同维度换模型仍无法区分，那种情况需要手动 FT.DROPINDEX。
    """
    return f"chunks_idx_d{dim}"
