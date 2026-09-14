"""确定性进程内检索执行者（夹具关键词预言机）。

**这不是真实检索。** 它替换掉的只有 corpus-api 节点侧的 `federation.run_probe`
到真实索引之间的那段 I/O：给定一个已在 scope 里的公开集合目标，它按
"查询内容词与证据文本的重叠数"排序返回该集合的证据信封（`(-overlap, evidence_id)`
稳定排序，`candidate_limit` 截断）。覆盖数学、目标枚举、候选排序、预算与账本
全部由 `ddp_core` / 协调者的真实函数完成，见 `harness.py`。

为什么是关键词重叠而不是"返回整集合"：整集合返回会让"范围内无证据"这类
问题也永远召回别题的证据，评测就失去了诚实的零命中情形。重叠打分只影响
**集合内选哪些证据**；夹具的构造自检保证：只要必需证据所在集合被探到，
必需证据一定落在这个候选上限之内（`dataset._validate` 用同一个
`rank_evidence` 复算）。

两条硬性行为，正是覆盖诚实性需要的：
- 私有集合（`publication != "published"`）或未知集合一律当场抛
  `FixtureScopeError` —— 私有诱饵既不许被探，也不许被返回；
- 返回的证据只能来自目标集合本身；任何越界 id 由 `harness` 的检查抓住。

调用日志（`.calls`）是评测的隐私轴证据：`harness` 用它证明被 deny/未选中的
目标一个字节都没发出去。执行者不暴露可审计的调用日志时，评测拒绝运行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 与路由内核 `routing._TOKEN` 同一正则；这里只做检索模拟，不重写覆盖数学。
_TOKEN = re.compile(r"[0-9a-z\u4e00-\u9fff]+")
#: 合成检索的内容词过滤：不滤的话 "the"/"is" 会把任意两段文本凑成"命中"，
#: 无证据问题就会假命中。这是一张小口径停用词表，不追求通用检索质量。
STOPWORDS = frozenset(
    "a an and are as at be by do does for from how in is it of on or say the to "
    "was were what when where which who why will with".split())


def content_tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.casefold())
            if token not in STOPWORDS and (len(token) > 1 or not token.isascii())}


def rank_evidence(dataset: dict, collection_id: str, query: str,
                  candidate_limit: int) -> list[str]:
    """该集合内按重叠度返回证据 id（稳定、确定性）；0 重叠不返回。"""
    query_tokens = content_tokens(query)
    scored = []
    for evidence_id in sorted(dataset["evidence"]):
        meta = dataset["evidence"][evidence_id]
        if meta["collection_id"] != collection_id:
            continue
        overlap = len(query_tokens & content_tokens(meta["text"]))
        if overlap:
            scored.append((-overlap, evidence_id))
    scored.sort()
    return [evidence_id for _, evidence_id in scored[:candidate_limit]]


class FixtureScopeError(RuntimeError):
    """目标集合不在夹具的公开索引里（私有或未知）。"""


@dataclass
class RetrievalResult:
    """一次集合检索的结果：证据信封 + 它实际来自哪个集合。"""

    collection_id: str
    items: list[dict] = field(default_factory=list)


class FixtureExecutor:
    """在夹具上做关键词召回；同一输入永远给同一输出、同一顺序。"""

    def __init__(self, dataset: dict):
        self.dataset = dataset
        #: 审计日志：每次 retrieve 记下目标三元组，供隐私/覆盖检查。
        self.calls: list[dict] = []

    def retrieve(self, *, target: dict, query: str, candidate_limit: int) -> RetrievalResult:
        collection_id = target["collection_id"]
        origin_node_id = target["origin_node_id"]
        self.calls.append({"origin_node_id": origin_node_id,
                           "collection_id": collection_id,
                           "operation": target["operation"]})
        collection = self.dataset["collections"].get(collection_id)
        if collection is None:
            raise FixtureScopeError(f"unknown collection {collection_id!r}")
        if collection["publication"] != "published":
            raise FixtureScopeError(
                f"collection {collection_id!r} is not published; refusing to retrieve")
        if collection["origin_node_id"] != origin_node_id:
            raise FixtureScopeError(
                f"collection {collection_id!r} belongs to {collection['origin_node_id']!r}, "
                f"not {origin_node_id!r}")
        ranked = rank_evidence(self.dataset, collection_id, query, candidate_limit)
        return RetrievalResult(collection_id,
                               [self._envelope(evidence_id) for evidence_id in ranked])

    def _envelope(self, evidence_id: str) -> dict:
        meta = self.dataset["evidence"][evidence_id]
        return {
            "schema": "ddp-evidence/1#FederatedEvidence",
            "evidence_id": evidence_id,
            "origin_node_id": meta["origin_node_id"],
            "authority_node_id": meta["origin_node_id"],
            "resource_id": meta["document_id"],
            "source_version_id": "v1",
            "source_digest": meta["source_digest"],
            "parse_revision": meta["parse_revision"],
            "excerpt_digest": meta["excerpt_digest"],
            "locator": dict(meta["locator"]),
            "source_type": "source",
            "derived_from": None,
            "uploader_ref": None,
            "retrieval_receipt_ref": None,
            "policy_revision": "published:1",
            "block_type": meta["block_type"],
            "excerpt": meta["text"],
        }
