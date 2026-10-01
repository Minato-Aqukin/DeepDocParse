import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/tasks', () => ({ tasksApi: {
  identity: vi.fn().mockResolvedValue({ data: { profile: { subject: 'user-a' } } }),
} }))

import PlanApproval from '@/components/federation/PlanApproval.vue'
import type { TaskPlan } from '@/federation/task-model'

describe('PlanApproval generation placement consent', () => {
  it.each(['answer', 'wiki_pages'])('exposes the %s operation and selected executor in the actual approval card', async operation => {
    const plan: TaskPlan = {
      schema: 'ddp-plan-admission/1#TaskPlan', plan_id: 'plan-abc', revision: 1,
      plan_digest: 'sha256:' + 'a'.repeat(64), task_spec_digest: 'sha256:' + 'b'.repeat(64),
      root_coordinator_node_id: 'node-a', planning_state: 'ready',
      steps: [{ step_id: 'generate-c', operation, executor_node_id: 'node-c', depends_on: [] }],
      data_edges: [{ edge_id: 'excerpts-c', from_node_id: 'node-a', to_node_id: 'node-c',
        payload_kind: 'evidence_excerpts', retention: 'temporary', authorised_by: 'explore-1' }],
      budget: { max_requests: 8, max_bytes: 1024, max_hops: 2, deadline: new Date(Date.now() + 60000).toISOString() },
      final_result_writer: 'node-a', valid_until: new Date(Date.now() + 60000).toISOString(),
    }
    const wrapper = mount(PlanApproval, { props: { rootTaskId: 'task-abc', plan }, global: { plugins: [ElementPlus] } })
    await flushPromises()
    const generation = wrapper.find('[aria-label="本次批准的生成步骤"]')
    expect(generation.exists()).toBe(true)
    expect(generation.text()).toContain(operation)
    expect(generation.text()).toContain('node-c')
    expect(wrapper.find('[aria-label="本次批准的有向数据边"]').text()).toContain('node-a → node-c')
    wrapper.unmount()
  })
})
