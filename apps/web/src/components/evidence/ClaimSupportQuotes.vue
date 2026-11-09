<script setup lang="ts">
import type { WikiClaimSupport } from '@/api/wiki'

defineProps<{ support?: WikiClaimSupport[] }>()
const emit = defineEmits<{ select: [evidenceId: string] }>()
</script>

<template>
  <details v-if="support?.length" class="support-quotes">
    <summary>模型选取的原文摘录 · 语义待复核</summary>
    <blockquote v-for="(item, index) in support" :key="`${item.evidence_id}:${index}`">
      <p>{{ item.quote }}</p>
      <button type="button" @click="emit('select', item.evidence_id)">打开此摘录的原文</button>
    </blockquote>
  </details>
</template>

<style scoped>
.support-quotes { grid-column: 1 / -1; color: var(--ddp-ink-2); font-size: 13px; }
summary { display: list-item; min-height: 44px; padding-top: 10px; box-sizing: border-box; cursor: pointer; }
blockquote { margin: 0 0 12px; padding-left: 12px; border-left: 1px solid var(--ddp-line); }
p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
button { min-height: 44px; border: 0; padding: 0; background: transparent; color: var(--ddp-cite); cursor: pointer; font: inherit; }
button:hover { text-decoration: underline; }
</style>
