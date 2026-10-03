import { expect, test } from '@playwright/test'
import { fakeLogin, stubApi } from './stub-api'
import { realErrors, watchErrors } from './console-guard'
import type { DirectoryMember, DirectoryUnexpandedSubtree } from '../src/api/directory'

test('互联公开目录区分来源与归属，部分枚举保留离线分支和目录水位', async ({ page }, info) => {
  await fakeLogin(page)
  await stubApi(page)
  const errors = watchErrors(page)
  const members = [
    { node_id: 'node-offline', state: 'approved', health: 'unhealthy', revision: 2, expansion_state: 'unexpanded_subtree', accepting_admissions: false, configured: true },
    { node_id: 'node-unknown', state: 'approved', health: 'unknown', revision: 1, expansion_state: 'unexpanded_subtree', accepting_admissions: false, configured: false },
    { node_id: 'node-configured', state: 'approved', health: 'configured', revision: 1, expansion_state: 'not_requested', accepting_admissions: false, configured: true },
    { node_id: 'node-draining', state: 'approved', health: 'draining', revision: 1, expansion_state: 'not_requested', accepting_admissions: false, configured: true },
  ] satisfies DirectoryMember[]
  const unexpanded = [{ node_id: 'node-offline', reason: 'timeout' }, { node_id: 'node-denied', reason: 'denied' }] satisfies DirectoryUnexpandedSubtree[]
  await page.route(url => url.pathname === '/api/v1/federation/member-snapshots', route => route.fulfill({ json: {
    snapshot_id: 'members-a', authority_node_id: 'node-a', registry_revision: 3,
    created_at: '2026-10-03T12:00:00Z', expires_at: '2026-10-03T15:00:00Z',
  } }))
  await page.route(url => url.pathname === '/api/v1/federation/member-snapshots/members-a/members', route => route.fulfill({ json: {
    members, next_cursor: null, complete: true,
  } }))
  await page.route(url => url.pathname === '/api/v1/federation/scopes', route => route.fulfill({ json: {
    manifest: { scope_id: 'scope-a', valid_until: '2026-10-03T15:00:00Z', enumeration_state: 'partial', registry_revision_vector: [{ node_id: 'node-a', registry_revision: 3, fetched_at: '2026-10-03T12:00:00Z', directory_ref: 'members' }, { node_id: 'node-b', registry_revision: 7, fetched_at: '2026-10-03T12:01:00Z', directory_ref: 'collections' }], unexpanded_subtrees: unexpanded }, effective_enumeration_state: 'partial', total_targets: 2, expired: false, content_snapshot: 'not_frozen',
  } }))
  await page.route(url => url.pathname === '/api/v1/federation/scopes/scope-a/targets', route => {
    const terminal = new URL(route.request().url()).searchParams.get('cursor') === 'terminal'
    return route.fulfill({ json: { targets: terminal ? [] : [
      { target_key: { origin_node_id: 'node-a', collection_id: 'local-col', operation: 'corpus.retrieve' }, state: 'planned' },
      { target_key: { origin_node_id: 'node-b', collection_id: 'remote-col', operation: 'corpus.retrieve' }, state: 'planned' },
    ], total_targets: 2, next_cursor: terminal ? null : 'terminal', complete: terminal, expired: false } })
  })
  await page.route(url => url.pathname === '/api/v1/collections/local-col', route => route.fulfill({ json: {
    collection_id: 'local-col', name: '本站手册集合', publication: 'published', owner_id: 'owner-a',
  } }))
  await page.route(url => url.pathname === '/api/resources', route => route.fulfill({ json: { items: [
    { id: 'resource-a', display_name: '本站公开手册', publication: 'published', owner_id: 'owner-a', uploader_ref: { issuer: 'center-A', subject: 'alice' }, versions: [{ id: 'fixed-v1' }] },
    { id: 'temp-a', display_name: '临时计算文件', publication: 'private', versions: [{ id: 'temp-v1' }] },
  ], has_more: false } }))

  await page.goto('/#/tasks')
  await page.getByRole('button', { name: '互联目录', exact: true }).click()
  await page.getByRole('button', { name: '封存当前可见目录', exact: true }).click()
  const directory = page.getByRole('region', { name: '互联公开目录', exact: true })
  await expect(directory.getByRole('region', { name: '本站公开集合' })).toContainText('node-a')
  await expect(directory.getByRole('region', { name: '本站公开集合' })).toContainText('owner-a')
  await expect(directory.getByRole('region', { name: '远端公开集合' })).toContainText('node-b / remote-col')
  await expect(directory.getByRole('region', { name: '远端公开集合' })).toContainText('远端目录未提供归属引用')
  await expect(directory.getByRole('region', { name: '本站公开资源' })).toContainText('center-A / alice')
  await expect(directory).not.toContainText('临时计算文件')
  const coverage = directory.getByRole('region', { name: '目录覆盖与水位' })
  await expect(coverage).toContainText('部分枚举')
  await expect(coverage.getByRole('row').filter({ hasText: 'node-b' })).toContainText('7')
  await expect(coverage).toContainText('2026-10-03T12:01:00Z')
  await expect(coverage.getByRole('region', { name: '未展开节点' })).toContainText('node-offline · timeout')
  await expect(coverage).toContainText('node-denied · denied')
  const health = directory.locator('[aria-label="节点健康统计"]')
  await expect(health).toContainText('不可用 1')
  await expect(health).toContainText('能力状态未知 1')
  await expect(health).toContainText('已配置（未验证可用） 1')
  await expect(health).toContainText('正在排空 1')
  await directory.getByRole('button', { name: '继续读集合下一页' }).click()
  await expect(directory).toContainText('已读到本范围终止页')
  await expect(coverage).toContainText('partial')
  await expect(coverage).toContainText('本范围已观测 2 个公开集合目标')
  await page.screenshot({ path: info.outputPath('federation-directory-partial.png'), fullPage: true, animations: 'disabled' })
  expect(realErrors(errors)).toEqual([])
})
