import { expect, test } from '@playwright/test'
import { fakeLogin, stubApi } from './stub-api'
import { realErrors, watchErrors } from './console-guard'

const resource = {
  id: 'resource-alice', organization_id: 'org-1', owner_id: 'u-1',
  uploader_ref: { issuer: 'center-A', subject: 'u-1' }, display_name: '控制器技术手册', publication: 'private',
  versions: [{ id: 'fixed-v2', resource_id: 'resource-alice', version_no: 2,
    document_id: 'demo-id', source_digest: 'd'.repeat(64), filename: '控制器手册.pdf',
    size_bytes: 1200, parse_job_id: 'fixed-parse-2' }],
}

test('资源列表区分本站范围、归属与固定版本，打开时保留完整证据上下文',async({page},info)=>{
  await fakeLogin(page);await stubApi(page)
  const errors=watchErrors(page), scopes:string[]=[]
  await page.route(url=>url.pathname==='/api/resources',route=>{
    const scope=new URL(route.request().url()).searchParams.get('scope')!;scopes.push(scope)
    return route.fulfill({json:{items:[{...resource,publication:scope==='mine'?'private':'published'}],has_more:false}})
  })
  await page.goto('/#/resources')
  await expect(page.getByRole('heading',{name:'控制器技术手册'})).toBeVisible()
  await expect(page.locator('.identity')).toContainText('center-A / u-1')
  await expect(page.getByRole('button',{name:'公开到本站'})).toBeVisible()
  await page.getByText('本站公开',{exact:true}).click()
  await expect(page.getByRole('radio',{name:'本站公开'})).toBeChecked()
  await expect(page.getByText('● 已公开')).toBeVisible()
  await expect(page.getByRole('button',{name:'删除资源'})).toHaveCount(0)
  await page.screenshot({path:info.outputPath('resources-public.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('button',{name:'控制器手册.pdf',exact:true}).click()
  await expect(page).toHaveURL(/resource_id=resource-alice/)
  await expect(page).toHaveURL(/version_id=fixed-v2/)
  await expect(page).toHaveURL(/job=fixed-parse-2/)
  expect(scopes).toEqual(['mine','site_public']);expect(realErrors(errors)).toEqual([])
})

test('资源接口不兼容时显示可见失败，页面和范围切换仍可用',async({page})=>{
  await fakeLogin(page);await stubApi(page)
  const errors=watchErrors(page)
  await page.route(url=>url.pathname==='/api/resources',route=>route.fulfill({json:[]}))
  await page.goto('/#/resources')
  await expect(page.getByRole('alert')).toContainText('格式不兼容')
  await expect(page.getByRole('radio',{name:'本站公开'})).toBeEnabled()
  expect(realErrors(errors)).toEqual([])
})
