"""`rerank_hits` 的 HTTP 路径 —— 阶段 2a 二次验收发现这一整条路零覆盖。

发现的方式是变异：把 `cfg.endpoint` 换成 `http://bogus.invalid/nope`、
把 `_headers()` 改成返回 `{}`（丢掉 Bearer）、把 404 分支的
`rerank_unavailable` 改成 `None`（**静默吞掉降级**）—— 三个变异，167 例全绿。

此前唯一沾边的用例只检查 `"rerank_unavailable"` 在契约词汇表里，
自己拼了个假响应 dict，**从不调用 `rerank_hits`**。
阶段 2a 刚好改了这个函数的签名（settings -> RerankConfig），
签名变了而行为没人验，正是这个项目反复吃亏的形状。

这里守的是 plan.md §9 不变式 2：**每一次降级都必须可见**。
`rerank_unavailable` 一旦被吞掉，表现是"上了 rerank 之后指标没变好"——
一个查不出原因的悬案（M4a 向量检索静默退回 BM25 就这么拖了很久）。
"""
import httpx
import pytest
import respx

from ddp_core.hits import Hit
from ddp_core.rerank import RerankConfig, rerank_hits

ENDPOINT = "http://tei-rerank:18080/rerank"
CFG = RerankConfig(enabled=True, endpoint=ENDPOINT, token="rr-token",
                   model="BAAI/bge-reranker-v2-m3", timeout=5.0)


def _hits(n: int = 3) -> list[Hit]:
    """n 条命中，similarity 递减 —— 与 RRF 出来的原名次一致。"""
    return [Hit(chunk_id=f"c{i}", document_id="d1", parse_job_id="j1", seq=i,
                page_idx=i, bbox=[0, 0, 1, 1], page_size=[612, 792],
                text=f"第 {i} 段", block_type="text", table_html=None,
                score=0.03 - i * 0.001, similarity=0.8 - i * 0.1)
            for i in range(n)]


@pytest.fixture
async def http():
    async with httpx.AsyncClient(trust_env=False) as client:
        yield client


@respx.mock
async def test_reranks_and_keeps_similarity_untouched(http):
    """正例：真的按上游给的名次重排，且**不覆盖 similarity**。

    similarity 是余弦相似度，有校准过的量纲，界面上的"相关度"显示的就是它。
    rerank 分是另一把未校准的尺子，只能单独放一列 —— 覆盖掉它等于让
    "相关度 0.72" 这个数字在开没开 rerank 时含义不同，而用户看不出来。
    """
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=[
        {"index": 2, "score": 0.91}, {"index": 0, "score": 0.44},
    ]))

    hits = _hits()
    before = [(h["chunk_id"], h["similarity"]) for h in hits]
    ordered, degraded = await rerank_hits(http, "问题", hits, top_k=3, cfg=CFG)

    assert degraded is None
    assert [h["chunk_id"] for h in ordered] == ["c2", "c0"], "没有按上游名次重排"
    assert [h["rerank_score"] for h in ordered] == [0.91, 0.44]
    assert [(h["chunk_id"], h["similarity"]) for h in ordered] == [before[2], before[0]], \
        "similarity 被 rerank 分覆盖了 —— 界面上的相关度会变成另一把尺子"

    # 请求真的发到了配置的端点，且带上了 Bearer（这两条各自都被变异验证过会红）
    assert route.called
    request = route.calls.last.request
    assert str(request.url) == ENDPOINT
    assert request.headers["Authorization"] == "Bearer rr-token"
    import json
    body = json.loads(request.content)
    assert body["query"] == "问题" and body["texts"] == ["第 0 段", "第 1 段", "第 2 段"]
    assert body["model"] == "BAAI/bge-reranker-v2-m3"


@respx.mock
async def test_model_is_omitted_when_not_configured(http):
    """留空 = 由上游注册表选 default。传一个空串会被上游当成"要一个叫 '' 的模型"。"""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=[{"index": 0, "score": 1.0}]))
    await rerank_hits(http, "问题", _hits(), top_k=3,
                      cfg=RerankConfig(enabled=True, endpoint=ENDPOINT, token="t"))
    import json
    assert "model" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_upstream_404_is_a_visible_degradation(http):
    """上游没注册 rerank 模型 —— 照常返回原名次，但**必须打标**。

    这是 plan.md §9 不变式 2 的正面用例。悄悄不重排会让
    "上了 rerank 之后指标没变好"变成一个查不出原因的悬案。
    """
    respx.post(ENDPOINT).mock(return_value=httpx.Response(404, json={"error": "no model"}))

    ordered, degraded = await rerank_hits(http, "问题", _hits(), top_k=2, cfg=CFG)
    assert degraded == "rerank_unavailable", "404 被静默吞掉了"
    assert [h["chunk_id"] for h in ordered] == ["c0", "c1"], "降级时必须原样返回原名次"
    assert all("rerank_score" not in h for h in ordered), "没重排却留下了 rerank 分"


@respx.mock
@pytest.mark.parametrize("response,why", [
    (httpx.Response(500, text="boom"), "上游 5xx"),
    # **不可迭代**的 JSON 是这里唯一非要靠 `isinstance(ranked, list)` 挡住的一种：
    # 遍历那一段在 try 之外，`for item in 42` 会直接抛 TypeError 把请求打挂。
    # 写成 `{"not": "a list"}` 是不够的 —— dict 可迭代，会被后面
    # 「一条都没排上」那个分支兜住，于是砍掉形状检查也照样绿（实测如此）
    (httpx.Response(200, json=42), "响应是个数字（不可迭代）"),
    (httpx.Response(200, json={"not": "a list"}), "响应是 dict 不是列表"),
    (httpx.Response(200, json=[]), "响应是空列表"),
    (httpx.Response(200, json=[{"index": 99, "score": 1.0}]), "名次全部越界"),
])
async def test_every_failure_mode_is_visible(http, response, why):
    """所有失败形态都要打 `rerank_unavailable`，一个都不许静默 —— 也一个都不许抛。"""
    respx.post(ENDPOINT).mock(return_value=response)
    ordered, degraded = await rerank_hits(http, "问题", _hits(), top_k=2, cfg=CFG)
    assert degraded == "rerank_unavailable", f"{why} 时降级不可见"
    assert [h["chunk_id"] for h in ordered] == ["c0", "c1"]


@respx.mock
async def test_connection_error_is_visible(http):
    """连不上（容器没起、DNS 不通、超时）同样要打标，不许当没发生。"""
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("no route"))
    ordered, degraded = await rerank_hits(http, "问题", _hits(), top_k=2, cfg=CFG)
    assert degraded == "rerank_unavailable"
    assert [h["chunk_id"] for h in ordered] == ["c0", "c1"]


@respx.mock
async def test_disabled_does_not_call_upstream_and_does_not_flag(http):
    """`enabled=False` 是"没开这个功能"，**不是降级** —— 不打标，也不发请求。

    与上面 404 那条是一对：两者都返回原名次，但只有后者要让用户看见。
    分不清的话，没部署 rerank 的用户每次问答都会看到一个吓人的降级提示。
    """
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=[]))
    ordered, degraded = await rerank_hits(
        http, "问题", _hits(), top_k=2,
        cfg=RerankConfig(enabled=False, endpoint=ENDPOINT, token="t"))
    assert degraded is None and not route.called
    assert [h["chunk_id"] for h in ordered] == ["c0", "c1"]


@respx.mock
async def test_single_hit_skips_the_round_trip(http):
    """只有一条命中时重排没有意义，别白花一个往返。"""
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=[]))
    ordered, degraded = await rerank_hits(http, "问题", _hits(1), top_k=3, cfg=CFG)
    assert degraded is None and not route.called and len(ordered) == 1
