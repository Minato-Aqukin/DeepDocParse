import { AxiosHeaders } from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { createPinia, disposePinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { http, isCurrentSourceResponse, TOKEN_KEY } from '@/api/http'
import { bootSource } from '@/platform/desktop'
import { useAuthStore } from '@/stores/auth'

function setDesktop(bridge: unknown) {
  Object.defineProperty(window, 'ddpDesktop', { value: bridge, configurable: true, writable: true })
}

function clearDesktop() {
  Object.defineProperty(window, 'ddpDesktop', { value: undefined, configurable: true, writable: true })
}

const originalAdapter = http.defaults.adapter
let pinia: Pinia

function respond(config: InternalAxiosRequestConfig, headers: Record<string, string>) {
  return {
    data: { ok: true }, status: 200, statusText: 'OK',
    headers: new AxiosHeaders(headers), config,
  }
}

beforeEach(() => {
  localStorage.clear()
  pinia = createPinia()
  setActivePinia(pinia)
  location.hash = '#/resources'
})

afterEach(() => {
  http.defaults.adapter = originalAdapter
  vi.unstubAllGlobals()
  clearDesktop()
  bootSource.value = null
  disposePinia(pinia)
  localStorage.clear()
})

describe('桌面来源校验', () => {
  it('浏览器不做来源检查', () => {
    expect(isCurrentSourceResponse({ 'x-ddp-source': 'other' })).toBe(true)
  })

  it('桌面下响应头与启动源不一致 → 请求失败并带 source_changed', async () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 'boot-1', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    http.defaults.adapter = async (config) =>
      respond(config, { 'x-ddp-source': 'other-source' })
    await expect(http.get('/api/resources')).rejects.toMatchObject({ code: 'source_changed' })
  })

  it('桌面下响应头一致 → 放行', async () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 'boot-1', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    http.defaults.adapter = async (config) =>
      respond(config, { 'x-ddp-source': 'boot-1' })
    const resp = await http.get('/api/resources')
    expect(resp.status).toBe(200)
  })

  it('桌面下错误响应的串源头同样丢弃', async () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 'boot-1', kind: 'local', label: 'ws', state: 'ready',
      readOnly: false, features: [], active: true, reason: null,
    }
    http.defaults.adapter = async (config) => {
      throw Object.assign(new Error('Request failed'), {
        isAxiosError: true,
        config,
        response: {
          status: 500, data: { error: { code: 'x', message: 'm' } },
          headers: new AxiosHeaders({ 'x-ddp-source': 'other-source' }),
        },
      })
    }
    await expect(http.get('/api/documents')).rejects.toMatchObject({ code: 'source_changed' })
  })

  it('桌面请求不带 Authorization 头', async () => {
    setDesktop({})
    localStorage.setItem(TOKEN_KEY, 'browser-jwt')
    let seen: unknown
    http.defaults.adapter = async (config) => {
      seen = config.headers?.Authorization
      return respond(config, {})
    }
    await http.get('/api/resources')
    expect(seen).toBeUndefined()
  })

  it('浏览器请求照常带 Authorization 头', async () => {
    localStorage.setItem(TOKEN_KEY, 'browser-jwt')
    let seen: unknown
    http.defaults.adapter = async (config) => {
      seen = config.headers?.Authorization
      return respond(config, {})
    }
    await http.get('/api/resources')
    expect(seen).toBe('Bearer browser-jwt')
  })

  it('桌面 401 → 去数据源页并带过期原因，不去登录页', async () => {
    setDesktop({})
    bootSource.value = {
      sourceId: 'boot-1', kind: 'center', label: 'c', state: 'ready',
      readOnly: true, features: [], active: true, reason: null,
    }
    http.defaults.adapter = async (config) => {
      throw Object.assign(new Error('Request failed'), {
        isAxiosError: true,
        config,
        response: {
          status: 401, data: { error: { code: 'x', message: 'm' } },
          headers: new AxiosHeaders({}),
        },
      })
    }
    location.hash = '#/documents'
    await expect(http.get('/api/documents')).rejects.toMatchObject({
      response: { status: 401 },
    })
    expect(location.hash).toBe('#/sources?reason=source_signed_out')
    expect(useAuthStore().profile).toBeNull()
  })
})
