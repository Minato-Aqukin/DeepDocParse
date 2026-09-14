#!/usr/bin/env python3
"""One versioned real-model Wiki experiment; every model attempt is retained, never retried."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import platform
import socket
import subprocess
import time
import uuid
from pathlib import Path

from ddp_core.application.ports import ApplicationError
from ddp_local.runtime import LocalRuntime


def fixture():
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    page = canvas.Canvas(output, invariant=True)
    page.setFont('Helvetica', 13)
    for y, text in ((740, 'Aurora Pipeline: Collector module'),
                    (710, 'Collector accepts sensor records in batches of 32.'),
                    (680, 'Collector forwards each batch to Validator for schema checks.'),
                    (650, 'Collector is the input stage of the Aurora Pipeline.')):
        page.drawString(45, y, text)
    page.showPage()
    page.setFont('Helvetica', 13)
    for y, text in ((740, 'Aurora Pipeline: Validator module'),
                    (710, 'Validator receives batches from Collector.'),
                    (680, 'Validator rejects records with a missing timestamp.'),
                    (650, 'Validator sends valid records to the Archive.')):
        page.drawString(45, y, text)
    page.save()
    return output.getvalue()


async def evaluate(args):
    if args.output.exists():
        raise RuntimeError('choose a new report path; previous failures are immutable evidence')
    if socket.if_nameindex() != [(1, 'lo')]:
        raise RuntimeError('run with an isolated loopback-only network namespace')
    subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
    routes = json.loads(subprocess.check_output(['ip', '-j', 'route']))
    if routes:
        raise RuntimeError('external routes are forbidden')
    run_id = uuid.uuid4().hex
    runtime = LocalRuntime(args.workspace)
    data = fixture()
    report = {'schema': 'ddp-local-wiki-eval/1', 'run_id': run_id, 'python': platform.python_version(),
              'platform': platform.platform(), 'net_namespace': os.readlink('/proc/self/ns/net'),
              'interfaces': socket.if_nameindex(), 'ipv4_routes': routes, 'started_at': time.time(),
              'source_sha256': hashlib.sha256(data).hexdigest(), 'status': 'running'}
    task_id = None
    try:
        await runtime.start_model('qwen3-1.7b-q8_0')
        report['provider'] = runtime.provider.model.provenance
        task = runtime.upload_stream(io.BytesIO(data), filename='aurora-wiki-source.pdf', operation_key='wiki-eval-source-v1')
        if runtime.store.task(task['id'])['status'] != 'succeeded':
            await runtime.work_once()
        version = runtime.store.version(task['version_id'])
        report['source'] = runtime.source(version)
        body = {'title': 'Aurora Pipeline: Collector and Validator',
                'sources': [{'resource_id': version['resource_id'], 'source_version_id': version['id']}],
                'max_pages': 2, 'max_output_tokens': 4096, 'execution_policy': 'local_only', 'allow_remote': False}
        report['request'] = body
        key = 'wiki-eval-' + run_id
        began = time.monotonic()
        try:
            built = await runtime.build_wiki(body, operation_key=key)
        finally:
            task_id = runtime.store.receipt(key)['id']
        report['build_seconds'] = time.monotonic() - began
        report['build'] = built
        revision = built['revision']
        evidence = {d['evidence_id'] for d in revision['dependency_manifest']}
        supported = all(set(c['evidence_ids']).issubset(evidence) and c['evidence_ids'] and not c['unsupported']
            for p in revision['pages'] for s in p['generated_sections'] for c in s['sentences'])
        relations = bool(revision['relations']) and all(r['evidence_ids'] and set(r['evidence_ids']).issubset(evidence)
                                                        for r in revision['relations'])
        originals = all(d['original']['source_type'] == 'source' and d['original']['locator']['bbox']
                        for d in revision['dependency_manifest'])
        if not (len(revision['pages']) == 2 and supported and relations and originals):
            raise RuntimeError('actual output did not satisfy two pages, supported relation and original bbox acceptance')
        wid, page = built['wiki']['id'], revision['pages'][0]['page_key']
        edited = await runtime.edit_wiki(wid, page, {'base_revision_id': revision['id'],
            'paragraphs': [{'id': 'review-note', 'text': 'Operator note: semantic review remains pending.'}]},
            operation_key='wiki-edit-' + run_id)
        report['edit'] = edited
        try:
            await runtime.edit_wiki(wid, page, {'base_revision_id': revision['id'], 'paragraphs': []},
                                    operation_key='wiki-stale-edit-' + run_id)
        except ApplicationError as exc:
            report['concurrent_edit_rejection'] = exc.code
        else:
            raise RuntimeError('stale editor unexpectedly overwrote the new revision')
        await runtime.stop_model()
        runtime.close()
        runtime = LocalRuntime(args.workspace)
        reopened = runtime.wikis.get(wid)
        report['restart'] = {'revision_id': reopened['revision']['id'],
            'original_revision_preserved': runtime.wikis.get(wid, revision['id'])['revision']['id'] == revision['id'],
            'human_paragraph_preserved': bool(reopened['revision']['pages'][0]['human_paragraphs']),
            'build_task_state': runtime.store.receipt(key)['status']}
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error=getattr(exc, 'code', type(exc).__name__), message=str(exc))
    finally:
        if task_id:
            report['model_attempts'] = [dict(row) for row in runtime.store.db.execute(
                'SELECT * FROM wiki_attempts WHERE task_id=? ORDER BY created_at,id', (task_id,))]
        await runtime.stop_model()
        runtime.close()
        report['finished_at'] = time.time()
        args.output.parent.mkdir(exist_ok=True, parents=True)
        with args.output.open('x') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write('\n')
    print(json.dumps({'status': report['status'], 'error': report.get('error'), 'report': str(args.output)}))
    return 0 if report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--output', required=True, type=Path)
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))


if __name__ == '__main__':
    main()
