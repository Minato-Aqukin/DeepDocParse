import { ref, shallowRef } from 'vue'

/**
 * loading / error / data / refresh 四件套。
 *
 * 页面里到处手写 `loading.value = true; try {...} finally {...}` 很容易漏掉 finally，
 * 一旦漏掉页面就永远转圈。
 */
export function useAsyncData<T>(fetcher: () => Promise<T>, initial: T) {
  const data = shallowRef<T>(initial)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function refresh() {
    loading.value = true
    error.value = null
    try {
      data.value = await fetcher()
    } catch (e) {
      // 错误提示由 axios 拦截器统一弹，这里只留给页面做空态/重试用
      error.value = (e as Error).message || '请求失败'
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, refresh }
}
