"""交叉编码器重排。**两侧共用的唯一一份**（阶段 2a 从 web 层迁入）。

## 它解决什么

RRF 融合只看名次，不看"有多相关"。向量路排第一和关键词路排第一的块得到同样的分，
而两路都可能只是"沾边"。交叉编码器把 (query, chunk) 一起送进模型算一个真实的相关度分，
是这一层唯一能把"勉强及格"和"绝佳命中"分开的手段。

## 为什么是 TEI

TEI 原生支持 `/rerank`，与已经在跑的 embed 是**同一类容器换个 model**
（`BAAI/bge-reranker-v2-m3`），几乎零新增运维面。这是 D1 选它的全部理由。

## 降级必须可见

没部署 rerank 服务时上游返回 404。**照常返回原名次，但打 `rerank_unavailable` 标记。**
悄悄不重排会让"上了 rerank 之后指标没变好"变成一个查不出原因的悬案 ——
M4a 向量检索静默退回 BM25 就是这么拖了很久没人发现。

## 配置为什么是入参而不是全局 settings

搬进 ddp_core 时把散落的 `settings` 读取换成了一个显式的 `RerankConfig`（五个字段：
`enabled` / `endpoint` / `token` / `model` / `timeout`）。不是为了好看：
**core 是两个仓库共用的叶子模块，不能 import 任何一侧的 `app.config`**
（两边各有一个 `app` 顶层包，import 谁都是错的）。
调用方从自己的 settings 构造一个传进来，core 只认这个形状。

注意 `token` **不是** `settings.rerank_token` 的直译：web 层的装配
（`app/config.py::rerank_config`）是 `rerank_token or service_token` ——
"没单独配 rerank token 就复用内网服务令牌"这条兜底属于部署约定，
留在 web 层，core 不该知道有 `service_token` 这回事。
"""
from dataclasses import dataclass

import httpx

from ddp_core.hits import Hit


@dataclass(frozen=True, slots=True)
class RerankConfig:
    """重排需要知道的全部东西。调用方从自己的 settings 装配。

    `enabled=False` 时 `rerank_hits` 直接返回原名次前 top_k，不打降级标 ——
    "没开这个功能"和"开了但用不了"是两件事，后者才要打 `rerank_unavailable`。
    """

    enabled: bool = False
    endpoint: str = ""
    token: str = ""
    model: str = ""
    timeout: float = 20.0


def _headers(cfg: RerankConfig) -> dict:
    return {"Authorization": f"Bearer {cfg.token}"}


async def rerank_hits(http: httpx.AsyncClient, query: str, hits: list[Hit], *,
                      top_k: int, cfg: RerankConfig) -> tuple[list[Hit], str | None]:
    """按相关度重排并截断到 top_k。

    返回 (重排后的命中, 降级标记)。降级标记非空表示**没有真的重排**，
    此时返回的是原名次的前 top_k 条 —— 调用方必须把它透出到界面上。
    """
    if not cfg.enabled or len(hits) <= 1:
        return hits[:top_k], None

    payload: dict = {"query": query, "texts": [h["text"] for h in hits]}
    if cfg.model:
        payload["model"] = cfg.model

    try:
        resp = await http.post(cfg.endpoint, json=payload, headers=_headers(cfg),
                               timeout=httpx.Timeout(10.0, read=cfg.timeout))
        if resp.status_code == 404:
            # 上游没注册 rerank 模型。这是**部署状态**，不是故障 —— 但它改变了
            # 检索质量，所以必须让用户看得见，不能当没发生
            return hits[:top_k], "rerank_unavailable"
        resp.raise_for_status()
        ranked = resp.json()
    except Exception:
        return hits[:top_k], "rerank_unavailable"

    if not isinstance(ranked, list) or not ranked:
        return hits[:top_k], "rerank_unavailable"

    ordered: list[Hit] = []
    seen: set[int] = set()
    for item in ranked:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index in seen or not (0 <= index < len(hits)):
            continue
        seen.add(index)
        hit = Hit(hits[index])
        # **不覆盖 similarity**：它是余弦相似度，有校准过的量纲，界面上的
        # "相关度"显示的就是它（qa_low_similarity 那条提示线也按它判）。
        # rerank 分是另一把尺子（未校准、模型相关），单独放一列
        hit["rerank_score"] = round(float(item.get("score", 0.0)), 6)
        ordered.append(hit)
        if len(ordered) >= top_k:
            break

    if not ordered:
        return hits[:top_k], "rerank_unavailable"
    return ordered, None
