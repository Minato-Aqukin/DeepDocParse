<script setup lang="ts">
defineProps<{ label: string; value: number | string; hint?: string; icon?: string }>()
</script>

<template>
  <el-card shadow="never" class="stat">
    <div class="row">
      <div>
        <div class="label">{{ label }}</div>
        <div class="value">{{ value }}</div>
        <div v-if="hint" class="hint">{{ hint }}</div>
      </div>
      <el-icon v-if="icon" class="icon"><component :is="icon" /></el-icon>
    </div>
  </el-card>
</template>

<style scoped>
.row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}
.label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.value {
  font-size: 26px;
  font-weight: 600;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.icon {
  font-size: 22px;
  color: var(--el-color-primary);
  opacity: 0.5;
}
</style>
