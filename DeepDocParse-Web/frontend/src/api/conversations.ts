import type { ChatMessage, Citation, ConversationInfo } from '@/types/api'

import { TOKEN_KEY, http } from './http'

export const conversationsApi = {
  create: (documentId: string) =>
    http.post<ConversationInfo>(`/api/documents/${documentId}/conversations`),
  list: (documentId: string) =>
    http.get<ConversationInfo[]>('/api/conversations', { params: { document: documentId } }),
  messages: (cid: string) => http.get<ChatMessage[]>(`/api/conversations/${cid}/messages`),
  remove: (cid: string) => http.delete(`/api/conversations/${cid}`),
}

export interface AskHandlers {
  onMeta?: (data: { retrieval: { chunk_ids: string[] } }) => void
  onDelta: (text: string) => void
  onCitations?: (citations: Citation[]) => void
  onDone?: (data: { message_id: string; verified: boolean; degraded: string | null }) => void
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
      const resp = await fetch(`/api/conversations/${cid}/ask`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY)}`,
        },
        body: JSON.stringify({ question }),
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => null)
        handlers.onError?.({
          message: body?.error?.message || `请求失败（${resp.status}）`,
          code: body?.error?.code || 'request_failed',
        })
        return
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE 以空行分帧；最后一段可能不完整，留在 buffer 里等下一轮
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''
        for (const block of blocks) dispatch(block, handlers)
      }
      if (buffer.trim()) dispatch(buffer, handlers)
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
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
  else if (event === 'done')
    handlers.onDone?.(data as Parameters<NonNullable<AskHandlers['onDone']>>[0])
  else if (event === 'error')
    handlers.onError?.(data as Parameters<NonNullable<AskHandlers['onError']>>[0])
}
