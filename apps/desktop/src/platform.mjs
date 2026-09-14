import { constants } from 'node:fs'
import { lstat, mkdir, open } from 'node:fs/promises'
import { HostError } from './policy.mjs'

const FILESYSTEM = Object.freeze({ lstat, mkdir, open })
const POSIX_READ = constants.O_RDONLY | constants.O_NOFOLLOW
// Same allowlist the Linux broker has always trusted. Other backends store secrets
// in plaintext or in a store we cannot call "encrypted" honestly.
const APPROVED = new Set(['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6'])

export function platformName(platform = process.platform) {
  return platform === 'linux' || platform === 'win32' ? platform : 'other'
}

/**
 * Create and/or verify one host-private directory.
 *
 * POSIX gates on owner-only mode and uid. Windows never gets a mode check: NTFS
 * ignores mkdir modes, so privacy there derives from the user-profile ACL this
 * directory inherits. The win32 branch therefore only refuses symlinks/reparse
 * points and non-directories; callers and hostStatus report backend `ntfs_acl`
 * instead of claiming a mode we cannot enforce.
 *
 * `code` is the caller's HostError code (`unsafe_credential_directory`, ...).
 * `fileSystem` exists so tests can observe the exact filesystem sequence.
 */
export async function secureDirectory(directory,
  { create = true, platform = process.platform, code = 'unsafe_directory', fileSystem = FILESYSTEM } = {}) {
  const windows = platform === 'win32'
  let created = false
  if (create) {
    try { await fileSystem.lstat(directory) } catch (error) { if (error.code !== 'ENOENT') throw error; created = true }
    // No mode on Windows: passing 0o700 there would pretend an enforcement that does not happen.
    if (windows) await fileSystem.mkdir(directory, { recursive: true })
    else await fileSystem.mkdir(directory, { recursive: true, mode: 0o700 })
  }
  const stat = await fileSystem.lstat(directory)
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new HostError(code)
  if (!windows && ((stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid()))) {
    throw new HostError(code)
  }
  return { backend: windows ? 'ntfs_acl' : 'posix_mode', created }
}

/**
 * Verify one host-private file: no symlink/reparse point, regular file, size at
 * most `maxBytes`, and on POSIX owner-only mode and uid. A missing path stays a
 * raw ENOENT so callers can keep treating absence separately from an unsafe file.
 *
 * `code` is the caller's HostError code (`unsafe_runtime_token`, ...).
 * `fileSystem` exists so tests can observe the exact filesystem sequence.
 */
export async function secureFile(file,
  { platform = process.platform, maxBytes = 8192, code = 'unsafe_file', fileSystem = FILESYSTEM } = {}) {
  if (platform === 'win32') {
    const stat = await fileSystem.lstat(file)
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maxBytes) throw new HostError(code)
    return { backend: 'ntfs_acl' }
  }
  let handle
  try { handle = await fileSystem.open(file, POSIX_READ) }
  catch (error) { if (error.code === 'ENOENT') throw error; throw new HostError(code) }
  try {
    const stat = await handle.stat()
    if (!stat.isFile() || stat.size > maxBytes || (stat.mode & 0o077) !== 0
        || (process.getuid && stat.uid !== process.getuid())) throw new HostError(code)
  } finally { await handle.close() }
  return { backend: 'posix_mode' }
}

/**
 * Report where a credential could be persisted, never a capability the platform
 * does not have. Linux keeps the approved secret-service allowlist and its exact
 * reason strings. Windows persists only through DPAPI; otherwise the credential
 * is session-only. Other platforms are session-only without probing safeStorage.
 */
export function credentialBackend(safeStorage, platform = process.platform) {
  if (platform === 'win32') {
    try {
      return safeStorage.isEncryptionAvailable()
        ? { backend: 'dpapi', persistentAvailable: true, reason: null }
        : { backend: 'session', persistentAvailable: false, reason: 'session_only_dpapi_unavailable' }
    } catch {
      return { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_dpapi_unavailable' }
    }
  }
  if (platform !== 'linux') {
    return { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_platform_unsupported' }
  }
  try {
    const backend = safeStorage.getSelectedStorageBackend()
    const allowed = APPROVED.has(backend) && safeStorage.isEncryptionAvailable()
    return { backend, persistentAvailable: allowed, reason: allowed ? null : 'session_only_secret_service_required' }
  } catch {
    return { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_secret_service_unavailable' }
  }
}
