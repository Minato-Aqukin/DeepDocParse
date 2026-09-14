import { describe, expect, it } from 'vitest'
import { selectedResource, selectedVersion } from '../resource-context'

describe('explicit resource navigation context', () => {
  const current = '#/documents/d1?resource_id=r1&version_id=v1'
  it('binds all operations on the selected document and its evidence', () => {
    for (const path of ['/api/documents/d1', '/api/documents/d1/pages', '/api/documents/d1/conversations', '/api/evidence/e1']) {
      expect(selectedResource(path,current)).toBe('r1')
      expect(selectedVersion(path,current)).toBe('v1')
    }
    expect(selectedResource('/api/conversations',current,{document:'d1'})).toBe('r1')
  })
  it('does not attach context to other documents, accounts, or hosts', () => {
    for (const path of ['/api/documents/d2', '/api/documents/d10', '/api/auth/me', 'https://other/api/documents/d1', '//other/api/documents/d1']) {
      expect(selectedResource(path,current)).toBeUndefined()
      expect(selectedVersion(path,current)).toBeUndefined()
    }
    expect(selectedResource('/api/conversations',current,{document:'d2'})).toBeUndefined()
    expect(selectedResource('/api/evidence/e1','#/wiki')).toBeUndefined()
  })
  it('uses the selected asset when same bytes have multiple logical owners', () => {
    expect(selectedResource('/api/documents/d1', '#/documents/d1?resource_id=r2')).toBe('r2')
    expect(selectedResource('/api/documents/d1', '#/documents/d1')).toBeUndefined()
    expect(selectedResource('/api/documents/d1/jobs','#/documents/d1/versions?resource_id=r2')).toBe('r2')
  })
})
