"""历史出处回填（阶段 3）。

plan.md 把阶段 3 标成**全重构最容易出假出处的一步**，验收要求三条：

1. 老出处要么正确接回，要么显式标失效
2. **绝不出现带已验证标记的假出处** —— 用负样本测
3. 回填计数对账：一条不丢

这个文件逐条对应。**关键在于分清"验证过"和"没验证过"**：
给每条老记录都补一个"当前块的指纹"会让对账数字很好看，
而那等于替历史宣布"它一直指着这里" —— 凭空造证。
"""
import pytest
from sqlalchemy import select

from app.backfill import backfill
from app.models import Chunk, Conversation, Document, ExtractionItem, Message, ParseJob, User
from ddp_core.anchor import digest_of
from ddp_core.models import Citation, Evidence
from ddp_core.tokenize import tokenized

TEXTS = ["第一段：设备额定电压为 380V，允许偏差正负百分之十。",
         "第二段：过载保护阈值设定为额定电流的 1.25 倍。",
         "第三段：接地电阻不得大于 4 欧姆。"]


async def _seed(session) -> tuple[Document, ParseJob, Conversation]:
    user = User(username="bf", password_hash="x")
    session.add(user)
    await session.flush()
    document = Document(uploaded_by=user.id, doc_id="b" * 64, filename="manual.pdf",
                        mime="application/pdf", object_key="", index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    for seq, text in enumerate(TEXTS):
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=seq, bbox=[72, 100, 500, 130], page_size=[595, 842],
                          text=text, char_len=len(text), block_type="text",
                          text_tokenized=tokenized(text)))
    conversation = Conversation(document_id=document.id, user_id=user.id, title="c")
    session.add(conversation)
    await session.flush()
    await session.commit()
    return document, job, conversation


def _old(job_id: str, seq: int, snippet: str | None, **over) -> dict:
    """阶段 2b 之前那种出处：**没有 content_digest，只有截断过的 snippet**。"""
    base = {"chunk_id": "旧的悬空值", "parse_job_id": job_id, "seq": seq,
            "page_idx": seq, "bbox": [72, 100, 500, 130], "crop_key": None,
            "score": 0.03, "similarity": 0.7}
    if snippet is not None:
        base["snippet"] = snippet
    return base | over


async def _run(session):
    return await session.run_sync(lambda sync: backfill(sync.connection()))


async def test_matching_snippet_becomes_an_anchored_evidence(session):
    """块还在、snippet 对得上 -> 写指纹，从此走**严格**判据。"""
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 0, TEXTS[0][:20])]))
    await session.commit()

    report = await _run(session)
    assert (report.total, report.anchored, report.unanchored) == (1, 1, 0), str(report)

    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.content_digest == digest_of(TEXTS[0])
    assert evidence.page_size == [595, 842], "page_size 要从块上补回来（老 JSON 里没有）"
    assert evidence.provider == {"backfilled": True}, "回填来的行必须打标，否则 downgrade 会误删双写的行"


async def test_mismatching_snippet_is_kept_but_never_anchored(session):
    """**负样本**：snippet 对不上 -> 迁过来但**不写指纹**，读出来必然失效。

    这是本阶段最要命的一条。给它补一个"当前块的指纹"对账会很好看，
    但那等于宣布"这条出处一直指着这里" —— 而它明明指不上。
    """
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 0, "这段文字在任何一个块里都不存在")]))
    await session.commit()

    report = await _run(session)
    assert (report.total, report.anchored, report.unanchored) == (1, 0, 1), str(report)

    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.content_digest == "", "对不上的出处被写了指纹 —— 等于凭空造证"

    # 而且读出来必须是失效的
    from app.evidence import load_citations

    message = (await session.execute(select(Message))).scalars().one()
    out = await load_citations(session, source_kind="message", source_ids=[message.id])
    assert out[message.id][0]["resolved"] is False
    assert out[message.id][0]["chunk_id"] is None


async def test_missing_snippet_stays_unanchored_and_is_not_wrongly_invalidated(session):
    """0003 之前的老记录连 snippet 都没有 —— **无从判断就不冤枉它**。

    这是既有行为（`same_content` 的 snippet 为空返回 True），切到新表时必须
    原样保留：改成"标失效"的话，一批其实没问题的老回答会突然集体显示出处已失效。
    """
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 1, None)]))
    await session.commit()

    report = await _run(session)
    assert (report.anchored, report.unanchored) == (0, 1), str(report)
    assert (await session.execute(select(Evidence))).scalars().one().content_digest == ""

    from app.evidence import load_citations

    message = (await session.execute(select(Message))).scalars().one()
    out = await load_citations(session, source_kind="message", source_ids=[message.id])
    assert out[message.id][0]["resolved"] is True, "无从判断的老记录被冤枉成失效了"


async def test_vanished_chunk_is_kept_and_marked(session):
    """块没了：证据照建（审计事实要留住），但不写指纹，读出来失效。"""
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 99, "指向一个不存在的块")]))
    await session.commit()

    report = await _run(session)
    assert (report.total, report.unanchored, report.skipped_no_job) == (1, 1, 0), str(report)
    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.content_digest == ""
    assert evidence.page_idx == 99 or evidence.bbox == [72, 100, 500, 130], \
        "块没了就得回放老 JSON 记下的区域 —— 那是审计事实"


async def test_citation_without_locator_is_counted_not_lost(session):
    """连定位键都没有 -> 建不出证据行，但**必须计数**。

    阶段 4 删老列时这些出处会随之消失，所以现在就得知道有多少条。
    静默丢掉的话，那一步会变成"不知道弄丢了什么"。
    """
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 0, TEXTS[0][:20]),
                                   _old(job.id, 0, "x") | {"parse_job_id": None},
                                   _old(job.id, 0, "y") | {"seq": None}]))
    await session.commit()

    report = await _run(session)
    assert report.total == 3
    assert report.skipped_no_locator == 2, str(report)
    assert report.anchored == 1
    report.check()          # 恒等式：三个去处加起来 == 老记录总数


async def test_dead_parse_job_is_counted_not_crashed(session):
    """那次解析已经不存在 -> 建不出证据（外键），计数并继续，不许炸整批。"""
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old("已经没有的解析", 0, "x"),
                                   _old(job.id, 0, TEXTS[0][:20])]))
    await session.commit()

    report = await _run(session)
    assert (report.total, report.skipped_no_job, report.anchored) == (2, 1, 1), str(report)
    report.check()


async def test_backfill_is_idempotent(session):
    """跑第二遍不许翻倍 —— 迁移会被重跑（灾备重建、downgrade 后再 upgrade）。"""
    document, job, conversation = await _seed(session)
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 0, TEXTS[0][:20]), _old(job.id, 1, TEXTS[1][:20])]))
    await session.commit()

    first = await _run(session)
    second = await _run(session)
    assert first.anchored == 2 and second.already_present == 2, f"{first} / {second}"
    assert len((await session.execute(select(Evidence))).scalars().all()) == 2
    assert len((await session.execute(select(Citation))).scalars().all()) == 2
    second.check()


async def test_rank_preserves_the_old_list_order(session):
    """老 JSON 是**有序列表**，顺序就是检索名次 —— 回填必须把它记进 rank。

    靠 score 排替代不了：RRF 分只由名次决定，两路都排第一的块分数完全相同。
    """
    document, job, conversation = await _seed(session)
    # 三条出处**分数完全一样**，只有列表顺序区分得开名次
    session.add(Message(conversation_id=conversation.id, role="assistant", content="答",
                        citations=[_old(job.id, 2, TEXTS[2][:20], score=0.0328),
                                   _old(job.id, 0, TEXTS[0][:20], score=0.0328),
                                   _old(job.id, 1, TEXTS[1][:20], score=0.0328)]))
    await session.commit()
    await _run(session)

    from app.evidence import load_citations

    message = (await session.execute(select(Message))).scalars().one()
    out = await load_citations(session, source_kind="message", source_ids=[message.id])
    assert [c["seq"] for c in out[message.id]] == [2, 0, 1], "名次顺序在回填中丢了"


async def test_extraction_fields_are_backfilled_per_field(session):
    """抽取的出处是字段级的 —— 回填不能把它压回 item 级。"""
    from app.models import ExtractionRun

    document, job, conversation = await _seed(session)
    user_id = document.uploaded_by
    run = ExtractionRun(user_id=user_id, name="r", schema_json={}, kind="object")
    session.add(run)
    await session.flush()
    item = ExtractionItem(run_id=run.id, document_id=document.id, parse_job_id=job.id,
                          record_index=0, status="ok", fields={
                              "voltage": {"status": "found", "value": "380V",
                                          "citations": [_old(job.id, 0, TEXTS[0][:20])]},
                              "ground": {"status": "found", "value": "4Ω",
                                         "citations": [_old(job.id, 2, TEXTS[2][:20])]}})
    session.add(item)
    await session.commit()

    report = await _run(session)
    assert (report.total, report.anchored, report.sources) == (2, 2, 2), str(report)

    rows = (await session.execute(select(Citation))).scalars().all()
    assert sorted(c.source_id for c in rows) == sorted(
        [f"{item.id}:voltage", f"{item.id}:ground"])


async def test_backfill_never_overwrites_a_digest_written_by_the_dual_write(session):
    """**回填不许覆盖双写记下的指纹。**

    双写的指纹是**引用发生的那一刻**记下的当前块内容；回填算的是**今天**的内容。
    覆盖掉就意味着：块在这期间被改过的话，这条出处会被重新锚定到新内容上，
    从此报告 resolved=True —— 一条明明已经指不上原文的出处，
    被回填**制造**成了"已验证"。这是这一阶段最隐蔽的假出处来源。
    """
    from app.evidence import load_citations, record_evidence

    document, job, conversation = await _seed(session)
    citation = _old(job.id, 0, TEXTS[0][:20])
    message = Message(conversation_id=conversation.id, role="assistant", content="答",
                      citations=[citation])
    session.add(message)
    await session.flush()
    # 双写：此刻块的内容是 TEXTS[0]，指纹按它记
    await record_evidence(session, [citation], source_kind="message", source_id=message.id)
    await session.commit()
    original = (await session.execute(select(Evidence))).scalars().one().content_digest
    assert original == digest_of(TEXTS[0])

    # 块内容后来被改了（重建索引、换引擎重解析都会）
    chunk = (await session.execute(select(Chunk).where(Chunk.seq == 0))).scalars().one()
    chunk.text = TEXTS[0] + "（后来追加的一段）"
    await session.commit()

    await _run(session)

    evidence = (await session.execute(select(Evidence))).scalars().one()
    assert evidence.content_digest == original, \
        "回填把双写记下的指纹覆盖成了今天的内容 —— 失效的出处被重新锚定成有效"
    out = await load_citations(session, source_kind="message", source_ids=[message.id])
    assert out[message.id][0]["resolved"] is False, "块内容已经变了，这条出处必须是失效的"


async def test_report_counts_are_not_decorative(session):
    """对账恒等式必须真的会抛 —— 它是"一条不丢"唯一的机械保障。"""
    from app.backfill import BackfillReport

    bad = BackfillReport(total=5, anchored=2, unanchored=1)
    with pytest.raises(RuntimeError, match="回填计数对不上"):
        bad.check()

    ok = BackfillReport(total=5, anchored=2, unanchored=1, skipped_no_locator=1,
                        skipped_no_job=1)
    ok.check()
