import test from 'node:test'
import assert from 'node:assert/strict'
import { parseArguments, assertReport } from '../scripts/smoke-windows.mjs'
import { HostError, WSL_HOST_UNAVAILABLE_CODES, smokeLocalRuntimeFailure } from '../src/policy.mjs'

test('smoke-windows accepts both --opt=value and --opt value, and rejects unknown flags', () => {
  const spaced = parseArguments(['C:/apps/deepdocparse.exe', '--timeout', '5000',
    '--report', 'C:/tmp/report.json'])
  assert.equal(spaced.timeoutMs, 5000)
  assert.equal(spaced.report.endsWith('report.json'), true)
  assert.equal(spaced.target.endsWith('deepdocparse.exe'), true)

  const equals = parseArguments(['--timeout=7000', '--report=C:/tmp/equal.json'])
  assert.equal(equals.timeoutMs, 7000)
  assert.equal(equals.report.endsWith('equal.json'), true)

  assert.throws(() => parseArguments(['--timeout']), /needs a value/)
  assert.throws(() => parseArguments(['--timeout', '--host']), /needs a value/)
  assert.throws(() => parseArguments(['--timeout', 'zero']), /positive number/)
  assert.throws(() => parseArguments(['--nope']), /unknown option/)
  assert.throws(() => parseArguments(['a', 'b']), /at most one TARGET/)
})

const COMMON = {
  renderer: { nodeRequire: 'undefined', nodeProcess: 'undefined', rendered: 1,
    methods: ['hostStatus'], host: { ok: true }, rejected: { ok: false } },
  webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  display: { session: null },
}

test('smoke report accepts a full local run and a Tier A unavailable run', () => {
  assertReport({ ...COMMON, localRuntime: { state: 'ready', reason: null },
    ready: { state: 'ready', identity: { environment_id: 'local', workspace_id: 'ws' } },
    suspended: { state: 'stopped' }, resumed: { state: 'ready', identity: { environment_id: 'local', workspace_id: 'ws' } },
    stopped: { state: 'stopped' },
    sharedClient: { pdfRendered: true, fileResults: { originalBytes: 10,
      exported: { value: { saved: true } } } } })

  assertReport({ ...COMMON, localRuntime: { state: 'unavailable', reason: 'wsl_unavailable' },
    ready: null, suspended: null, resumed: null, stopped: { state: 'stopped' }, sharedClient: null })
})

test('smoke report rejects a non-WSL local failure and a truncated ready report', () => {
  assert.throws(() => assertReport({ ...COMMON,
    localRuntime: { state: 'unavailable', reason: 'host_operation_failed' },
    sharedClient: null }), /unexpected local runtime reason/)
  assert.throws(() => assertReport({ ...COMMON,
    localRuntime: { state: 'ready', reason: null },
    ready: { state: 'ready', identity: {} }, suspended: { state: 'stopped' },
    resumed: { state: 'ready', identity: {} }, stopped: { state: 'stopped' },
    sharedClient: { pdfRendered: false, fileResults: { originalBytes: 0,
      exported: { value: { saved: true } } } } }))
})

test('a broken WSL package or runtime is a smoke failure, never "WSL unavailable"', () => {
  // Package/runtime defects: the old accept-list contained these, so a wrong
  // tarball or ABI passed CI as if the runner simply had no WSL.
  for (const reason of ['wsl_backend_unavailable', 'wsl_runtime_archive_mismatch',
    'wsl_runtime_abi_mismatch', 'wsl_runtime_provision_failed', 'wsl_runtime_not_installed']) {
    assert.equal(WSL_HOST_UNAVAILABLE_CODES.has(reason), false, reason)
    assert.equal(smokeLocalRuntimeFailure(new HostError(reason), { kind: 'wsl', connected: false }),
      null, `${reason} must fail the smoke`)
    assert.throws(() => assertReport({ ...COMMON, localRuntime: { state: 'unavailable', reason },
      ready: null, sharedClient: null }), /unexpected local runtime reason/, reason)
  }
  // Host capability codes before connecting are the honest Tier A state.
  for (const reason of ['wsl_missing', 'wsl1_unsupported', 'wsl_distro_not_found', 'wsl_unavailable']) {
    assert.deepEqual(smokeLocalRuntimeFailure(new HostError(reason), { kind: 'wsl', connected: false }),
      { state: 'unavailable', reason })
  }
  // After a successful connect (suspend/resume), even a capability code is a regression.
  assert.equal(smokeLocalRuntimeFailure(new HostError('wsl_unavailable'), { kind: 'wsl', connected: true }), null)
  // Native hosts never get the WSL exemption; non-HostErrors never qualify.
  assert.equal(smokeLocalRuntimeFailure(new HostError('wsl_unavailable'), { kind: 'native', connected: false }), null)
  assert.equal(smokeLocalRuntimeFailure(new Error('wsl_unavailable'), { kind: 'wsl', connected: false }), null)
  // An "unavailable" report cannot also carry a ready runtime.
  assert.throws(() => assertReport({ ...COMMON, localRuntime: { state: 'unavailable', reason: 'wsl_unavailable' },
    ready: { state: 'ready' }, sharedClient: null }))
})
