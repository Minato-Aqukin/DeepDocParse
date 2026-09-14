import test from 'node:test'
import assert from 'node:assert/strict'
import { parseArguments, assertReport } from '../scripts/smoke-windows.mjs'

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
