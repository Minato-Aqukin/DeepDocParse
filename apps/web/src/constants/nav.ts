/** 侧边栏分组。菜单项本身由路由 meta 派生（见 layouts/AppShell.vue），这里只定义分组顺序与标题。 */
export type NavGroup = 'workspace' | 'developer' | 'account'

export const NAV_GROUPS: { key: NavGroup; label: string }[] = [
  { key: 'workspace', label: '工作区' },
  { key: 'developer', label: '开发者' },
  { key: 'account', label: '账号' },
]
