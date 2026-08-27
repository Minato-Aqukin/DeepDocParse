"""文档问答：SSE 帧、出处、以及四种降级的可见性。

降级可见是硬要求：这个项目吃过静默降级的大亏（M4a 的向量检索悄悄退回 BM25），
所以"没做视觉验证""没检索到内容""上游挂了"都必须出现在返回里。
"""
import asyncio
import io
import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import Chunk, Conversation, Document, Message
from tests.conftest import CHAT, EMBEDDINGS
from tests.test_documents import _callback, _embed_response, _mock_service, _upload


def _real_pdf() -> bytes:
    """造一份真 PDF：裁剪路径要能真的渲染出来，才谈得上"视觉验证"。"""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(2):
        doc.new_page(612, 792)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


PDF = _real_pdf()


def _chat_sse(*texts: str) -> httpx.Response:
    async def frames():
        for text in texts:
            yield (f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}\n\n').encode()
        yield b"data: [DONE]\n\n"

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=frames())


async def _ready_document(auth_client) -> dict:
    _mock_service()
    document = await _upload(auth_client, PDF)
    await _callback(auth_client)
    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "ready", detail
    return detail


async def _conversation(auth_client, document_id: str) -> str:
    resp = await auth_client.post(f"/api/documents/{document_id}/conversations")
    assert resp.status_code == 201
    return resp.json()["id"]


# 默认问题与第 1 页 chunk（"第二页的表格数据"）字面高度重合：字符袋假向量下余弦约 0.87，
# 远高于 qa_min_similarity。正例不能贴着阈值走，否则调阈值就会连带弄翻一堆用例。
async def _ask(auth_client, cid: str, question: str = "第二页的表格") -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with auth_client.stream("POST", f"/api/conversations/{cid}/ask",
                                  json={"question": question}) as resp:
        assert resp.status_code == 200, (await resp.aread())[:300]
        assert resp.headers["content-type"].startswith("text/event-stream")
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
        for block in buffer.split("\n\n"):
            if not block.strip():
                continue
            name = block.splitlines()[0].removeprefix("event: ")
            data = json.loads(block.splitlines()[1].removeprefix("data: "))
            events.append((name, data))
    return events


@respx.mock
async def test_ask_streams_answer_with_citations(auth_client, session):
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("第二页", "讲的是表格数据。"))

    events = await _ask(auth_client, cid)
    names = [name for name, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert names.count("delta") == 2, "必须逐帧流式返回，不能攒完一次性给"

    answer = "".join(d["text"] for n, d in events if n == "delta")
    assert answer == "第二页讲的是表格数据。"

    citations = dict(events)["citations"]["citations"]
    # 断到具体页：问的是"表格"，只有第 2 页（page_idx=1）那块讲表格。
    # 写成 `page_idx in (0, 1)` 在两页文档上恒真，等于没断言
    assert [c["page_idx"] for c in citations] == [1], citations
    assert citations[0]["snippet"], "出处要带可读片段"

    # 回答必须落库（流式生成器里另开 session —— 复用请求作用域的那个会炸）
    messages = (await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )).scalars().all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == answer and messages[1].citations

    conversation = await session.get(Conversation, cid)
    await session.refresh(conversation)
    assert conversation.title != "新会话", "首个问题应成为会话标题"


@respx.mock
async def test_ask_degrades_visibly_when_vision_runtime_is_down(auth_client, session):
    """VQA 起不来是 dev 常态。要能回答，但必须标出"没做视觉验证"。"""
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])

    # 分开数两种调用：回答用的，与出处一致性核对用的（A4 的抄写请求）。
    # 只数总数的话，加一个并发的核对请求就会让这条用例莫名其妙变红
    calls = {"answer": 0, "verify": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parts = [part for message in body["messages"]
                 if isinstance(message["content"], list) for part in message["content"]]
        has_image = any(part.get("type") == "image_url" for part in parts)
        is_verify = any("原样" in (part.get("text") or "") for part in parts)
        calls["verify" if is_verify else "answer"] += 1
        if has_image:
            return httpx.Response(502, json={"error": {"message": "vqa unreachable"}})
        return _chat_sse("纯文本回答")

    respx.post(CHAT).mock(side_effect=handler)

    events = await _ask(auth_client, cid)
    done = dict(events)["done"]
    assert done["verified"] is False
    assert done["degraded"] == "vision_unavailable", "降级必须可见"
    assert calls["answer"] == 2, "带图失败后要退回纯文本再试一次"
    # 核对也打不通（同一个视觉运行时），此时必须判"没测出来"而不是"对不上"：
    # 把"不知道"说成"有问题"会毁掉这个标记的可信度
    assert done["degraded"] != "parse_mismatch"

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.degraded == "vision_unavailable" and message.content == "纯文本回答"


@respx.mock
async def test_ask_reports_upstream_failure_instead_of_hanging(auth_client, session):
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))

    events = await _ask(auth_client, cid)
    assert "error" in [n for n, _ in events]
    assert dict(events)["done"]["degraded"] == "upstream_error"
    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.verified is False


@respx.mock
async def test_ask_survives_midstream_upstream_failure(auth_client, session):
    """上游吐了一半断了：已产出的文本要留住，并如实报"上游中断"。

    不处理的话异常会冒到 StreamingResponse，响应体截断在半路，
    客户端只看到连接莫名断开，库里还被记成"客户端主动中断"（真机 e2e 上遇到过）。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])

    async def half_then_die():
        yield f'data: {json.dumps({"choices": [{"delta": {"content": "开头"}}]})}\n\n'.encode()
        raise httpx.ReadTimeout("upstream stalled")

    respx.post(CHAT).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=half_then_die()))

    events = await _ask(auth_client, cid)
    names = [n for n, _ in events]
    assert "error" in names and names[-1] == "done", f"帧序列={names}"
    assert dict(events)["error"]["code"] == "upstream_interrupted"
    assert dict(events)["done"]["degraded"] == "upstream_error"

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.content == "开头", "已产出的部分必须留住"


@respx.mock
async def test_ask_reports_no_hits_for_unrelated_question(auth_client, session):
    """与文档完全无关的问题：必须零出处 + 标 no_hits。

    没有相似度下限的话 top-k 永远返回东西——用户会拿到几条不相干的"出处"，
    裁剪成功时还标着"已做视觉验证"。那样出处就成了假证据，比不给更糟。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("文档中未找到相关内容"))

    events = await _ask(auth_client, cid, question="量子纠缠退相干时间")
    done = dict(events)["done"]
    assert done["degraded"] == "no_hits", f"done={done}"
    assert done["verified"] is False
    assert dict(events)["citations"]["citations"] == [], "无关问题不得凭空给出处"

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.degraded == "no_hits" and message.citations == []


@respx.mock
async def test_keyword_only_match_below_floor_is_not_a_citation(auth_client, session):
    """回归：相似度下限必须同时管住关键词路。

    RRF 是并集融合。下限只加在向量路上时，向量路已判定"全都不相关"的问题，
    仍会靠共现词捞出 chunk 当出处；而 verified 只看有没有裁剪图，
    于是假出处还会被打上"已做视觉验证" —— 比不给出处更糟。

    这里的问题与第 1 页仅共享一个高频字"的"，字符袋余弦远低于 qa_min_similarity，
    但词面确实命中 —— 正是旧实现会漏过去的那一类。

    ⚠️ **这一条只钉住了向量路**，别拿它当关键词路的守卫（阶段 2a 二次验收实测：
    把 `similar_enough` 改成恒 True，167 例全绿）。原因是 `"的"` 会被 jieba 当停用词
    滤掉 -> `terms` 为空 -> 关键词路压根产不出候选 -> 那个过滤器从未被执行。
    下面那条 `..._keyword_path...` 才是关键词路的守卫。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("文档中未找到"))

    chunks = (await session.execute(select(Chunk).order_by(Chunk.seq))).scalars().all()
    texts = [c.text for c in chunks]

    from app.config import settings as cfg
    from ddp_core.search import MemoryIndex, _cosine, _query_tokens

    question = "的"
    qvec = _embed_response(
        httpx.Request("POST", EMBEDDINGS, json={"input": [question]})).json()["data"][0]["embedding"]
    sims = [_cosine(qvec, c.embedding) for c in chunks]
    assert all(s <= cfg.qa_min_similarity for s in sims), \
        f"前提不成立：问题与 {texts} 的相似度 {sims} 未全部低于下限"
    assert any(question in t for t in texts), "前提不成立：词面应当命中"
    # 但**裸子串命中 ≠ 检索里的词面命中**：检索走的是 tokenizer。这里如实记下
    # 这条用例的真实覆盖面 —— `"的"` 被滤成空，关键词路本就没有候选
    assert _query_tokens(question).split() == [], \
        "前提变了：`的` 现在能产出检索词了，这条用例的覆盖面随之改变，请重读 docstring"

    hits = await MemoryIndex().search(session, vector=qvec, query=question,
                                      document_id=document["id"],
                                      min_similarity=cfg.qa_min_similarity,
                                      limit=cfg.qa_top_k, candidates=cfg.qa_candidates)
    assert hits == [], f"低于相似度下限的词面命中不得成为出处，实际返回 {hits}"

    events = await _ask(auth_client, cid, question=question)
    done = dict(events)["done"]
    assert done["degraded"] == "no_hits" and done["verified"] is False
    assert dict(events)["citations"]["citations"] == []


@respx.mock
async def test_similarity_floor_also_filters_the_keyword_path(auth_client, session):
    """回归：下限对**关键词路**同样生效 —— 上一条用例覆盖不到的那一半。

    RRF 是并集融合，两条腿各自出候选。下限只管住向量路的话，
    "向量路判定全都不相关"的问题仍会靠共现词把 chunk 捞进出处，
    而 `verified` 只看有没有裁剪图 —— 假出处还会被打上"已做视觉验证"。
    **这是本项目定义的最恶劣错误**（plan.md §9 不变式 1）。

    与上一条的区别全在选题：这里的问题**分词后仍有词能落到块里**（"表格"），
    但整句与块的字符袋余弦远低于下限。于是关键词路真的产出了候选，
    `similar_enough` 那道过滤器才第一次被执行到。

    前提断言用的是**真 tokenizer**（`_query_tokens` + `tokenized`），
    不是裸子串 —— 裸子串命中与检索里的词面命中不是一回事，
    上一条用例正是栽在这里（阶段 2a 二次验收抓到）。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("文档中未找到"))

    from app.config import settings as cfg
    from ddp_core.search import MemoryIndex, _cosine, _query_tokens
    from ddp_core.tokenize import tokenized

    chunks = (await session.execute(select(Chunk).order_by(Chunk.seq))).scalars().all()
    question = "表格究竟从什么时候开始不再被任何主流实现所支持"
    qvec = _embed_response(
        httpx.Request("POST", EMBEDDINGS, json={"input": [question]})).json()["data"][0]["embedding"]

    # 前提一：向量路必须判定"全都不相关"
    sims = [_cosine(qvec, c.embedding) for c in chunks]
    assert all(s <= cfg.qa_min_similarity for s in sims), \
        f"前提不成立：相似度 {sims} 未全部低于下限 {cfg.qa_min_similarity}"
    # 前提二：关键词路必须真的产出候选（否则那道过滤器还是执行不到，用例又成了摆设）
    terms = [t for t in _query_tokens(question).split() if t]
    kw_hits = [sum(tokenized(c.text).count(t) for t in terms) for c in chunks]
    assert terms and any(n > 0 for n in kw_hits), \
        f"前提不成立：分词 {terms} 在块里一个都没命中（{kw_hits}），关键词路没有候选"

    hits = await MemoryIndex().search(session, vector=qvec, query=question,
                                      document_id=document["id"],
                                      min_similarity=cfg.qa_min_similarity,
                                      limit=cfg.qa_top_k, candidates=cfg.qa_candidates)
    assert hits == [], \
        f"关键词路绕过了相似度下限：{[h['text'] for h in hits]} 成了出处，而它们全都不相关"

    events = await _ask(auth_client, cid, question=question)
    done = dict(events)["done"]
    assert done["degraded"] == "no_hits" and done["verified"] is False
    assert dict(events)["citations"]["citations"] == []


@respx.mock
async def test_keyword_path_still_works_when_embedding_is_down(auth_client, session):
    """但向量化挂掉时不能连带把关键词路也关掉 —— 那时无从测量，只能放行。

    降级本身已经由 degraded=embedding_unavailable 标出来了。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))
    respx.post(CHAT).mock(return_value=_chat_sse("表格在第二页"))

    events = await _ask(auth_client, cid, question="表格")
    done = dict(events)["done"]
    assert done["degraded"] == "embedding_unavailable"
    citations = dict(events)["citations"]["citations"]
    assert citations, "向量化不可用时关键词路必须还能给出出处"
    assert citations[0]["page_idx"] == 1


@respx.mock
async def test_ask_marks_embedding_outage_instead_of_faking_it(auth_client, session):
    """向量化挂掉时只能走关键词路，并且必须打标。

    早先的实现是回落成全零向量——检索照跑、结果照返，用户以为是语义命中，
    实际是一堆噪声还挤掉了关键词命中。这正是铁律 3 要杜绝的静默降级。
    """
    _mock_service(embed=httpx.Response(503, text="embedding down"))
    document = await _upload(auth_client, PDF)
    await _callback(auth_client)
    # 先让索引建好，再让 embedding 挂掉
    doc_row = await session.get(Document, document["id"])
    if doc_row.index_status != "ready":
        respx.post(EMBEDDINGS).mock(side_effect=_embed_response)
        await auth_client.post(f"/api/documents/{document['id']}/reindex")
        respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503, text="embedding down"))
    await session.refresh(doc_row)
    if doc_row.index_status != "ready":
        pytest.skip("索引未就绪，本例只验降级标记")

    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("基于关键词的回答"))
    events = await _ask(auth_client, cid, question="表格")

    assert dict(events)["done"]["degraded"] == "embedding_unavailable", f"events={events}"
    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.degraded == "embedding_unavailable"


@respx.mock
async def test_ask_marks_crop_unsupported_for_non_pdf(auth_client, session):
    """非 PDF 裁不出区域图 -> 不能声称做了视觉验证。"""
    _mock_service()
    document = await _upload(auth_client, b"\x89PNG fake image bytes", "scan.png", "image/png")
    await _callback(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("看起来是一张图"))

    events = await _ask(auth_client, cid, question="第二页的表格数据")
    done = dict(events)["done"]
    assert done["verified"] is False
    assert done["degraded"] == "crop_unsupported", f"done={done}"


@respx.mock
async def test_ask_persists_partial_answer_when_client_disconnects(auth_client, session):
    """用户关页面：已产出的部分回答要落库并标 client_aborted。

    落库跑在生成器的 finally 里，而那时作用域已被取消 —— 不 shield 就根本写不进去，
    用户回来只会看到一条有问无答的会话。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])

    async def slow():
        yield f'data: {json.dumps({"choices": [{"delta": {"content": "开头"}}]})}\n\n'.encode()
        # 客户端读到第一帧就立刻掉头，所以这里只要"还没结束"即可。
        # 别写成几十秒：进程内 ASGI 传输会等这个生成器跑完，那个时长会原样计到
        # 套件总时间上（曾经是 30s，占掉整个 backend 单测的一半以上）
        await asyncio.sleep(1)

    respx.post(CHAT).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=slow()))

    async with auth_client.stream("POST", f"/api/conversations/{cid}/ask",
                                  json={"question": "第二页的表格"}) as resp:
        async for _chunk in resp.aiter_bytes():
            break                   # 读到第一帧就掉头走人
    await asyncio.sleep(0.2)        # 给 shield 住的落库一点时间

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.content == "开头", "已产出的部分必须留住"
    # 标记在这条路径上不保证：httpx 的进程内 ASGI 传输不会把断开变成生成器里的异常，
    # 流是"正常结束"。标记本身由下面那条用 aclose() 直接驱动生成器的用例硬测。
    assert message.degraded in (None, "client_aborted"), message.degraded


@respx.mock
async def test_generator_close_marks_client_aborted_and_persists(auth_client, session):
    """直接驱动生成器验 client_aborted：aclose() 会把 GeneratorExit 投到挂起的 yield 处。

    绕开 ASGI 传输才测得到这个标记——真实 uvicorn 下 Starlette 检测到断开会取消流任务，
    异常是会到的；测不到只是进程内传输的局限。
    """
    from app.qa import Retrieval
    from app.routers.conversations import _stream_answer

    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])

    async def never_ends():
        yield f'data: {json.dumps({"choices": [{"delta": {"content": "半句"}}]})}\n\n'.encode()
        await asyncio.sleep(30)

    respx.post(CHAT).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=never_ends()))

    user_id = (await session.get(Document, document["id"])).uploaded_by
    gen = _stream_answer(auth_client._transport.app.state.http,  # type: ignore[attr-defined]
                         [{"role": "user", "content": "问题"}], Retrieval(),
                         conversation_id=cid, document_id=document["id"], user_id=user_id,
                         has_image=False)
    assert b"event: meta" in await anext(gen)
    await anext(gen)                 # 收到第一帧 delta，此时生成器挂在 yield 上
    await gen.aclose()               # 模拟客户端断开

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.content == "半句", "已产出的文本不能丢"
    assert message.degraded == "client_aborted"
    # 注意：这条**没有**覆盖 finally 里的 asyncio.shield。
    # aclose() 投的是 GeneratorExit，finally 里的 await 照常能跑完，去掉 shield 也一样通过。
    # shield 真正防的是 uvicorn 下的 anyio cancel scope（取消域内每个 await 立即抛），
    # 那条路径进程内测不到——补测它需要真起 uvicorn，留到下一轮。


@respx.mock
async def test_ask_rejects_when_index_not_ready(auth_client, session):
    _mock_service()
    document = await _upload(auth_client, PDF)      # 没走回调 -> 没归档也没索引
    cid = await _conversation(auth_client, document["id"])

    resp = await auth_client.post(f"/api/conversations/{cid}/ask", json={"question": "在吗"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "index_not_ready"


@respx.mock
async def test_index_failure_is_visible_and_blocks_ask(auth_client, session):
    """索引失败不能静默：状态与原因都要能在 UI 上看到，且问答明确拒绝。"""
    _mock_service(embed=httpx.Response(503, text="embedding runtime down"))
    document = await _upload(auth_client, PDF)
    await _callback(auth_client)

    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "failed"
    assert "向量化失败" in detail["index_error"]

    cid = await _conversation(auth_client, document["id"])
    resp = await auth_client.post(f"/api/conversations/{cid}/ask", json={"question": "在吗"})
    assert resp.status_code == 409 and "索引建立失败" in resp.json()["error"]["message"]


@respx.mock
async def test_conversation_isolation_and_history(auth_client, client, session):
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid)

    listed = (await auth_client.get(f"/api/conversations?document={document['id']}")).json()
    assert [c["id"] for c in listed] == [cid]
    history = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert "crop_url" in history[1]["citations"][0]

    from tests.conftest import register
    other = await register(client, username="bob")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert (await client.get(f"/api/conversations/{cid}/messages",
                             headers=headers)).status_code == 404


@respx.mock
async def test_search_across_documents(auth_client, session):
    document = await _ready_document(auth_client)
    resp = await auth_client.get("/api/search?q=表格")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert groups and groups[0]["document_id"] == document["id"]
    assert groups[0]["hits"][0]["page_idx"] == 1, "命中要能定位到页码"


@respx.mock
async def test_reupload_after_delete_restores_askability(auth_client, session):
    """删了再传回来：文档必须重新可问答。

    删除会清空 chunks 并把 index_status 置回 none；复活时如果不重新排队建索引，
    文档看着好好的却永远问不了，而对账只捞 pending，自愈不了。
    """
    document = await _ready_document(auth_client)
    await auth_client.delete(f"/api/documents/{document['id']}")
    assert (await session.execute(select(Chunk))).scalars().all() == []

    again = await auth_client.post("/api/documents",
                                   files={"file": ("sample.pdf", PDF, "application/pdf")})
    assert again.status_code == 202 and again.json()["id"] == document["id"]

    detail = (await auth_client.get(f"/api/documents/{document['id']}")).json()
    assert detail["index_status"] == "ready", detail
    assert (await session.execute(select(Chunk))).scalars().all(), "索引必须重建"


@respx.mock
async def test_deleted_document_is_not_searchable(auth_client, session):
    """删文档要连会话一起清掉。

    顺序很关键：messages 有指向 conversations 的外键，先删会话会被数据库拒掉。
    （真机 e2e 上这里 500 过一次——当时 SQLite 没开 PRAGMA foreign_keys，单测放行了。）
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("有答案"))
    await _ask(auth_client, cid)
    assert (await session.execute(select(Message))).scalars().all(), "先造出消息再删"

    resp = await auth_client.delete(f"/api/documents/{document['id']}")
    assert resp.status_code == 204, resp.text
    assert (await auth_client.get("/api/search?q=表格")).json()["groups"] == []
    assert (await session.execute(select(Message))).scalars().all() == []

    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.deleted_at is not None


@respx.mock
async def test_citations_survive_reindex(auth_client, session):
    """P0 回归：重建索引会重铸全部 chunk_id，历史出处必须还接得回原文。

    `Chunk.id` 是随机 UUID，而 indexing.py 先 DELETE 再 add_all —— 只存 chunk_id 的话，
    一次 reindex 之后"这个回答当时基于哪段原文"就永久还原不回来（citations 里
    只剩 160 字 snippet）。稳定定位键是 `(parse_job_id, seq)`。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid)

    before = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    cited = before[1]["citations"][0]
    assert cited["parse_job_id"] and cited["seq"] is not None, "落库时就要带上定位键"
    old_ids = {c.id for c in (await session.execute(select(Chunk))).scalars().all()}
    assert cited["chunk_id"] in old_ids

    resp = await auth_client.post(f"/api/documents/{document['id']}/reindex")
    assert resp.status_code == 202, resp.text
    session.expire_all()
    new_ids = {c.id for c in (await session.execute(select(Chunk))).scalars().all()}
    assert new_ids and new_ids.isdisjoint(old_ids), "前提不成立：reindex 应当重铸 chunk_id"

    after = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    refreshed = after[1]["citations"][0]
    assert refreshed["resolved"] is True, "重建索引后出处必须还能接回当前 chunk"
    assert refreshed["chunk_id"] in new_ids, "chunk_id 要刷新成当前值，否则前端点不开"
    # 但"当时拿哪块区域作证"是审计事实，不许被后来的重新分块改写 ——
    # crop_url 指向的截图也是按当时的 bbox 存的，跟着改会让高亮框和截图对不上
    assert refreshed["page_idx"] == cited["page_idx"]
    assert refreshed["bbox"] == cited["bbox"]
    assert refreshed["snippet"] == cited["snippet"]


@respx.mock
async def test_unresolvable_citation_is_marked_not_silently_dropped(auth_client, session):
    """块没了 —— 出处必须显式标 resolved=False，且**不给 chunk_id**。

    静默把它当成好的，用户会点开一个空高亮；静默丢掉，回答就成了无出处的断言。
    两种都不行——降级必须可见。

    ⚠️ 阶段 3 读切换之后，这条用例改的是 **evidence 表**而不是
    `messages.citations`（老 JSON 还在写，但不再被读）。改错地方的话用例会
    在"读根本没看那份数据"的情况下绿着通过。
    """
    from ddp_core.models import Evidence

    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid)

    evidence = (await session.execute(select(Evidence))).scalars().first()
    assert evidence is not None, "前提不成立：双写应当已经写下证据"
    # 模拟"这条出处属于另一次解析"：定位键指向一个已经没有块的 parse_job
    await session.execute(
        Chunk.__table__.delete().where(Chunk.parse_job_id == evidence.parse_job_id,
                                       Chunk.seq == evidence.seq))
    await session.commit()

    after = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    citation = after[1]["citations"][0]
    assert citation["resolved"] is False
    assert citation["chunk_id"] is None, "接不回去却给了 chunk_id —— 前端会把高亮指到错块"


@respx.mock
async def test_changed_block_content_invalidates_the_citation(auth_client, session):
    """**负样本**：块还在、seq 也对，但内容被改了 —— 必须标失效。

    这是 plan.md 给阶段 3 定的核心验收：`seq` 是块在文档里的序号，分块规则一变
    （M9 让表格/公式/图片独立成块、标题作前缀），同一份归档重建索引就会切出
    不同数量、不同 seq 的块 —— 老出处的 seq 照样查得到，指的却是另一段原文。
    而 UI 只看 `resolved`：用户会看到一条"可点开"的出处，snippet 是旧文本、
    高亮框指向别处。**这正是本项目定义的最恶劣错误：带着已验证标记的假出处。**

    阶段 2b 之后写的出处有 content_digest，走的是**指纹**比对 ——
    比老的 snippet 包含判据严格得多：块尾被改掉也会被抓到。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid)

    before = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    assert before[1]["citations"][0]["resolved"] is True, "前提不成立：这条出处本该是好的"

    from ddp_core.models import Evidence

    evidence = (await session.execute(select(Evidence))).scalars().first()
    chunk = (await session.execute(select(Chunk).where(
        Chunk.parse_job_id == evidence.parse_job_id,
        Chunk.seq == evidence.seq))).scalars().one()
    # 只在**块尾**追加 —— snippet 是前 160 字，老的包含判据抓不到这种改动，
    # 指纹能。这两条判据的差别正体现在这里
    chunk.text = chunk.text + "（后来被人改掉的一段）"
    await session.commit()

    after = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    citation = after[1]["citations"][0]
    assert citation["resolved"] is False, "块内容变了却仍标成有效 —— 这是假出处"
    assert citation["chunk_id"] is None, "不许刷新指向：刷了就等于把高亮指到错块"


@respx.mock
async def test_answer_records_model_and_retrieval_snapshot(auth_client, session):
    """回答要带模型戳：不记下用了哪个模型/哪套检索参数，换模型后历史无法分组对比。"""
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid)

    from app.config import settings as cfg

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    meta = message.model_meta
    assert meta["embedding_dim"] == cfg.embedding_dim
    assert meta["chat_endpoint"] == cfg.chat_endpoint
    assert meta["retrieval"]["min_similarity"] == cfg.qa_min_similarity
    assert meta["retrieval"]["top_k"] == cfg.qa_top_k
    # 历史接口也要吐出来，否则前端/评测脚本拿不到分组依据
    listed = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    assert listed[1]["model_meta"]["retrieval"]["candidates"] == cfg.qa_candidates


@respx.mock
async def test_confidence_is_reported_and_separates_strong_from_marginal(auth_client):
    """A2：出处要带"有多相关"，而且勉强及格的必须和绝佳命中区分开。

    不能用 citation 里的 score —— 那是 RRF 名次分，两路都排第一就恒为 0.0328，
    两种情况长得一模一样。要看的是有校准量纲的余弦相似度。
    """
    from app.config import settings as cfg

    document = await _ready_document(auth_client)
    # side_effect 而不是 return_value：一个 httpx 流式响应只能被消费一次，
    # 这个用例要问两轮
    respx.post(CHAT).mock(side_effect=lambda _request: _chat_sse("答案"))

    strong = await _conversation(auth_client, document["id"])
    done = dict(await _ask(auth_client, strong, question="表格数据"))["done"]
    assert done["confidence"]["level"] == "high"
    assert done["confidence"]["top_similarity"] >= cfg.qa_low_similarity

    marginal = await _conversation(auth_client, document["id"])
    done = dict(await _ask(auth_client, marginal, question="表格"))["done"]
    assert done["confidence"]["level"] == "low", done["confidence"]
    top = done["confidence"]["top_similarity"]
    assert cfg.qa_min_similarity < top < cfg.qa_low_similarity, top


@respx.mock
async def test_confidence_is_unknown_not_high_when_similarity_cannot_be_measured(auth_client):
    """向量化挂了 -> 只有关键词路 -> 相似度无从测量。

    这时必须说"不知道"，不能默认成 high —— 那就是又一次静默降级：
    用户会以为这条出处经过了语义确认。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(EMBEDDINGS).mock(return_value=httpx.Response(503))
    respx.post(CHAT).mock(return_value=_chat_sse("关键词回答"))

    done = dict(await _ask(auth_client, cid, question="表格"))["done"]
    assert done["degraded"] == "embedding_unavailable"
    assert done["confidence"]["level"] == "unknown"
    assert done["confidence"]["top_similarity"] is None


@respx.mock
async def test_history_carries_similarity_and_confidence(auth_client):
    """历史消息也要带可信度，否则翻回去看旧回答时这个信息就丢了。"""
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_chat_sse("答案"))
    await _ask(auth_client, cid, question="表格数据")

    listed = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()
    answer = listed[1]
    assert answer["confidence"]["level"] == "high"
    assert answer["citations"][0]["similarity"] > 0.5
    # RRF 分同时保留（排序用），但它不是"相关度"
    assert answer["citations"][0]["score"] < 0.05


def _verify_aware_chat(transcript: str | None, answer: str = "回答"):
    """按请求类型分流：抄写请求返回 transcript，回答请求返回 SSE 流。

    transcript=None 表示视觉模型对抄写请求也失败（核对做不了）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parts = [part for message in body["messages"]
                 if isinstance(message["content"], list) for part in message["content"]]
        if any("原样" in (part.get("text") or "") for part in parts):
            if transcript is None:
                return httpx.Response(503, text="vqa down")
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": transcript}}]})
        return _chat_sse(answer)

    return handler


@respx.mock
async def test_parse_mismatch_when_image_text_contradicts_chunk(auth_client, session):
    """A4：图上的字与 chunk 文本严重不符 -> 标 parse_mismatch，且不许再说"已验证"。

    这是七种降级里唯一没被覆盖的洞。解析错了的时候 chunk 文本是错的，
    但语义相似度照样过阈值、照样裁图、照样标 verified —— 产出这个类别最恶劣的
    错误：**带着"已做视觉验证"标记的假出处**。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(side_effect=_verify_aware_chat("完全无关的另一段文字与库存报表"))

    done = dict(await _ask(auth_client, cid))["done"]
    assert done["degraded"] == "parse_mismatch", done
    assert done["verified"] is False, "对不上就不能再声称做过视觉验证"

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    assert message.degraded == "parse_mismatch" and message.verified is False


@respx.mock
async def test_no_mismatch_when_image_text_matches_chunk(auth_client):
    """抄写结果与 chunk 文本一致时不许打标 —— 误报比不报更伤信任。"""
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(side_effect=_verify_aware_chat("第二页的表格数据"))

    done = dict(await _ask(auth_client, cid))["done"]
    assert done["degraded"] is None, done
    assert done["verified"] is True


@respx.mock
async def test_unverifiable_parse_is_not_reported_as_mismatch(auth_client):
    """核对本身做不了（视觉模型抄写失败）时判"没测出来"，不是"对不上"。

    把"不知道"说成"有问题"是另一种撒谎，而且会让用户不再相信这个标记。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(side_effect=_verify_aware_chat(None))

    done = dict(await _ask(auth_client, cid))["done"]
    assert done["degraded"] is None and done["verified"] is True


@respx.mock
async def test_refusal_style_short_transcript_does_not_trigger_mismatch(auth_client):
    """模型答"我看不清这张图"这种短回复不能被当成分歧证据。"""
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(side_effect=_verify_aware_chat("看不清"))

    done = dict(await _ask(auth_client, cid))["done"]
    assert done["degraded"] is None, done


@respx.mock
async def test_parse_verification_can_be_switched_off(auth_client, monkeypatch):
    """核对多打一次视觉模型。不想付这个成本的部署要能关掉，且关掉后不打标。"""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "qa_verify_parse", False)
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    route = respx.post(CHAT).mock(side_effect=_verify_aware_chat("完全无关的另一段文字"))

    done = dict(await _ask(auth_client, cid))["done"]
    assert done["degraded"] is None and done["verified"] is True
    assert route.call_count == 1, "关掉之后不该再有抄写请求"
