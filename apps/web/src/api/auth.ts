import type { AuthToken, GuestMode, Profile } from '@/types/api'

import { http } from './http'

export const authApi = {
  register: (username: string, password: string) =>
    http.post<AuthToken>('/api/auth/register', { username, password }),
  login: (username: string, password: string) =>
    http.post<AuthToken>('/api/auth/login', { username, password }),
  /** 校验会话是否仍然有效，同时拿到账号信息。 */
  me: () => http.get<Profile>('/api/auth/me'),
  /**
   * 游客模式开关。免认证；守卫在放行游客前才问一次。
   * 失败由调用方静默回落到登录页，不弹全局 toast。
   */
  guest: () => http.get<GuestMode>('/api/auth/guest', { suppressErrorToast: true }),
}
