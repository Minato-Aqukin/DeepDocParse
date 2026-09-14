"""Durability and policy protocols; model transport fixtures are not real inference."""

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.http import create_app
from ddp_local.providers import ModelSelection
from ddp_local.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def ready(tmp_path):
    runtime = LocalRuntime(tmp_path / "workspace")
    task = runtime.upload_stream(io.BytesIO((ROOT / 'tests/fixtures/sample.pdf').read_bytes()),
                                 filename="source.pdf", operation_key="wiki-source")
    await runtime.work_once()
    runtime.provider.model = ModelSelection("http://127.0.0.1:18990/v1", "fixture", provenance={
        "model_id": "fixture-reviewed", "model_sha256": "fixture-only"})
    version = runtime.store.version(task["version_id"])
    body = {"title": "Contract and Answer", "sources": [{"resource_id": version["resource_id"],
        "source_version_id": version["id"]}], "max_pages": 3}
    yield runtime, body
    runtime.close()


def protocol(runtime, *, bad=False, wait=None, began=None, rename=False):
    calls = []
    async def generate(messages, **kwargs):
        calls.append((messages, kwargs))
        stage = json.loads(messages[1]["content"])["stage"]
        if stage == "plan":
            if began:
                began.set()
            if wait:
                await wait.wait()
            value = {"pages": [{"title": "Contract changed" if rename else "Contract", "references": [1]},
                               {"title": "Answer", "references": [1]}]}
        elif stage == "relations":
            value = {"selected_relations": [1] if json.loads(messages[1]["content"]).get("candidates") else []}
        else:
            value = {"pages": [{"page": n, "sections": [{"heading": "Facts", "sentences": [
                {"text": "The answer is 42.", "references": [99] if bad else [1]}]}]} for n in (1, 2)],
                "relations": [{"subject_page": 1, "object_page": 2, "predicate": "Answer: 42",
                               "references": [1]}]}
        return json.dumps(value), {"name": "fixture", "location": "local"}
    runtime.provider.generate = generate
    return calls


async def test_persistent_pages_relations_attempts_and_restart_replay(ready):
    runtime, body = ready
    calls = protocol(runtime)
    result = await runtime.build_wiki(body, operation_key="build-1")
    wiki_id, revision = result["wiki"]["id"], result["revision"]
    assert len(revision["pages"]) == 2 and len(revision["relations"]) == 1
    assert revision["source_type"] == "generated" and revision["dependency_manifest"]
    assert all(row["original"]["source_type"] == "source" for row in revision["dependency_manifest"])
    assert len(runtime.wikis.attempts(result["task_id"])) == 3
    assert (await runtime.build_wiki(body, operation_key="build-1"))["revision"]["id"] == revision["id"]
    assert len(calls) == 3
    another = LocalRuntime(runtime.store.db.execute('PRAGMA database_list').fetchone()[2].rsplit('/', 1)[0])
    try:
        another.provider.model = ModelSelection("http://127.0.0.1:19991/v1", "new-random-alias", provenance={
            "model_id": "fixture-reviewed", "model_sha256": "fixture-only"})
        assert (await another.build_wiki(body, operation_key="build-1"))["revision"]["id"] == revision["id"]
        assert another.wikis.get(wiki_id)["revision"]["id"] == revision["id"]
        assert another.store.receipt("build-1")["status"] == "succeeded"
    finally:
        another.close()
    events = runtime.store.events()
    commit = next(e for e in events if e["kind"] == "wiki.revision_committed")
    success = next(e for e in events if e["kind"] == "task.succeeded" and e["task_id"] == result["task_id"])
    assert commit["seq"] < success["seq"]
    with pytest.raises(sqlite3.IntegrityError):
        runtime.store.db.execute('UPDATE wiki_revisions SET body=? WHERE id=?', ('{}', revision['id']))
    with pytest.raises(sqlite3.IntegrityError):
        runtime.store.db.execute('DELETE FROM evidence WHERE id=?', (revision['dependency_manifest'][0]['evidence_id'],))
    with pytest.raises(sqlite3.IntegrityError):
        runtime.store.db.execute('DELETE FROM versions WHERE id=?', (body['sources'][0]['source_version_id'],))


async def test_concurrent_edit_cas_preserves_history_and_human_provenance(ready):
    runtime, body = ready
    protocol(runtime)
    built = await runtime.build_wiki(body, operation_key="base")
    wid, rev = built['wiki']['id'], built['revision']
    other = LocalRuntime(runtime.store.db.execute('PRAGMA database_list').fetchone()[2].rsplit('/', 1)[0])
    edits = [{'base_revision_id': rev['id'], 'paragraphs': [{'id': 'note', 'text': text}]}
             for text in ('first edit', 'second edit')]
    try:
        values = await asyncio.gather(runtime.edit_wiki(wid, rev['pages'][0]['page_key'], edits[0], operation_key='edit-a'),
            other.edit_wiki(wid, rev['pages'][0]['page_key'], edits[1], operation_key='edit-b'), return_exceptions=True)
        assert sum(isinstance(v, ApplicationError) and v.code == 'revision_conflict' for v in values) == 1
        won = next(v for v in values if isinstance(v, dict))
        human = won['revision']['pages'][0]['human_paragraphs'][0]
        assert human['kind'] == 'human' and human['unsupported'] and not human['evidence_ids']
        assert human['source_type'] == 'generated' and human['review_state'] == 'unreviewed'
        assert not runtime.wikis.get(wid, rev['id'])['revision']['pages'][0]['human_paragraphs']
        assert len(runtime.wikis.revisions(wid)["items"]) == 2
        protocol(runtime, rename=True)
        rebuilt = await runtime.build_wiki({**body, 'base_revision_id': won['revision']['id']},
            wiki_id=wid, operation_key='rebuild')
        assert len(rebuilt['revision']['pages']) == 3
        assert rebuilt['revision']['merge_conflicts'][0]['reason'] == 'edited_page_missing_in_plan'
        assert any(p['human_paragraphs'] for p in rebuilt['revision']['pages'])
    finally:
        other.close()


async def test_build_publication_cas_rejects_concurrent_edit_after_model_started(ready):
    runtime, body = ready
    protocol(runtime)
    initial = await runtime.build_wiki(body, operation_key='base')
    wid, rev = initial['wiki']['id'], initial['revision']
    begin, release = asyncio.Event(), asyncio.Event()
    protocol(runtime, wait=release, began=begin)
    build = asyncio.create_task(runtime.build_wiki({**body, 'base_revision_id': rev['id']}, wiki_id=wid, operation_key='slow-build'))
    await begin.wait()
    edit = await runtime.edit_wiki(wid, rev['pages'][0]['page_key'],
        {'base_revision_id': rev['id'], 'paragraphs': [{'id': 'keep', 'text': 'Manual note'}]}, operation_key='edit')
    release.set()
    with pytest.raises(ApplicationError) as conflict:
        await build
    assert conflict.value.code == 'revision_conflict'
    assert runtime.wikis.get(wid)['revision']['id'] == edit['revision']['id']
    assert len(runtime.wikis.revisions(wid)["items"]) == 2
    assert runtime.store.receipt('slow-build')['status'] == 'failed'


async def test_invalid_evidence_never_publishes_and_workspace_ids_cannot_cross(ready, tmp_path):
    runtime, body = ready
    protocol(runtime, bad=True)
    with pytest.raises(ApplicationError) as rejected:
        await runtime.build_wiki(body, operation_key='bad')
    assert rejected.value.code == 'unsupported_generation'
    assert not runtime.wikis.list()["items"] and runtime.store.receipt('bad')['status'] == 'failed'
    assert len(runtime.wikis.attempts(runtime.store.receipt('bad')['id'])) == 3
    protocol(runtime)
    good = await runtime.build_wiki(body, operation_key='good')
    second = LocalRuntime(tmp_path / 'other')
    try:
        with pytest.raises(ApplicationError) as denied:
            second.wikis.get(good['wiki']['id'])
        assert denied.value.code == 'not_found'
        with pytest.raises(ApplicationError):
            second.wikis.freeze(body['sources'], {'max_evidence': 40, 'max_input_chars': 16000})
        other = await runtime.build_wiki(body, operation_key='other')
        with pytest.raises(ApplicationError):
            runtime.wikis.get(good['wiki']['id'], other['revision']['id'])
    finally:
        second.close()
    version = body['sources'][0]['source_version_id']
    runtime.store.db.execute('UPDATE versions SET parse_revision=? WHERE id=?', ('new-parse', version))
    stale = runtime.wikis.get(good['wiki']['id'])['revision']
    assert stale['stale'] and all(p['stale'] for p in stale['pages'])


async def test_fixed_http_wiki_and_model_receipts(ready):
    runtime, body = ready
    protocol(runtime)
    token = 'a' * 48
    app = create_app(runtime, session_token=token, allowed_hosts={'127.0.0.1:18763'}, start_worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18763',
                                 headers={'Authorization': 'Bearer ' + token}) as client:
        missing = await client.post('/api/v1/wikis', json=body)
        assert missing.status_code == 400
        built = await client.post('/api/v1/wikis', json=body, headers={'Idempotency-Key': 'http-build'})
        assert built.status_code == 201, built.text
        wid, rev = built.json()['wiki']['id'], built.json()['revision']
        assert (await client.get('/api/v1/wikis')).json()['items'][0]['wiki']['id'] == wid
        assert (await client.get(f'/api/v1/wikis/{wid}/revisions/{rev["id"]}')).status_code == 200
        edited = await client.patch(f'/api/v1/wikis/{wid}/pages/{rev["pages"][0]["page_key"]}',
            headers={'Idempotency-Key': 'http-edit'}, json={'base_revision_id': rev['id'], 'paragraphs': []})
        assert edited.status_code == 201, edited.text
        conflict = await client.patch(f'/api/v1/wikis/{wid}/pages/{rev["pages"][0]["page_key"]}',
            headers={'Idempotency-Key': 'http-conflict'}, json={'base_revision_id': rev['id'], 'paragraphs': []})
        assert conflict.status_code == 409
        stop = await client.post('/api/v1/models/stop', headers={'Idempotency-Key': 'model-stop'})
        assert stop.status_code == 200 and stop.json()['status'] == 'stopped'
        receipt = (await client.get('/api/v1/client/receipts/model-stop')).json()
        assert receipt['status'] == 'succeeded' and receipt['id'] == stop.json()['task_id']
        assert (await client.post('/api/v1/models/stop', headers={'Idempotency-Key': 'model-stop'})).json()['task_id'] == receipt['id']
        assert (await client.post('/api/v1/models/stop')).status_code == 400


async def test_frozen_source_change_during_inference_cannot_publish(ready):
    runtime, body = ready
    begin, release = asyncio.Event(), asyncio.Event()
    protocol(runtime, wait=release, began=begin)
    pending = asyncio.create_task(runtime.build_wiki(body, operation_key='changed-source'))
    await begin.wait()
    runtime.store.db.execute('UPDATE versions SET parse_revision=? WHERE id=?',
                            ('changed-after-freeze', body['sources'][0]['source_version_id']))
    release.set()
    with pytest.raises(ApplicationError) as changed:
        await pending
    assert changed.value.code == 'wiki_source_unavailable'
    assert not runtime.wikis.list()["items"]
    assert not any(event['kind'] == 'wiki.revision_committed' for event in runtime.store.events())


def test_v1_migration_is_atomic_and_preserves_existing_resources(tmp_path, monkeypatch):
    from ddp_local.store import DDL
    from ddp_local import wiki_store
    directory = tmp_path / 'old-workspace'
    directory.mkdir()
    db = sqlite3.connect(directory / 'workspace.sqlite3', isolation_level=None)
    db.executescript(DDL)
    db.execute('INSERT INTO resources VALUES(?,?,?)', ('old-resource', 'Old title', 1))
    original = wiki_store.SCHEMA
    monkeypatch.setattr(wiki_store, 'SCHEMA', original + '\nBROKEN MIGRATION;\n')
    with pytest.raises(sqlite3.OperationalError):
        LocalRuntime(directory)
    assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='wiki_revisions'").fetchone()
    monkeypatch.setattr(wiki_store, 'SCHEMA', original)
    runtime = LocalRuntime(directory)
    try:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('SELECT title FROM resources WHERE id=?', ('old-resource',)).fetchone()[0] == 'Old title'
    finally:
        runtime.close()
        db.close()


async def test_wiki_metadata_windows_bound_scope_and_do_not_silently_hide_rows(ready, tmp_path):
    runtime, body = ready
    protocol(runtime)
    built = [await runtime.build_wiki(body, operation_key=f'window-{n}') for n in range(3)]
    first = runtime.wikis.list(limit=1)
    assert first['visible_total'] == 3 and first['has_more'] and first['next_cursor']
    assert 'pages' not in first['items'][0]['revision']
    assert first['items'][0]['revision']['page_count'] == 2
    await runtime.build_wiki(body, operation_key='new-after-window')
    seen = [first['items'][0]['wiki']['id']]
    window = first
    while window['next_cursor']:
        window = runtime.wikis.list(limit=1, cursor=window['next_cursor'])
        assert window['visible_total'] == 3
        seen.extend(row['wiki']['id'] for row in window['items'])
    assert len(seen) == len(set(seen)) == 3 and not window['has_more']
    assert runtime.wikis.list()['visible_total'] == 4
    other = LocalRuntime(tmp_path / 'another-window')
    try:
        with pytest.raises(ApplicationError) as cross:
            other.wikis.list(cursor=first['next_cursor'])
        assert cross.value.code == 'cursor_expired'
    finally:
        other.close()
    with pytest.raises(ApplicationError):
        runtime.wikis.revisions(built[0]['wiki']['id'], cursor=first['next_cursor'])
    with pytest.raises(ApplicationError):
        runtime.wikis.list(limit=101)
