"""检索命中的形状 —— **两侧共用的唯一一份**。

从 `DeepDocParse-Web/backend/app/search.py` 迁入（阶段 2a）。
抽出来单独一个模块是因为它是**检索层与消费方之间的契约**：
rerank / qa / extraction 都拿它，而那几个模块正在陆续迁进 core。
放在 search.py 里的话，谁想用 Hit 就得把整个 PgVectorIndex（连同 SQLAlchemy
与那一大段裸 SQL）一起拖进来。
"""


class Hit(dict):
    """命中：{chunk_id, document_id, parse_job_id, seq, page_idx, bbox, page_size, text,
              block_type, table_html, score, similarity}

    `block_type` / `table_html` 是 v1.1 加的：抽取平面按块类型优先看表格块，
    并靠 table_html 把表格映射成记录数组（拼出来的单元格文字已经丢了行列关系）。

    **score 与 similarity 是两回事，别混用**：
    - `score` 是 RRF 融合分，只由名次决定，上限 2/(60+1)≈0.0328。它能排序，
      但表达不了"有多相关"——两路都排第一的块永远是 0.0328，不管它其实多勉强。
    - `similarity` 是问题与块的余弦相似度，有校准过的量纲（下限 0.45，实测真实命中
      0.725~0.786、无关问题 0.246~0.381）。**要给用户看的是它**。
      向量路没跑（向量化挂了）或该块没有向量时为 None。

    `chunk_id` 是随机 UUID，**每次 reindex 都会重铸**（indexing.py 先 DELETE 再 add_all）。
    因此出处的稳定定位键是 `(document_id, parse_job_id, seq)`，chunk_id 只作即时引用。
    检索层必须把这三个字段一并带出来，否则出处一旦落库就再也接不回原文（P0）。
    """
