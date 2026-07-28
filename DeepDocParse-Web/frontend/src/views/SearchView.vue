<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'

/** 跨文档检索：命中带页码，点击直达工作台对应页。 */
const route = useRoute()
const router = useRouter()

const keyword = ref(String(route.query.q || ''))
const groups = ref<Awaited<ReturnType<typeof api.search>>['data']['groups']>([])
const degraded = ref<string | null>(null)
const loading = ref(false)

async function run() {
  if (!keyword.value.trim()) return
  loading.value = true
  try {
    const { data } = await api.search(keyword.value)
    groups.value = data.groups
    degraded.value = data.degraded ?? null
    router.replace({ name: 'search', query: { q: keyword.value } })
  } finally {
    loading.value = false
  }
}

watch(() => route.query.q, run, { immediate: true })
</script>

<template>
  <div class="bar">
    <el-input v-model="keyword" placeholder="在你的全部文档里检索" clearable class="search"
              @keyup.enter="run" />
    <el-button type="primary" :loading="loading" @click="run">搜索</el-button>
  </div>

  <!-- 降级要说出来：只走了关键词路却装作语义检索还在工作，就是静默降级 -->
  <el-alert
    v-if="degraded === 'embedding_unavailable'"
    type="warning"
    :closable="false"
    class="degraded"
    title="向量化服务不可用，本次仅做了关键词检索（语义相近但用词不同的内容可能漏掉）"
  />

  <el-empty v-if="!groups.length && !loading" description="没有命中" />

  <el-card v-for="group in groups" :key="group.document_id" shadow="never" class="group">
    <template #header>
      <router-link :to="`/documents/${group.document_id}`" class="filename">
        {{ group.filename }}
      </router-link>
      <span class="count">{{ group.hits.length }} 处命中</span>
    </template>
    <div v-for="(hit, i) in group.hits" :key="i" class="hit"
         @click="router.push(`/documents/${group.document_id}`)">
      <el-tag size="small" type="info">第 {{ hit.page_idx + 1 }} 页</el-tag>
      <span class="snippet">{{ hit.snippet }}</span>
    </div>
  </el-card>
</template>

<style scoped>
.bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.search {
  max-width: 520px;
}
.degraded {
  margin-bottom: 12px;
}
.group {
  margin-bottom: 12px;
}
.filename {
  font-weight: 600;
  margin-right: 8px;
}
.count {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.hit {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 6px 0;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.hit:hover .snippet {
  color: var(--el-color-primary);
}
.snippet {
  line-height: 1.6;
}
</style>
