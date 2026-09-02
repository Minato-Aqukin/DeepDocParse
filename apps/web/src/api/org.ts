import type { Member, Organization } from '@/types/api'
import type { Role } from '@deepdocparse/contracts'

import { http } from './http'

/**
 * 组织与成员。
 *
 * **首发是单组织独占部署**：一次部署 = 一个组织 = 一份语料，组织内成员
 * 共享全部语料。所以这里没有"切换组织"——角色控制的是"能做什么"，
 * 不是"能看见什么"。
 */
export const orgApi = {
  current: () => http.get<Organization>('/api/org'),
  members: () => http.get<Member[]>('/api/org/members'),
  addMember: (username: string, role: Role) =>
    http.post<Member>('/api/org/members', { username, role }),
  setRole: (userId: string, role: Role) =>
    http.patch<Member>(`/api/org/members/${userId}`, { role }),
  removeMember: (userId: string) => http.delete(`/api/org/members/${userId}`),
}
