<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * 单序列日柱状图。
 *
 * 刻意做成"小倍数"：页数与请求数量纲不同，绝不共用一张图的双 Y 轴 ——
 * 两个测量各占一张图，各自一条序列（单序列不需要图例，标题即身份）。
 */
const props = defineProps<{
  title: string
  unit: string
  data: { date: string; value: number }[]
  color: string
}>()

const W = 720
const H = 180
const PAD = { top: 12, right: 8, bottom: 22, left: 44 }
const GAP = 2 // 相邻柱之间留出的表面缝隙

const hover = ref<number | null>(null)

const max = computed(() => Math.max(1, ...props.data.map((d) => d.value)))
const plotW = W - PAD.left - PAD.right
const plotH = H - PAD.top - PAD.bottom
const baseline = PAD.top + plotH

const step = computed(() => plotW / Math.max(1, props.data.length))
const barW = computed(() => Math.max(1, step.value - GAP))

/** 轴刻度：0 / 半 / 满，只要三条，网格保持退让。 */
const ticks = computed(() =>
  [0, max.value / 2, max.value].map((v) => ({
    value: v,
    y: baseline - (v / max.value) * plotH,
    label: v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(Math.round(v)),
  })),
)

function barPath(value: number, index: number): string {
  const h = (value / max.value) * plotH
  if (h <= 0) return ''
  const x = PAD.left + index * step.value + GAP / 2
  const w = barW.value
  const y = baseline - h
  const r = Math.min(4, w / 2, h) // 数据端 4px 圆角，底部贴基线
  return `M${x},${baseline} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${baseline} Z`
}

/** x 轴标签按密度抽稀，避免碰撞。 */
const labelEvery = computed(() => Math.ceil(props.data.length / 8))

const hovered = computed(() => (hover.value === null ? undefined : props.data[hover.value]))
</script>

<template>
  <figure class="viz-root">
    <figcaption class="title">{{ title }}</figcaption>
    <div class="plot">
      <svg :viewBox="`0 0 ${W} ${H}`" role="img" :aria-label="title" preserveAspectRatio="none">
        <g class="grid">
          <line
            v-for="t in ticks"
            :key="`g-${t.value}`"
            :x1="PAD.left"
            :x2="W - PAD.right"
            :y1="t.y"
            :y2="t.y"
          />
        </g>
        <g class="axis-text">
          <text
            v-for="t in ticks"
            :key="`t-${t.value}`"
            :x="PAD.left - 8"
            :y="t.y + 4"
            text-anchor="end"
          >
            {{ t.label }}
          </text>
        </g>

        <path
          v-for="(d, i) in data"
          :key="d.date"
          :d="barPath(d.value, i)"
          :fill="color"
          :opacity="hover === null || hover === i ? 1 : 0.55"
        />

        <g class="axis-text">
          <text
            v-for="(d, i) in data"
            :key="`x-${d.date}`"
            v-show="i % labelEvery === 0"
            :x="PAD.left + i * step + step / 2"
            :y="H - 6"
            text-anchor="middle"
          >
            {{ d.date.slice(5) }}
          </text>
        </g>

        <!-- 命中区比柱子宽：鼠标不需要精确对准细柱 -->
        <rect
          v-for="(d, i) in data"
          :key="`hit-${d.date}`"
          :x="PAD.left + i * step"
          :y="PAD.top"
          :width="step"
          :height="plotH"
          fill="transparent"
          @mouseenter="hover = i"
          @mouseleave="hover = null"
        />
      </svg>
      <div v-if="hovered" class="tooltip">
        <strong>{{ hovered.date }}</strong>
        <span>{{ hovered.value }} {{ unit }}</span>
      </div>
    </div>
  </figure>
</template>

<style scoped>
.viz-root {
  --grid: var(--ddp-line);
  --muted: var(--ddp-ink-3);
  margin: 0;
}
.title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}
.plot {
  position: relative;
}
svg {
  width: 100%;
  height: 180px;
  display: block;
}
.grid line {
  stroke: var(--grid);
  stroke-width: 1;
}
.axis-text text {
  fill: var(--muted);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.tooltip {
  position: absolute;
  top: 0;
  right: 0;
  display: flex;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  pointer-events: none;
}
</style>
