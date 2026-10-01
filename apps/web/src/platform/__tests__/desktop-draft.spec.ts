import { describe, expect, it, vi } from 'vitest'

import { saveDesktopDraft } from '@/platform/desktop'
import type { DesktopBridge, Json } from '@/platform/desktop'

describe('草稿 JSON 边界', () => {
  it.each([
    ['Date', () => new Date('2026-10-01T00:00:00Z')],
    ['function', () => ({ callback: () => 'not JSON' })],
    ['cycle', () => {
      const cycle: Record<string, unknown> = {}
      cycle.self = cycle
      return cycle
    }],
  ])('拒绝非 JSON %s，IPC 调用前返回 invalid_arguments', async (_name, value) => {
    const clientSaveDraft = vi.fn()
    const bridge = { clientSaveDraft } as unknown as DesktopBridge
    await expect(saveDesktopDraft(bridge, {
      connectionId: 'local-0', key: 'federation-plan', expectedRevision: 0,
      value: value() as unknown as Json,
    })).rejects.toThrow('invalid_arguments')
    expect(clientSaveDraft).not.toHaveBeenCalled()
  })
})
