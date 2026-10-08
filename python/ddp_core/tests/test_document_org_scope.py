"""Document 去重是组织内的：同 (doc_id, origin) 不同组织可并存。"""
from sqlalchemy.exc import IntegrityError

from ddp_core.models import Document


async def _mk(session, doc_id, origin, org):
    session.add(Document(uploaded_by="u", organization_id=org, doc_id=doc_id,
                         origin=origin, filename="m.pdf",
                         mime="application/pdf", object_key=""))
    await session.flush()


async def test_same_digest_different_org_coexists(sqlite_sessionmaker):
    maker = await sqlite_sessionmaker()
    async with maker() as session:
        await _mk(session, "abc", "web", "org-a")
        await _mk(session, "abc", "web", "org-b")
        await session.commit()


async def test_same_digest_same_org_conflicts(sqlite_sessionmaker):
    maker = await sqlite_sessionmaker()
    async with maker() as session:
        await _mk(session, "abc", "web", "org-a")
        try:
            await _mk(session, "abc", "web", "org-a")
        except IntegrityError:
            return
        raise AssertionError("same (doc_id, origin, org) must conflict")
