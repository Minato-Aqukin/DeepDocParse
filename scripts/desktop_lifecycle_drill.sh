#!/usr/bin/env bash
# Private-prefix lifecycle exercise. Requires the real desktop build inputs,
# a display and already downloaded, catalog-pinned model/runtime artifacts.
# Both package versions are built from this checkout, not historical releases.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export DDP_LIFECYCLE_ROOT="$ROOT"
exec "${PY:-$ROOT/.venv/bin/python}" - "$@" <<'PY'
import argparse
import asyncio
import fcntl
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ['DDP_LIFECYCLE_ROOT']).resolve()
parser = argparse.ArgumentParser(description='Real packaged desktop install/update/rollback/uninstall in a private prefix')
parser.add_argument('--keep', action='store_true', default=os.environ.get('DDP_DRILL_KEEP') == '1')
parser.add_argument('--model-cache', type=Path, default=os.environ.get('DDP_DRILL_MODEL_CACHE'))
parser.add_argument('--model-id')
parser.add_argument('--runtime-id')
args = parser.parse_args()
if not args.model_cache or not args.model_cache.is_dir():
    parser.error('--model-cache must name existing catalog-pinned model/runtime artifacts; no downloads or fake weights are used')
if not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')):
    parser.error('a real Wayland/X11 display is required for the packaged App exercise')
os.umask(0o077)
WORK = Path(tempfile.mkdtemp(prefix='ddp-lifecycle-drill.'))
INSTALL = WORK / 'opt' / 'deepdocparse'
WORKSPACE = WORK / 'workspaces' / 'default'
BACKUPS = WORK / 'backups'
REPORT = WORK / 'lifecycle-report.json'
PDF = ROOT / 'tests' / 'fixtures' / 'sample.pdf'
report = {'status': 'running', 'scope': 'private Linux prefix; two versions built from the same working tree',
          'signing': 'ephemeral drill Ed25519 key, not a production release identity',
          'not_covered': ['Windows host', 'historically published binaries', 'production traffic'],
          'checks': [], 'logs': [], 'work': str(WORK)}
current = 'initializing'


def save():
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def phase(name):
    global current
    current = name
    print(f'>>> {name}', flush=True)
    save()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(label, command, *, environment=None, success=True, timeout=600):
    log = WORK / (label + '.log')
    with log.open('wb') as output:
        result = subprocess.run([str(value) for value in command], cwd=ROOT,
                                env=environment, stdout=output, stderr=subprocess.STDOUT,
                                timeout=timeout)
    report['logs'].append({'step': label, 'exit_code': result.returncode, 'path': str(log)})
    save()
    require((result.returncode == 0) == success,
            f'{label}: unexpected exit {result.returncode}; see {log}')
    return result


def checked(name, **details):
    report['checks'].append({'name': name, **details})
    save()


def update(label, *arguments, success=True):
    return run(label, [sys.executable, ROOT / 'scripts/update_check.py', *arguments], success=success)


def version():
    return json.loads((INSTALL / 'RELEASE-MANIFEST.json').read_text())['version']


def sqlite_rows(directory):
    with sqlite3.connect(f'file:{directory / "workspace.sqlite3"}?mode=ro', uri=True) as connection:
        return connection.execute('SELECT id, resource_id, source_digest, state FROM versions ORDER BY id').fetchall()


def packaged_app(label, expected_version, model, runtime_id):
    directory = WORK / label
    directory.mkdir(mode=0o700)
    environment = dict(os.environ)
    environment.pop('ELECTRON_RUN_AS_NODE', None)
    environment.update(DDP_DESKTOP_SMOKE='1', DDP_DESKTOP_SMOKE_DIRECTORY=str(directory),
                       DDP_DESKTOP_SMOKE_WORKSPACE=str(WORKSPACE), DDP_DESKTOP_SMOKE_PDF=str(PDF),
                       DDP_DESKTOP_SMOKE_MODEL=model, DDP_DESKTOP_SMOKE_RUNTIME=runtime_id)
    display = '--ozone-platform=wayland' if environment.get('WAYLAND_DISPLAY') else '--ozone-platform=x11'
    run(label, [INSTALL / 'deepdocparse', '--smoke', display], environment=environment, timeout=240)
    value = json.loads((directory / 'report.json').read_text())
    require(value['application'] == {'version': expected_version, 'packaged': True}, 'did not execute the expected packaged App')
    require(value['renderer']['nodeRequire'] == value['renderer']['nodeProcess'] == 'undefined', 'renderer escaped isolation')
    require(value['webPreferences'] == {'sandbox': True, 'contextIsolation': True, 'nodeIntegration': False}, 'unsafe BrowserWindow')
    require(value['ready']['state'] == value['resumed']['state'] == 'ready', 'packaged local runtime did not recover')
    require(value['ready']['identity'] == value['resumed']['identity'], 'suspend/resume changed workspace identity')
    require(value['suspended']['state'] == value['stopped']['state'] == 'stopped', 'owned runtime did not shut down')
    require(value['sharedClient']['pdfRendered'], 'packaged App did not render the original PDF')
    require(value['sharedClient']['fileResults']['exported']['value']['saved'], 'packaged bundle export failed')
    require([case['operation'] for case in value['sharedClient']['modelResults']['cases']] == ['answer.generate', 'wiki.create'],
            'real model answer/versioned Wiki were not exercised')
    checked(label, version=expected_version, report=str(directory / 'report.json'), screenshot=str(directory / 'desktop.png'))
    return value


try:
    phase('building two actual directory packages')
    packages = {}
    for release in ('0.1.0', '0.1.1'):
        output = WORK / 'dist' / release
        run('build-' + release, [sys.executable, ROOT / 'scripts/build_desktop.py', '--version', release, '--output', output])
        stem = 'deepdocparse-' + release + '-linux-x64'
        packages[release] = {'directory': output / stem, 'archive': output / (stem + '.tar.gz'),
                             'manifest': output / (stem + '.release.json')}
        run('verify-directory-' + release, [sys.executable, ROOT / 'scripts/build_desktop.py', '--verify', output / stem])
    INSTALL.parent.mkdir(parents=True)
    shutil.copytree(packages['0.1.0']['directory'], INSTALL)
    require(version() == '0.1.0', 'initial installation version differs')

    phase('signing and verifying the real update with a private drill key')
    key = WORK / 'drill-key'
    run('create-private-signing-key', ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'ddp-lifecycle-drill', '-f', key])
    signers = WORK / 'allowed-signers'
    signers.write_text('ddp-lifecycle-drill ' + key.with_suffix('.pub').read_text())
    upgrade = packages['0.1.1']
    update('sign-update', 'sign', '--key', key, '--manifest', upgrade['manifest'], '--identity', 'ddp-lifecycle-drill')
    verification = ['--manifest', upgrade['manifest'], '--archive', upgrade['archive'], '--allowed-signers', signers]
    update('verify-signed-update', 'verify', '--root', INSTALL, *verification)
    checked('signed_archive_verified', manifest=str(upgrade['manifest']))

    # Import the installed distribution, not the checkout's editable ddp_local.
    sys.path.insert(0, str(INSTALL / 'resources/runtime/site-packages'))
    from ddp_local.runtime import LocalRuntime
    from ddp_local.store import DDL
    from ddp_local import wiki_store
    spec = importlib.util.spec_from_file_location('lifecycle_update_check', ROOT / 'scripts/update_check.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)

    phase('creating actual workspace data and importing verified model artifacts')
    runtime = LocalRuntime(WORKSPACE)
    try:
        catalog = runtime.model_installer.definitions['artifacts']
        choices = [item for item in catalog if item['kind'] == 'model' and
                   (args.model_cache / runtime.model_installer.name(item)).is_file()]
        model = next((item for item in choices if not args.model_id or item['id'] == args.model_id), None)
        require(model is not None, 'selected model is absent from the reviewed cache')
        model_id = model['id']
        runtime_id = args.runtime_id or model['runtime_id']
        require(runtime_id in model.get('runtime_ids', [model['runtime_id']]), 'runtime does not belong to the selected model')
        for identifier in (model_id, runtime_id):
            artifact = runtime.model_installer.artifact(identifier)
            print(f'    importing {identifier}: {artifact["bytes"]} bytes; verifying publisher SHA-256', flush=True)
            installed = runtime.model_installer.import_file(identifier, args.model_cache / runtime.model_installer.name(artifact))
            require(installed['status'] == 'installed', 'artifact did not pass the real installer')
        queued = runtime.upload_file(str(PDF), operation_key='lifecycle-queued-parse')
        require(queued['status'] == 'queued', 'real parse task was not queued')
        workspace_id = runtime.store.workspace_id
    finally:
        runtime.close()
    models = WORKSPACE / 'models'
    apply = ['apply', '--root', INSTALL, *verification, '--workspace', WORKSPACE,
             '--models', models, '--backup-dir', BACKUPS]
    update('queued-task-blocks-upgrade', *apply, success=False)
    require(version() == '0.1.0' and not INSTALL.with_name(INSTALL.name + '.previous').exists(), 'refused upgrade mutated installation')
    runtime = LocalRuntime(WORKSPACE)
    try:
        runtime.store.cancel(queued['id'])
        require(runtime.store.task(queued['id'])['status'] == 'cancelled', 'runtime cancellation did not persist')
        update('idle-runtime-blocks-upgrade', *apply, '--allow-active', success=False)
        require(version() == '0.1.0', 'idle runtime barrier failed')
    finally:
        runtime.close()
    checked('queued_and_idle_runtime_barriers')

    phase('launching the installed 0.1.0 App, actual model answer and Wiki')
    packaged_app('app-installed-010', '0.1.0', model_id, runtime_id)
    before = sqlite_rows(WORKSPACE)
    # Starting a model legitimately refreshes verification metadata; maintenance
    # itself must preserve both pinned bytes and the current metadata snapshot.
    model_digest = updater.tree_digest(models)

    phase('rejecting tampered bytes before swapping the application')
    tampered = WORK / 'tampered.tar.gz'
    shutil.copy2(upgrade['archive'], tampered)
    with tampered.open('r+b') as stream:
        stream.seek(tampered.stat().st_size // 2)
        byte = stream.read(1)
        stream.seek(-1, 1)
        stream.write(bytes([byte[0] ^ 0xff]))
    update('tampered-archive-refused', 'apply', '--root', INSTALL, '--manifest', upgrade['manifest'],
           '--archive', tampered, '--allowed-signers', signers, '--workspace', WORKSPACE,
           '--models', models, '--backup-dir', BACKUPS, success=False)
    require(version() == '0.1.0' and sqlite_rows(WORKSPACE) == before, 'tampered update changed code or data')
    checked('tampered_archive_refused_before_swap')

    phase('upgrading to 0.1.1 with actual SQLite backups and preserved model bytes')
    update('apply-011', *apply)
    require(version() == '0.1.1' and sqlite_rows(WORKSPACE) == before, 'upgrade changed existing versions')
    state = json.loads((INSTALL / 'UPDATE-STATE.json').read_text())
    backup = state['workspace_backup']
    for entry in backup['workspaces']:
        for item in entry['files']:
            if item['name'] in updater.WORKSPACE_DATABASES:
                updater.verify_workspace_backup_file(Path(backup['directory']) / entry['backup'] / item['name'], item['sha256'])
    require(updater.tree_digest(models) == model_digest, 'upgrade changed the real model cache')
    packaged_app('app-upgraded-011', '0.1.1', model_id, runtime_id)
    runtime = LocalRuntime(WORKSPACE)
    try:
        require(runtime.store.workspace_id == workspace_id, 'upgrade changed workspace identity')
        post = runtime.upload_file(str(PDF), operation_key='lifecycle-post-upgrade-write')
        completed = asyncio.run(runtime.work_once())
        require(completed['id'] == post['id'] and completed['status'] == 'succeeded', 'post-upgrade parse failed')
    finally:
        runtime.close()
    checked('upgrade_preserves_data_models_and_runs_new_binary', backup=backup['directory'])

    phase('rolling back the binary while retaining post-upgrade user data')
    preserved = sqlite_rows(WORKSPACE)
    update('rollback-keep-data', 'rollback', '--root', INSTALL, '--workspace', WORKSPACE)
    require(version() == '0.1.0' and sqlite_rows(WORKSPACE) == preserved, 'default rollback discarded user writes')
    packaged_app('app-rolled-back-010', '0.1.0', model_id, runtime_id)
    checked('default_rollback_keeps_post_upgrade_writes', retained_version=post['version_id'])

    phase('restoring only the explicitly selected second pre-upgrade snapshot')
    second_backups = WORK / 'backups-second'
    second_apply = [*apply[:-1], second_backups]
    snapshot = sqlite_rows(WORKSPACE)
    update('apply-011-again', *second_apply)
    runtime = LocalRuntime(WORKSPACE)
    try:
        later = runtime.upload_file(str(PDF), operation_key='lifecycle-after-second-backup')
        completed = asyncio.run(runtime.work_once())
        require(completed['id'] == later['id'] and completed['status'] == 'succeeded', 'second user write failed')
    finally:
        runtime.close()
    update('rollback-explicit-data-restore', 'rollback', '--root', INSTALL,
           '--workspace', WORKSPACE, '--restore-workspaces')
    require(sqlite_rows(WORKSPACE) == snapshot, 'explicit restore did not recover its own pre-upgrade snapshot')
    require(any(row[0] == post['version_id'] for row in snapshot), 'second snapshot lost earlier user writes')
    checked('explicit_restore_uses_correct_backup', removed_version=later['version_id'], retained_version=post['version_id'])

    phase('running the actual v1-to-v2 migration with a failing final statement')
    legacy = WORK / 'legacy-v1'
    legacy.mkdir(mode=0o700)
    # Canonical v1 schema plus actual application-created rows. No miniature or
    # substitute schema is used; v2 adds only the Wiki tables.
    with sqlite3.connect(legacy / 'workspace.sqlite3') as connection:
        connection.executescript(DDL)
        connection.execute('ATTACH DATABASE ? AS current_workspace', (str(WORKSPACE / 'workspace.sqlite3'),))
        for table in ('metadata', 'resources', 'versions', 'tasks', 'evidence', 'evidence_fts', 'events', 'outputs'):
            connection.execute(f'INSERT INTO main.{table} SELECT * FROM current_workspace.{table}')
    shutil.copytree(WORKSPACE / 'blobs', legacy / 'blobs')
    legacy_before = sqlite_rows(legacy)
    original_schema = wiki_store.SCHEMA
    wiki_store.SCHEMA += '\nINTENTIONALLY INVALID MIGRATION STATEMENT;\n'
    try:
        try:
            unexpected = LocalRuntime(legacy)
        except sqlite3.OperationalError:
            pass
        else:
            unexpected.close()
            raise RuntimeError('injected migration unexpectedly succeeded')
    finally:
        wiki_store.SCHEMA = original_schema
    require(updater._sqlite_user_version(legacy / 'workspace.sqlite3') == 1, 'failed migration changed schema version')
    require(sqlite_rows(legacy) == legacy_before, 'failed migration changed source data')
    with (legacy / '.runtime.lock').open('r+b') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    recovered = LocalRuntime(legacy)
    try:
        require(updater._sqlite_user_version(legacy / 'workspace.sqlite3') == 2, 'real migration did not recover')
        require(sqlite_rows(legacy) == legacy_before, 'successful migration rewrote source identities')
        require(any('42' in hit['text'] for hit in recovered.search('contract')['hits']), 'migrated evidence is not searchable')
    finally:
        recovered.close()
    checked('actual_sqlite_migration_failure_and_recovery')

    phase('uninstalling without deleting data, backups, models or a foreign process')
    foreign = INSTALL.with_name(INSTALL.name + '.staging-foreign')
    foreign.mkdir()
    (foreign / 'owner.txt').write_text('not an application-owned directory')
    user_rows = sqlite_rows(WORKSPACE)
    uninstall_model_digest = updater.tree_digest(models)
    foreign_process = subprocess.Popen(['sleep', '300'])
    try:
        update('uninstall-application-only', 'uninstall', '--root', INSTALL,
               '--workspace', WORKSPACE, '--models', models)
        require(foreign_process.poll() is None, 'uninstall terminated an unrelated owned probe process')
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=10)
    require(not INSTALL.exists(), 'uninstall left the application tree')
    require(sqlite_rows(WORKSPACE) == user_rows, 'uninstall deleted workspace state')
    require(updater.tree_digest(models) == uninstall_model_digest, 'uninstall changed actual model bytes')
    require(Path(backup['directory']).is_dir(), 'default uninstall deleted backups')
    require((foreign / 'owner.txt').read_text() == 'not an application-owned directory', 'uninstall claimed a foreign neighbour')
    checked('uninstall_preserves_workspace_models_backups_and_foreign_process', models_sha256=uninstall_model_digest)
    report['status'] = 'passed'
    save()
    print(json.dumps({'status': 'passed', 'checks': report['checks'], 'report': str(REPORT), 'kept': args.keep}, ensure_ascii=False, indent=2))
    if not args.keep:
        shutil.rmtree(WORK)
except BaseException as exc:
    report.update(status='failed', failed_step=current, failure=f'{type(exc).__name__}: {exc}')
    save()
    print(json.dumps({'status': 'failed', 'step': current, 'report': str(REPORT)}, ensure_ascii=False), file=sys.stderr)
    raise
PY
