"""evidence / citation 双写（阶段 2b）。

**验收标准只有一条：老 JSON 里的定位元组与新表行逐条相同。**
所以这里的用例几乎都是"对拍"形状 —— 拿老路径写下的那份 JSON，
去比新表查出来的东西，不允许任何一边多、任何一边少。

读仍然走老路，因此这一步的风险不在"读错"，而在**新表悄悄少了一批**：
阶段 3 一切读，那些回答就变成"没有出处"。这正是要盯的东西。
"""
import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.evidence import record_evidence
from app.models import Chunk, Document, Message, ParseJob
from ddp_core.models import Citation, Evidence, digest_of
from ddp_core.tokenize import tokenized

from tests.conftest import CHAT, EMBEDDINGS
from tests.test_qa import _ask, _conversation, _ready_document


async def _a_user(session) -> str:
    from app.models import User
    user = User(username="ev", password_hash="x")
    session.add(user)
    await session.flush()
    return user.id


async def _seed(session, *, texts: list[str], page_size=None) -> tuple[Document, ParseJob]:
    document = Document(uploaded_by=await _a_user(session), doc_id="e" * 64,
                        filename="m.pdf", mime="application/pdf", object_key="",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    for seq, text in enumerate(texts):
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=seq, bbox=[72, 100, 500, 130],
                          page_size=page_size if page_size is not None else [612, 792],
                          text=text, char_len=len(text), block_type="table" if seq else "text",
                          text_tokenized=tokenized(text)))
    await session.commit()
    return document, job


def _citation(job_id: str, seq: int, **over) -> dict:
    """问答侧的出处形状 —— **注意它没有 page_size**（抽取侧才有）。"""
    base = {"chunk_id": "ignored", "parse_job_id": job_id, "seq": seq,
            "page_idx": seq, "bbox": [72, 100, 500, 130], "crop_key": None,
            "snippet": "片段", "score": 0.03, "similarity": 0.72}
    return base | over


# ------------------------------------------------------------------ 双写本身

async def test_evidence_takes_page_size_from_the_chunk_not_the_citation(session):
    """`page_size` 必须来自 chunks 行。

    **问答侧的 citation dict 里根本没有这个字段**（只有抽取侧有）。
    照着 dict 建证据会让一半的证据静默存成 page_size=NULL，
    而缺它遇到 CropBox 偏移/旋转页就会裁错区域 —— 出处图对不上原文，
    是这个项目定义的最恶劣的一类错。
    """
    document, job = await _seed(session, texts=["第一段正文"], page_size=[595, 842])
    citation = _citation(job.id, 0)
    assert "page_size" not in citation, "前提变了：问答侧的 citation 现在带 page_size 了"

    assert await record_evidence(session, [citation],
                                 source_kind="message", source_id="m1") == 1
    await session.commit()

    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.page_size == [595, 842], "page_size 没从 chunk 取，裁剪会用错基准"


async def test_content_digest_is_the_whole_chunk_not_the_snippet(session):
    """指纹算的是**整块文本**，不是截断过的 snippet。

    阶段 3 靠它判断"这个块还是不是当初那段话"。拿 snippet 算的话，
    块尾被改掉不会被发现 —— 而"内容变了却判成没变"正好产出假出处。
    """
    text = "这是一段很长的正文。" * 30
    document, job = await _seed(session, texts=[text])
    await record_evidence(session, [_citation(job.id, 0, snippet=text[:160] + "…")],
                          source_kind="message", source_id="m1")
    await session.commit()

    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.content_digest == digest_of(text)
    assert evidence.content_digest != digest_of(text[:160]), "指纹算的是 snippet，块尾改动会漏掉"


async def test_same_block_cited_twice_is_one_evidence_two_citations(session):
    """同一个块被两条 message 引 —— 证据只有一行，引用有两行。

    证据是"那个区域的稳定身份"，不是"某次引用"。每次引用都新建一行的话，
    复核状态会散落在 N 行上，反查"这条证据被谁引过"也就无从谈起。
    """
    document, job = await _seed(session, texts=["共享的那一段"])
    await record_evidence(session, [_citation(job.id, 0)], source_kind="message", source_id="m1")
    await record_evidence(session, [_citation(job.id, 0)], source_kind="message", source_id="m2")
    await session.commit()

    assert len((await session.execute(select(Evidence))).scalars().all()) == 1
    rows = (await session.execute(select(Citation))).scalars().all()
    assert sorted(c.source_id for c in rows) == ["m1", "m2"]


async def test_crop_key_is_filled_in_later_but_never_erased(session):
    """先引时没裁到图、后来裁到了要补上；反过来不许把已有的抹掉。"""
    document, job = await _seed(session, texts=["一段"])
    await record_evidence(session, [_citation(job.id, 0, crop_key=None)],
                          source_kind="message", source_id="m1")
    await record_evidence(session, [_citation(job.id, 0, crop_key="crops/a.png")],
                          source_kind="message", source_id="m2")
    await session.commit()
    assert (await session.execute(select(Evidence))).scalars().one().crop_key == "crops/a.png"

    await record_evidence(session, [_citation(job.id, 0, crop_key=None)],
                          source_kind="message", source_id="m3")
    await session.commit()
    assert (await session.execute(select(Evidence))).scalars().one().crop_key == "crops/a.png", \
        "已有的裁图被这次没裁图的引用抹掉了"


async def test_citations_without_a_locator_are_skipped(session):
    """缺 `(parse_job_id, seq)` 的出处不建证据 —— 老路径对它同样是"接不回去"。

    造一行"证据"出来反而更糟：它指不到任何块，却在新表里长得像一条真证据。
    """
    document, job = await _seed(session, texts=["一段"])
    from app import evidence as mod

    bad = [_citation(job.id, 0) | {"parse_job_id": None},
           _citation(job.id, 0) | {"seq": None}]
    before = mod.DUAL_WRITE_FAILURES.labels(source_kind="message")._value.get()

    assert await record_evidence(session, bad, source_kind="message", source_id="m1") == 0
    await session.commit()
    assert (await session.execute(select(Evidence))).scalars().all() == []
    # **而且不许记成"双写失败"**：这是一条本来就定位不到的出处，不是新表出了问题。
    # 混进那个计数里，阶段 3 读切换前的"计数必须为 0"这道闸就永远过不了，
    # 于是要么卡死，要么被人调松 —— 后者更糟
    assert mod.DUAL_WRITE_FAILURES.labels(source_kind="message")._value.get() == before, \
        "定位不到的出处被记成了双写失败"


async def test_citation_pointing_at_a_vanished_chunk_is_skipped(session):
    """块已经不在了（重建索引换了分块规则）—— 跳过，不猜。"""
    document, job = await _seed(session, texts=["一段"])
    assert await record_evidence(session, [_citation(job.id, 99)],
                                 source_kind="message", source_id="m1") == 0
    await session.commit()
    assert (await session.execute(select(Evidence))).scalars().all() == []


async def test_evidence_write_failure_does_not_lose_the_answer(session, monkeypatch):
    """**出处写挂了，回答本身不许跟着丢。**

    只是 try/except 吞掉是不够的：session 已经被 IntegrityError 之类污染，
    接下来老路径那次 commit 照样会炸。savepoint 才能真的把影响圈住。
    """
    from app import evidence as mod

    from app.models import Conversation

    document, job = await _seed(session, texts=["一段"])
    # 单测的 SQLite 是**开着外键约束**的，随手编一个 conversation_id 会撞 FK ——
    # 那样测到的就不是 savepoint 而是我自己造的另一个错
    conversation = Conversation(document_id=document.id, user_id=document.uploaded_by, title="c")
    session.add(conversation)
    await session.flush()
    message = Message(conversation_id=conversation.id, role="assistant", content="答案")
    session.add(message)

    async def boom(sess, *a, **kw):
        """**必须是一个真的把 session 弄脏的错**，不能只是 `raise RuntimeError`。

        裸抛异常的话，session 从头到尾干净，有没有 savepoint 都一样能提交 ——
        实测：把 `begin_nested()` 换成裸 `try/except`，9 例照样全绿。
        真实的双写失败长的是这个样子：flush 撞约束 -> session 进入
        "必须先 rollback" 的状态 -> 老路径那次 commit 跟着炸。
        """
        sess.add(Citation(evidence_id="不存在的证据", source_kind="message",
                          source_id="x", role="primary"))
        await sess.flush()

    monkeypatch.setattr(mod, "_record", boom)
    before = mod.DUAL_WRITE_FAILURES.labels(source_kind="message")._value.get()

    assert await mod.record_evidence(session, [_citation(job.id, 0)],
                                     source_kind="message", source_id=message.id) == 0
    # 回答必须还能提交 —— 出处没了很糟，但连回答一起丢更糟
    await session.commit()

    assert (await session.get(Message, message.id)) is not None
    assert (await session.execute(select(Evidence))).scalars().all() == []
    assert mod.DUAL_WRITE_FAILURES.labels(source_kind="message")._value.get() == before + 1, \
        "双写失败没有计数 —— 新表少了一批会变成查不出原因的悬案"


# ------------------------------------------------------------------ 端到端对拍

@respx.mock
async def test_qa_citations_come_back_from_the_new_tables(auth_client, session):
    """问答走完一遍：接口返回的出处与 evidence/citations 两张表逐条相同。

    阶段 4 删掉 `messages.citations` 之后，**新表是唯一真相** ——
    这条用例因此从"对拍老 JSON"变成了"对拍接口返回"：
    表里有什么，用户就该看到什么，不多不少。
    """
    document = await _ready_document(auth_client)
    cid = await _conversation(auth_client, document["id"])
    respx.post(CHAT).mock(return_value=_sse_answer())

    await _ask(auth_client, cid)

    message = (await session.execute(
        select(Message).where(Message.role == "assistant"))).scalars().one()
    rows = sorted((await session.execute(
        select(Evidence.parse_job_id, Evidence.seq)
        .join(Citation, Citation.evidence_id == Evidence.id)
        .where(Citation.source_kind == "message", Citation.source_id == message.id)
    )).all())
    assert rows, "前提不成立：这一问应当给出出处"

    shown = (await auth_client.get(f"/api/conversations/{cid}/messages")).json()[1]["citations"]
    assert sorted((c["parse_job_id"], c["seq"]) for c in shown) == rows, \
        f"接口返回的出处与表里对不上：接口 {shown}，表 {rows}"


@respx.mock
async def test_extraction_citations_are_stored_per_field(auth_client, session):
    """抽取的出处是**字段级**的，存储不能把它压回 item 级。

    "这个字段的值是从哪一块抽出来的"正是本产品相对"字段 + 置信度"那类
    抽取产品的差异点。source_id 丢了字段名，这个差异点就没了。
    """
    import asyncio

    from app.models import ExtractionItem, ExtractionRun
    from app.routers.extractions import _BACKGROUND_TASKS

    document = await _ready_document(auth_client)
    respx.post(CHAT).mock(return_value=Response(200, json={"choices": [
        {"message": {"content": json.dumps(
            {"found": True, "value": "第二页的表格数据", "source": 1},
            ensure_ascii=False)}}]}))

    resp = await auth_client.post("/api/extractions/runs", json={
        "document_ids": [document["id"]], "name": "t",
        # 描述要与第 2 页那一块字面高度重合：夹具用的是字符袋假向量，
        # 描述写"主题"的话余弦远低于 qa_min_similarity -> 零命中 -> not_found，
        # 于是一条字段级出处都产不出来，这条用例就什么都验不到
        "schema_json": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "第二页的表格数据"}}}})
    assert resp.status_code == 202, resp.text

    # run 是后台 asyncio task（202 就返回了）。等它跑完再看结果 ——
    # 不等的话既拿不到 item，还会在 fixture 拆到一半时撞上仍在用 session 的后台任务
    if _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS))

    run = (await session.execute(select(ExtractionRun))).scalars().one()
    items = (await session.execute(select(ExtractionItem))).scalars().all()
    assert items, f"前提不成立：抽取没有产出 item（run.status={run.status} error={run.error}）"

    # **落库的 fields JSON 里不许再有 citations**（阶段 4）：
    # 留一份就有两个真相，而那一份重建索引之后永远不会更新
    for item in items:
        for name, cell in (item.fields or {}).items():
            assert "citations" not in cell, f"字段 {name} 的出处又被写回 JSON 了"

    checked = 0
    for item in items:
        for name in (item.fields or {}):
            rows = sorted((await session.execute(
                select(Evidence.parse_job_id, Evidence.seq)
                .join(Citation, Citation.evidence_id == Evidence.id)
                .where(Citation.source_kind == "extract_field",
                       Citation.source_id == f"{item.id}:{name}")
            )).all())
            checked += len(rows)
    assert checked, "前提不成立：一条字段级出处都没有，这条用例什么都没验到"

    # 接口仍然按字段给出处 —— 存储换了，对外形状不变
    detail = (await auth_client.get(f"/api/extractions/runs/{run.id}")).json()
    shown = [c for i in detail["items"] for f in i["fields"].values()
             for c in f["citations"]]
    assert len(shown) == checked, f"接口给了 {len(shown)} 条出处，表里有 {checked} 条"


def _sse_answer():
    from tests.test_qa import _chat_sse
    return _chat_sse("第二页", "讲的是表格数据。")


async def test_two_citations_of_one_block_across_a_reindex_are_judged_separately(session):
    """同一个块被跨重建引两次 —— **后一次不许被前一次的指纹判成失效**。

    这是把指纹从 evidence 挪到 citation 的原因（阶段 3 发现的 2b 设计缺陷）：

        一月：引 seq=0，块内容是 A -> 证据建立，锚定 A
        （重建索引，分块规则变了，seq=0 的内容变成 B）
        三月：又引 seq=0，这次看到的是 B -> **共用那一行证据**

    指纹只挂在证据上的话，两次引用共享"锚定 A"这一个事实，
    于是三月那次刚问完就显示"出处已失效" —— 而它明明指得好好的。
    反过来一月那次必须失效：它当时作证的那段内容已经不在了。
    """
    from app.evidence import load_citations

    document, job = await _seed(session, texts=["一月的内容"])
    await record_evidence(session, [_citation(job.id, 0, snippet="一月的内容")],
                          source_kind="message", source_id="m-jan")
    await session.commit()

    # 重建索引：同一个 (job, seq) 上的内容换了
    chunk = (await session.execute(select(Chunk).where(Chunk.seq == 0))).scalars().one()
    chunk.text = "三月的内容"
    await session.commit()

    await record_evidence(session, [_citation(job.id, 0, snippet="三月的内容")],
                          source_kind="message", source_id="m-mar")
    await session.commit()

    assert len((await session.execute(select(Evidence))).scalars().all()) == 1, \
        "前提不成立：同一个块应当只有一行证据"

    out = await load_citations(session, source_kind="message",
                              source_ids=["m-jan", "m-mar"])
    assert out["m-mar"][0]["resolved"] is True, \
        "三月这次看到的就是当前内容，却被一月那次的指纹判成了失效"
    assert out["m-jan"][0]["resolved"] is False, \
        "一月作证的那段内容已经不在了，必须失效"
