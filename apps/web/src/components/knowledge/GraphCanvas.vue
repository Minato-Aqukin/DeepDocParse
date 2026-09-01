<script setup lang="ts">
import {
  forceCenter,
  forceLink,
  forceManyBody,
  forceSimulation,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { KnowledgeEdge, KnowledgeEntity } from '@/types/api'

interface NodeDatum extends SimulationNodeDatum {
  id: string
  row: KnowledgeEntity
}
interface LinkDatum extends SimulationLinkDatum<NodeDatum> {
  id: string
  row: KnowledgeEdge
}

const props = withDefaults(defineProps<{
  entities: KnowledgeEntity[]
  edges: KnowledgeEdge[]
  height?: number
  selectedEntityId?: string
}>(), { height: 520, selectedEntityId: '' })
const emit = defineEmits<{
  (event: 'select-node', row: KnowledgeEntity): void
  (event: 'select-edge', row: KnowledgeEdge): void
}>()

const host = ref<HTMLDivElement>()
const canvas = ref<HTMLCanvasElement>()
let context: CanvasRenderingContext2D | null = null
let simulation: Simulation<NodeDatum, LinkDatum> | null = null
let resizeObserver: ResizeObserver | null = null
let nodes: NodeDatum[] = []
let links: LinkDatum[] = []
let width = 0
let ratio = 1
let transform = { x: 0, y: 0, k: 1 }
let hovered: NodeDatum | null = null
let dragged: NodeDatum | null = null
let panning = false
let pointerStart = { x: 0, y: 0 }
let transformStart = { x: 0, y: 0 }

function css(name: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

function rebuild() {
  simulation?.stop()
  const old = new Map(nodes.map((node) => [node.id, node]))
  nodes = props.entities.map((row, index) => ({
    id: row.id,
    row,
    x: old.get(row.id)?.x ?? width / 2 + Math.cos(index) * 20,
    y: old.get(row.id)?.y ?? props.height / 2 + Math.sin(index) * 20,
  }))
  const ids = new Set(nodes.map((node) => node.id))
  links = props.edges.filter((edge) => ids.has(edge.subject_id) && ids.has(edge.object_id)).map(
    (row) => ({ id: row.id, row, source: row.subject_id, target: row.object_id }),
  )
  simulation = forceSimulation<NodeDatum>(nodes)
    .force('link', forceLink<NodeDatum, LinkDatum>(links).id((node) => node.id).distance(76).strength(0.25))
    .force('charge', forceManyBody().strength(nodes.length > 500 ? -18 : -80))
    .force('center', forceCenter(width / 2, props.height / 2))
    .alphaDecay(nodes.length > 500 ? 0.08 : 0.035)
    .on('tick', draw)
  draw()
}

function resize() {
  if (!host.value || !canvas.value) return
  width = Math.max(240, host.value.clientWidth)
  ratio = Math.min(window.devicePixelRatio || 1, 2)
  canvas.value.width = Math.floor(width * ratio)
  canvas.value.height = Math.floor(props.height * ratio)
  canvas.value.style.width = `${width}px`
  canvas.value.style.height = `${props.height}px`
  context = canvas.value.getContext('2d')
  rebuild()
}

function point(node: NodeDatum) {
  return { x: (node.x ?? 0) * transform.k + transform.x,
           y: (node.y ?? 0) * transform.k + transform.y }
}

function connectedIds() {
  if (!hovered) return null
  const ids = new Set([hovered.id])
  for (const link of links) {
    const source = typeof link.source === 'object' ? link.source.id : String(link.source)
    const target = typeof link.target === 'object' ? link.target.id : String(link.target)
    if (source === hovered.id) ids.add(target)
    if (target === hovered.id) ids.add(source)
  }
  return ids
}

function draw() {
  if (!context) return
  const ctx = context
  const neighbors = connectedIds()
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, width, props.height)
  ctx.lineWidth = 1
  for (const link of links) {
    const source = point(link.source as NodeDatum)
    const target = point(link.target as NodeDatum)
    const active = !neighbors || neighbors.has((link.source as NodeDatum).id)
      && neighbors.has((link.target as NodeDatum).id)
    ctx.globalAlpha = active ? 0.72 : 0.08
    ctx.strokeStyle = link.row.unsupported ? css('--ddp-cite', '#ad2f2f') : css('--ddp-line-strong', '#85817a')
    ctx.beginPath()
    ctx.moveTo(source.x, source.y)
    ctx.lineTo(target.x, target.y)
    ctx.stroke()
  }
  for (const node of nodes) {
    const p = point(node)
    const active = !neighbors || neighbors.has(node.id)
    const radius = node.id === props.selectedEntityId ? 7 : 5
    ctx.globalAlpha = active ? 1 : 0.12
    ctx.fillStyle = node.row.entity_merge_uncertain
      ? css('--ddp-cite', '#ad2f2f') : css('--ddp-panel', '#fff')
    ctx.strokeStyle = node.row.entity_merge_uncertain
      ? css('--ddp-cite', '#ad2f2f') : css('--ddp-ink', '#25231f')
    ctx.lineWidth = node.id === props.selectedEntityId ? 2 : 1
    ctx.beginPath()
    ctx.arc(p.x, p.y, radius, 0, Math.PI * 2)
    ctx.fill()
    ctx.stroke()
    if (active && (nodes.length < 180 || hovered === node || node.id === props.selectedEntityId)) {
      ctx.globalAlpha = 0.92
      ctx.fillStyle = css('--ddp-ink-2', '#504d47')
      ctx.font = `12px ${css('--ddp-font-sans', 'sans-serif')}`
      ctx.fillText(node.row.canonical_name.slice(0, 30), p.x + 9, p.y + 4)
    }
  }
  ctx.globalAlpha = 1
}

function pointer(event: PointerEvent | WheelEvent) {
  const rect = canvas.value!.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}

function hitNode(at: { x: number; y: number }): NodeDatum | null {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const node = nodes[i]
    if (!node) continue
    const p = point(node)
    if (Math.hypot(p.x - at.x, p.y - at.y) <= 10) return node
  }
  return null
}

function hitEdge(at: { x: number; y: number }) {
  let best: { row: KnowledgeEdge; distance: number } | null = null
  for (const link of links) {
    const a = point(link.source as NodeDatum), b = point(link.target as NodeDatum)
    const dx = b.x - a.x, dy = b.y - a.y
    const length2 = dx * dx + dy * dy || 1
    const t = Math.max(0, Math.min(1, ((at.x - a.x) * dx + (at.y - a.y) * dy) / length2))
    const distance = Math.hypot(at.x - (a.x + t * dx), at.y - (a.y + t * dy))
    if (distance <= 6 && (!best || distance < best.distance)) best = { row: link.row, distance }
  }
  return best?.row ?? null
}

function onMove(event: PointerEvent) {
  const at = pointer(event)
  if (dragged) {
    dragged.fx = (at.x - transform.x) / transform.k
    dragged.fy = (at.y - transform.y) / transform.k
    simulation?.alphaTarget(0.18).restart()
  } else if (panning) {
    transform.x = transformStart.x + at.x - pointerStart.x
    transform.y = transformStart.y + at.y - pointerStart.y
    draw()
  } else {
    hovered = hitNode(at)
    if (canvas.value) canvas.value.style.cursor = hovered ? 'grab' : hitEdge(at) ? 'pointer' : 'default'
    draw()
  }
}

function onDown(event: PointerEvent) {
  const at = pointer(event)
  pointerStart = at
  dragged = hitNode(at)
  if (dragged) {
    dragged.fx = dragged.x
    dragged.fy = dragged.y
  } else {
    panning = true
    transformStart = { x: transform.x, y: transform.y }
  }
  canvas.value?.setPointerCapture(event.pointerId)
}

function onUp(event: PointerEvent) {
  const moved = Math.hypot(pointer(event).x - pointerStart.x, pointer(event).y - pointerStart.y) > 4
  if (!moved) {
    const node = hitNode(pointer(event))
    const edge = node ? null : hitEdge(pointer(event))
    if (node) emit('select-node', node.row)
    else if (edge) emit('select-edge', edge)
  }
  if (dragged) dragged.fx = dragged.fy = null
  dragged = null
  panning = false
  simulation?.alphaTarget(0)
}

function onWheel(event: WheelEvent) {
  event.preventDefault()
  const at = pointer(event)
  const old = transform.k
  const next = Math.max(0.25, Math.min(4, old * Math.exp(-event.deltaY * 0.001)))
  transform.x = at.x - (at.x - transform.x) * (next / old)
  transform.y = at.y - (at.y - transform.y) * (next / old)
  transform.k = next
  draw()
}

function resetView() {
  transform = { x: 0, y: 0, k: 1 }
  simulation?.alpha(0.5).restart()
  draw()
}

watch(() => [props.entities, props.edges], rebuild, { deep: true })
watch(() => props.selectedEntityId, draw)
onMounted(async () => {
  await nextTick()
  resizeObserver = new ResizeObserver(resize)
  if (host.value) resizeObserver.observe(host.value)
  resize()
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  simulation?.stop()
})
</script>

<template>
  <div ref="host" class="graph-host">
    <canvas ref="canvas" tabindex="0" aria-label="实体关系图谱；可拖拽节点、滚轮缩放、点击边查看证据"
            @pointermove="onMove" @pointerdown="onDown" @pointerup="onUp"
            @pointercancel="onUp" @pointerleave="hovered = null; draw()" @wheel="onWheel" />
    <button class="reset" type="button" @click="resetView">复位视图</button>
    <p class="legend"><span />普通实体　<i />证据缺失或低置信合并</p>
  </div>
</template>

<style scoped>
.graph-host { position: relative; min-width: 0; overflow: hidden; border: 1px solid var(--ddp-line); background: var(--ddp-panel); }
canvas { display: block; touch-action: none; outline: none; }
canvas:focus-visible { outline: 2px solid var(--ddp-ink); outline-offset: -3px; }
.reset { position: absolute; top: 10px; right: 10px; min-height: 36px; padding: 0 12px; border: 1px solid var(--ddp-line-strong); background: var(--ddp-panel); color: var(--ddp-ink); cursor: pointer; }
.legend { position: absolute; left: 10px; bottom: 8px; margin: 0; padding: 4px 7px; background: color-mix(in srgb, var(--ddp-panel) 90%, transparent); color: var(--ddp-ink-3); font-size: 11px; }
.legend span, .legend i { display: inline-block; width: 8px; height: 8px; margin-right: 3px; border: 1px solid var(--ddp-ink); border-radius: 50%; background: var(--ddp-panel); }
.legend i { margin-left: 8px; border-color: var(--ddp-cite); background: var(--ddp-cite); }
</style>
