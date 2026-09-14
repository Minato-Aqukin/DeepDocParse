"""Explicit model commands share durable receipts; transport fixtures never claim inference."""

import asyncio
import hashlib
import time

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.runtime import LocalRuntime


async def test_install_cancel_has_receipt_keeps_partial_and_explicit_retry_is_idempotent(tmp_path, monkeypatch):
    runtime = LocalRuntime(tmp_path / 'workspace')
    payload = b'GGUF\x03\0\0\0' + b'fixture-only' * 100
    artifact = {'id': 'fixture-model', 'kind': 'model', 'filename': 'fixture.gguf',
        'version': 'protocol-only', 'format': 'gguf-v3', 'sha256': hashlib.sha256(payload).hexdigest(),
        'bytes': len(payload), 'license': 'MIT', 'url': 'https://fixture.example/model',
        'license_url': 'https://fixture.example/license', 'backend': 'llama.cpp', 'device': 'cpu'}
    runtime.model_installer.definitions['artifacts'].append(artifact)
    began, release = asyncio.Event(), asyncio.Event()
    class Transfer(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload[:100]
            began.set()
            await release.wait()
            yield payload[100:]
    calls = []
    def transport(request):
        calls.append(request)
        if request.headers.get('range'):
            return httpx.Response(206, content=payload[100:], headers={
                'Content-Range': f'bytes 100-{len(payload)-1}/{len(payload)}'})
        return httpx.Response(200, stream=Transfer())
    original = runtime.model_installer.download
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            async def download(identifier):
                return await original(identifier, client=client)
            monkeypatch.setattr(runtime.model_installer, 'download', download)
            pending = asyncio.create_task(runtime.model_operation('install', 'fixture-model', operation_key='install-cancel'))
            await began.wait()
            task = runtime.store.receipt('install-cancel')
            runtime.store.cancel(task['id'])
            with pytest.raises(ApplicationError) as cancelled:
                await asyncio.wait_for(pending, 3)
            assert cancelled.value.code == 'execution_cancelled'
            assert runtime.store.receipt('install-cancel')['status'] == 'cancelled'
            assert runtime.model_installer.status('fixture-model')['status'] == 'partial'
            with pytest.raises(ApplicationError):
                await runtime.model_operation('install', 'fixture-model', operation_key='install-cancel')
            assert len(calls) == 1
            result = await runtime.model_operation('install', 'fixture-model', operation_key='explicit-install-retry')
            assert result['status'] == 'installed'
            replay = await runtime.model_operation('install', 'fixture-model', operation_key='explicit-install-retry')
            assert replay['task_id'] == result['task_id'] and len(calls) == 2
            assert runtime.store.receipt('explicit-install-retry')['status'] == 'succeeded'
    finally:
        runtime.close()


async def test_start_receipt_replays_after_restart_and_expired_operation_never_reexecutes(tmp_path, monkeypatch):
    runtime = LocalRuntime(tmp_path / 'workspace')
    calls = []
    async def start(identifier):
        calls.append(identifier)
        return {'status': 'ready', 'model_id': identifier}
    monkeypatch.setattr(runtime, 'start_model', start)
    result = await runtime.model_operation('start', 'qwen3-1.7b-q8_0', operation_key='start-once')
    runtime.close()
    reopened = LocalRuntime(tmp_path / 'workspace')
    try:
        monkeypatch.setattr(reopened, 'start_model', start)
        same = await reopened.model_operation('start', 'qwen3-1.7b-q8_0', operation_key='start-once')
        assert same['task_id'] == result['task_id'] and len(calls) == 1
        assert reopened.models()['runtime']['status'] == 'stopped', 'receipt is historical; current status stays truthful'
        with pytest.raises(ApplicationError) as reused:
            await reopened.model_operation('stop', operation_key='start-once')
        assert reused.value.code == 'idempotency_conflict'
        task, _ = reopened.store.begin_generation('model_install', {'artifact_id': 'fixture'}, 'crash')
        reopened.store.db.execute('UPDATE tasks SET lease_until=? WHERE id=?', (time.time()-1, task['id']))
        assert reopened.store.claim() is None
        assert reopened.store.receipt('crash')['error'] == 'execution_interrupted'
    finally:
        reopened.close()
