import type {
  ChatMessage,
  Citation,
  ConversationInfo,
  AnswerAssertion,
  CandidateDecision,
  EvidenceDetail,
  EvidenceVerification,
  QueryDecision,
  RetrievalConfidence,
} from '@/types/api'

import { apiUrl, checkSourceResponse, isDesktop } from '@/platform/desktop'
import { expireRejectedSession, TOKEN_KEY, http } from './http'
import type { ResourceContext } from './resource-context'

export const conversationsApi = {
  create: (documentId: string) =>
    http.post<ConversationInfo>(`/api/documents/${documentId}/conversations`),
  list: (documentId: string) =>
    http.get<ConversationInfo[]>('/api/conversations', { params: { document: documentId } }),
  messages: (cid: string) => http.get<ChatMessage[]>(`/api/conversations/${cid}/messages`),
  remove: (cid: string) => http.delete(`/api/conversations/${cid}`),
  evidence: (evidenceId: string, context?: ResourceContext) =>
    http.get<EvidenceDetail>(`/api/evidence/${evidenceId}`, { params: context }),
  verifyEvidence: (
    evidenceId: string,
    data: {
      verdict: 'pass' | 'reject' | 'question'
      reason_code?: string
      reason_text?: string
    },
    context?: ResourceContext,
  ) => http.post<EvidenceVerification & { review_state: EvidenceDetail['review_state'] }>(
    `/api/evidence/${evidenceId}/verification`, data, { params: context },
  ),
}

export interface AskHandlers {
  onMeta?: (data: {
    query_decision: QueryDecision
    retrieval: { chunk_ids: string[]; candidates: CandidateDecision[] }
  }) => void
  onDelta: (text: string) => void
  onCitations?: (citations: Citation[]) => void
  onAssertions?: (assertions: AnswerAssertion[]) => void
  onDone?: (data: {
    message_id: string
    verified: boolean
    degraded: string | null
    confidence: RetrievalConfidence
  }) => void
  onError?: (data: { message: string; code: string }) => void
  /**
   * 流以任何方式结束时都会调用一次（正常完成、请求失败、网络中断、abort）。
   *
   * 存在的理由：`onDone` 只在后端真的发出 done 帧时才触发，而请求根本没建立起来的
   * 情况（429 限速、409 索引未就绪、断网）压根到不了那一步。调用方若只在 onDone 里
   * 复位 streaming 标志，就会永久卡在"回答中"。收尾动作一律挂这里。
   */
  onSettled?: () => void
}

/**
 * 问答的 SSE 流。
 *
 * 用 fetch + ReadableStream 而不是 EventSource：后者发不出 Authorization 头。
 * 返回一个 abort 函数，组件卸载时要调用，否则流会一直挂着。
 */
export function askStream(cid: string, question: string, handlers: AskHandlers): () => void {
  const controller = new AbortController()

  void (async () => {
    try {
      // 浏览器：带 JWT（EventSource 发不出这个头，所以用 fetch）。
      // 桌面：不带任何令牌 —— 宿主按当前源附进程令牌或中心 JWT；跨源检查
      // 只认启动时的当前源，`checkSourceResponse` 负责丢弃串源的流。
      const authorization = isDesktop() ? null : `Bearer ${localStorage.getItem(TOKEN_KEY)}`
      const resp = await fetch(apiUrl(`/api/conversations/${cid}/ask`), {
        method: 'POST',
        headers: authorization
          ? { 'Content-Type': 'application/json', Authorization: authorization }
          : { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      })
      if (!checkSourceResponse(resp.headers.get('X-DDP-Source'))) {
        handlers.onError?.({ message: '数据源已切换，此结果已丢弃', code: 'source_changed' })
        return
      }
      if (!resp.ok || !resp.body) {
        if (resp.status === 401) expireRejectedSession(authorization)
        const body: unknown = await resp.json().catch(() => null)
        const detail = body && typeof body === 'object' && 'error' in body ? body.error : null
        handlers.onError?.({
          message: detail && typeof detail === 'object' && 'message' in detail
            && typeof detail.message === 'string' ? detail.message : `请求失败（${resp.status}）`,
          code: detail && typeof detail === 'object' && 'code' in detail
            && typeof detail.code === 'string' ? detail.code : 'request_failed',
        })
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      // 服务端说明过结局（done，或像撤销访问那样只发一个 error 就收流）才算正常结束
      let explained = false
      const tracked: AskHandlers = {
        ...handlers,
        onDone: (data) => {
          explained = true
          handlers.onDone?.(data)
        },
        onError: (data) => {
          explained = true
          handlers.onError?.(data)
        },
      }
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以空行分帧；最后一段可能不完整，留在 buffer 里等下一轮
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''
        for (const block of blocks) dispatch(block, tracked)
      }
      if (buffer.trim()) dispatch(buffer, tracked)
      // 生成路径上的每一轮都以 done 收尾（出错也先发 error、落库、再发 done）。什么都没说明
      // 就结束的流说明进程在生成中途没了：入口代理把连接正常关掉，读到的是一次"干净"的结束，
      // 不报任何错，半截文字随后被清掉 —— 问题下面空空如也（2026-09-24 E 实测）。
      if (!explained) {
        handlers.onError?.({
          message: '回答在完成前中断（生成过程中连接或服务断开），以会话记录为准，没有回答的问题可以重新提问',
          code: 'stream_incomplete',
        })
      }
    } catch (error) {
      if (!(error && typeof error === 'object' && 'name' in error && error.name === 'AbortError')) {
        handlers.onError?.({ message: String(error), code: 'network_error' })
      }
    } finally {
      handlers.onSettled?.()
    }
  })()

  return () => controller.abort()
}

function dispatch(block: string, handlers: AskHandlers) {
  const lines = block.split('\n')
  const event = lines.find((l) => l.startsWith('event: '))?.slice(7).trim()
  const raw = lines.find((l) => l.startsWith('data: '))?.slice(6)
  if (!event || !raw) return
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return // 半截帧：丢掉即可，下一轮 buffer 会补齐
  }
  if (event === 'meta') handlers.onMeta?.(data as Parameters<NonNullable<AskHandlers['onMeta']>>[0])
  else if (event === 'delta') handlers.onDelta((data as { text: string }).text)
  else if (event === 'citations')
    handlers.onCitations?.((data as { citations: Citation[] }).citations)
  else if (event === 'assertions')
    handlers.onAssertions?.((data as { assertions: AnswerAssertion[] }).assertions)
  else if (event === 'done')
    handlers.onDone?.(data as Parameters<NonNullable<AskHandlers['onDone']>>[0])
  else if (event === 'error')
    handlers.onError?.(data as Parameters<NonNullable<AskHandlers['onError']>>[0])
}
