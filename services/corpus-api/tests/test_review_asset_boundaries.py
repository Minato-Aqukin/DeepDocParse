"""Independent v3 acceptance probes; keep failures until the implementation is repaired."""

import respx
from ddp_corpus.models import Document, ResourceVersion
from tests.test_document_context import seed_shared
from tests.test_documents import _mock_service
from tests.test_resource_migrations import migration


@respx.mock
async def test_review_shared_document_does_not_expose_other_job_error(
    actor_client, session, app_state
):
    _mock_service()
    await seed_shared(session, app_state.storage)
    document = await session.get(Document, "shared-doc")
    document.index_status = "failed"
    document.index_error = "PRIVATE_A_TASK_ERROR: /private/project-secret.pdf"
    document.compile_degraded = ["PRIVATE_A_COMPILE_PROVIDER"]
    await session.commit()
    for path in ("/api/documents/shared-doc", "/api/documents"):
        response = await actor_client.get(path)
        assert response.status_code == 200
        assert "PRIVATE_A" not in response.text, response.text


@respx.mock
async def test_review_explicit_evidence_version_is_preserved(actor_client, session, app_state):
    _mock_service()
    await seed_shared(session, app_state.storage)
    # Re-selecting a previously parsed revision is permitted by 0022. Both
    # immutable versions can bind the same parse; version identity is distinct.
    session.add(
        ResourceVersion(
            id="v-b-newer",
            resource_id="r-b",
            document_id="shared-doc",
            source_digest="e" * 64,
            filename="newer-name.pdf",
            version_no=2,
            parse_job_id="parse-b",
        )
    )
    await session.commit()
    response = await actor_client.get(
        "/api/evidence/ev-b", params={"resource_id": "r-b", "version_id": "v-b"}
    )
    assert response.status_code == 200
    assert response.json()["source_version_id"] == "v-b", response.json()
    assert response.json()["document"]["filename"] == "my-manual.pdf"


async def test_review_missing_first_uploader_organization_remains_repairable(session):
    document = Document(
        id="old-doc",
        uploaded_by="old-owner",
        organization_id="",
        filename="old-file.pdf",
        doc_id="f" * 64,
        object_key="old-source",
    )
    session.add(document)
    await session.flush()
    await (await session.connection()).run_sync(migration("0015_resource_layer.py").backfill_assets)
    from ddp_corpus.models import Resource
    from sqlalchemy import select

    resource = await session.scalar(select(Resource).where(Resource.owner_id == "old-owner"))
    assert resource.organization_id == "migration:unresolved"


@respx.mock
async def test_review_unknown_historical_parse_actor_is_not_guessed(
    actor_client, session, app_state
):
    _mock_service()
    await seed_shared(session, app_state.storage)
    from ddp_corpus.models import ParseJob
    from tests.conftest import actor_headers

    # Migration 0006 retained jobs from other uploaders after deduplication,
    # with initiated_by NULL. The surviving Document uploader proves no job owner.
    session.add(
        ParseJob(
            id="legacy-unknown-job",
            document_id="shared-doc",
            engine="borndigital",
            options_hash="unknown-actor-options",
            document_version=3,
            initiated_by=None,
            resource_id=None,
            status="succeeded",
            result_prefix="legacy/private/",
        )
    )
    await session.flush()
    await app_state.storage.put(
        "legacy/private/document.md", b"OTHER_UPLOADER_PRIVATE_PARSE", "text/markdown"
    )
    await (await session.connection()).run_sync(
        migration("0018_resource_context.py").backfill_contexts
    )
    await session.commit()
    session.expire_all()
    job = await session.get(ParseJob, "legacy-unknown-job")
    response = await actor_client.get(
        "/api/documents/shared-doc/result",
        params={"resource_id": "r-a", "job": job.id},
        headers=actor_headers("first-owner", organization_id="first-org"),
    )
    assert response.status_code == 404, response.text
    assert job.resource_id is None, "NULL initiator was assigned to the surviving Document uploader"
