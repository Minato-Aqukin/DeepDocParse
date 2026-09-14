import test from 'node:test'
import assert from 'node:assert/strict'
import { chmod, lstat, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { credentialBackend, platformName, secureDirectory, secureFile } from '../src/platform.mjs'
import { WorkspaceHandles } from '../src/workspaces.mjs'
import { ClientHost } from '../src/client-host.mjs'
import { posixFilesystemSkipReason } from './helpers/platform.mjs'

async function temporary(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'ddp-platform-test-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  return directory
}
async function symlinkOrSkip(t, target, link) {
  try { await symlink(target, link); return true }
  catch (error) {
    if (['EPERM', 'EACCES', 'ENOTSUP', 'UNKNOWN'].includes(error.code)) {
      t.skip(`symlink creation unavailable on this host: ${error.code}`)
      return false
    }
    throw error
  }
}
const directoryStat = overrides => ({
  isDirectory: () => true, isSymbolicLink: () => false, dev: 7, ino: 42, ...overrides,
})
function workspaceFileSystem({ stat, canonical }) {
  return { lstat: async () => stat(), realpath: async () => canonical() }
}

test('platform names separate linux and win32 from everything else', () => {
  assert.equal(platformName('linux'), 'linux')
  assert.equal(platformName('win32'), 'win32')
  for (const other of ['darwin', 'freebsd', 'aix']) assert.equal(platformName(other), 'other')
})

test('POSIX directory gate keeps owner-only mode and the caller error code',
  { skip: posixFilesystemSkipReason() }, async t => {
  const root = await temporary(t), directory = path.join(root, 'credentials')
  assert.deepEqual(await secureDirectory(directory, { platform: 'linux', code: 'unsafe_credential_directory' }),
    { backend: 'posix_mode', created: true })
  assert.equal((await lstat(directory)).mode & 0o777, 0o700)
  assert.deepEqual(await secureDirectory(directory, { platform: 'linux', code: 'unsafe_credential_directory' }),
    { backend: 'posix_mode', created: false })
  await chmod(directory, 0o755)
  await assert.rejects(secureDirectory(directory, { platform: 'linux', code: 'unsafe_credential_directory' }),
    /unsafe_credential_directory/)
  await chmod(directory, 0o700)
  const link = path.join(root, 'linked')
  if (await symlinkOrSkip(t, directory, link)) {
    await assert.rejects(secureDirectory(link, { platform: 'linux', code: 'unsafe_runtime_directory' }),
      /unsafe_runtime_directory/)
  }
})

test('win32 directory branch relies on the profile ACL and never pretends a mode', async t => {
  const root = await temporary(t), directory = path.join(root, 'private')
  assert.deepEqual(await secureDirectory(directory, { platform: 'win32', code: 'unsafe_credential_directory' }),
    { backend: 'ntfs_acl', created: true })
  await chmod(directory, 0o777)
  // A world-readable mode must not reject: NTFS ignores it, and claiming otherwise would be false.
  assert.deepEqual(await secureDirectory(directory, { platform: 'win32', code: 'unsafe_credential_directory' }),
    { backend: 'ntfs_acl', created: false })
  const link = path.join(root, 'linked')
  if (await symlinkOrSkip(t, directory, link)) {
    await assert.rejects(secureDirectory(link, { platform: 'win32', code: 'unsafe_client_directory' }),
      /unsafe_client_directory/)
  }
})

test('POSIX file gate keeps mode/size checks and lets ENOENT stay distinguishable',
  { skip: posixFilesystemSkipReason() }, async t => {
  const root = await temporary(t), file = path.join(root, 'token.json')
  await writeFile(file, 'x'.repeat(32), { mode: 0o600 })
  await chmod(file, 0o600)
  assert.deepEqual(await secureFile(file, { platform: 'linux', code: 'unsafe_runtime_token' }), { backend: 'posix_mode' })
  await chmod(file, 0o644)
  await assert.rejects(secureFile(file, { platform: 'linux', code: 'unsafe_runtime_token' }), /unsafe_runtime_token/)
  await chmod(file, 0o600)
  await assert.rejects(secureFile(file, { platform: 'linux', maxBytes: 8, code: 'unsafe_runtime_token' }),
    /unsafe_runtime_token/)
  await assert.rejects(secureFile(root, { platform: 'linux', code: 'unsafe_runtime_token' }), /unsafe_runtime_token/)
  await assert.rejects(secureFile(path.join(root, 'absent'), { platform: 'linux' }), error => error.code === 'ENOENT')
  const link = path.join(root, 'token-link')
  if (await symlinkOrSkip(t, file, link)) {
    await assert.rejects(secureFile(link, { platform: 'linux', code: 'unsafe_runtime_token' }), /unsafe_runtime_token/)
  }
})

test('win32 file gate rejects reparse/symlink and oversize but not POSIX modes', async t => {
  const root = await temporary(t), file = path.join(root, 'token.json')
  await writeFile(file, 'x'.repeat(32), { mode: 0o644 })
  assert.deepEqual(await secureFile(file, { platform: 'win32', code: 'unsafe_runtime_token' }),
    { backend: 'ntfs_acl' })
  await assert.rejects(secureFile(file, { platform: 'win32', maxBytes: 8, code: 'unsafe_runtime_token' }),
    /unsafe_runtime_token/)
  await assert.rejects(secureFile(root, { platform: 'win32', code: 'unsafe_runtime_token' }), /unsafe_runtime_token/)
  await assert.rejects(secureFile(path.join(root, 'absent'), { platform: 'win32' }), error => error.code === 'ENOENT')
  const link = path.join(root, 'token-link')
  if (await symlinkOrSkip(t, file, link)) {
    await assert.rejects(secureFile(link, { platform: 'win32', code: 'unsafe_runtime_token' }),
      /unsafe_runtime_token/)
  }
})

test('credential backend selection keeps Linux reasons and admits Windows only through DPAPI', () => {
  for (const backend of ['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6']) {
    assert.deepEqual(credentialBackend({ getSelectedStorageBackend: () => backend,
      isEncryptionAvailable: () => true }, 'linux'),
    { backend, persistentAvailable: true, reason: null })
  }
  const weak = { getSelectedStorageBackend: () => 'basic_text',
    isEncryptionAvailable: () => assert.fail('weak backends must not be probed for encryption') }
  assert.deepEqual(credentialBackend(weak, 'linux'),
    { backend: 'basic_text', persistentAvailable: false, reason: 'session_only_secret_service_required' })
  assert.deepEqual(credentialBackend({ getSelectedStorageBackend: () => 'gnome_libsecret',
    isEncryptionAvailable: () => false }, 'linux'),
  { backend: 'gnome_libsecret', persistentAvailable: false, reason: 'session_only_secret_service_required' })
  assert.deepEqual(credentialBackend({ getSelectedStorageBackend: () => { throw new Error('no secret service') },
    isEncryptionAvailable: () => true }, 'linux'),
  { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_secret_service_unavailable' })

  const dpapi = { getSelectedStorageBackend: () => assert.fail('win32 must never call getSelectedStorageBackend'),
    isEncryptionAvailable: () => true }
  assert.deepEqual(credentialBackend(dpapi, 'win32'),
    { backend: 'dpapi', persistentAvailable: true, reason: null })
  assert.deepEqual(credentialBackend({ getSelectedStorageBackend: dpapi.getSelectedStorageBackend,
    isEncryptionAvailable: () => false }, 'win32'),
  { backend: 'session', persistentAvailable: false, reason: 'session_only_dpapi_unavailable' })
  assert.deepEqual(credentialBackend({ getSelectedStorageBackend: dpapi.getSelectedStorageBackend,
    isEncryptionAvailable: () => { throw new Error('dpapi probe failed') } }, 'win32'),
  { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_dpapi_unavailable' })

  const untouched = { getSelectedStorageBackend: () => assert.fail('unsupported platform must not probe'),
    isEncryptionAvailable: () => assert.fail('unsupported platform must not claim encryption') }
  assert.deepEqual(credentialBackend(untouched, 'darwin'),
    { backend: 'unavailable', persistentAvailable: false, reason: 'session_only_platform_unsupported' })
})

test('win32 workspace identity folds case and falls back to the canonical path at inode 0', async () => {
  let canonical = 'C:\\Users\\Alice\\Docs', stat = directoryStat({ ino: 0, dev: 7 })
  const handles = new WorkspaceHandles({ platform: 'win32',
    fileSystem: workspaceFileSystem({ stat: () => stat, canonical: () => canonical }) })
  const first = await handles.selectedByNativeDialog('/workspace/Docs')
  canonical = 'C:\\USERS\\ALICE\\docs'
  const second = await handles.selectedByNativeDialog('/workspace/docs')
  assert.equal(second.workspaceId, first.workspaceId)
  assert.equal(second.name, 'Docs')
  // No usable inode and a changed device: only the folded canonical string can bind it.
  stat = directoryStat({ ino: 0, dev: 99 })
  assert.equal(await handles.directory(first.workspaceId), 'C:\\Users\\Alice\\Docs')
})

test('win32 workspace identity still refuses symlink/reparse entries and replacements', async () => {
  let stat = directoryStat()
  const handles = new WorkspaceHandles({ platform: 'win32',
    fileSystem: workspaceFileSystem({ stat: () => stat, canonical: () => 'C:\\Users\\Alice\\Docs' }) })
  const selected = await handles.selectedByNativeDialog('/workspace/Docs')
  stat = directoryStat({ isSymbolicLink: () => true })
  await assert.rejects(handles.directory(selected.workspaceId), /workspace_changed/)
  await assert.rejects(handles.selectedByNativeDialog('/workspace/link'), /invalid_workspace/)
})

test('POSIX workspace identity keeps case-distinct directories distinct',
  { skip: posixFilesystemSkipReason() }, async t => {
  const root = await temporary(t)
  await mkdir(path.join(root, 'Docs'))
  await mkdir(path.join(root, 'docs'))
  const handles = new WorkspaceHandles({ platform: 'linux' })
  const upper = await handles.selectedByNativeDialog(path.join(root, 'Docs'))
  const lower = await handles.selectedByNativeDialog(path.join(root, 'docs'))
  assert.notEqual(upper.workspaceId, lower.workspaceId)
  assert.equal(upper.name, 'Docs')
  assert.equal(lower.name, 'docs')
  assert.equal(path.basename(await handles.directory(upper.workspaceId)), 'Docs')
})

test('ClientHost applies the shared directory gate with its existing error code', async t => {
  const root = await temporary(t)
  const unsafe = path.join(root, 'unsafe-client')
  await mkdir(unsafe, { recursive: true })
  await chmod(unsafe, 0o755)
  await assert.rejects(new ClientHost({ platform: 'linux', directory: unsafe, workspaces: {}, runtime: {},
    credentials: {} }).initialize(), /unsafe_client_directory/)
  const windows = path.join(root, 'windows-client')
  await mkdir(windows, { recursive: true })
  await chmod(windows, 0o755)
  const clients = await new ClientHost({ platform: 'win32', directory: windows, workspaces: {}, runtime: {},
    credentials: {} }).initialize()
  await clients.close()
})
