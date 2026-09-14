import { createHash, randomUUID } from 'node:crypto'
import { constants } from 'node:fs'
import { mkdir, lstat, open, rename, unlink } from 'node:fs/promises'
import path from 'node:path'
import { HostError, identity, validate } from './policy.mjs'
import { credentialBackend, secureDirectory, secureFile } from './platform.mjs'

const FILESYSTEM = Object.freeze({ mkdir, lstat, open, rename, unlink })

export class CredentialBroker {
  #secrets = new Map()
  #queues = new Map()
  #sessionGeneration = 0
  #files
  constructor({ directory, safeStorage, platform = process.platform, fileSystem = FILESYSTEM }) {
    this.directory = directory
    this.safeStorage = safeStorage
    this.platform = platform
    this.#files = fileSystem
  }
  policy() { return credentialBackend(this.safeStorage, this.platform) }
  #key(pair) { return createHash('sha256').update(JSON.stringify(identity(pair))).digest('hex') }
  #serialize(key, operation) {
    const generation = this.#sessionGeneration
    const previous = this.#queues.get(key) ?? Promise.resolve()
    const result = previous.then(async () => {
      if (generation !== this.#sessionGeneration) throw new HostError('credential_unavailable')
      try {
        const value = await operation()
        if (generation !== this.#sessionGeneration) throw new HostError('credential_unavailable')
        return value
      } finally {
        // Also fence a load/set whose cleanup fails after clearSession invalidated it.
        if (generation !== this.#sessionGeneration) this.#secrets.delete(key)
      }
    })
    // A failed operation must not poison later clear/set calls for this identity.
    const settled = result.then(() => {}, () => {})
    this.#queues.set(key, settled)
    void settled.then(() => { if (this.#queues.get(key) === settled) this.#queues.delete(key) })
    return result
  }
  #file(key) { return path.join(this.directory, `${key}.json`) }
  async #directory() {
    await secureDirectory(this.directory, { platform: this.platform,
      code: 'unsafe_credential_directory', fileSystem: this.#files })
  }
  async #remove(key) {
    try { await this.#files.unlink(this.#file(key)) } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  set(input) {
    const value = validate('setCredential', input)
    const pair = { environmentId: value.environmentId, profileId: value.profileId }
    return this.#serialize(this.#key(pair), () => this.#set(value))
  }
  async #set(input) {
    const { secret, persist, ...pair } = validate('setCredential', input)
    const key = this.#key(pair), policy = this.policy()
    await this.#directory()
    if (!persist || !policy.persistentAvailable) {
      // A replaced credential must never resurrect from an older persistent copy.
      await this.#remove(key)
      this.#secrets.set(key, { secret, mode: 'session' })
      return { ...policy, present: true, mode: 'session', reason: persist ? policy.reason : 'session_requested' }
    }
    let encrypted
    try { encrypted = this.safeStorage.encryptString(JSON.stringify({ ...pair, secret })) }
    catch {
      await this.#remove(key)
      this.#secrets.set(key, { secret, mode: 'session' })
      return { ...policy, present: true, mode: 'session', reason: 'session_only_encryption_failed' }
    }
    const temporary = path.join(this.directory, `${randomUUID()}.tmp`)
    const handle = await this.#files.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    try {
      await handle.writeFile(JSON.stringify({ version: 1, data: encrypted.toString('base64') }))
      await handle.sync()
    } finally { await handle.close() }
    try { await this.#files.rename(temporary, this.#file(key)) }
    finally { await this.#files.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error }) }
    this.#secrets.set(key, { secret, mode: 'persistent' })
    return { ...policy, present: true, mode: 'persistent' }
  }
  async #load(pair) {
    const key = this.#key(pair)
    if (this.#secrets.has(key)) return this.#secrets.get(key)
    if (!this.policy().persistentAvailable) return undefined
    await this.#directory()
    let file
    try {
      await secureFile(this.#file(key), { platform: this.platform, maxBytes: 131072,
        code: 'credential_unavailable', fileSystem: this.#files })
      file = await this.#files.open(this.#file(key), constants.O_RDONLY | constants.O_NOFOLLOW)
    } catch (error) {
      if (error.code === 'ENOENT') return undefined
      throw new HostError('credential_unavailable')
    }
    try {
      const encoded = JSON.parse(await file.readFile('utf8'))
      if (encoded.version !== 1 || typeof encoded.data !== 'string') throw new Error('invalid')
      const decoded = JSON.parse(this.safeStorage.decryptString(Buffer.from(encoded.data, 'base64')))
      if (decoded.environmentId !== pair.environmentId || decoded.profileId !== pair.profileId) throw new Error('identity')
      validate('setCredential', { ...decoded, persist: true })
      const value = { secret: decoded.secret, mode: 'persistent' }
      this.#secrets.set(key, value)
      return value
    } catch { throw new HostError('credential_unavailable') }
    finally { await file.close() }
  }
  status(pair) {
    const selected = identity(pair)
    return this.#serialize(this.#key(selected), async () => {
      const value = await this.#load(selected)
      return { ...this.policy(), present: Boolean(value), mode: value?.mode ?? 'absent' }
    })
  }
  clear(pair) {
    const selected = identity(pair)
    return this.#serialize(this.#key(selected), () => this.#clear(selected))
  }
  async #clear(pair) {
    const key = this.#key(pair)
    this.#secrets.delete(key)
    await this.#directory()
    await this.#remove(key)
    return { present: false, mode: 'absent', ...this.policy() }
  }
  withCredential(pair, operation) {
    const selected = identity(pair)
    return this.#serialize(this.#key(selected), async () => {
      const value = await this.#load(selected)
      if (!value) throw new HostError('credential_required')
      return operation(value.secret)
    })
  }
  clearSession() { this.#sessionGeneration++; this.#secrets.clear() }
}
