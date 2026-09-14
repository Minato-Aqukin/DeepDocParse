import { randomUUID } from 'node:crypto'
import { lstat, realpath } from 'node:fs/promises'
import path from 'node:path'
import { HostError } from './policy.mjs'

const FILESYSTEM = Object.freeze({ lstat, realpath })
const WSL_DIRECTORY = /^[A-Za-z0-9._~/-]+$/
export class WorkspaceHandles {
  #entries = new Map()
  #platform
  #files
  #path
  constructor({ platform = process.platform, fileSystem = FILESYSTEM } = {}) {
    this.#platform = platform
    this.#files = fileSystem
    this.#path = platform === 'win32' ? path.win32 : path
  }
  // Windows paths are case-insensitive and report dev/ino 0 on some filesystems;
  // identity there is the canonical path folded to lower case, not a case-sensitive
  // POSIX string. POSIX keeps the exact canonical string.
  #identity(canonical) { return this.#platform === 'win32' ? canonical.toLowerCase() : canonical }
  async selectedByNativeDialog(directory) {
    if (typeof directory !== 'string' || !this.#path.isAbsolute(directory)) throw new HostError('invalid_workspace')
    const stat = await this.#files.lstat(directory)
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new HostError('invalid_workspace')
    const canonical = await this.#files.realpath(directory)
    const identity = this.#identity(canonical)
    const existing = [...this.#entries.values()].find(entry => entry.identity === identity)
    if (existing) return this.public(existing.id)
    const entry = { id: randomUUID(), directory: canonical, identity, device: stat.dev, inode: stat.ino }
    this.#entries.set(entry.id, entry)
    return this.public(entry.id)
  }
  selectedWsl({ directory, name } = {}) {
    if (typeof directory !== 'string' || !WSL_DIRECTORY.test(directory)
        || !(directory.startsWith('~') || directory.startsWith('/'))) {
      throw new HostError('invalid_workspace')
    }
    const identity = `wsl:${directory}`
    const existing = [...this.#entries.values()].find(entry => entry.identity === identity)
    if (existing) return this.public(existing.id)
    const entry = { id: randomUUID(), directory, identity, kind: 'wsl',
      name: name ?? path.posix.basename(directory) }
    this.#entries.set(entry.id, entry)
    return this.public(entry.id)
  }
  public(id) {
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_workspace')
    return { workspaceId: entry.id, name: entry.name ?? this.#path.basename(entry.directory) }
  }
  kind(id) {
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_workspace')
    return entry.kind ?? 'native'
  }
  async directory(id) {
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_workspace')
    if (entry.kind === 'wsl') return entry.directory
    const stat = await this.#files.lstat(entry.directory)
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new HostError('workspace_changed')
    if (this.#platform === 'win32' && entry.inode === 0) {
      // No usable inode to bind; the canonical string (folded) is the remaining identity.
      const canonical = await this.#files.realpath(entry.directory)
      if (this.#identity(canonical) !== entry.identity) throw new HostError('workspace_changed')
    } else if (stat.dev !== entry.device || stat.ino !== entry.inode) {
      throw new HostError('workspace_changed')
    }
    return entry.directory
  }
}
