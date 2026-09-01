<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import AppShell from '@/layouts/AppShell.vue'
import BlankLayout from '@/layouts/BlankLayout.vue'

// 布局由路由 meta 决定：免登录页（登录）用空壳，其余走应用外壳
const route = useRoute()
const layout = computed(() => (route.meta.public ? BlankLayout : AppShell))
</script>

<template>
  <!--
    Element Plus 的内置文案默认是英文（空表格是 "No Data"、分页是 "Go to"），
    在一个全中文界面里很扎眼。视觉规范要求英文只在它是真信息时出现
    （编号、哈希、字段名），UI 骨架文案不算 —— 所以挂上中文 locale。
  -->
  <el-config-provider :locale="zhCn">
    <component :is="layout" />
  </el-config-provider>
</template>
