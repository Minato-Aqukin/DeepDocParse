<script setup lang="ts">
import { computed } from 'vue'

import type { StatusMeta, TagType } from '@/constants/status'

/**
 * 状态标签 —— 视觉规范准则三：**状态先靠形状，颜色是第二通道**。
 *
 * 实心点＝终态（已完成 / 失败 / 已吊销），空心圈＝进行中（排队 / 解析 / 索引）。
 * 换掉原来的 el-tag 彩色药丸，因为那种只靠颜色区分的做法在色觉障碍、黑白打印、
 * 12px 缩放下全部失效 —— 而文档库是要一屏三十行被扫读的。
 *
 * 样式全在 assets/ddp/ddp-base.css 的 .ddp-status，这里只做映射，不写颜色。
 * constants/status.ts 一个字都不用改，它仍是全站唯一的文案来源。
 */
const props = defineProps<{
  /** 直接传 parseStatusOf() / indexStatusOf() / confidenceOf() 的返回值 */
  meta?: StatusMeta
  /** 也可以不走文案表，手写一次性的状态（如 API key 的已吊销/可用） */
  label?: string
  type?: TagType
  /** 是否"还在动"。传了 meta 时默认取 meta.active */
  active?: boolean
}>()

/** Element Plus 的 tag type → 规范里的语义色。是形状之外的第二通道。 */
const DOT_CLASS: Record<TagType, string> = {
  success: 'is-ok',
  warning: 'is-warn',
  danger: 'is-danger',
  info: 'is-info',
  primary: 'is-plain',
}

const text = computed(() => props.label ?? props.meta?.label ?? '')

const classes = computed(() => {
  const type = props.type ?? props.meta?.type ?? 'info'
  // tsconfig 开了 noUncheckedIndexedAccess，查表要兜底
  const dot = DOT_CLASS[type] ?? 'is-plain'
  const live = props.active ?? props.meta?.active ?? false
  return live ? [dot, 'is-live'] : [dot]
})
</script>

<template>
  <span class="ddp-status" :class="classes">{{ text }}</span>
</template>
