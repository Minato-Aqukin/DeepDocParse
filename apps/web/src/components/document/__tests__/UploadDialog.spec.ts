import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

const { uploadDirectMock, waitForVerificationMock, waitForIngestMock } = vi.hoisted(() => ({
  uploadDirectMock: vi.fn(),
  waitForVerificationMock: vi.fn(),
  waitForIngestMock: vi.fn(),
}))

vi.mock('@/api/uploads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/uploads')>()
  return {
    ...actual,
    uploadDirect: uploadDirectMock,
    waitForVerification: waitForVerificationMock,
    waitForIngest: waitForIngestMock,
  }
})
vi.mock('@/components/engine/EngineOptionsForm.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/utils/preferences', () => ({
  loadEnginePreference: () => ({ engine: 'borndigital', options: {} }),
}))
vi.mock('element-plus', () => ({
  ElMessage: { warning: vi.fn(), success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import UploadDialog from '../UploadDialog.vue'
import type { UploadSession } from '@/api/uploads'

type DialogWrapper = VueWrapper<InstanceType<typeof UploadDialog>>

// Element Plus 未在单测里注册（见 src/__tests__/setup.ts）：未解析的 el-dialog
// 只渲染成空壳元素，**具名 footer 插槽里的「上传」按钮根本不在 DOM 里** ——
// 那样点到的是别的按钮，用例断在空处。这里给一个把两个插槽都画出来的桩。
const DialogStub = defineComponent({
  setup(_, { slots }) {
    return () => h('div', [slots.default?.(), slots.footer?.()])
  },
})

function mountDialog(props: { modelValue: boolean, resourceId?: string }): DialogWrapper {
  return mount(UploadDialog, { props, global: { stubs: { ElDialog: DialogStub } } })
}

function session(overrides: Partial<UploadSession> = {}): UploadSession {
  return {
    id: 'u1', status: 'ready', object_key: 'k', filename: 'a.pdf',
    mime: 'application/pdf', declared_size: 1, part_size: 8,
    expires_at: new Date().toISOString(), target_resource_id: null, ingest_status: 'ready', ingest_error: null,
    ...overrides,
  }
}

function pdf(name = 'a.pdf'): File {
  return new File(['x'], name, { type: 'application/pdf' })
}

async function selectFiles(wrapper: DialogWrapper, files: File[]) {
  const input = wrapper.find('#upload-input')
  Object.defineProperty(input.element, 'files', { value: files, configurable: true })
  await input.trigger('change')
}

async function clickUpload(wrapper: DialogWrapper) {
  // Element Plus 未在单测里注册（见 src/__tests__/setup.ts）：el-button 渲染为
  // 同名自定义元素而非原生 button，按标签名找，最后一个是 footer 的提交按钮
  const buttons = wrapper.findAll('el-button')
  const submit = buttons[buttons.length - 1]!
  await submit.trigger('click')
  await flushPromises()
}

function findRetry(wrapper: DialogWrapper) {
  return wrapper.findAll('el-button').find((b) => b.text() === '重试查询')
}

beforeEach(() => {
  uploadDirectMock.mockReset()
  waitForVerificationMock.mockReset()
  waitForIngestMock.mockReset()
  uploadDirectMock.mockImplementation(async () => session({ status: 'verifying', ingest_status: null }))
  waitForVerificationMock.mockImplementation(async () => session({ status: 'ready', ingest_status: null }))
  waitForIngestMock.mockImplementation(async () => session({ status: 'ready', ingest_status: 'ready' }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('UploadDialog 完成语义', () => {
  it('字节就绪但登记未确认时不算完成、不清队列', async () => {
    // 字节 ready，登记还是 pending：waitForIngest 决不能被跳过
    waitForIngestMock.mockImplementation(async () => {
      throw new Error('登记确认超时，会话已保留，可重试继续查询登记状态')
    })
    const wrapper = mountDialog({ modelValue: true })
    await selectFiles(wrapper, [pdf()])
    await clickUpload(wrapper)
    expect(uploadDirectMock).toHaveBeenCalledTimes(1)
    expect(waitForVerificationMock).toHaveBeenCalledWith('u1')
    expect(waitForIngestMock).toHaveBeenCalledWith('u1', expect.anything())
    // 没成功：不发 uploaded，文件留在队列里等状态重试
    expect(wrapper.emitted('uploaded')).toBeUndefined()
    expect(wrapper.text()).toContain('a.pdf')
    wrapper.unmount()
  })

  it('终态 rejected 留在队列展示契约文案，不提供重试', async () => {
    waitForIngestMock.mockImplementation(async () =>
      session({ status: 'ready', ingest_status: 'rejected', ingest_error: 'resource_version_exists' }))
    const wrapper = mountDialog({ modelValue: true })
    await selectFiles(wrapper, [pdf()])
    await clickUpload(wrapper)
    expect(wrapper.text()).toContain('该内容已是目标资源的一个版本')
    expect(wrapper.text()).not.toContain('登记重试中')
    expect(wrapper.emitted('uploaded')).toBeUndefined()
    // rejected 是终态：重试按钮只给 failed，不给 rejected
    expect(findRetry(wrapper)).toBeUndefined()
    wrapper.unmount()
  })

  it('传输失败后再点上传：同一队列项沿用同一创建键，取回原会话而不是新建', async () => {
    uploadDirectMock.mockImplementationOnce(async () => {
      throw new Error('分片 1 上传失败（503）')
    })
    const wrapper = mountDialog({ modelValue: true })
    await selectFiles(wrapper, [pdf()])
    await clickUpload(wrapper)
    expect(wrapper.text()).toContain('分片 1 上传失败')
    await clickUpload(wrapper)
    expect(uploadDirectMock).toHaveBeenCalledTimes(2)
    const [first, second] = uploadDirectMock.mock.calls.map((call) => call[1].idempotencyKey)
    expect(first).toBeTruthy()
    expect(second).toBe(first)
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    wrapper.unmount()
  })
  it('状态重试复用原会话 id，只查不传', async () => {
    waitForIngestMock.mockImplementationOnce(async () => {
      throw new Error('登记确认超时，会话已保留，可重试继续查询登记状态')
    })
    const wrapper = mountDialog({ modelValue: true })
    await selectFiles(wrapper, [pdf()])
    await clickUpload(wrapper)
    expect(uploadDirectMock).toHaveBeenCalledTimes(1)

    // 第二次查询确认成功：同一会话 id，不重传字节
    waitForIngestMock.mockImplementationOnce(async () =>
      session({ status: 'ready', ingest_status: 'ready' }))
    const retry = findRetry(wrapper)!
    await retry.trigger('click')
    await flushPromises()
    expect(uploadDirectMock).toHaveBeenCalledTimes(1)
    expect(waitForVerificationMock).toHaveBeenCalledWith('u1')
    expect(waitForIngestMock).toHaveBeenLastCalledWith('u1', expect.anything())
    expect(wrapper.emitted('uploaded')).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('UploadDialog 追加目标冻结', () => {
  it('入队时冻结目标：之后 prop 变化不改写已排队文件', async () => {
    const wrapper = mountDialog({ modelValue: true, resourceId: 'res-a' })
    await selectFiles(wrapper, [pdf()])
    await wrapper.setProps({ resourceId: 'res-b' })
    await clickUpload(wrapper)
    expect(uploadDirectMock).toHaveBeenCalledTimes(1)
    expect(uploadDirectMock.mock.calls[0]![1]).toMatchObject({ targetResourceId: 'res-a' })
    wrapper.unmount()
  })

  it('关闭重开不改写快照：同一文件仍用原目标', async () => {
    waitForIngestMock.mockImplementationOnce(async () => {
      throw new Error('登记确认超时，会话已保留，可重试继续查询登记状态')
    })
    const wrapper = mountDialog({ modelValue: true, resourceId: 'res-a' })
    await selectFiles(wrapper, [pdf()])
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true, resourceId: 'res-b' })
    await clickUpload(wrapper)
    expect(uploadDirectMock.mock.calls[0]![1]).toMatchObject({ targetResourceId: 'res-a' })
    wrapper.unmount()
  })

  it('追加模式一次只收一个文件', async () => {
    const wrapper = mountDialog({ modelValue: true, resourceId: 'res-a' })
    await selectFiles(wrapper, [pdf('a.pdf'), pdf('b.pdf')])
    await clickUpload(wrapper)
    expect(uploadDirectMock).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('独立上传不带目标', async () => {
    const wrapper = mountDialog({ modelValue: true })
    await selectFiles(wrapper, [pdf()])
    await clickUpload(wrapper)
    expect(uploadDirectMock.mock.calls[0]![1]).toMatchObject({ targetResourceId: null })
    wrapper.unmount()
  })
})
