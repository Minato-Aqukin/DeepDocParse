"""Same source bytes never expose another asset's metadata or parse output."""
import json
import respx
from ddp_corpus.models import (Chunk, Document, Evidence, ParseJob, Resource, ResourceVersion)
from tests.conftest import ACTOR, ORG
from tests.test_documents import _mock_service


async def seed_shared(session, storage):
    document = Document(id='shared-doc', uploaded_by='first-owner', organization_id='first-org',
        filename='PRIVATE_ORIGINAL_NAME.pdf', doc_id='e'*64, object_key='original',
        current_job_id='parse-a')
    session.add(document)
    await session.flush()
    for suffix, owner, org, name in [('a','first-owner','first-org','PRIVATE_ORIGINAL_NAME.pdf'),
                                     ('b', ACTOR, ORG, 'my-manual.pdf')]:
        session.add(Resource(id='r-'+suffix, owner_id=owner, uploaded_by=owner,
            organization_id=org, display_name=name))
        await session.flush()
        session.add(ParseJob(id='parse-'+suffix, document_id=document.id, resource_id='r-'+suffix,
            initiated_by=owner, engine='borndigital', options_hash=suffix, document_version=1 if suffix=='a' else 2,
            status='succeeded', result_prefix='results/'+suffix+'/'))
        session.add(ResourceVersion(id='v-'+suffix, resource_id='r-'+suffix,
            document_id=document.id, source_digest=document.doc_id, filename=name, parse_job_id='parse-'+suffix))
        await session.flush()
        session.add(Evidence(id='ev-'+suffix, document_id=document.id, parse_job_id='parse-'+suffix,
            seq=0, content='independent-'+suffix, content_digest=suffix*64))
        await session.flush()
        session.add(Chunk(document_id=document.id, parse_job_id='parse-'+suffix, seq=0,
            text='needle independent-'+suffix, text_tokenized='needle independent '+suffix,
            evidence_id='ev-'+suffix))
        await storage.put('results/'+suffix+'/document.md', ('result-'+suffix).encode(), 'text/markdown')
        await storage.put('results/'+suffix+'/layout.json', json.dumps({'pdf_info':[]}).encode(), 'application/json')
    await session.commit()


@respx.mock
async def test_shared_metadata_and_parse_context(actor_client, session, app_state):
    _mock_service()
    await seed_shared(session, app_state.storage)
    response = await actor_client.get('/api/documents/shared-doc')
    assert response.status_code == 200
    assert response.json()['filename'] == 'my-manual.pdf'
    assert response.json()['current_job_id'] == 'parse-b'
    assert 'PRIVATE_ORIGINAL_NAME' not in response.text
    listed = await actor_client.get('/api/documents')
    assert listed.status_code == 200
    assert listed.json()[0]['resource_id'] == 'r-b'
    assert listed.json()[0]['filename'] == 'my-manual.pdf'
    assert (await actor_client.get('/api/documents', params={'q':'PRIVATE_ORIGINAL_NAME'})).json() == []
    result = await actor_client.get('/api/documents/shared-doc/result')
    assert result.status_code == 200
    assert result.json()['markdown'] == 'result-b'
    assert result.json()['filename'] == 'my-manual.pdf'
    assert (await actor_client.get('/api/documents/shared-doc/result', params={'job':'parse-a'})).status_code == 404
    access = await actor_client.get('/internal/file-access/shared-doc')
    assert access.json()['filename'] == 'my-manual.pdf'
    evidence = await actor_client.get('/api/evidence/ev-b')
    assert evidence.json()['document']['filename'] == 'my-manual.pdf'
    search = await actor_client.get('/api/search',params={'q':'needle'})
    assert search.status_code == 200
    assert search.json()['groups'][0]['resource_id'] == 'r-b'
    assert search.json()['groups'][0]['filename'] == 'my-manual.pdf'
    assert all('independent-a' not in h['snippet'] for group in search.json()['groups'] for h in group['hits'])


@respx.mock
async def test_version_context_must_match_requested_parse(actor_client, session, app_state):
    _mock_service()
    await seed_shared(session, app_state.storage)
    session.add(ParseJob(id='parse-b2', document_id='shared-doc', resource_id='r-b',
        initiated_by=ACTOR, engine='borndigital', options_hash='b2', document_version=3,
        status='succeeded', result_prefix='results/b2/'))
    session.add(ResourceVersion(id='v-b2', resource_id='r-b', document_id='shared-doc',
        source_digest='e'*64, filename='my-manual.pdf', version_no=2, parse_job_id='parse-b2'))
    await session.commit()
    await app_state.storage.put('results/b2/document.md',b'new revision','text/markdown')
    # Old version is stable even after a newer version becomes selected by default.
    params={'resource_id':'r-b','version_id':'v-b'}
    old = await actor_client.get('/api/documents/shared-doc/result',params=params)
    assert old.json()['markdown'] == 'result-b'
    assert (await actor_client.get('/api/documents/shared-doc/result',params={**params,'job':'parse-b2'})).status_code == 404
