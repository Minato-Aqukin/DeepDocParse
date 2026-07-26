import { defineStore } from 'pinia'
import { ref } from 'vue'

import { TOKEN_KEY, api } from '@/api/client'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const username = ref<string>(localStorage.getItem('ddp.username') || '')

  function persist(t: string, name: string) {
    token.value = t
    username.value = name
    localStorage.setItem(TOKEN_KEY, t)
    localStorage.setItem('ddp.username', name)
  }

  async function login(name: string, password: string) {
    const { data } = await api.login(name, password)
    persist(data.access_token, data.username)
  }

  async function register(name: string, password: string) {
    const { data } = await api.register(name, password)
    persist(data.access_token, data.username)
  }

  function logout() {
    token.value = null
    username.value = ''
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem('ddp.username')
  }

  return { token, username, login, register, logout }
})
