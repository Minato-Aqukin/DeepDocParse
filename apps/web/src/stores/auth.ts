import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { TOKEN_KEY, authApi } from '@/api'
import { bootSource, getActiveSource, isDesktop } from '@/platform/desktop'
import type { Profile } from '@/types/api'
import { ROLE_VALUES, type Role } from '@deepdocparse/contracts'

const NAME_KEY = 'ddp.username'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const username = ref<string>(localStorage.getItem(NAME_KEY) || '')
  const profile = ref<Profile | null>(null)
  const profileError = ref<'unavailable' | null>(null)

  // 桌面：已认证 = 当前源可用（profile 照常经代理从 /api/auth/me 拿）。
  // 浏览器：有 token（原有行为，不变）。
  // 循环 import 是安全的：platform/desktop.ts 不引用 store 或 api 层。
  const isAuthenticated = computed(() => {
    if (isDesktop()) return (getActiveSource() ?? bootSource.value)?.state === 'ready'
    return Boolean(token.value)
  })

  /**
   * 只读（中心源）。读启动时的当前源：切换即整页重载，启动后它不变。
   * 组件问它来禁用写按钮；禁用文案统一用 `approvedPlanLabel()`。
   */
  const readOnly = computed(() => isDesktop() && ((getActiveSource() ?? bootSource.value)?.readOnly ?? false))

  /** 宿主报告中心登录过期（代理 401）：profile 清空并标不可用，页面状态不断。 */
  function markSourceSignedOut() {
    profile.value = null
    profileError.value = 'unavailable'
  }

  /**
   * 角色。**只在拿到 profile 之后才有值** —— 它是每次请求回查出来的，
   * 不是从 token 里解出来的（把角色写进 JWT 意味着降权要等 token 过期）。
   * 拿不到时按最低权限处理：宁可少给，不可多给。
   */
  const role = computed<Role | null>(() => profile.value?.role ?? null)

  /** 角色比大小。契约里 role 的声明顺序就是权限高低。 */
  function atLeast(need: Role): boolean {
    const have = role.value ? ROLE_VALUES.indexOf(role.value) : -1
    return have >= 0 && have >= ROLE_VALUES.indexOf(need)
  }

  // 能力，不是角色名。**组件里问能力** —— 写 `role === 'admin'` 的话，
  // 以后加一个更高的角色会把它静默挡在外面
  // 桌面只读源：上传一律关掉（按钮留在界面上，见各视图的禁用态）。
  const canUpload = computed(() => !readOnly.value && atLeast('contributor'))
  const canReview = computed(() => atLeast('reviewer'))
  const canManageOrg = computed(() => atLeast('admin'))

  function persist(t: string, name: string) {
    token.value = t
    username.value = name
    localStorage.setItem(TOKEN_KEY, t)
    localStorage.setItem(NAME_KEY, name)
  }

  async function login(name: string, password: string) {
    const { data } = await authApi.login(name, password)
    persist(data.access_token, data.user.username)
    profile.value = data.user
    profileError.value = null
  }

  async function register(name: string, password: string) {
    const { data } = await authApi.register(name, password)
    persist(data.access_token, data.user.username)
    profile.value = data.user
    profileError.value = null
  }

  /**
   * 校验会话是否还有效，顺带拿到账号信息（设置页展示用）。
   * 桌面：经宿主代理读 `/api/auth/me`（本机=单一属主，中心=登录身份），
   * 没有 token 可对；401 由拦截器送去数据源页，这里只记不可用。
   */
  async function fetchProfile() {
    if (isDesktop()) {
      profileError.value = null
      try {
        const { data } = await authApi.me()
        profile.value = data
        username.value = data.username
        return data
      } catch (cause) {
        profile.value = null
        profileError.value = 'unavailable'
        throw cause
      }
    }
    const requestedToken = token.value
    if (!requestedToken) return null
    profileError.value = null
    try {
      const { data } = await authApi.me()
      if (token.value !== requestedToken) return null
      profile.value = data
      username.value = data.username
      localStorage.setItem(NAME_KEY, data.username)
      return data
    } catch (cause) {
      if (token.value === requestedToken) {
        profile.value = null
        const response = cause && typeof cause === 'object' && 'response' in cause
          ? cause.response : null
        if (response && typeof response === 'object' && 'status' in response
          && response.status === 401) logout()
        else profileError.value = 'unavailable'
      }
      throw cause
    }
  }

  function logout() {
    token.value = null
    username.value = ''
    profile.value = null
    profileError.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(NAME_KEY)
  }

  return {
    token, username, profile, profileError, role, isAuthenticated, readOnly,
    canUpload, canReview, canManageOrg, atLeast,
    login, register, fetchProfile, logout, markSourceSignedOut,
  }
})
