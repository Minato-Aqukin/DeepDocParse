// Loud Windows test gating. The A+C slice ships remote mode (Tier A) and a bundled
// Linux runtime inside WSL2 (Tier C); it deliberately does not port the native
// Python runtime. Tests that need it skip with a printed reason instead of failing
// (or silently passing) on a clean windows-latest checkout. DDP_TEST_FORCE_WIN32=1
// makes that gating observable on Linux.
//
// The override cannot fake POSIX filesystem semantics, so tests that assert real
// owner-only modes or case-sensitive directory identity check the host platform
// directly: they still run under the override (where the host is Linux) and skip
// only on a real Windows host, where NTFS could never satisfy them.
const NATIVE_RUNTIME = 'native runtime is Linux-only in the A+C slice; the WSL path is covered by runtime-wsl.test.mjs'
const POSIX_FILESYSTEM = 'POSIX owner-only modes and case-sensitive directory identity need a POSIX filesystem; the injected win32 branch in platform.test.mjs covers the Windows semantics'

export function nativeRuntimeSkipReason() {
  if (process.platform === 'win32') return NATIVE_RUNTIME
  if (process.env.DDP_TEST_FORCE_WIN32 === '1') return `${NATIVE_RUNTIME} (forced by DDP_TEST_FORCE_WIN32=1)`
  return false
}

export function posixFilesystemSkipReason() {
  return process.platform === 'win32' ? POSIX_FILESYSTEM : false
}
