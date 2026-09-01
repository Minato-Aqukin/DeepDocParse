import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { TOKEN_KEY, authApi } from '@/api'
import type { Profile } from '@/types/api'

const NAME_KEY = 'ddp.username'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const username = ref<string>(localStorage.getItem(NAME_KEY) || '')
  const profile = ref<Profile | null>(null)

  const isAuthenticated = computed(() => Boolean(token.value))

  function persist(t: string, name: string) {
    token.value = t
    username.value = name
    localStorage.setItem(TOKEN_KEY, t)
    localStorage.setItem(NAME_KEY, name)
  }

  async function login(name: string, password: string) {
    const { data } = await authApi.login(name, password)
    persist(data.access_token, data.username)
  }

  async function register(name: string, password: string) {
    const { data } = await authApi.register(name, password)
    persist(data.access_token, data.username)
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

  return { token, username, profile, isAuthenticated, login, register, fetchProfile, logout }
})
