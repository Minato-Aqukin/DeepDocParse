import { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '@/api/http'
import { ingestStatusOf, sha256Hex, uploadDirect, uploadsApi, waitForIngest, waitForVerification } from '../uploads'
import type { UploadSession } from '../uploads'
const originalAdapter = http.defaults.adapter
const originalFetch = globalThis.fetch

function session(overrides: Partial<UploadSession> = {}): UploadSession {
  return {
    id: 'u1', status: 'ready', object_key: 'k', filename: 'a.pdf',
    mime: 'application/pdf', declared_size: 3, part_size: 8,
    expires_at: new Date().toISOString(), target_resource_id: null,
    ingest_status: 'ready', ingest_error: null,
    ...overrides,
  }
}

function okResponse<T>(data: T, configUrl = '/api/uploads/u1'): AxiosResponse<T> {
  return {
    data, status: 200, statusText: 'OK', headers: new AxiosHeaders(),
    config: { headers: new AxiosHeaders(), url: configUrl } as never,
  }
}

beforeEach(() => {
  location.hash = '#/resources'
})

afterEach(() => {
  http.defaults.adapter = originalAdapter
  globalThis.fetch = originalFetch
})

describe('uploadDirect 可续传', () => {
  it('摘要是完整文件的 sha256：服务端拿它把重试绑定到同一内容，算错整个会话会作废', async () => {
    expect(await sha256Hex(new Blob(['abc'])))
      .toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })

  it('同键重来取回原会话：只补传缺的分片，finalize 同一会话', async () => {
    const creates: InternalAxiosRequestConfig[] = []
    const finalized: string[] = []
    http.defaults.adapter = async (config) => {
      if (String(config.url) === '/api/uploads') {
        creates.push(config)
        // 第一片已在对象存储里：服务端只重签第二片
        return okResponse(session({
          id: 'u-resume', status: 'uploading', allocation_state: 'ready', ingest_status: null,
          part_size: 2, completed_parts: [{ part_number: 1, etag: 'e1', size: 2 }],
          parts: [{ part_number: 2, url: 'https://s3/p2' }],
        }), String(config.url))
      }
      finalized.push(String(config.url))
      return okResponse(session({ id: 'u-resume', status: 'verifying', ingest_status: null }), String(config.url))
    }
    const puts: string[] = []
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      puts.push(String(url))
      return new Response(null, { status: 200 })
    }) as unknown as typeof fetch
    const progress: number[] = []
    const file = new File(['abc'], 'a.pdf', { type: 'application/pdf' })

    const out = await uploadDirect(file, { idempotencyKey: 'entry-1', onProgress: (p) => progress.push(p) })

    expect(out.status).toBe('verifying')
    expect(puts).toEqual(['https://s3/p2'])
    expect(finalized).toEqual(['/api/uploads/u-resume/finalize'])
    // 进度按总片数算：补传的最后一片就是 100%，而不是"1/1 片"
    expect(progress).toEqual([100])
    const sent = JSON.parse(String(creates[0]!.data))
    expect(creates[0]!.headers?.['Idempotency-Key']).toBe('entry-1')
    expect(sent.sha256).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })

  it('对象存储分配未确认时不传字节、不 finalize，交给同键重试', async () => {
    const urls: string[] = []
    http.defaults.adapter = async (config) => {
      urls.push(String(config.url))
      return okResponse(session({ status: 'created', allocation_state: 'unknown', ingest_status: null, parts: [] }), String(config.url))
    }
    globalThis.fetch = vi.fn() as unknown as typeof fetch
    const file = new File(['abc'], 'a.pdf', { type: 'application/pdf' })
    await expect(uploadDirect(file, { idempotencyKey: 'entry-2' })).rejects.toThrow('会话已保留')
    expect(urls).toEqual(['/api/uploads'])
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })
})

describe('waitForVerification 只管字节', () => {
  it('字节 ready 即返回，不关心 ingest_status', async () => {
    http.defaults.adapter = async (config) =>
      okResponse(session({ status: 'ready', ingest_status: 'pending' }), String(config.url))
    const settled = await waitForVerification('u1', { intervalMs: 1, timeoutMs: 1000 })
    expect(settled.status).toBe('ready')
    // ingest 还是 pending：调用方必须继续等 waitForIngest，不算完成
    expect(ingestStatusOf(settled)).toBe('pending')
  })

  it('verifying 会继续轮询直到字节落定', async () => {
    const queue = [session({ status: 'verifying', ingest_status: null }), session({ status: 'ready', ingest_status: null })]
    http.defaults.adapter = async (config) =>
      okResponse(queue.shift() ?? session(), String(config.url))
    const settled = await waitForVerification('u1', { intervalMs: 1, timeoutMs: 1000 })
    expect(settled.status).toBe('ready')
  })
})

describe('waitForIngest 登记确认', () => {
  it('延迟确认：pending 先展示，最终 ready 才返回', async () => {
    const seen: string[] = []
    const queue = [
      session({ status: 'ready', ingest_status: 'pending' }),
      session({ status: 'ready', ingest_status: 'retrying' }),
      session({ status: 'ready', ingest_status: 'ready' }),
    ]
    http.defaults.adapter = async (config) =>
      okResponse(queue.shift() ?? session(), String(config.url))
    const settled = await waitForIngest('u1', {
      intervalMs: 1,
      timeoutMs: 1000,
      onPoll: (polled) => seen.push(String(ingestStatusOf(polled))),
    })
    expect(ingestStatusOf(settled)).toBe('ready')
    expect(seen).toEqual(['pending', 'retrying', 'ready'])
  })

  it('终态 rejected 直接返回，不再轮询', async () => {
    let calls = 0
    http.defaults.adapter = async (config) => {
      calls += 1
      return okResponse(
        session({ status: 'ready', ingest_status: 'rejected', ingest_error: 'resource_not_found' }),
        String(config.url),
      )
    }
    const settled = await waitForIngest('u1', { intervalMs: 1, timeoutMs: 1000 })
    expect(ingestStatusOf(settled)).toBe('rejected')
    expect(settled.ingest_error).toBe('resource_not_found')
    expect(calls).toBe(1)
  })

  it('未知取值按"还没确认"处理，绝不误报完成', async () => {
    const queue = [
      session({ status: 'ready', ingest_status: 'mystery' as never }),
      session({ status: 'ready', ingest_status: 'ready' }),
    ]
    http.defaults.adapter = async (config) =>
      okResponse(queue.shift() ?? session(), String(config.url))
    const settled = await waitForIngest('u1', { intervalMs: 1, timeoutMs: 1000 })
    expect(ingestStatusOf(settled)).toBe('ready')
  })

  it('超时抛错但会话保留：调用方可用同一 id 继续查', async () => {
    http.defaults.adapter = async (config) =>
      okResponse(session({ status: 'ready', ingest_status: 'pending' }), String(config.url))
    await expect(waitForIngest('u1', { intervalMs: 1, timeoutMs: 20 })).rejects.toThrow()
    // 会话还在：同一 id 再查一次仍能拿到状态
    const { data } = await uploadsApi.get('u1')
    expect(ingestStatusOf(data)).toBe('pending')
  })

  it('GET 失败直接抛给调用方展示，不伪造状态', async () => {
    http.defaults.adapter = async (config) => {
      throw new AxiosError('boom', 'ERR_BAD_REQUEST', config, undefined, {
        data: { error: { code: 'boom', message: 'boom' } },
        status: 500, statusText: 'boom', headers: new AxiosHeaders(), config,
      })
    }
    await expect(waitForIngest('u1', { intervalMs: 1, timeoutMs: 1000 })).rejects.toBeInstanceOf(AxiosError)
  })
})
