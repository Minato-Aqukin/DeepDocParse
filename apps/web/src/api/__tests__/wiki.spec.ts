import { AxiosHeaders } from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it } from 'vitest'

import { http } from '@/api/http'
import { versionedWikiApi } from '../wiki'

const originalAdapter = http.defaults.adapter

afterEach(() => {
  http.defaults.adapter = originalAdapter
})

describe('versionedWikiApi.editPage', () => {
  it('读回的人工段落带服务端标注，写回时只发 {id, text}：多一个字段服务端就 422', async () => {
    let sent: unknown
    http.defaults.adapter = async (config: InternalAxiosRequestConfig) => {
      sent = JSON.parse(config.data as string)
      return { data: {}, status: 201, statusText: 'Created', headers: new AxiosHeaders(), config }
    }
    await versionedWikiApi.editPage('w1', 'p1', {
      base_revision_id: 'r1',
      paragraphs: [
        { id: 'old', text: '已有段落', kind: 'human', unsupported: true } as { id: string; text: string },
        { id: 'new', text: '新段落' },
      ],
    }, 'key-1')
    expect(sent).toEqual({
      base_revision_id: 'r1',
      paragraphs: [{ id: 'old', text: '已有段落' }, { id: 'new', text: '新段落' }],
    })
  })
})
