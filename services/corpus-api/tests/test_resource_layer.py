"""逻辑资源层 —— 计划 v3 的不变量 I01 / T01 / T02 / T03 / T04。

## 这组用例守的是什么

`docs/refactor/BASELINE-v3.md` 的 C-01 记着：计划要求「同内容不同用户 →
两条独立资源」，而迁移 0006 刻意把去重做成了全局的。选定方案 B ——
**在内容层之上加一层逻辑资源，不回退 0006**：

    Resource（每人一条）
      └─ ResourceVersion（固定版本）
           └─ document_id ──→ Document（全局去重，只解析一次）

所以这里要同时钉住**两件相反方向**的事：
  ① 资产是独立的（删一个不影响另一个）—— 那是 I01 要的
  ② 内容仍然只有一份（不重复解析/索引/embedding）—— 那是 0006 的收益

只验其中一件都会让另一件悄悄退化，所以每个用例都同时断言两侧。
"""
import pytest
from sqlalchemy import select

from ddp_corpus.models import Document, Resource, ResourceVersion, UploadEvent, new_id


async def _make_document(session, *, doc_id: str, uploader: str,
                         filename: str = "handbook.pdf") -> Document:
    doc = Document(id=new_id(), uploaded_by=uploader, organization_id="org1",
                   doc_id=doc_id, origin="web", filename=filename,
                   mime="application/pdf", size_bytes=1024,
                   object_key=f"corpus/{filename}", page_count=5)
    session.add(doc)
    await session.flush()
    return doc


async def _claim(session, doc: Document, owner: str, *, version_no: int = 1) -> Resource:
    """某人把一份内容认领成自己的资产。"""
    res = Resource(id=new_id(), organization_id=doc.organization_id, owner_id=owner,
                   uploaded_by=owner, display_name=doc.filename)
    session.add(res)
    await session.flush()
    session.add(ResourceVersion(id=new_id(), resource_id=res.id, version_no=version_no,
                                document_id=doc.id, source_digest=doc.doc_id,
                                filename=doc.filename, size_bytes=doc.size_bytes))
    await session.flush()
    return res


async def test_same_bytes_two_users_get_independent_resources(session):
    """T01：两人上传相同字节 → 两条独立资产，**但只有一份内容**。"""
    doc = await _make_document(session, doc_id="ab" * 32, uploader="userA")
    ra = await _claim(session, doc, "userA")
    rb = await _claim(session, doc, "userB")

    assert ra.id != rb.id, "两个人的资产必须是两条记录"
    assert ra.owner_id == "userA" and rb.owner_id == "userB"

    # **内容只有一份** —— 0006 的收益不能因为加了资源层就丢掉
    docs = (await session.execute(select(Document))).scalars().all()
    assert len(docs) == 1, "同字节不该产生第二条 Document（重复解析 = 重复 GPU 账单）"

    # 两条版本指向同一份内容
    versions = (await session.execute(select(ResourceVersion))).scalars().all()
    assert len(versions) == 2
    assert {v.document_id for v in versions} == {doc.id}


async def test_deleting_one_resource_leaves_the_other_and_the_content(session):
    """T03：删一个人的资产，不影响另一个人的，也不删共享内容。"""
    doc = await _make_document(session, doc_id="cd" * 32, uploader="userA")
    ra = await _claim(session, doc, "userA")
    rb = await _claim(session, doc, "userB")

    await session.delete(ra)
    await session.flush()

    remaining = (await session.execute(select(Resource))).scalars().all()
    assert [r.id for r in remaining] == [rb.id], "只该删掉 userA 那一条"

    # 级联只带走 userA 自己的版本
    versions = (await session.execute(select(ResourceVersion))).scalars().all()
    assert len(versions) == 1 and versions[0].resource_id == rb.id

    # **内容一个字节都不能动** —— 还有人引用着它
    assert (await session.get(Document, doc.id)) is not None
    assert (await session.get(Document, doc.id)).deleted_at is None


async def test_content_stays_until_the_last_reference_goes(session):
    """GC 的判据是"还有没有活的资源版本引用它"，不是"文档被删了吗"。"""
    doc = await _make_document(session, doc_id="ef" * 32, uploader="userA")
    ra = await _claim(session, doc, "userA")
    rb = await _claim(session, doc, "userB")

    async def live_refs() -> int:
        rows = (await session.execute(
            select(ResourceVersion).where(ResourceVersion.document_id == doc.id,
                                          ResourceVersion.deleted_at.is_(None)))).scalars().all()
        return len(rows)

    assert await live_refs() == 2
    await session.delete(ra)
    await session.flush()
    assert await live_refs() == 1, "还剩一个人要它，内容不能回收"
    await session.delete(rb)
    await session.flush()
    assert await live_refs() == 0, "最后一个引用没了，这时才轮到 GC"


async def test_a_fixed_version_is_a_new_row_not_an_edit(session):
    """T04：固定版本内容不能原地变化 —— 换版是加一行。

    改 `document_id` 等于让一条已经被引用过的出处悄悄指向别的内容，
    而出处漂移是这个项目最不能接受的一类错。
    """
    doc1 = await _make_document(session, doc_id="11" * 32, uploader="userA", filename="v1.pdf")
    doc2 = await _make_document(session, doc_id="22" * 32, uploader="userA", filename="v2.pdf")
    res = await _claim(session, doc1, "userA")

    session.add(ResourceVersion(id=new_id(), resource_id=res.id, version_no=2,
                                document_id=doc2.id, source_digest=doc2.doc_id,
                                filename=doc2.filename, size_bytes=doc2.size_bytes))
    await session.flush()

    versions = (await session.execute(
        select(ResourceVersion).where(ResourceVersion.resource_id == res.id)
        .order_by(ResourceVersion.version_no))).scalars().all()
    assert [v.version_no for v in versions] == [1, 2]
    # v1 仍然指着**原来那份内容** —— 它是固定的
    assert versions[0].document_id == doc1.id
    assert versions[0].source_digest == doc1.doc_id
    assert versions[1].document_id == doc2.id


async def test_same_source_can_have_multiple_fixed_parse_versions(session):
    """Re-parsing changes artifact identity, without changing old source bindings."""
    doc = await _make_document(session, doc_id="33" * 32, uploader="userA")
    res = await _claim(session, doc, "userA")
    first = await session.scalar(select(ResourceVersion).where(ResourceVersion.resource_id == res.id))
    first.parse_job_id = "old-parse"
    session.add(ResourceVersion(id=new_id(), resource_id=res.id, version_no=2,
        document_id=doc.id, source_digest=doc.doc_id, parse_job_id="new-parse"))
    await session.flush()
    versions = list((await session.scalars(select(ResourceVersion).where(
        ResourceVersion.resource_id == res.id).order_by(ResourceVersion.version_no))).all())
    assert [v.parse_job_id for v in versions] == ["old-parse", "new-parse"]
    assert {v.document_id for v in versions} == {doc.id}


async def test_idempotency_key_domain_is_per_actor(session):
    """T80：不同用户的幂等键域不能串用。

    只按 key 全局唯一的话，A 用过的键 B 再用会被判成重试，
    于是 B 的上传静默变成"已存在"，**拿到的是 A 的资产**。
    """
    from sqlalchemy.exc import IntegrityError

    doc = await _make_document(session, doc_id="44" * 32, uploader="userA")
    ra = await _claim(session, doc, "userA")
    rb = await _claim(session, doc, "userB")
    va_id = (await session.execute(select(ResourceVersion.id)
                                   .where(ResourceVersion.resource_id == ra.id))).scalar_one()
    vb_id = (await session.execute(select(ResourceVersion.id)
                                   .where(ResourceVersion.resource_id == rb.id))).scalar_one()

    session.add(UploadEvent(id=new_id(), resource_version_id=va_id,
                            actor_id="userA", idempotency_key="upload-1"))
    await session.flush()

    # 同一个人同一个键 = 重试，**不是新上传**。
    # 用 SAVEPOINT 兜住这次预期失败 —— 直接 rollback 会把上面建好的资产也冲掉，
    # 那样后半段就不是在验"换个人能不能用同一个键"了
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(UploadEvent(id=new_id(), resource_version_id=va_id,
                                    actor_id="userA", idempotency_key="upload-1"))

    # 换个人用同一个键：这是一次**独立的**上传，必须放行
    session.add(UploadEvent(id=new_id(), resource_version_id=vb_id,
                            actor_id="userB", idempotency_key="upload-1"))
    await session.flush()

    events = (await session.execute(select(UploadEvent))).scalars().all()
    assert {e.actor_id for e in events} == {"userA", "userB"}
    assert len(events) == 2, "同键换人必须是两条，不能被当成同一次上传"
