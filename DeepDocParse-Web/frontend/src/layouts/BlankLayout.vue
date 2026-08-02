<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <div class="blank">
    <RouterView />
  </div>
</template>

<style scoped>
.blank {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-bg-color-page);
}
</style>
