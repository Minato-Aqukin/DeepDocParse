import { HostError } from './policy.mjs'
import { createNativeBackend } from './runtime-native.mjs'

export async function createRuntimeBackend(options) {
  const { kind } = options
  if (kind === 'native') return createNativeBackend(options)
  if (kind === 'wsl') {
    let module
    try { module = await import('./runtime-wsl.mjs') } catch { throw new HostError('wsl_backend_unavailable') }
    if (typeof module.createWslBackend !== 'function') throw new HostError('wsl_backend_unavailable')
    try { return await module.createWslBackend(options) }
    catch (error) {
      if (error instanceof HostError) throw error
      throw new HostError('wsl_backend_unavailable')
    }
  }
  throw new HostError('unknown_runtime_backend')
}
