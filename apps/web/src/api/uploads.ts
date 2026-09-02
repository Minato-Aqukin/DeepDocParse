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
 */
import type { UploadStatus } from '@deepdocparse/contracts'

import { http } from './http'

export interface UploadPart {
  part_number: number
  url: string
}

export interface UploadSession {
  id: string
  status: UploadStatus
  object_key: string
  filename: string
  mime: string
  declared_size: number
  part_size: number
  parts?: UploadPart[]
  expires_at: string
  error?: string
}

export interface DirectUploadOptions {
  engine?: string
  options?: Record<string, unknown>
  /** 0–100。分片完成即刻上报，所以进度是**真的传上去了多少**，不是排队了多少 */
  onProgress?: (percent: number) => void
  signal?: AbortSignal
}

export const uploadsApi = {
  create: (file: File, sha256?: string) =>
    http.post<UploadSession>('/api/uploads', {
      filename: file.name,
      size: file.size,
      mime: file.type || 'application/octet-stream',
      sha256: sha256 ?? null,
    }),

  get: (id: string) => http.get<UploadSession>(`/api/uploads/${id}`),

  finalize: (
    id: string,
    body: { parts?: { part_number: number; etag: string }[]; engine?: string; options?: unknown },
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
 */
export async function uploadDirect(file: File, opts: DirectUploadOptions = {}) {
  const { data: session } = await uploadsApi.create(file)
  const parts = session.parts ?? []
  if (parts.length === 0) {
    throw new Error('服务端没有下发分片预签名，无法上传')
  }

  const partSize = session.part_size
  const completed: { part_number: number; etag: string }[] = []

  for (const part of parts) {
    const start = (part.part_number - 1) * partSize
    const chunk = file.slice(start, Math.min(start + partSize, file.size))

    // **用原生 fetch 而不是 axios 实例**：那个实例会自动带上
    // `Authorization: Bearer <JWT>`，而预签名 URL 已经把凭证放在查询串里了。
    // 多带一个 Authorization 头会让 S3 兼容实现认为这是一次 SigV4 请求，
    // 直接 400 —— 而错误信息与"签名不对"长得一模一样，很难往这上面想。
    const resp = await fetch(part.url, {
      method: 'PUT',
      body: chunk,
      signal: opts.signal,
    })
    if (!resp.ok) {
      throw new Error(`分片 ${part.part_number} 上传失败（${resp.status}）`)
    }
    const etag = resp.headers.get('ETag')
    if (etag) completed.push({ part_number: part.part_number, etag })

    opts.onProgress?.(Math.round((part.part_number / parts.length) * 100))
  }

  // ETag 拿不到时**不传** —— 服务端会自己去对象存储列一遍。
  // 跨域下 ETag 需要 `Access-Control-Expose-Headers`，而那要对象存储配合；
  // 拿不到不是错误，只是让服务端多列一次
  const { data } = await uploadsApi.finalize(
    session.id,
    {
      parts: completed.length === parts.length ? completed : undefined,
      engine: opts.engine,
      options: opts.options ?? {},
    },
    session.id,
  )
  return data
}

/**
 * 轮询上传会话直到离开 `verifying`。
 *
 * 校验是后台流式重算 sha256，大文件要几秒到几十秒。**这段时间必须让用户
 * 看得见**——显示"已上传，校验中"，而不是转一个没有说明的圈。
 */
export async function waitForVerification(
  id: string,
  { intervalMs = 1500, timeoutMs = 300_000 } = {},
): Promise<UploadSession> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const { data } = await uploadsApi.get(id)
    if (data.status !== 'verifying' && data.status !== 'uploading') return data
    if (Date.now() > deadline) {
      // 超时**如实报出来**，不要静默当成成功 —— 那会让一份没通过校验的
      // 文档看起来像已经入库了
      throw new Error('上传校验超时，请稍后在文档列表查看结果')
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
}
