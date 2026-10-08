import { computed, ref, type Ref } from 'vue'

import {
  DESKTOP_ERROR_META, SOURCE_ERROR_META, sourceErrorLabelOf, sourceStateLabelOf,
  desktopErrorLabelOf, type DesktopError, type SourceError,
} from '@deepdocparse/contracts'

import type { DesktopClientBridge, Result } from '../../../desktop/bridge'

export type { ClientView, ConnectionSummary, Json, PlanDetail, Result } from '../../../desktop/bridge'
export interface DesktopBridge extends DesktopClientBridge {
  hostStatus(): Promise<Result<HostStatusInfo>>
  selectWorkspace(): Promise<Result<{ workspaceId: string; name: string } | null>>
  setCredential(input: { environmentId: string; profileId: string; secret: string; persist: boolean }): Promise<Result<unknown>>
}
declare global { interface Window { ddpDesktop?: DesktopBridge } }

export function unwrap<T>(result: Result<T>): T {
  if (!result.ok) throw new Error(result.error.code)
  return result.value
}

/** Drafts are JSON snapshots; never pass renderer reactive proxies across IPC. */
export async function saveDesktopDraft(
  bridge: DesktopClientBridge, input: Parameters<DesktopClientBridge['clientSaveDraft']>[0],
): Promise<Result<{ revision: number }>> {
  let plain: typeof input
  try {
    plain = JSON.parse(JSON.stringify(input, function (key, value: unknown) {
      const original: unknown = (this as Record<string, unknown>)[key]
      if (original !== null && typeof original === 'object'
        && !Array.isArray(original) && Object.getPrototypeOf(original) !== Object.prototype) {
        throw new Error('invalid_arguments')
      }
      if (typeof value === 'undefined' || typeof value === 'function' || typeof value === 'symbol'
        || typeof value === 'bigint' || (typeof value === 'number' && !Number.isFinite(value))) {
        throw new Error('invalid_arguments')
      }
      return value
    })) as typeof input
  } catch {
    throw new Error('invalid_arguments')
  }
  try {
    return await bridge.clientSaveDraft(plain)
  } catch (cause) {
    // Electron may discard DOMException.name while crossing the isolated bridge.
    if (cause instanceof Error && cause.message === 'An object could not be cloned.') {
      throw new DOMException(cause.message, 'DataCloneError')
    }
    throw cause
  }
}
// ---------------------------------------------------------------------------
// 桌面数据源（DESKTOP-APPSHELL-PLAN wave 1）。
//
// `SourceSummary` 等形状与共享契约一致（`apps/desktop/bridge.d.ts` 落定后
// 这里改为直接 import 它的类型；字段名已按契约写死，切换只是换来源）。
// ---------------------------------------------------------------------------
export type SourceKind = 'local' | 'center'
export type SourceState = 'ready' | 'connecting' | 'signed_out' | 'unavailable'
export type SourceFeature = 'resources' | 'documents' | 'search' | 'wiki' | 'federation_tasks'
export interface SourceSummary {
  sourceId: string
  kind: SourceKind
  label: string
  state: SourceState
  readOnly: boolean
  features: SourceFeature[]
  active: boolean
  reason: string | null
  /** 交付定位用身份（HostProxy：与 transport_bindings 六字段逐项比对，label 永不参比）。旧宿主可能不带。 */
  environment?: { environmentId: string; workspaceId: string; authorityNodeId: string }
  profile?: { profileId: string; issuer: string; subject: string }
}
export interface CenterConnectInput {
  endpoint: string
  username: string
  password: string
  persist: boolean
  storageOrigin?: string
}
/** centerConnect 的实际落账：CredentialBroker 报的真实持久化模式（永远不要假定 persist:true 落盘了）。 */
export interface CenterConnectResult extends SourceSummary {
  credential?: { mode: 'persistent' | 'session' | 'absent'; reason: string | null }
}
export interface SourceBridge {
  sourceList(): Promise<Result<SourceSummary[]>>
  sourceActivate(input: { sourceId: string }): Promise<Result<SourceSummary>>
  sourceReconnect(input: { sourceId: string }): Promise<Result<SourceSummary>>
  sourceRemove(input: { sourceId: string }): Promise<Result<null>>
  workspaceOpen(): Promise<Result<SourceSummary | null>>
  centerConnect(input: CenterConnectInput): Promise<Result<CenterConnectResult>>
  onSourceChange(listener: (sources: SourceSummary[]) => void): () => void
  /** 断开但保留登记（宿主若未提供则不显示断开按钮）。 */
  sourceDisconnect?(input: { sourceId: string }): Promise<Result<SourceSummary[]>>
}
export interface HostCredentialStorage {
  backend: string
  persistentAvailable: boolean
  reason?: string | null
}
export interface HostStatusInfo {
  platform?: string
  electron?: string
  /** Electron 应用版本（宿主补上 `app.getVersion()`；缺席时更新页显示占位）。 */
  version?: string
  secrets: HostCredentialStorage
  credentialStorage?: HostCredentialStorage
  runtimeBackend?: string
  runtimeAvailable?: boolean
  runtimeReason?: string | null
  isolation?: string
  lifecycle?: string
}


/** 具名数据源方法挂在同一个 `window.ddpDesktop` 对象上（宿主侧同一 preload）。 */
function sourceBridge(): SourceBridge | undefined {
  const host = window.ddpDesktop as (DesktopBridge & Partial<SourceBridge>) | undefined
  if (!host || typeof host.sourceList !== 'function') return undefined
  return host as SourceBridge
}

export function isDesktop(): boolean {
  return typeof window !== 'undefined' && !!window.ddpDesktop
}

/**
 * 桌面下把相对路径指回宿主（`ddp://app`），开发 URL 模式（页面跑在
 * `http://localhost:5173` 但桥接的是 Electron 宿主）下也一样。
 * 浏览器里原样返回；绝对地址（含宿主改写后的 `ddp://app/_object/...`、
 * 预签名 https）一律原样返回。
 */
export function apiUrl(path: string): string {
  if (!isDesktop()) return path
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(path)) return path
  return `ddp://app${path.startsWith('/') ? path : `/${path}`}`
}

// 启动时读到的当前源。切换 = 宿主记下新源 + 整页重载，所以启动后它不变；
// 用 ref 存是为了让顶栏、导航、权限都跟着它响应式更新。
const bootSource: Ref<SourceSummary | null> = ref(null)
let bootLoaded = false

export { bootSource }
export function getActiveSource(): SourceSummary | null {
  return bootSource.value
}
export function getActiveSourceId(): string | null {
  return bootSource.value?.sourceId ?? null
}
export function setActiveSource(source: SourceSummary | null): void {
  bootSource.value = source
}
/**
 * 当前是本机工作区数据源。它按 local-content-subset 只实现中心内容接口的一个子集：
 * 公开/撤下、授权副本、许可来源、重新解析、重建/校验索引、解析产物下载都不提供 ——
 * 这些入口在本机源上隐藏，而不是点了再报 not_supported_locally。
 */
// bootSource is read first so the computed always tracks it (isDesktop() is not reactive).
export const onLocalSource = computed(() => bootSource.value?.kind === 'local' && isDesktop())
/**
 * 当前是只读数据源（桌面里的中心）：非 GET 在宿主一律 `approved_plan_required`。
 * 与 `onLocalSource` 同一依据（启动源；切换数据源整页重载），不依赖 Pinia，
 * 供没有 auth store 的组件用；写入口禁用并写明 `approvedPlanLabel()`。
 */
export const onReadOnlySource = computed(() => bootSource.value?.readOnly === true && isDesktop())

function pickActive(sources: SourceSummary[]): SourceSummary | null {
  return sources.find((s) => s.active) ?? null
}

/** 启动时宿主可能正在后台重连上次的当前源（`connecting`）：等它落定再挂载，
 *  否则首屏会把一个几秒后就可用的源当成"没有数据源"。上限 60 秒，超时按当时状态走。 */
const CONNECTING_WAIT_MS = 60_000
async function settledActive(bridge: SourceBridge, initial: SourceSummary | null): Promise<SourceSummary | null> {
  let active = initial
  for (const deadline = Date.now() + CONNECTING_WAIT_MS; active?.state === 'connecting' && Date.now() < deadline;) {
    // 构建目标是 ES2022（见 tsconfig.app.json）：Promise.withResolvers 不在其内
    await new Promise<void>((resolve) => setTimeout(resolve, 250))
    const listed = await bridge.sourceList().catch(() => null)
    active = listed?.ok ? pickActive(listed.value) : active
  }
  return active
}

/** main.ts 在挂载前调用：读不到就保持 null，根路由会把首屏落在数据源页。 */
export async function initDesktopSource(): Promise<void> {
  const bridge = sourceBridge()
  if (!bridge) {
    setActiveSource(null)
    bootLoaded = true
    return
  }
  try {
    const result = await bridge.sourceList()
    setActiveSource(await settledActive(bridge, result.ok ? pickActive(result.value) : null))
  } catch {
    setActiveSource(null)
  } finally {
    bootLoaded = true
  }
}

export function isBootLoaded(): boolean {
  return bootLoaded
}

export async function refreshDesktopSource(): Promise<SourceSummary[]> {
  const bridge = sourceBridge()
  if (!bridge) return []
  const result = await bridge.sourceList()
  const sources = result.ok ? result.value : []
  setActiveSource(sources.find((s) => s.active) ?? null)
  return sources
}
/** 桌面 401 / 守卫拦截：去数据源页（带上过期原因），绝不去 Web 登录页。 */
export function goDesktopSources(reason?: string): void {
  const suffix = reason ? `?reason=${encodeURIComponent(reason)}` : ''
  location.hash = `#/sources${suffix}`
}

export function sourceStateLabel(state: SourceState | string | null | undefined): string {
  return sourceStateLabelOf(state) ?? '未知状态'
}
export function sourceErrorLabel(code: string | null | undefined): string {
  if (!code) return '操作未完成，请查看数据源状态。'
  // Source methods raise source_error codes and the shared host desktop_error codes
  // (identity_mismatch, authentication_required, connection_failed …).
  const meta = SOURCE_ERROR_META[code as SourceError] ?? DESKTOP_ERROR_META[code as DesktopError]
  return meta?.label ?? sourceErrorLabelOf(code)!
}

/**
 * 单值来源校验（fetch/SSE 那条不用 axios 的路）。
 * 浏览器永远 true；桌面下与 `isCurrentSourceResponse` 同一规则：
 * 已知启动源时，缺头/坏头/头不一致一律丢弃（fail closed）——宿主的每个 /api
 * 响应都带 `X-DDP-Source`（有当前源时的错误响应也带），收不到它说明字节
 * 不是来自已启动源（切换瞬间串源、去头路径），绝不在当前外发标签下显示。
 */
export function checkSourceResponse(seen: string | null | undefined): boolean {
  if (!isDesktop()) return true
  const boot = getActiveSourceId()
  if (!boot) return true
  if (seen == null) return false
  return seen === boot
}

/** 中心只读下写按钮的统一禁用说明。 */
export function approvedPlanLabel(): string {
  return sourceErrorLabel('approved_plan_required')
}

/**
 * 中心源的「作为联邦任务发起」预填暂存。
 *
 * 联邦任务只在本机账本建：中心源下入口先把预填（query/purpose/title）按目标
 * 本机源 `sourceId` 存进 sessionStorage，再切源（宿主记下新源 + 整页重载）。
 * 重载后 `LocalTaskPrepare` 用它预填表单，用后即删 —— 不经 URL 跨源传问题文本，
 * 不落 localStorage（另一源重载后不应再看到）。
 */
export const TASK_PREFILL_KEY = 'ddp.task-prefill'
export interface TaskPrefillTarget { sourceId: string; query?: string; purpose?: 'answer' | 'wiki'; title?: string }
export function stashTaskPrefill(target: TaskPrefillTarget): void {
  try {
    sessionStorage.setItem(TASK_PREFILL_KEY, JSON.stringify(target))
  } catch {
    // 暂存失败不挡切源：准备页按无预填打开，用户手动再填一次。
  }
}
/** 取出暂存的预填：只认当前本机源的，用后即删；他源/损坏一律丢弃。 */
export function takeTaskPrefill(sourceId: string): TaskPrefillTarget | null {
  let raw: string | null = null
  try {
    raw = sessionStorage.getItem(TASK_PREFILL_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<TaskPrefillTarget>
    if (parsed.sourceId !== sourceId) return null
    try {
      sessionStorage.removeItem(TASK_PREFILL_KEY)
    } catch {
      // 删不掉也不挡预填：下次同源打开会复用同一份，用后还会再删。
    }
    return {
      sourceId: parsed.sourceId,
      ...(typeof parsed.query === 'string' && parsed.query ? { query: parsed.query } : {}),
      ...(parsed.purpose === 'wiki' ? { purpose: 'wiki' as const } : {}),
      ...(typeof parsed.title === 'string' && parsed.title ? { title: parsed.title } : {}),
    }
  } catch {
    try {
      sessionStorage.removeItem(TASK_PREFILL_KEY)
    } catch {
      // 删不掉也不挡预填：损坏的暂存下次还会解析失败再删一次。
    }
    return null
  }
}

 /** 「作为联邦任务发起」的目标：问题/用途/标题经 query 参数预填新建页（wave 2 扩展了用途与标题）。 */
export interface FederationTaskPrefill { query?: string; purpose?: 'answer' | 'wiki'; title?: string }
export function federationTaskNewLocation(prefill: string | FederationTaskPrefill): { name: string; query: Record<string, string> } {
  const params = typeof prefill === 'string' ? { query: prefill } : prefill
  const query: Record<string, string> = {}
  if (params.query) query.query = params.query
  if (params.purpose) query.purpose = params.purpose
  if (params.title) query.title = params.title
  return { name: 'federation-task-new', query }
}

/** 顶栏外发状态。文案按 plan §1.2/顶栏硬要求钉死。 */
export function egressStatus(source: SourceSummary | null): { text: string; kind: 'local' | 'center' | 'none' } {
  if (!source) return { text: '未选择数据源', kind: 'none' }
  if (source.kind === 'local') return { text: '本机执行 · 原件不外发', kind: 'local' }
  return { text: '只读 · 写入须作为联邦任务发起并批准', kind: 'center' }
}

/**
 * 桌面本机错误码的用户文案。**全部来自契约生成物**（`enums.yaml` 的 `desktop_error` 组），
 * 这里只查表，不许再手写第二份中文 —— 旧 `reasons` 表已搬入契约。
 * 未知码给兜底（不变式 2：不留白），但那意味着契约漏了码，应该去补。
 */
export function workspaceError(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  return desktopErrorLabelOf(code) ?? '操作未完成，请查看连接和任务状态。'
}

/** 已落账的失败码：有契约文案时写「文案（码）」便于对账；未知码的兜底文案本身已带码，不再重复。 */
export function workspaceFailure(code: string): string {
  return Object.hasOwn(DESKTOP_ERROR_META, code) ? `${desktopErrorLabelOf(code)}（${code}）` : workspaceError(new Error(code))
}
