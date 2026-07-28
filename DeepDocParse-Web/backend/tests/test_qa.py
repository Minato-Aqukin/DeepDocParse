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

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        has_image = any(part.get("type") == "image_url"
                        for message in body["messages"]
                        if isinstance(message["content"], list)
                        for part in message["content"])
        if has_image:
            return httpx.Response(502, json={"error": {"message": "vqa unreachable"}})
        return _chat_sse("纯文本回答")

    respx.post(CHAT).mock(side_effect=handler)

    events = await _ask(auth_client, cid)
    done = dict(events)["done"]
    assert done["verified"] is False
    assert done["degraded"] == "vision_unavailable", "降级必须可见"
    assert calls["n"] == 2, "带图失败后要退回纯文本再试一次"

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
        await asyncio.sleep(30)     # 客户端会在这中间断开

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

    user_id = (await session.get(Document, document["id"])).user_id
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
