<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { searchApi } from '@/api'
import StatusTag from '@/components/common/StatusTag.vue'
import { DEFAULT_WARN_BELOW, similarityText } from '@/constants/status'
import type { SearchHit, SearchResult } from '@/types/api'

/** 跨文档检索：命中带页码，点击直达工作台对应页。 */
const route = useRoute()
const router = useRouter()

const keyword = ref(String(route.query.q || ''))
const groups = ref<SearchResult['groups']>([])
const degraded = ref<string | null>(null)
const loading = ref(false)
const failed = ref(false)

let generation = 0
onBeforeUnmount(() => { generation++ })

function target(group: SearchResult['groups'][number], hit?: SearchHit) {
  return { name: 'workbench', params: { id: group.document_id }, query: {
    resource_id: group.resource_id || undefined, version_id: group.source_version_id || undefined,
    job: group.parse_revision, chunk: hit?.chunk_id,
    page: hit ? String(hit.page_idx + 1) : undefined,
  } }
}

async function loadResults(query: string) {
  const current = ++generation
  groups.value = []
  degraded.value = null
  failed.value = false
  if (!query.trim()) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const { data } = await searchApi.query(query)
    if (current !== generation) return
    groups.value = data.groups
    degraded.value = data.degraded ?? null
  } catch {
    if (current === generation) failed.value = true
  } finally {
    if (current === generation) loading.value = false
  }
}

function run() {
  const query = keyword.value.trim()
  if (route.query.q !== (query || undefined)) {
    void router.replace({ name: 'search', query: query ? { q: query } : {} })
  } else {
    void loadResults(query)
  }
}

watch(() => route.query.q, query => {
  keyword.value = typeof query === 'string' ? query : ''
  void loadResults(keyword.value)
}, { immediate: true })
</script>

<template>
  <div class="bar">
    <el-input v-model="keyword" placeholder="在可访问的资源中检索" clearable class="search"
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

  <el-alert v-if="degraded === 'resource_index_unavailable'" type="warning" :closable="false"
    title="资源尚无可用的固定版本索引，请查看解析任务状态。" />

  <el-alert v-if="failed" type="error" :closable="false" title="检索失败，请重试。" />
  <el-empty v-if="!groups.length && !loading && !failed"
            :description="route.query.q ? '没有命中' : '输入关键词开始检索'" />

  <el-card v-for="group in groups" :key="group.source_version_id || group.document_id" shadow="never" class="group">
    <template #header>
      <router-link :to="target(group)" class="filename">
        {{ group.filename }}
      </router-link>
      <!-- 同一资源的各版本都可检索且文件名相同，不写版本号就分不清哪条是旧版 -->
      <span class="count">
        <template v-if="group.source_version_no">第 {{ group.source_version_no }} 版 · </template>{{ group.hits.length }} 处命中
      </span>
    </template>
    <router-link v-for="hit in group.hits" :key="hit.chunk_id" class="hit"
                 :to="target(group, hit)">
      <!-- 页码是元信息不是状态，按准则二排成普通文字，不做成标签 -->
      <span class="page ddp-cite-page">PDF 第 {{ hit.page_idx + 1 }} 页</span>
      <!-- 相关度用 similarity（有校准量纲），不用 score（RRF 名次分，表达不了相关度）。
           阈值收在 constants/status.ts，不再在这里写第二个字面量 -->
      <StatusTag
        v-if="similarityText(hit.similarity)"
        :label="similarityText(hit.similarity)!"
        :type="(hit.similarity ?? 0) >= DEFAULT_WARN_BELOW ? 'success' : 'warning'"
      />
      <span class="snippet">{{ hit.snippet }}</span>
    </router-link>
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
/* 页码：等宽 + 不换行。**颜色交给 .ddp-cite-page** —— 页码属于"出处"，
   按准则一该是红的；这里再写 color 会以 (0,2,0) 压过全局那条 (0,1,0)。 */
.page {
  font-family: var(--ddp-font-mono);
  font-size: 12px;
  white-space: nowrap;
  flex: none;
}
.hit {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 6px 0;
  color: inherit;
  text-decoration: none;
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
