import { AxiosHeaders } from 'axios'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authApi } from '@/api'
import { useAuthStore } from '../auth'

const guestResponse = (enabled: boolean) => ({
  data: { enabled }, status: 200, statusText: 'OK', headers: {},
  config: { headers: new AxiosHeaders() },
})

describe('auth store 游客模式', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    setActivePinia(createPinia())
    localStorage.clear()
    Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
  })

  it('loadGuestMode 失败按关处理（回落登录，不抛）', async () => {
    vi.spyOn(authApi, 'guest').mockRejectedValue(new Error('network down'))
    const auth = useAuthStore()
    await expect(auth.loadGuestMode()).resolves.toBe(false)
    expect(auth.guestEnabled).toBe(false)
    expect(auth.isGuest).toBe(false)
  })

  it('开着时未登录是游客，全部能力 getter 都是 false', async () => {
    vi.spyOn(authApi, 'guest').mockResolvedValue(guestResponse(true))
    const auth = useAuthStore()
    await expect(auth.loadGuestMode()).resolves.toBe(true)
    expect(auth.isGuest).toBe(true)
    expect(auth.role).toBeNull()
    expect(auth.canUpload).toBe(false)
    expect(auth.canReview).toBe(false)
    expect(auth.canManageOrg).toBe(false)
  })

  it('登录后不再是游客（isGuest 只在未认证时成立）', async () => {
    vi.spyOn(authApi, 'guest').mockResolvedValue(guestResponse(true))
    const auth = useAuthStore()
    await auth.loadGuestMode()
    auth.token = 'session'
    expect(auth.isAuthenticated).toBe(true)
    expect(auth.isGuest).toBe(false)
  })

  it('幂等：重复调用只问一次', async () => {
    const guest = vi.spyOn(authApi, 'guest').mockResolvedValue(guestResponse(true))
    const auth = useAuthStore()
    await Promise.all([auth.loadGuestMode(), auth.loadGuestMode(), auth.loadGuestMode()])
    await auth.loadGuestMode()
    expect(guest).toHaveBeenCalledTimes(1)
    expect(auth.guestEnabled).toBe(true)
  })

  it('桌面恒 false 且不发请求', async () => {
    Object.defineProperty(window, 'ddpDesktop', { value: {}, configurable: true, writable: true })
    const guest = vi.spyOn(authApi, 'guest')
    const auth = useAuthStore()
    await expect(auth.loadGuestMode()).resolves.toBe(false)
    expect(guest).not.toHaveBeenCalled()
    expect(auth.isGuest).toBe(false)
  })
})
