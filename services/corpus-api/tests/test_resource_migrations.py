"""Exercise actual data backfill functions, including lost historical metadata."""
import importlib.util
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from ddp_corpus.models import (Conversation, Document, DocumentUpload, ExtractionItem,
    ExtractionRun, ParseJob, Resource, ResourceVersion, utcnow)


def migration(name):
    path = Path(__file__).resolve().parents[3] / 'database/corpus/alembic/versions' / name
    spec = importlib.util.spec_from_file_location(name.removesuffix('.py'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_backfill_full_length_ids_and_unknown_organization(session):
    now = utcnow() - timedelta(days=1)
    doc = Document(id='a'*32, uploaded_by='first', organization_id='org-first',
        filename='PRIVATE_FIRST_UPLOADER.pdf', doc_id='b'*64, object_key='source',
        created_at=now, updated_at=now)
    session.add(doc)
    await session.flush()
    session.add_all([DocumentUpload(document_id=doc.id, user_id=who, created_at=now)
                     for who in ('first', 'second')])
    await session.flush()
    connection = await session.connection()
    backfill = migration('0015_resource_layer.py').backfill_assets
    await connection.run_sync(backfill)
    await connection.run_sync(backfill)
    resources = list((await session.scalars(select(Resource).order_by(Resource.owner_id))).all())
    versions = list((await session.scalars(select(ResourceVersion))).all())
    assert len(resources) == len(versions) == 2
    assert len({r.id for r in resources}) == 2
    assert len({v.id for v in versions}) == 2
    first, second = resources
    assert first.organization_id == 'org-first'
    assert second.organization_id == 'migration:unresolved'
    assert second.display_name == 'Recovered document.pdf'
    assert next(v for v in versions if v.resource_id == second.id).filename == 'Recovered document.pdf'


async def test_history_only_unique_original_owner_is_bound(session):
    old, now = utcnow() - timedelta(days=1), utcnow()
    doc = Document(id='doc', uploaded_by='owner', organization_id='org',
        filename='manual.pdf', doc_id='c'*64, object_key='source', created_at=old)
    session.add(doc)
    await session.flush()
    session.add_all([Resource(id=rid, owner_id=owner, uploaded_by=owner, organization_id=org,
                              display_name='manual', created_at=old)
        for rid, owner, org in [('r-own','owner','org'),('r-other','reader','other-org')]])
    await session.flush()
    session.add_all([ResourceVersion(id='v-'+rid, resource_id=rid, document_id=doc.id,
                                     source_digest=doc.doc_id, created_at=old)
                     for rid in ('r-own', 'r-other')])
    session.add_all([Conversation(id=cid, document_id=doc.id, actor_id=actor,
                                  organization_id='org', created_at=now)
                    for cid, actor in [('c-own','owner'),('c-reader','reader')]])
    job = ParseJob(id='j-own', document_id=doc.id, engine='borndigital', options_hash='h',
                   initiated_by='owner', created_at=now)
    run = ExtractionRun(id='run', actor_id='owner', organization_id='org', created_at=now)
    session.add_all([job, run]); await session.flush()
    session.add(ExtractionItem(run_id=run.id, document_id=doc.id, record_index=0))
    await session.flush()
    backfill = migration('0018_resource_context.py').backfill_contexts
    await (await session.connection()).run_sync(backfill)
    session.expire_all()
    assert (await session.get(Conversation, 'c-own')).resource_id == 'r-own'
    assert (await session.get(Conversation, 'c-reader')).resource_id is None
    assert (await session.get(ParseJob, 'j-own')).resource_id == 'r-own'
    assert (await session.get(ExtractionRun, 'run')).resource_context == {
        'principal_id':'owner', 'resources': {'doc':'r-own'}}
    # A second plausible original asset makes historical repair ambiguous.
    session.add(Resource(id='r-ambiguous', owner_id='owner', uploaded_by='owner',
                         organization_id='org', created_at=old))
    await session.flush()
    session.add(ResourceVersion(resource_id='r-ambiguous', document_id='doc',
                                source_digest='c'*64, created_at=old))
    session.add(Conversation(id='c-ambiguous', document_id='doc', actor_id='owner',
                            organization_id='org', created_at=now))
    await session.flush()
    await (await session.connection()).run_sync(backfill)
    assert (await session.get(Conversation, 'c-ambiguous')).resource_id is None


async def test_first_uploader_without_historical_organization_is_quarantined(session):
    doc = Document(id='empty-org-doc', uploaded_by='owner', organization_id='',
        filename='known-first-name.pdf', doc_id='f'*64, object_key='source')
    session.add(doc)
    await session.flush()
    await (await session.connection()).run_sync(migration('0015_resource_layer.py').backfill_assets)
    resource = await session.scalar(select(Resource))
    assert resource.organization_id == 'migration:unresolved'
    assert resource.display_name == 'known-first-name.pdf'
