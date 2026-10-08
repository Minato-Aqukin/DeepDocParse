"""Web 上传按组织隔离：同字节跨组织各存各的 Document 行。"""
import respx
from sqlalchemy import select

from ddp_corpus.models import Document
from tests.conftest import ORG, submit_document
from tests.test_documents import _mock_service


@respx.mock
async def test_same_bytes_across_orgs_create_independent_documents(
        client, session, app_state):
    """组织 B 上传与 A 完全相同的字节，必须建出自己的行，而不是复用/复活 A 的。

    变异确认：把 ingest.py 的 web 查找改回全局 (doc_id, origin)，
    本用例必须红（只剩一行，且第二份上传没有自己的 object_key）。
    """
    _mock_service()
    content = b"%PDF-1.4 same bytes both orgs"
    first = await submit_document(client, app_state.storage, content,
                                  organization_id=ORG, actor_id="actor-alice")
    assert first.status_code == 200, first.text
    second = await submit_document(client, app_state.storage, content,
                                   organization_id="org-other", actor_id="actor-bob")
    assert second.status_code == 200, second.text
    assert first.json()["result_id"] != second.json()["result_id"]

    rows = (await session.execute(select(Document))).scalars().all()
    by_org = {row.organization_id: row for row in rows}
    assert set(by_org) == {ORG, "org-other"}
    assert by_org[ORG].origin == by_org["org-other"].origin == "web"
    assert by_org[ORG].doc_id == by_org["org-other"].doc_id
    assert by_org[ORG].object_key != by_org["org-other"].object_key
    assert by_org[ORG].id == first.json()["result_id"]
    assert by_org["org-other"].id == second.json()["result_id"]


@respx.mock
async def test_revive_in_one_org_never_touches_other_org_row(
        client, session, app_state):
    """A 删掉再传回来只复活 A 自己的行；B 的行（同字节）纹丝不动。

    变异确认：同上，全局查找会把 B 的上传接到 A 的行上，
    B 删行后再传会复活 A 的 deleted_at 而不是自己的。
    """
    import hashlib
    from ddp_corpus.models import utcnow

    _mock_service()
    content = b"%PDF-1.4 revive isolation probe"
    doc_id = hashlib.sha256(content).hexdigest()
    first = await submit_document(client, app_state.storage, content,
                                  organization_id=ORG, actor_id="actor-alice")
    second = await submit_document(client, app_state.storage, content,
                                   organization_id="org-other", actor_id="actor-bob")
    assert first.status_code == 200 and second.status_code == 200
    own = await session.get(Document, first.json()["result_id"])
    foreign = await session.get(Document, second.json()["result_id"])
    foreign.deleted_at = utcnow()
    await session.commit()

    third = await submit_document(client, app_state.storage, content,
                                  organization_id="org-other", actor_id="actor-bob")
    assert third.status_code == 200, third.text
    assert third.json()["result_id"] == foreign.id

    await session.refresh(own)
    await session.refresh(foreign)
    assert foreign.doc_id == doc_id == own.doc_id
    assert foreign.deleted_at is None
    assert own.deleted_at is None
    assert foreign.object_key != own.object_key
