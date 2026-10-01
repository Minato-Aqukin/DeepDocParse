import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PdfCanvas from '../PdfCanvas.vue'

const { getDocument } = vi.hoisted(() => ({ getDocument: vi.fn() }))
vi.mock('pdfjs-dist', () => ({ GlobalWorkerOptions: {}, getDocument }))

function documentWithPages(numPages: number) {
  return { numPages, getPage: vi.fn().mockResolvedValue({
    getViewport: () => ({ width: 612, height: 792 }),
  }) }
}

function deferredTask() {
  const pending = Promise.withResolvers<ReturnType<typeof documentWithPages>>()
  return { promise: pending.promise, destroy: vi.fn().mockResolvedValue(undefined), resolve: pending.resolve }
}

function viewer(src: string) {
  return mount(PdfCanvas, { props: { src, pageIdx: 0, pageSize: null, highlights: [] },
    global: { directives: { loading: () => {} } } })
}

beforeEach(() => {
  getDocument.mockReset()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => vi.restoreAllMocks())

describe('PDF source lifecycle', () => {
  it('does not start a PDF load after its view has unmounted', async () => {
    const task = deferredTask()
    getDocument.mockReturnValue(task)
    const wrapper = viewer('/first.pdf')
    wrapper.unmount()
    await flushPromises()
    expect(getDocument).not.toHaveBeenCalled()
  })

  it('ignores an older source that settles after a replacement source starts', async () => {
    const first = deferredTask(), second = deferredTask()
    getDocument.mockReturnValueOnce(first).mockReturnValueOnce(second)
    const wrapper = viewer('/first.pdf')
    await flushPromises()
    await wrapper.setProps({ src: '/second.pdf' })
    await flushPromises()
    first.resolve(documentWithPages(7))
    await flushPromises()
    expect(wrapper.emitted('loaded')).toBeUndefined()
    second.resolve(documentWithPages(2))
    await flushPromises()
    expect(wrapper.emitted('loaded')).toEqual([[2]])
    wrapper.unmount()
    expect(first.destroy).toHaveBeenCalled()
    expect(second.destroy).toHaveBeenCalled()
  })
})
