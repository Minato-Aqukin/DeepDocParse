import { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { http, TOKEN_KEY } from '@/api/http'
import { askStream } from '@/api/conversations'
import { useAuthStore } from '@/stores/auth'
import type { Profile } from '@/types/api'

const profile: Profile = {
  id: 'old-user', username: 'contributor', email: null, role: 'contributor',
  organization_id: 'core-org', created_at: '2026-09-23T00:00:00Z',
}
const originalAdapter = http.defaults.adapter
let pinia: Pinia

function unauthorized(config: InternalAxiosRequestConfig) {
  return new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
    data: { error: { code: 'unauthorized', message: 'Unauthorized' } },
    status: 401, statusText: 'Unauthorized', headers: new AxiosHeaders(), config,
  })
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem(TOKEN_KEY, 'old-session')
  pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().profile = profile
  location.hash = '#/resources'
})
afterEach(() => {
  http.defaults.adapter = originalAdapter
  vi.unstubAllGlobals()
  disposePinia(pinia)
  localStorage.clear()
})

describe('HTTP authentication boundary', () => {
  it('expires the loaded identity and write capabilities even when the caller owns the error UI', async () => {
    http.defaults.adapter = async config => { throw unauthorized(config) }
    await expect(http.get('/api/auth/me', { suppressErrorToast: true })).rejects.toBeInstanceOf(AxiosError)
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(useAuthStore().canUpload).toBe(false)
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(location.hash).toBe('#/login')
  })

  it('does not clear a newer login when an old request returns unauthorized', async () => {
    const entered = Promise.withResolvers<InternalAxiosRequestConfig>()
    const response = Promise.withResolvers<AxiosResponse>()
    http.defaults.adapter = config => {
      entered.resolve(config)
      return response.promise
    }
    const pending = http.get('/api/auth/me')
    const rejected = expect(pending).rejects.toBeInstanceOf(AxiosError)
    const oldRequest = await entered.promise
    const auth = useAuthStore()
    auth.token = 'new-session'
    auth.profile = { ...profile, id: 'new-user', role: 'viewer' }
    localStorage.setItem(TOKEN_KEY, 'new-session')
    response.reject(unauthorized(oldRequest))
    await rejected
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.profile?.id).toBe('new-user')
    expect(localStorage.getItem(TOKEN_KEY)).toBe('new-session')
    expect(location.hash).toBe('#/resources')
  })

  it('expires identity and write capabilities when the answer stream rejects the current session', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })))
    const settled = Promise.withResolvers<void>()
    askStream('conversation', 'A grounded question', {
      onDelta: vi.fn(),
      onSettled: settled.resolve,
    })
    await settled.promise
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(useAuthStore().profile).toBeNull()
    expect(useAuthStore().canUpload).toBe(false)
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(location.hash).toBe('#/login')
  })

  it('keeps a newer login when an answer request from the old session returns unauthorized', async () => {
    const response = Promise.withResolvers<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(response.promise))
    const settled = Promise.withResolvers<void>()
    askStream('conversation', 'A grounded question', {
      onDelta: vi.fn(),
      onSettled: settled.resolve,
    })
    const auth = useAuthStore()
    auth.token = 'new-session'
    auth.profile = { ...profile, id: 'new-user', role: 'viewer' }
    localStorage.setItem(TOKEN_KEY, 'new-session')
    response.resolve(new Response('{}', { status: 401 }))
    await settled.promise
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.profile?.id).toBe('new-user')
    expect(localStorage.getItem(TOKEN_KEY)).toBe('new-session')
    expect(location.hash).toBe('#/resources')
  })
})
