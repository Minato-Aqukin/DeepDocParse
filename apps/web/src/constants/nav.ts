/** 侧边栏分组。菜单项本身由路由 meta 派生（见 layouts/AppShell.vue），这里只定义分组顺序与标题。 */
// 「工作区」分组已改名「内容」：把「工作区」一词留给本机工作区（DESKTOP-APPSHELL-PLAN §1.2）。
export type NavGroup = 'workspace' | 'developer' | 'account' | 'local'

export const NAV_GROUPS: { key: NavGroup; label: string }[] = [
  { key: 'workspace', label: '内容' },
  { key: 'developer', label: '开发者' },
  { key: 'account', label: '账号' },
  { key: 'local', label: '本机' },
]
