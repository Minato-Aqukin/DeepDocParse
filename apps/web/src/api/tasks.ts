/**
 * 联邦任务 API：`packages/contracts/openapi/federation-tasks-v1.yaml` 的协调者端点，
 * 经 control-api 会话鉴权转发到 corpus-api。
 */
import type { CoverageLedger, EventPage, TaskListPage, TaskPlan, TaskStatus } from '@/federation/task-model'

import { http } from './http'

const task = (id: string) => `/api/v1/tasks/${encodeURIComponent(id)}`

/**
 * 任务页的失败一律由页面自己内联说明（哪一块读不到就在哪一块说），
 * 所以这些请求不走全局 toast —— 否则同一个错误会报两遍，而且 toast 那份
 * 是后端原文，会把页面刻意合并的 404/无权限文案绕过去。
 */
const inline = { suppressErrorToast: true } as const

export const tasksApi = {
  /** 本人任务列表（`listTasks`）：只带状态轴，结果按 id 读。 */
  list: (params: { limit?: number; cursor?: string } = {}) =>
    http.get<TaskListPage>('/api/v1/tasks', { params, ...inline }),
  read: (rootTaskId: string) => http.get<TaskStatus>(task(rootTaskId), inline),
  coverage: (rootTaskId: string) => http.get<CoverageLedger>(`${task(rootTaskId)}/coverage`, inline),
  events: (rootTaskId: string, after: number) =>
    http.get<EventPage>(`${task(rootTaskId)}/events`, { params: { after }, ...inline }),
  /**
   * 读当前计划修订。契约没有单独的 GET：`createTaskPlan` 对已是 ready/approved 的任务
   * 是**幂等重放**，原样返回存着的计划，不会重新规划、不会再发 Probe。
   */
  plan: (rootTaskId: string) => http.post<TaskPlan>('/api/v1/task-plans', { root_task_id: rootTaskId }, inline),
}
