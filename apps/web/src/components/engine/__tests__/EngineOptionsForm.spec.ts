import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref } from 'vue'
import type { Ref } from 'vue'

vi.mock('@/api/documents', () => ({
  documentsApi: { parseEngines: vi.fn() },
}))

import { documentsApi } from '@/api/documents'
import { __resetParseEnginesForTest } from '@/composables/useParseEngines'
import EngineOptionsForm from '../EngineOptionsForm.vue'
import type { EngineChoice, ParseEngines } from '@/types/api'

const LISTING: ParseEngines = {
  default_engine: 'borndigital',
  capability_status: 'observed',
  engines: [
    { engine: 'vlm-ocr', model: 'deepseek-ocr-2', readiness: 'ready' },
    { engine: 'borndigital', model: 'borndigital', readiness: 'ready' },
  ],
}

interface FormHarness {
  wrapper: VueWrapper
  model: Ref<EngineChoice>
}

/** 用真实 useParseEngines 挂表单：只把 documentsApi.parseEngines 换成桩。 */
function mountForm(initial: EngineChoice, payload: ParseEngines | 'pending'): FormHarness {
  if (payload === 'pending') {
    vi.mocked(documentsApi.parseEngines).mockReturnValue(Promise.withResolvers<never>().promise as never)
  } else {
    vi.mocked(documentsApi.parseEngines).mockResolvedValue({ data: payload } as never)
  }
  const model = ref<EngineChoice>({ ...initial, options: { ...initial.options } })
  const Comp = defineComponent({
    setup() {
      return () => h(EngineOptionsForm, {
        modelValue: model.value,
        'onUpdate:modelValue': (v: EngineChoice) => { model.value = v },
      })
    },
  })
  // Element Plus 未在单测里注册：el-option 渲染成同名空壳元素，
  // 断言读的是它的 prop（value/label/disabled），不是渲染文本
  const wrapper: VueWrapper = mount(Comp)
  return { wrapper, model }
}

beforeEach(() => {
  // composable 的模块状态在同文件用例间是共享的，必须清回 loading
  __resetParseEnginesForTest()
})

describe('EngineOptionsForm 引擎清单', () => {
  it('只列服务端给的引擎：顺序照服务端，vlm-ocr 缀模型 id', async () => {
    const { wrapper } = mountForm({ engine: 'borndigital', options: {} }, LISTING)
    await flushPromises()
    await nextTick()
    const options = wrapper.findAll('el-option').map((o) => o.attributes())
    // **正好两条**：目录里还有 mineru schema，但服务端没给就不能列
    expect(options.map((o) => o.value)).toEqual(['vlm-ocr', 'borndigital'])
    expect(options[0]!.label).toBe('大模型 OCR（deepseek-ocr-2）')
    // 模型 id 与引擎名相同时不缀 —— 缀了就是"Born-digital（borndigital）"这种废话
    expect(options[1]!.label).toBe('Born-digital（无 GPU）')
    wrapper.unmount()
  })

  it('目录里没有的引擎仍然可选，按注册表名本身列出', async () => {
    const extended: ParseEngines = {
      ...LISTING,
      engines: [...LISTING.engines, { engine: 'future-ocr', model: 'future-1', readiness: 'ready' }],
    }
    const { wrapper } = mountForm({ engine: 'borndigital', options: {} }, extended)
    await flushPromises()
    await nextTick()
    const options = wrapper.findAll('el-option').map((o) => o.attributes())
    expect(options.map((o) => o.value)).toContain('future-ocr')
    expect(options.find((o) => o.value === 'future-ocr')!.label).toBe('future-ocr（future-1）')
    wrapper.unmount()
  })

  it('unhealthy / draining 禁选并缀「当前不可用」，unknown / configured 可选', async () => {
    const mixed: ParseEngines = {
      ...LISTING,
      engines: [
        { engine: 'vlm-ocr', model: 'deepseek-ocr-2', readiness: 'draining' },
        { engine: 'borndigital', model: 'borndigital', readiness: 'unhealthy' },
        { engine: 'scan-ocr', model: 'scan-ocr', readiness: 'unknown' },
        { engine: 'pdf-text', model: 'pdf-text', readiness: 'configured' },
      ],
    }
    const { wrapper, model } = mountForm({ engine: '', options: {} }, mixed)
    await flushPromises()
    await nextTick()
    const options = wrapper.findAll('el-option').map((o) => o.attributes())
    expect(options.find((o) => o.value === 'vlm-ocr')!.label).toContain('当前不可用')
    expect(options.find((o) => o.value === 'borndigital')!.label).toContain('当前不可用')
    expect(options.find((o) => o.value === 'vlm-ocr')!.disabled).toBeDefined()
    expect(options.find((o) => o.value === 'borndigital')!.disabled).toBeDefined()
    expect(options.find((o) => o.value === 'scan-ocr')!.label).not.toContain('当前不可用')
    expect(options.find((o) => o.value === 'pdf-text')!.label).not.toContain('当前不可用')
    // 空串偏好落到第一个可用引擎 —— 不是被禁掉的那两个
    expect(model.value.engine).toBe('scan-ocr')
    wrapper.unmount()
  })

  it('陈旧的 mineru 偏好被服务端缺省替换，参数按新引擎重置', async () => {
    const { wrapper, model } = mountForm({ engine: 'mineru', options: { lang: 'ch' } }, LISTING)
    await flushPromises()
    await nextTick()
    expect(model.value.engine).toBe('borndigital')
    expect(model.value.options).toEqual({})
    wrapper.unmount()
  })

  it('服务端缺省不在清单里时落到第一条可用引擎', async () => {
    const { wrapper, model } = mountForm(
      { engine: 'mineru', options: {} },
      { ...LISTING, default_engine: 'not-registered' },
    )
    await flushPromises()
    await nextTick()
    expect(model.value.engine).toBe('vlm-ocr')
    wrapper.unmount()
  })

  it('清单不可用时不画选择器，提示用服务端缺省并把 engine 置空', async () => {
    const unknown: ParseEngines = {
      default_engine: 'borndigital', capability_status: 'unknown', engines: [],
    }
    const { wrapper, model } = mountForm({ engine: 'mineru', options: { lang: 'ch' } }, unknown)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('el-select').exists()).toBe(false)
    expect(wrapper.text()).toContain('引擎列表暂时不可用，将使用服务端的默认引擎解析')
    expect(model.value).toEqual({ engine: '', options: {} })
    wrapper.unmount()
  })

  it('loading 期间不碰绑定值：清单到之前用户已经能打开对话框', async () => {
    // 请求永不落定：settleEngine 必须直接 return
    const { wrapper, model } = mountForm({ engine: 'mineru', options: { lang: 'ch' } }, 'pending')
    await flushPromises()
    await nextTick()
    expect(model.value).toEqual({ engine: 'mineru', options: { lang: 'ch' } })
    wrapper.unmount()
  })
})
