<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import AppShell from '@/layouts/AppShell.vue'
import BlankLayout from '@/layouts/BlankLayout.vue'

// 布局由路由 meta 决定：免登录页（登录）用空壳，其余走应用外壳
const route = useRoute()
const layout = computed(() => (route.meta.public ? BlankLayout : AppShell))
</script>

<template>
  <component :is="layout" />
</template>
