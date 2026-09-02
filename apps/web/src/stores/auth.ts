import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { TOKEN_KEY, authApi } from '@/api'
import type { Profile } from '@/types/api'
import { ROLE_VALUES, type Role } from '@deepdocparse/contracts'

const NAME_KEY = 'ddp.username'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const username = ref<string>(localStorage.getItem(NAME_KEY) || '')
  const profile = ref<Profile | null>(null)

  const isAuthenticated = computed(() => Boolean(token.value))

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
  const canUpload = computed(() => atLeast('contributor'))
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
  }

  async function register(name: string, password: string) {
    const { data } = await authApi.register(name, password)
    persist(data.access_token, data.user.username)
    profile.value = data.user
  }

  /** 校验会话是否还有效，顺带拿到账号信息（设置页展示用）。 */
  async function fetchProfile() {
    if (!token.value) return null
    const { data } = await authApi.me()
    profile.value = data
    username.value = data.username
    localStorage.setItem(NAME_KEY, data.username)
    return data
  }

  function logout() {
    token.value = null
    username.value = ''
    profile.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(NAME_KEY)
  }

  return {
    token, username, profile, role, isAuthenticated,
    canUpload, canReview, canManageOrg, atLeast,
    login, register, fetchProfile, logout,
  }
})
