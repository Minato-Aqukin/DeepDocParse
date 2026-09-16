import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StatusTag from '../StatusTag.vue'

describe('StatusTag', () => {
  it('没传 active 时取文案表的 active：进行中画空心圈', () => {
    const live = mount(StatusTag, { props: { meta: { label: '执行中', type: 'warning', active: true } } })
    expect(live.classes()).toContain('is-live')
    const settled = mount(StatusTag, { props: { meta: { label: '已完成', type: 'success' } } })
    expect(settled.classes()).not.toContain('is-live')
  })

  it('显式传 active 覆盖文案表', () => {
    const forced = mount(StatusTag, { props: { meta: { label: '执行中', type: 'warning', active: true }, active: false } })
    expect(forced.classes()).not.toContain('is-live')
    const manual = mount(StatusTag, { props: { label: '同步中', type: 'info', active: true } })
    expect(manual.classes()).toContain('is-live')
  })
})
