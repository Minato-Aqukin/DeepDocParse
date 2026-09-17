import { TASK_STATUS_VALUES, type EnumerationState } from '@deepdocparse/contracts'
import { describe, expect, it } from 'vitest'

import {
  buildExecutionConsent,
  buildIntent,
  canCancel,
  canResume,
  citationIndex,
  collectEvents,
  exhaustiveAllowed,
  isSettled,
  recipientsOf,
  retentionOf,
  splitReason,
  type EventPage,
  type FederatedEvidence,
  type TaskDraft,
  type TaskEvent,
  type TaskPlan,
} from '../task-model'

describe('isSettled', () => {
  it('按契约的 active 标记判断：进行中的状态继续轮询，终态停下', () => {
    expect(isSettled({ status: 'queued' })).toBe(false)
    expect(isSettled({ status: 'claimed' })).toBe(false)
    expect(isSettled({ status: 'running' })).toBe(false)
    expect(isSettled({ status: 'succeeded' })).toBe(true)
    expect(isSettled({ status: 'failed' })).toBe(true)
    expect(isSettled({ status: 'cancelled' })).toBe(true)
  })

  it('契约里的每个状态都有明确结论；认不出的状态不无限轮询', () => {
    for (const value of TASK_STATUS_VALUES) expect(typeof isSettled({ status: value })).toBe('boolean')
    expect(isSettled({ status: 'teleporting' as never })).toBe(true)
  })
})

describe('splitReason', () => {
  it('按第一个冒号拆成代码与细节', () => {
    expect(splitReason('receipt_binding_mismatch:plan_digest')).toEqual({ code: 'receipt_binding_mismatch', detail: 'plan_digest' })
    expect(splitReason('peer_unavailable:http_503')).toEqual({ code: 'peer_unavailable', detail: 'http_503' })
    expect(splitReason('insufficient_evidence')).toEqual({ code: 'insufficient_evidence', detail: null })
  })
})

describe('citationIndex', () => {
  it('答案里的 [n] 对应证据列表的第 n 条（从 1 开始）', () => {
    const index = citationIndex([{ evidence_id: 'ev-a' }, { evidence_id: 'ev-b' }] as FederatedEvidence[])
    expect(index.get('ev-a')).toBe(1)
    expect(index.get('ev-b')).toBe(2)
    expect(index.get('ev-missing')).toBeUndefined()
  })
})

describe('collectEvents', () => {
  const event = (seq: number): TaskEvent => ({ seq, type: 'plan_ready', at: '2026-09-15T08:00:00Z' })

  it('按 seq 去重：重连后服务端重发同一段，界面不出现两条同号事件', async () => {
    const pages: EventPage[] = [
      { root_task_id: 'r', events: [event(2), event(3)], next_seq: 3, complete: true },
    ]
    const result = await collectEvents(async () => pages.shift()!, 1, [2])
    expect(result.events.map((item) => item.seq)).toEqual([3])
    expect(result.next).toBe(3)
  })

  it('没追平就继续读，追平或空页就停', async () => {
    const afters: number[] = []
    const pages: EventPage[] = [
      { root_task_id: 'r', events: [event(1)], next_seq: 1, complete: false },
      { root_task_id: 'r', events: [event(2)], next_seq: 2, complete: true },
    ]
    const result = await collectEvents(async (after: number) => { afters.push(after); return pages.shift()! }, 0, [])
    expect(afters).toEqual([0, 1])
    expect(result.events.map((item) => item.seq)).toEqual([1, 2])

    const empty = await collectEvents(async () => ({ root_task_id: 'r', events: [], next_seq: 9, complete: false }), 9, [])
    expect(empty.events).toEqual([])
    expect(empty.next).toBe(9)
  })

  it('一次刷新最多读 cap 页，剩下的留给下一轮', async () => {
    let calls = 0
    const result = await collectEvents(async (after: number) => {
      calls++
      return { root_task_id: 'r', events: [event(after + 1)], next_seq: after + 1, complete: false }
    }, 0, [], { cap: 3 })
    expect(calls).toBe(3)
    expect(result.next).toBe(3)
  })

  it('这次续读作废了就立刻停，不再白打后面的请求', async () => {
    let calls = 0
    let alive = true
    const result = await collectEvents(async (after: number) => {
      calls++
      alive = false   // 第一页回来时路由已经切走
      return { root_task_id: 'r', events: [event(after + 1)], next_seq: after + 1, complete: false }
    }, 0, [], { stale: () => !alive })
    expect(calls).toBe(1)
    expect(result.events.map((item) => item.seq)).toEqual([1])
  })

  it('形状不对当场抛，不把 after 变成 undefined 反复从头拉', async () => {
    await expect(collectEvents(async () => ({ root_task_id: 'r', events: [event(1)] } as never), 0, []))
      .rejects.toThrow('事件流格式不兼容')
  })
})

// ---------------------------------------------------------------- 组装

const NOW = Date.parse('2026-09-16T09:00:00Z')

function envelope(members: string[], validUntil = '2026-09-16T10:00:00Z', sealed = true) {
  return {
    manifest: {
      schema: 'ddp-scope-coverage/1#ScopeManifest' as const,
      scope_id: 'scope-1', caller_scope_hash: 'sha256:c0', created_at: '2026-09-16T08:59:00Z',
      valid_until: validUntil, registry_revision_vector: [],
      expanded_members: members.map((node) => ({
        origin_node_id: node, collection_id: `${node}-col`, operation: 'corpus.retrieve',
      })),
      unexpanded_subtrees: [], enumeration_state: 'sealed' as const, manifest_digest: 'sha256:m0',
    },
    first_cursor: '', terminal_cursor: '', total_targets: members.length,
    content_snapshot: 'not_frozen', expired: false,
    effective_enumeration_state: (sealed ? 'sealed' : 'partial') as EnumerationState,
  }
}

function makeDraft(over: Partial<TaskDraft> = {}): TaskDraft {
  return {
    query: '这台设备的额定电压是多少？', operation: 'rag.answer.cited', scopeKind: 'site_public',
    resourceRefs: [], mode: 'fast', payload: ['query_text'],
    maxProbeRequests: 8, maxEgressBytes: 1024, ...over,
  }
}

const OPTIONS = {
  localNodeId: 'node-a', grantedBy: 'user-1', workspaceRef: 'org-1',
  nonce: 'abc123', now: NOW,
}

describe('buildIntent', () => {
  it('没有远端接收方：外发许可必须是 local_only，载荷、接收方、预算全空', () => {
    const body = buildIntent(makeDraft(), OPTIONS)
    expect(body.exploration_consent.egress_mode).toBe('local_only')
    expect(body.exploration_consent.allowed_payload).toEqual([])
    expect(body.exploration_consent.allowed_recipients).toEqual([])
    expect(body.exploration_consent.budget).toEqual({ max_probe_requests: 0, max_egress_bytes: 0 })
    expect(body.task_spec.execution_policy).toEqual({ mode: 'local_only' })
    expect(body.scope_manifest).toBeUndefined()
  })

  /**
   * 回归：**执行策略看范围种类，不是看有没有远端目标。** 协调者的 `validate_spec`
   * 里 `local_only` 执行禁止 `federation_public` 范围，而一份联邦范围清单完全可能
   * 只枚举到本节点。按"有没有远端"选执行策略的话，这种任务在协调者那里当场被拒。
   */
  it('联邦范围里没有远端成员时，执行策略仍然是 trusted_federation', () => {
    const body = buildIntent(makeDraft({
      scopeKind: 'federation_public', scope: envelope(['node-a']),
    }), OPTIONS)
    expect(body.task_spec.execution_policy).toEqual({ mode: 'trusted_federation', coordinator_ref: 'node-a' })
    // 但没有远端接收方，所以外发许可照样是 local_only（两件事分开判）
    expect(body.exploration_consent.egress_mode).toBe('local_only')
    expect(body.exploration_consent.allowed_recipients).toEqual([])
  })

  it('有远端成员：接收方就是清单里的远端节点，去重排序，本节点不在其中', () => {
    const body = buildIntent(makeDraft({
      scopeKind: 'federation_public', scope: envelope(['node-c', 'node-a', 'node-b', 'node-c']),
      payload: ['entity_names', 'query_text', 'query_text'],
    }), OPTIONS)
    expect(body.exploration_consent.egress_mode).toBe('listed_nodes')
    expect(body.exploration_consent.allowed_recipients).toEqual(['node-b', 'node-c'])
    expect(body.exploration_consent.allowed_payload).toEqual(['entity_names', 'query_text'])
    expect(body.exploration_consent.budget.max_probe_requests).toBe(8)
    // 联邦范围的分母由清单给出，必须随意图一起发过去（缺了协调者 409 discovery_incomplete）
    expect(body.scope_manifest?.scope_id).toBe('scope-1')
    expect(body.task_spec.resource_scope.scope_ref).toBe('scope-1')
  })

  it('用户不勾任何载荷也照样生成：界面不替他补上外发项', () => {
    const body = buildIntent(makeDraft({
      scopeKind: 'federation_public', scope: envelope(['node-b']), payload: [],
    }), OPTIONS)
    expect(body.exploration_consent.egress_mode).toBe('listed_nodes')
    expect(body.exploration_consent.allowed_payload).toEqual([])
  })

  it('TaskSpec 引用的就是这份许可的 id（协调者按这个绑定校验）', () => {
    const body = buildIntent(makeDraft(), OPTIONS)
    expect(body.task_spec.consent_refs.exploration).toBe(body.exploration_consent.consent_id)
    expect(body.task_spec.consent_refs.execution).toBeNull()
  })

  it('许可有效期不超过范围清单的有效期：范围过期了许可也不该还活着', () => {
    const body = buildIntent(makeDraft({
      scopeKind: 'federation_public', scope: envelope(['node-b'], '2026-09-16T09:05:00Z'),
    }), OPTIONS)
    expect(body.exploration_consent.valid_until).toBe('2026-09-16T09:05:00Z')
  })

  it('指定资源：去重排序后进 resource_refs', () => {
    const body = buildIntent(makeDraft({
      scopeKind: 'fixed_resources', resourceRefs: ['res-b', 'res-a', 'res-b'],
    }), OPTIONS)
    expect(body.task_spec.resource_scope.resource_refs).toEqual(['res-a', 'res-b'])
  })

  it('只取证据的任务不要求出处', () => {
    expect(buildIntent(makeDraft({ operation: 'corpus.retrieve' }), OPTIONS)
      .task_spec.requirements?.citations).toBe('not_required')
    expect(buildIntent(makeDraft(), OPTIONS).task_spec.requirements?.citations).toBe('required')
  })

  it.each([
    ['问题不能为空', makeDraft({ query: '   ' }), OPTIONS],
    ['联邦范围需要先生成范围清单', makeDraft({ scopeKind: 'federation_public' }), OPTIONS],
    ['至少选择一份资源', makeDraft({ scopeKind: 'fixed_resources' }), OPTIONS],
    ['穷查只能绑定已生成的联邦范围清单', makeDraft({ mode: 'exhaustive_scope' }), OPTIONS],
    ['还没拿到本节点身份，请稍后重试', makeDraft(), { ...OPTIONS, localNodeId: '' }],
    ['还没拿到账号信息，请稍后重试', makeDraft(), { ...OPTIONS, workspaceRef: '' }],
  ])('拦在前端：%s', (message, draft, options) => {
    expect(() => buildIntent(draft, options)).toThrow(message)
  })
})

describe('exhaustiveAllowed', () => {
  it('只有绑定了联邦范围清单才能穷查 —— 别的范围没有可信分母', () => {
    expect(exhaustiveAllowed({ scopeKind: 'site_public' })).toBe(false)
    expect(exhaustiveAllowed({ scopeKind: 'federation_public' })).toBe(false)
    expect(exhaustiveAllowed({ scopeKind: 'federation_public', scope: envelope(['node-a']) })).toBe(true)
  })
})

const LOCAL_STEP = { step_id: 's1', operation: 'corpus.retrieve', executor_node_id: 'node-a', depends_on: [] }
const QUERY_EDGE = {
  edge_id: 'e1', from_node_id: 'node-a', to_node_id: 'node-b', payload_kind: 'query_text',
  relay_via: ['node-r'], retention: 'temporary' as const, authorised_by: 'explore-abc123',
}

const PLAN: TaskPlan = {
  schema: 'ddp-plan-admission/1#TaskPlan', plan_id: 'plan-1', revision: 2,
  plan_digest: 'sha256:' + 'a'.repeat(64), task_spec_digest: 'sha256:' + 'b'.repeat(64),
  root_coordinator_node_id: 'node-a', planning_state: 'ready',
  steps: [
    LOCAL_STEP,
    { step_id: 's2', operation: 'corpus.retrieve', executor_node_id: 'node-b', depends_on: ['s1'] },
  ],
  data_edges: [QUERY_EDGE],
  budget: { max_requests: 10, max_bytes: 1024, max_hops: 2, deadline: '2026-09-16T09:30:00Z' },
  final_result_writer: 'node-a', valid_until: '2026-09-16T09:30:00Z',
}

describe('recipientsOf', () => {
  /** 中继也是数据接收方 —— 漏掉它，协调者的 approve 会以 egress_denied 拒掉。 */
  it('执行者、数据边两端、以及中继，一个都不能少', () => {
    expect(recipientsOf(PLAN)).toEqual(['node-a', 'node-b', 'node-r'])
  })

  it('纯本地计划也至少有执行者（协调者要求接收方集合非空）', () => {
    expect(recipientsOf({ steps: [LOCAL_STEP], data_edges: [] })).toEqual(['node-a'])
  })
})

describe('retentionOf', () => {
  it('各边一致就用那一个', () => {
    expect(retentionOf(PLAN)).toBe('temporary')
  })

  it('没有数据边时取最保守的 temporary', () => {
    expect(retentionOf({ data_edges: [] })).toBe('temporary')
  })

  it('各边不一致时报出来，不替用户挑一个', () => {
    expect(() => retentionOf({
      data_edges: [QUERY_EDGE, { ...QUERY_EDGE, edge_id: 'e2', retention: 'persistent' }],
    })).toThrow('保留策略不一致')
  })
})

describe('buildExecutionConsent', () => {
  it('许可覆盖计划里的每一个端点与每一条边，并绑到这一份摘要', () => {
    const consent = buildExecutionConsent(PLAN, { rootTaskId: 'task-1', grantedBy: 'user-1', now: NOW })
    expect(consent.plan_digest).toBe(PLAN.plan_digest)
    expect(consent.allowed_recipients).toEqual(['node-a', 'node-b', 'node-r'])
    expect(consent.allowed_edges).toEqual(['e1'])
    expect(consent.retention).toBe('temporary')
    expect(consent.valid_until).toBe(PLAN.valid_until)
    expect(consent.consent_id).toBe('execute-task-1-r2')
  })

  it('计划过期了不签：签了也只会被协调者以 consent_expired 拒掉', () => {
    expect(() => buildExecutionConsent(PLAN, {
      rootTaskId: 'task-1', grantedBy: 'user-1', now: Date.parse('2026-09-16T09:31:00Z'),
    })).toThrow('计划已过期')
  })
})

describe('canCancel / canResume', () => {
  const base = { planning_state: 'approved' as const, retrieval_completeness: 'partial' as const }

  it('取消只对还没落定的任务给（判据与轮询同一个：契约的 active 标记）', () => {
    expect(canCancel({ status: 'queued' })).toBe(true)
    expect(canCancel({ status: 'running' })).toBe(true)
    expect(canCancel({ status: 'succeeded' })).toBe(false)
    expect(canCancel({ status: 'cancelled' })).toBe(false)
  })

  /**
   * 回归：协调者的 `resume` 要求 `planning_state == "approved"`。少了这一条，
   * 规划阶段就失败的任务会显示一个点下去必定 403 的"补做"按钮。
   */
  it('没有已批准的计划就没有续跑：协调者那边同样的判据', () => {
    expect(canResume({ ...base, status: 'failed', planning_state: 'ready' })).toBe(false)
    expect(canResume({ ...base, status: 'failed', planning_state: 'invalidated' })).toBe(false)
    expect(canResume({ ...base, status: 'failed' })).toBe(true)
  })

  it('取消是显式终态，不给续跑；查全了的成功任务也没什么可补', () => {
    expect(canResume({ ...base, status: 'cancelled' })).toBe(false)
    expect(canResume({ ...base, status: 'succeeded' })).toBe(true)
    expect(canResume({ ...base, status: 'succeeded', retrieval_completeness: 'complete' })).toBe(false)
    expect(canResume({ ...base, status: 'running' })).toBe(false)
  })
})
