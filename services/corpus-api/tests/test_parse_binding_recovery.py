"""T08: provenance-based recovery without guessing old ownership or replacing history."""

import copy
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from ddp_corpus.models import Document, Evidence, ParseJob, Resource, ResourceVersion, utcnow
from sqlalchemy import select
from tests.conftest import ACTOR, ORG
from tests.test_documents import _mock_service
from tests.test_resource_migrations import migration


def script():
    path = Path(__file__).resolve().parents[3] / "scripts/restore_parse_bindings.py"
    spec = importlib.util.spec_from_file_location("restore_parse_bindings", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def seed_history(session):
    then = utcnow() - timedelta(days=3)
    document = Document(
        id="history-doc",
        uploaded_by=ACTOR,
        organization_id=ORG,
        filename="private-history.pdf",
        doc_id="a" * 64,
        object_key="source",
        current_job_id="known-current",
        created_at=then,
    )
    session.add(document)
    await session.flush()
    session.add(
        Resource(
            id="history-resource",
            owner_id=ACTOR,
            uploaded_by=ACTOR,
            organization_id=ORG,
            display_name="history",
            created_at=then,
        )
    )
    await session.flush()
    session.add(
        ResourceVersion(
            id="original-v1",
            resource_id="history-resource",
            document_id=document.id,
            source_digest=document.doc_id,
            filename="history.pdf",
            created_at=then,
        )
    )
    for number, job_id in enumerate(("known-old", "known-current", "unknown-job"), 1):
        session.add(
            ParseJob(
                id=job_id,
                document_id=document.id,
                resource_id=None,
                initiated_by=None if job_id.startswith("unknown") else ACTOR,
                api_key_id="historical-key",
                options_hash=job_id,
                engine="borndigital",
                document_version=number,
                status="succeeded",
                result_prefix="results/" + job_id + "/",
                created_at=then + timedelta(hours=number),
                archived_at=then + timedelta(hours=number + 1),
            )
        )
        await session.flush()
        session.add(
            Evidence(
                id="e-" + job_id,
                document_id=document.id,
                parse_job_id=job_id,
                seq=0,
                content=job_id,
                content_digest=str(number) * 64,
            )
        )
    await session.commit()


def manifest(job_id="unknown-job"):
    return {
        "version": "ddp-parse-binding-recovery/1",
        "reviewed_by": "migration-operator@example",
        "bindings": [
            {
                "resource_id": "history-resource",
                "source_version_id": "original-v1",
                "parse_job_id": job_id,
                "document_id": "history-doc",
                "source_digest": "a" * 64,
                "owner_id": ACTOR,
                "organization_id": ORG,
            }
        ],
    }


@respx.mock
async def test_known_automatic_history_recovers_evidence_and_preserves_current(
    actor_client, session
):
    _mock_service()
    await seed_history(session)
    assert (await actor_client.get("/api/evidence/e-known-old")).status_code == 404
    connection = await session.connection()
    await connection.run_sync(migration("0018_resource_context.py").backfill_contexts)
    restore = migration("0024_restore_known_parse_versions.py").restore_known_versions
    await connection.run_sync(restore)
    await connection.run_sync(restore)
    await session.commit()
    session.expire_all()
    versions = list(
        await session.scalars(select(ResourceVersion).order_by(ResourceVersion.version_no))
    )
    assert len(versions) == 3
    assert versions[0].id == "original-v1" and versions[0].parse_job_id is None
    assert versions[-1].parse_job_id == "known-current"
    old = next(version for version in versions if version.parse_job_id == "known-old")
    assert old.binding_provenance["method"] == "migration:0024"
    response = await actor_client.get(
        "/api/evidence/e-known-old",
        params={"resource_id": "history-resource", "version_id": old.id},
    )
    assert response.status_code == 200
    assert response.json()["source_version_id"] == old.id
    assert response.json()["document"]["filename"] == "history.pdf"
    assert (await actor_client.get("/api/documents/history-doc")).json()[
        "current_job_id"
    ] == "known-current"
    assert (await actor_client.get("/api/evidence/e-unknown-job")).status_code == 404
    assert (await session.get(ParseJob, "unknown-job")).resource_id is None


async def test_existing_fixed_current_is_not_replaced_by_restored_older_job(session):
    await seed_history(session)
    session.add(
        ResourceVersion(
            id="already-current",
            resource_id="history-resource",
            version_no=2,
            document_id="history-doc",
            source_digest="a" * 64,
            filename="history.pdf",
            parse_job_id="known-current",
        )
    )
    await session.flush()
    connection = await session.connection()
    await connection.run_sync(migration("0018_resource_context.py").backfill_contexts)
    restore = migration("0024_restore_known_parse_versions.py").restore_known_versions
    await connection.run_sync(restore)
    await connection.run_sync(restore)
    versions = list(
        await session.scalars(select(ResourceVersion).order_by(ResourceVersion.version_no))
    )
    assert len(versions) == 4
    assert versions[1].id == "already-current"
    assert versions[-1].parse_job_id == "known-current"


@respx.mock
async def test_operator_manifest_dry_run_atomic_idempotent_audited_and_readable(
    actor_client, session
):
    _mock_service()
    await seed_history(session)
    recovery = script()
    proposed = manifest()
    before = await recovery.restore_manifest(proposed)
    assert before["new_bindings"] == 1
    assert (await session.get(ParseJob, "unknown-job")).resource_id is None
    await session.commit()
    applied = await recovery.restore_manifest(proposed, apply=True)
    repeated = await recovery.restore_manifest(proposed, apply=True)
    assert applied["new_bindings"] == 1
    assert repeated["new_bindings"] == 0 and repeated["existing_bindings"] == 1
    session.expire_all()
    job = await session.get(ParseJob, "unknown-job")
    assert job.resource_id == "history-resource"
    assert job.initiated_by is None and job.api_key_id == "historical-key"
    recovered = await session.scalar(
        select(ResourceVersion).where(ResourceVersion.parse_job_id == job.id)
    )
    audit = recovered.binding_provenance
    assert audit["manifest_digest"] == applied["manifest_digest"]
    assert audit["claimed_actor_id"] == ACTOR and audit["reviewed_by"] == proposed["reviewed_by"]
    assert audit["recorded_at"] and audit["source_digest"] == "a" * 64
    assert (
        await actor_client.get(
            "/api/evidence/e-unknown-job",
            params={"resource_id": "history-resource", "version_id": recovered.id},
        )
    ).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "other-owner"),
        ("organization_id", "other-org"),
        ("document_id", "other-doc"),
        ("source_digest", "b" * 64),
        ("source_version_id", "other-version"),
    ],
)
async def test_manifest_rejects_wrong_owner_org_document_or_digest(session, field, value):
    await seed_history(session)
    proposed = manifest()
    proposed["bindings"][0][field] = value
    with pytest.raises(ValueError):
        await script().restore_manifest(proposed, apply=True)
    assert len(list(await session.scalars(select(ResourceVersion)))) == 1
    assert (await session.get(ParseJob, "unknown-job")).resource_id is None


async def test_manifest_failure_rolls_back_all_prior_bindings(session):
    await seed_history(session)
    proposed = manifest("known-old")
    wrong = copy.deepcopy(manifest("unknown-job")["bindings"][0])
    wrong["source_digest"] = "b" * 64
    proposed["bindings"].append(wrong)
    with pytest.raises(ValueError):
        await script().restore_manifest(proposed, apply=True)
    session.expire_all()
    assert len(list(await session.scalars(select(ResourceVersion)))) == 1
    assert (await session.get(ParseJob, "known-old")).resource_id is None


async def test_manifest_cannot_replace_existing_binding_or_known_initiator(session):
    await seed_history(session)
    job = await session.get(ParseJob, "unknown-job")
    job.initiated_by = "another-actor"
    await session.commit()
    with pytest.raises(ValueError, match="initiator conflicts"):
        await script().restore_manifest(manifest(), apply=True)
    job.initiated_by = None
    session.add(
        ResourceVersion(
            id="existing-binding",
            version_no=2,
            resource_id="history-resource",
            document_id="history-doc",
            source_digest="a" * 64,
            parse_job_id=job.id,
        )
    )
    await session.commit()
    with pytest.raises(ValueError, match="cannot replace"):
        await script().restore_manifest(manifest(), apply=True)
