import { onUnmounted, ref } from 'vue'

/**
 * 条件轮询：只在"还有东西在动"时轮询，全部落定就自己停下来。
 *
 * 之前 DocumentsView 与 WorkbenchView 各写了一份 setInterval + onUnmounted(clearInterval)，
 * 且都得自己判断"要不要继续"。收拢到这里后，页面只需要给出 fetch 与 shouldContinue。
 */
export function usePolling(
  fetcher: () => Promise<void> | void,
  shouldContinue: () => boolean,
  intervalMs = 3000,
) {
  const timer = ref<number>()
  const running = ref(false)

  function stop() {
    if (timer.value) window.clearInterval(timer.value)
    timer.value = undefined
    running.value = false
  }

  function start() {
    stop()
    running.value = true
    timer.value = window.setInterval(async () => {
      if (!shouldContinue()) return stop()
      try {
        await fetcher()
      } catch {
        // 单次失败不该终止轮询：后端重启/网络毛刺过去后应当自己恢复
      }
    }, intervalMs)
  }

  onUnmounted(stop)
  return { start, stop, running }
}
