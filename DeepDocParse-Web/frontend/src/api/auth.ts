import type { AuthToken, Profile } from '@/types/api'

import { http } from './http'

export const authApi = {
  register: (username: string, password: string) =>
    http.post<AuthToken>('/api/auth/register', { username, password }),
  login: (username: string, password: string) =>
    http.post<AuthToken>('/api/auth/login', { username, password }),
  /** 校验会话是否仍然有效，同时拿到账号信息。 */
  me: () => http.get<Profile>('/api/auth/me'),
}
