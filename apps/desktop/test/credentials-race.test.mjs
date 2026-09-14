import test from 'node:test'
import assert from 'node:assert/strict'
import * as fs from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { CredentialBroker } from '../src/credentials.mjs'

const pair = { environmentId: 'race-env', profileId: 'race-profile' }
// Only the approved-backend control path is mocked; every filesystem operation is real.
const safeStorage = {
  getSelectedStorageBackend: () => 'gnome_libsecret', isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(value), decryptString: value => value.toString(),
}
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'ddp-credential-race-'))
  t.after(() => fs.rm(directory, { recursive: true, force: true }))
  await new CredentialBroker({ directory, safeStorage }).set({ ...pair, secret: 'synthetic-old', persist: true })
  const entered = deferred(), release = deferred()
  let paused = false, intercepted = false, overlapped = 0, reads = 0
  const fileSystem = {
    ...fs,
    mkdir(...args) {
      // This synchronous observation makes the ordering check deterministic: old
      // clear/set enters mkdir immediately while its older read is deliberately held.
      if (paused) overlapped++
      return fs.mkdir(...args)
    },
    async open(...args) {
      const file = await fs.open(...args)
      return new Proxy(file, { get(target, name) {
        if (name === 'readFile') return async (...readArgs) => {
          const value = await target.readFile(...readArgs)
          reads++
          if (!intercepted) {
            intercepted = true; paused = true; entered.resolve()
            await release.promise; paused = false
          }
          return value
        }
        const value = Reflect.get(target, name, target)
        return typeof value === 'function' ? value.bind(target) : value
      } })
    },
  }
  return { directory, reader: new CredentialBroker({ directory, safeStorage, fileSystem }),
    entered: entered.promise, release: release.resolve, overlap: () => overlapped, reads: () => reads }
}

for (const operation of ['status', 'withCredential']) {
  test(`completed clear cannot resurrect an older paused ${operation} disk load`, async t => {
    const gate = await fixture(t)
    let cleared = false
    const pending = operation === 'status' ? gate.reader.status(pair)
      : gate.reader.withCredential(pair, value => { assert.equal(cleared, false); return value })
    await gate.entered
    const clearing = gate.reader.clear(pair).then(value => { cleared = true; return value })
    try { assert.equal(gate.overlap(), 0, 'same-identity clear must wait for the older read/use') }
    finally { gate.release(); await Promise.allSettled([pending, clearing]) }
    await pending
    assert.equal((await clearing).present, false)
    assert.equal((await gate.reader.status(pair)).present, false)
    await assert.rejects(gate.reader.withCredential(pair, () => assert.fail('cleared key used')), /credential_required/)
    assert.deepEqual(await fs.readdir(gate.directory), [])
  })
}

for (const persist of [false, true]) {
  test(`older paused read cannot overwrite a newer ${persist ? 'persistent' : 'session'} replacement`, async t => {
    const gate = await fixture(t), pending = gate.reader.status(pair)
    await gate.entered
    const updating = gate.reader.set({ ...pair, secret: 'synthetic-new', persist })
    try { assert.equal(gate.overlap(), 0, 'same-identity replacement must wait for the older read') }
    finally { gate.release(); await Promise.allSettled([pending, updating]) }
    await pending; await updating
    assert.equal(await gate.reader.withCredential(pair, value => value), 'synthetic-new')
    const reopened = new CredentialBroker({ directory: gate.directory, safeStorage })
    assert.equal((await reopened.status(pair)).present, persist)
    if (persist) assert.equal(await reopened.withCredential(pair, value => value), 'synthetic-new')
  })
}

test('synchronous session clear fences an older disk read before it can refill the memory cache', async t => {
  const gate = await fixture(t), pending = gate.reader.status(pair)
  const rejected = assert.rejects(pending, /credential_unavailable/)
  await gate.entered
  gate.reader.clearSession(); gate.release()
  await rejected
  // Session clear preserves approved ciphertext, but the next read must reload it.
  assert.equal((await gate.reader.status(pair)).present, true)
  assert.equal(gate.reads(), 2)
})

test('one identity waiting on disk does not block another profile and failures do not poison the queue', async t => {
  const gate = await fixture(t), pending = gate.reader.status(pair)
  await gate.entered
  const other = { ...pair, profileId: 'other-profile' }
  try {
    await gate.reader.set({ ...other, secret: 'other-synthetic', persist: false })
    assert.equal(await gate.reader.withCredential(other, value => value), 'other-synthetic')
  } finally { gate.release(); await pending }
  await assert.rejects(gate.reader.withCredential(pair, () => { throw new Error('consumer failed') }), /consumer failed/)
  assert.equal((await gate.reader.clear(pair)).present, false)
})

test('independent review real-filesystem race has zero resurrection in 100 attempts', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'ddp-credential-repeat-'))
  t.after(() => fs.rm(directory, { recursive: true, force: true }))
  let resurrected = 0
  for (let i = 0; i < 100; i++) {
    await new CredentialBroker({ directory, safeStorage }).set({ ...pair, secret: 'synthetic-only', persist: true })
    const reader = new CredentialBroker({ directory, safeStorage })
    const pending = reader.status(pair)
    await reader.clear(pair); await pending
    if ((await reader.status(pair)).present) resurrected++
  }
  assert.equal(resurrected, 0)
})
