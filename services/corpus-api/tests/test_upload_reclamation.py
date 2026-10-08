"""Terminal upload originals are collected only after grace and reference checks."""
from datetime import timedelta

import pytest
from ddp_corpus.config import settings
from ddp_corpus.models import Document, utcnow


async def request_cleanup(client, headers, key, *, old=True):
    return await client.post('/internal/upload-reclamation', headers=headers, json={
        'object_key': key,
        'eligible_at': (utcnow() - timedelta(seconds=settings.gc_grace_seconds + 60)
                        if old else utcnow()).isoformat(),
    })


async def test_terminal_original_reclamation_grace_references_and_retry(
        client, service_client_headers, session, app_state):
    """grace 内 / 有引用时不删：这些分支在任何后端都不碰字节。

    无表锁可拿的后端（SQLite）大声拒绝删除而不是无防护地删；PG 上的真正
    删除与重试由 control 面的 TestTerminalUploadReclamation* 覆盖。
    变异确认：把 gc.py 的 409 改回"跳过表锁继续删"，本文件
    test_terminal_original_reclamation_refuses_without_pg_fence 必须红。
    """
    key = 'uploads/org-test/orphan.pdf'
    storage = app_state.storage
    await storage.put(key, b'original', 'application/pdf')
    response = await request_cleanup(client, service_client_headers, key, old=False)
    assert response.status_code == 200
    assert response.json() == {'reclaimed': False}
    assert await storage.get(key) == b'original'
    document = Document(doc_id='a' * 64, origin='web', filename='shared.pdf',
                        uploaded_by='alice', organization_id='org-test',
                        mime='application/pdf', size_bytes=8, object_key=key)
    session.add(document)
    await session.commit()
    response = await request_cleanup(client, service_client_headers, key)
    assert response.json() == {'reclaimed': False}
    assert await storage.get(key) == b'original'
    # Durable document-GC manifests protect bytes even after object_key is cleared.
    document.object_key = ''
    document.gc_pending_keys = [key]
    await session.commit()
    assert (await request_cleanup(client, service_client_headers, key)).json() == {'reclaimed': False}
    assert await storage.get(key) == b'original'


async def test_terminal_original_reclamation_refuses_without_pg_fence(
        client, service_client_headers, session, app_state):
    """无表锁可拿的后端（SQLite）大声拒绝删除，而不是无防护地删。

    变异确认：把 gc.py 的 409 改回"跳过表锁继续删"，本用例必须红
    （会回到 reclaimed:True 并删掉字节）。
    """
    from ddp_corpus import gc as gc_module

    key = 'uploads/org-test/unfenced.pdf'
    storage = app_state.storage
    await storage.put(key, b'original', 'application/pdf')
    assert session.bind.dialect.name == 'sqlite'
    assert gc_module._dialect_name(session) == 'sqlite'
    response = await request_cleanup(client, service_client_headers, key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reclamation_unsupported_dialect"
    assert await storage.get(key) == b'original'


async def test_terminal_original_reclamation_protects_compute(client, service_client_headers, session, app_state):
    from ddp_corpus.remote_compute_models import RemoteCompute
    key = 'tmp-remote-compute/org-test/task/input.pdf'
    await app_state.storage.put(key, b'original', 'application/pdf')
    session.add(RemoteCompute(id='task', organization_id='org-test', actor_id='alice',
        actor_kind='user', input_sha256='a' * 64, input_object_key=key, status='running'))
    await session.commit()
    assert (await request_cleanup(client, service_client_headers, key)).json() == {'reclaimed': False}
    assert await app_state.storage.get(key) == b'original'


async def test_terminal_original_reclamation_rejects_user_and_non_original_keys(actor_client, service_client_headers):
    response = await request_cleanup(actor_client, {}, 'uploads/org-test/input.pdf')
    assert response.status_code == 403
    response = await request_cleanup(actor_client, service_client_headers, 'results/job/layout.json')
    assert response.status_code == 422

async def test_terminal_original_reclamation_never_deletes_unfenced_on_sqlite(
    client, service_client_headers, session, app_state, monkeypatch):
    """SQLite 上不发 PG-only 的 LOCK TABLE，也不删：无防护删除比不删更坏。"""
    from ddp_corpus import gc as gc_module

    key = 'uploads/org-test/unfenced.pdf'
    await app_state.storage.put(key, b'original', 'application/pdf')
    assert session.bind.dialect.name == 'sqlite'
    assert gc_module._dialect_name(session) == 'sqlite'
    seen = []
    real_text = gc_module.text

    def spy_text(statement, *args, **kwargs):
        seen.append(str(statement))
        return real_text(statement, *args, **kwargs)

    monkeypatch.setattr(gc_module, "text", spy_text)
    response = await request_cleanup(client, service_client_headers, key)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reclamation_unsupported_dialect"
    assert await app_state.storage.get(key) == b'original'
    assert not any("LOCK TABLE" in statement for statement in seen)


async def test_deleted_document_reclaims_all_derived_bytes_but_keeps_shared_content_and_evidence(actor_client, session, app_state):
    from sqlalchemy import func, select
    from ddp_corpus import db
    from ddp_corpus.gc import collect_deleted_objects
    from ddp_corpus.models import Chunk, Evidence, ParseJob, Resource, ResourceVersion

    document = Document(id='cleanup-document', doc_id='b' * 64, origin='web',
        uploaded_by='actor-alice', organization_id='org-test', filename='cleanup.pdf',
        mime='application/pdf', size_bytes=8, object_key='uploads/org-test/shared.pdf')
    session.add(document)
    await session.flush()
    job = ParseJob(id='cleanup-job', document_id=document.id, engine='borndigital',
        options_hash='c' * 64, status='succeeded', result_prefix='results/cleanup-job/')
    resources = [Resource(id=f'cleanup-resource-{i}', organization_id='org-test',
        owner_id='actor-alice', uploaded_by='actor-alice') for i in range(2)]
    session.add_all([job, *resources])
    await session.flush()
    versions = [ResourceVersion(id=f'cleanup-version-{i}', resource_id=row.id,
        document_id=document.id, parse_job_id=job.id) for i, row in enumerate(resources)]
    evidence = Evidence(id='cleanup-evidence', document_id=document.id,
        parse_job_id=job.id, atom_key='source', content='Synthetic retained atom.')
    session.add_all([*versions, evidence])
    await session.flush()
    session.add(Chunk(document_id=document.id, parse_job_id=job.id,
        evidence_id=evidence.id, text='Synthetic retrieval text.', embedding=[1.0, 0.0]))
    await session.commit()
    keys = [document.object_key, f'results/{job.id}/full.md',
        f'results/{job.id}/layout.json', f'results/{job.id}/images/page.png',
        f'results/{job.id}/crops/region.png']
    for key in keys:
        await app_state.storage.put(key, b'synthetic artifact', 'application/octet-stream')

    async def delete_and_age(resource):
        response = await actor_client.delete(f'/api/v1/resources/{resource.id}')
        assert response.status_code == 204
        await session.refresh(resource)
        resource.deleted_at = utcnow() - timedelta(seconds=settings.gc_grace_seconds + 60)
        await session.refresh(document)
        if document.deleted_at is not None:
            document.deleted_at = resource.deleted_at
        await session.commit()

    await delete_and_age(resources[0])
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    for key in keys:
        assert await app_state.storage.exists(key)
    assert await session.scalar(select(func.count(Chunk.id))) == 1
    await delete_and_age(resources[1])
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 1
    for key in keys:
        assert not await app_state.storage.exists(key)
    assert await session.scalar(select(func.count(Chunk.id))) == 0
    await session.refresh(evidence)
    assert evidence.content == 'Synthetic retained atom.'
