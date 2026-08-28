import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useDocumentsStore } from '../documents'
import type { DocumentInfo } from '@/types/api'

function row(patch: Partial<DocumentInfo>): DocumentInfo {
  return {
    id: 'doc', filename: 'manual.pdf', doc_id: 'd', origin: 'web', mime: 'application/pdf',
    size_bytes: 1, page_count: 1, status: 'succeeded', error: null,
    index_status: 'failed', index_error: '需版本校验', compile_status: 'failed',
    compile_degraded: ['reindex_validation_required'], compile_fingerprint: '',
    layout_version: 'ddp-layout/1', code_detection: 'unavailable', current_job_id: 'job',
    created_at: new Date().toISOString(), uploaders: ['alice'], can_delete: true,
    ...patch,
  }
}

describe('文档轮询终止条件', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('待人工校验没有 worker，不得被当成活跃任务无限轮询', () => {
    const store = useDocumentsStore()
    store.items = [row({})]
    expect(store.hasActive).toBe(false)

    store.items = [row({ index_status: 'indexing', compile_status: 'compiling' })]
    expect(store.hasActive).toBe(true)
  })
})
