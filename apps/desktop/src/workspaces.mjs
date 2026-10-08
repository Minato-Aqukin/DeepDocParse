import { randomUUID } from 'node:crypto'
import { lstat, realpath } from 'node:fs/promises'
import path from 'node:path'
import { HostError } from './policy.mjs'

const FILESYSTEM = Object.freeze({ lstat, realpath })
const WSL_DIRECTORY = /^[A-Za-z0-9._~/-]+$/
const WSL_SEGMENT = /^[A-Za-z0-9._~-]+$/
export const WSL_DISTRO_PATTERN = /^[A-Za-z0-9._-]{1,64}$/
const WSL_PATH_LIMIT = 1024
export class WorkspaceHandles {
  #entries = new Map()
  #platform
  #files
  #path
  #defaultWslDistro = null
  constructor({ platform = process.platform, fileSystem = FILESYSTEM, defaultWslDistro = null } = {}) {
    this.#platform = platform
    this.#files = fileSystem
    this.#path = platform === 'win32' ? path.win32 : path
    this.defaultWslDistro = defaultWslDistro
  }
  // The single WSL2 distribution this host serves local mode from (main sets it
  // from the backend's resolved distro at startup). It binds WSL handle
  // identity so the same virtual path on different distros never collides.
  set defaultWslDistro(value) {
    if (value !== null && (typeof value !== 'string' || !WSL_DISTRO_PATTERN.test(value))) {
      throw new HostError('invalid_wsl_distro')
    }
    this.#defaultWslDistro = value
  }
  get defaultWslDistro() { return this.#defaultWslDistro }
  resolveWslDistro(explicit) {
    const distro = explicit ?? this.#defaultWslDistro
    if (distro !== null && (typeof distro !== 'string' || !WSL_DISTRO_PATTERN.test(distro))) {
      throw new HostError('invalid_wsl_distro')
    }
    return distro
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
  selectedWsl({ directory, name, distro } = {}) {
    const canonical = canonicalWslPath(directory)
    const bound = this.resolveWslDistro(distro)
    const identity = `wsl:${bound ?? ''}:${canonical}`
    const existing = [...this.#entries.values()].find(entry => entry.identity === identity)
    if (existing) return this.public(existing.id)
    const entry = { id: randomUUID(), directory: canonical, identity, kind: 'wsl', distro: bound,
      name: name ?? path.posix.basename(canonical) }
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
  distro(id) {
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_workspace')
    return entry.kind === 'wsl' ? (entry.distro ?? null) : null
  }
  async directory(id) {
    const entry = this.#entries.get(id)
    if (!entry) throw new HostError('unknown_workspace')
    if (entry.kind === 'wsl') {
      // Re-resolve the virtual path on every lookup like the native dev/ino
      // bind: a tampered or aliased entry fails here, not at handshake time.
      if (canonicalWslPath(entry.directory) !== entry.directory
          || `wsl:${entry.distro ?? ''}:${entry.directory}` !== entry.identity) {
        throw new HostError('workspace_changed')
      }
      return entry.directory
    }
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

// Lexical POSIX canonicalization for WSL virtual paths (no host fs access: the
// distro owns the filesystem). Collapses duplicate slashes, drops trailing
// slashes, and fail-closes on '.'/'..' segments so '~/a/../b' can never alias
// '~/b' into a second handle — and on any escape above the virtual root.
function canonicalWslPath(directory) {
  if (typeof directory !== 'string' || !WSL_DIRECTORY.test(directory)
      || !(directory.startsWith('~') || directory.startsWith('/'))
      || directory.length > WSL_PATH_LIMIT) {
    throw new HostError('invalid_workspace')
  }
  const rooted = directory.startsWith('/')
  const canonical = (rooted ? '/' : '') + directory.split('/').filter(Boolean).join('/')
  for (const segment of canonical.split('/').filter(Boolean)) {
    if (segment === '.' || segment === '..' || !WSL_SEGMENT.test(segment)) {
      throw new HostError('invalid_workspace')
    }
  }
  if (canonical !== '~' && !canonical.startsWith('~/') && !canonical.startsWith('/')) {
    throw new HostError('invalid_workspace')
  }
  return canonical
}
