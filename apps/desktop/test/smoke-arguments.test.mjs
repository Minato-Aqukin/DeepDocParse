import test from 'node:test'
import assert from 'node:assert/strict'
import { parseArguments } from '../scripts/smoke-windows.mjs'

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
