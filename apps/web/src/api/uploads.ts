/**
 * 直传上传 —— **字节流不经过任何应用进程**。
 *
 * 合仓前是 `POST /api/documents` 的 multipart：浏览器把文件发给后端，
 * 后端读进一个 `bytes` 再 put 到对象存储。200MB 的文件就是 200MB 的常驻内存，
 * 而扩容应用等于放大对象存储的带宽中转（不变式 6）。
 *
 * 现在的三步（契约见 `packages/contracts/openapi/control-v1.yaml` §9.1）：
 *
 *   1. POST /api/uploads            拿 multipart 预签名（服务端先校验权限/配额/MIME/大小）
 *   2. PUT  <每片的预签名 URL>       **直接传给对象存储**，不经过后端
 *   3. POST /api/uploads/{id}/finalize   服务端核对真实大小，异步校验摘要
 *
 * finalize 返回 202 与 `verifying` —— **不是 ready**。摘要还没校验完，
 * 文档在通过校验之前不得进入解析。前端据此显示"校验中"，而不是假装已经好了。
 *
 * 字节就绪之后还有一段**登记确认**（ingest）：服务端把 `DocumentSubmitted`
 * 投递给语料域，corpus 幂等消费确认之后，会话的 `ingest_status` 才变 `ready`。
 * 只有 2xx 或 `409 duplicate_event` 算确认；目标失效/同内容版本已存在等确定性拒绝
 * 是终态 `rejected`（`ingest_error` 为契约枚举 `ingest_rejection` 的码），
 * 投递抖动是可重试的 `retrying`。`ready` 只表示登记已确认 ——
 * 解析/索引是另一条链路，另行展示，绝不能把"已登记"说成"已解析"。
 *
 * 创建带 `Idempotency-Key` + 完整 sha256：创建响应丢了、分片传到一半断了，
 * 用同一个键重来拿回的是**同一个**上传会话（只补传缺的分片），不会多造一份资产。
 */
import { INGEST_STATUS_VALUES } from '@deepdocparse/contracts'
import type { IngestRejection, IngestStatus, UploadStatus } from '@deepdocparse/contracts'

import { apiUrl } from '@/platform/desktop'

import { http } from './http'

export interface UploadPart {
  part_number: number
  url: string
}

export interface CompletedUploadPart {
  part_number: number
  etag: string
  size: number
}

export interface UploadSession {
  id: string
  status: UploadStatus
  object_key: string
  filename: string
  mime: string
  declared_size: number
  part_size: number
  allocation_state?: 'pending' | 'allocating' | 'ready' | 'unknown'
  parts?: UploadPart[]
  /** 对象存储已确认的分片；续传只补 `parts` 里缺的那些 */
  completed_parts?: CompletedUploadPart[]
  expires_at: string
  error?: string
  /**
   * 追加目标资源。创建时冻结，是幂等摘要的一部分；null 表示建独立资源。
   * 临时计算上传永远不带它（那个入口不在这个对话框）。
   */
  target_resource_id: string | null
  /** 登记确认：永久上传字节就绪前为 null，之后是 pending|retrying|ready|rejected */
  ingest_status: IngestStatus | null
  /** 只在 rejected 时非空：确定性拒绝码，文案用 ingestRejectionLabelOf 取 */
  ingest_error: IngestRejection | null
}

export interface CreateUploadOptions {
  /** 完整文件 sha256（hex）。带幂等键时必填：服务端据此把重试绑定到同一内容 */
  sha256?: string
  targetResourceId?: string | null
  /** 调用方持久保存的创建键；同键同正文取回原会话 */
  idempotencyKey?: string
}

export interface CreateUploadRequest {
  filename: string
  size: number
  mime: string
  sha256: string | null
  target_resource_id?: string
}

export interface FinalizeUploadPart {
  part_number: number
  etag: string
}

export interface FinalizeUploadBody {
  parts?: FinalizeUploadPart[]
  engine?: string
  options?: unknown
}

export interface DirectUploadOptions {
  engine?: string
  options?: Record<string, unknown>
  /**
   * 追加目标资源 id。**冻结身份**：随创建请求一次写入，重试不得更换，
   * 换目标必须建新会话。不传表示创建独立资源。
   */
  targetResourceId?: string | null
  /**
   * 创建幂等键（调用方跨重试保持不变）。给了就先算完整 sha256 一并声明，
   * 重试时取回同一会话、只补传缺的分片。
   */
  idempotencyKey?: string
  /** 开始算摘要 / 开始传分片时回调，让调用方如实展示当前在干什么 */
  onStage?: (stage: 'hashing' | 'uploading') => void
  /** 0–100。分片完成即刻上报，所以进度是**真的传上去了多少**，不是排队了多少 */
  onProgress?: (percent: number) => void
  signal?: AbortSignal
}

export interface StatusPollOptions {
  intervalMs?: number
  timeoutMs?: number
}

export interface IngestWaitOptions extends StatusPollOptions {
  /** 每次轮询拿到会话即回调，让调用方实时展示 pending/retrying */
  onPoll?: (session: UploadSession) => void
}

/** 完整文件 sha256（小写 hex）。服务端不信它，它只把幂等重试绑定到同一内容 */
export async function sha256Hex(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
}

export const uploadsApi = {
  create: (file: File, { sha256, targetResourceId, idempotencyKey }: CreateUploadOptions = {}) => {
    const body: CreateUploadRequest = {
      filename: file.name,
      size: file.size,
      mime: file.type || 'application/octet-stream',
      sha256: sha256 ?? null,
    }
    // target 缺省时**直接省略字段**（而不是发 null）：省略 = 建独立资源，
    // 也保持老请求的创建摘要不变；空串同样视为未选，不发出去
    if (targetResourceId) body.target_resource_id = targetResourceId
    return http.post<UploadSession>('/api/uploads', body, {
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    })
  },

  get: (id: string) => http.get<UploadSession>(`/api/uploads/${id}`),

  finalize: (
    id: string,
    body: FinalizeUploadBody,
    idempotencyKey?: string,
  ) =>
    http.post<UploadSession>(`/api/uploads/${id}/finalize`, body, {
      // finalize **必须幂等**：重试不得创建两份任务。键缺省用 upload id
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    }),
}

/**
 * 走完整条直传链路，返回 finalize 之后的会话。
 *
 * **分片是串行的**，不是并发：浏览器对同一 origin 的并发连接本来就有限，
 * 而串行让进度条真实反映"传上去多少"。真要并发（大文件、高带宽）应当
 * 由调用方按网络情况决定，而不是在这里写死一个数字。
 *
 * 给了 `idempotencyKey` 就是**可续传**的：同键重来取回原会话，只补传
 * `parts` 里缺的分片（服务端合并时自己列对象存储，不信客户端报的 ETag）；
 * 会话已经 finalize 过则直接幂等 finalize 取回当前状态。
 *
 * 只负责把字节送达并 finalize。返回的会话是 `verifying`，不是完成 ——
 * 调用方还须按顺序等 `waitForVerification`（字节）与 `waitForIngest`（登记）。
 */
export async function uploadDirect(file: File, opts: DirectUploadOptions = {}) {
  let sha256: string | undefined
  if (opts.idempotencyKey) {
    opts.onStage?.('hashing')
    sha256 = await sha256Hex(file)
  }
  const { data: session } = await uploadsApi.create(file, {
    sha256, targetResourceId: opts.targetResourceId, idempotencyKey: opts.idempotencyKey,
  })
  if (session.allocation_state && session.allocation_state !== 'ready') {
    // 202：身份已持久化但对象存储回执未确认。**不能**换个键重建 —— 用同一个键稍后再来
    throw new Error('对象存储分配尚未确认，会话已保留，请稍后重试')
  }
  const parts = session.parts ?? []
  const alreadyDone = session.completed_parts?.length ?? 0
  const receiving = session.status === 'created' || session.status === 'uploading'
  if (receiving && parts.length === 0 && alreadyDone === 0) {
    throw new Error('服务端没有下发分片预签名，无法上传')
  }

  const partSize = session.part_size
  const total = Math.max(1, Math.ceil(file.size / partSize))
  opts.onStage?.('uploading')
  let sent = 0
  for (const part of parts) {
    const start = (part.part_number - 1) * partSize
    const chunk = file.slice(start, Math.min(start + partSize, file.size))

    // **用原生 fetch 而不是 axios 实例**：那个实例会自动带上
    // `Authorization: Bearer <JWT>`，而预签名 URL 已经把凭证放在查询串里了。
    // 多带一个 Authorization 头会让 S3 兼容实现认为这是一次 SigV4 请求，
    // 直接 400 —— 而错误信息与"签名不对"长得一模一样，很难往这上面想。
    // 桌面本机源的会话下发同源相对地址（`/api/uploads/...`），`apiUrl`
    // 把它指回宿主 `ddp://app`；浏览器里与绝对预签名地址都原样返回。
    const resp = await fetch(apiUrl(part.url), {
      method: 'PUT',
      body: chunk,
      signal: opts.signal,
    })
    if (!resp.ok) {
      throw new Error(`分片 ${part.part_number} 上传失败（${resp.status}）`)
    }
    sent += 1
    opts.onProgress?.(Math.round(((alreadyDone + sent) / total) * 100))
  }

  // 不报 ETag：服务端合并时自己列对象存储（跨域下 ETag 本来也常拿不到，
  // 而续传时只有新传的那几片的 ETag）。finalize 键用会话 id，重来也是同一次 finalize
  const { data } = await uploadsApi.finalize(
    session.id,
    { engine: opts.engine, options: opts.options ?? {} },
    session.id,
  )
  return data
}

/**
 * 轮询上传会话直到离开 `verifying`。
 *
 * **只管字节**，不管登记：校验是后台流式重算 sha256，大文件要几秒到几十秒。
 * **这段时间必须让用户看得见**——显示"已上传，校验中"，而不是转一个没有说明的圈。
 * 字节就绪（`ready`）之后，永久上传还须 `waitForIngest` 等登记确认。
 */
export async function waitForVerification(
  id: string,
  { intervalMs = 1500, timeoutMs = 300_000 }: StatusPollOptions = {},
): Promise<UploadSession> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const { data } = await uploadsApi.get(id)
    if (data.status !== 'verifying' && data.status !== 'uploading') return data
    if (Date.now() > deadline) {
      // 超时**如实报出来**，不要静默当成成功 —— 那会让一份没通过校验的
      // 文档看起来像已经入库了。会话本身保留，调用方用同一个 id 状态重试。
      throw new Error('上传校验超时，请稍后重试继续查询校验状态')
    }
    // 构建目标是 ES2022（见 tsconfig.app.json）：Promise.withResolvers 不在其内
    await new Promise<void>((resolve) => setTimeout(resolve, intervalMs))
  }
}

/**
 * 会话 ingest 状态的命名读取口径：未知/缺失一律折成 null（= 还没确认）。
 * 调用方继续等，而不是报错或误报完成。对话框与 waitForIngest 共用这一口径。
 */
export function ingestStatusOf(session: UploadSession): IngestStatus | null {
  const value: unknown = session.ingest_status
  return INGEST_STATUS_VALUES.find((known) => known === value) ?? null
}

/**
 * 等登记确认（ingest），与字节校验是**两段**。
 *
 * - `ready`：`DocumentSubmitted` 已被语料域确认（2xx 或 `409 duplicate_event`）。
 *   注意这**不是**解析/索引完成，那条链路另行展示。
 * - `rejected`：终态（目标非法、摘要冲突等确定性拒绝），直接返回会话，
 *   由调用方把 `ingest_error`（安全文本）展示出来，不再轮询。
 * - `pending`/`retrying`/null：还没确认，继续等；每次拿到会话都调 `onPoll`，
 *   让调用方把"登记中/登记重试中"实时展示出来。
 *
 * 超时只表示"还没确认"：抛错，但会话保留，调用方用同一个 id 状态重试，
 * 绝不重传字节、不建第二个会话。
 */
export async function waitForIngest(
  id: string,
  { intervalMs = 1500, timeoutMs = 300_000, onPoll }: IngestWaitOptions = {},
): Promise<UploadSession> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const { data } = await uploadsApi.get(id)
    onPoll?.(data)
    const ingest = ingestStatusOf(data)
    if (ingest === 'ready' || ingest === 'rejected') return data
    if (Date.now() > deadline) {
      throw new Error('登记确认超时，会话已保留，可重试继续查询登记状态')
    }
    await new Promise<void>((resolve) => setTimeout(resolve, intervalMs))
  }
}
