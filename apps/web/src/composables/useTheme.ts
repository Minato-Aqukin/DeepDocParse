import { useDark, useToggle } from '@vueuse/core'

/**
 * 深浅两档主题。视觉规范（assets/ddp/）把它当同一套令牌的两个取值，不是两种设计。
 *
 * `valueLight: 'light'` 不能省 —— useDark 默认在浅色时**不加任何 class**，
 * 而 ddp-tokens.css 的媒体查询守卫写的是 `:root:not([data-theme="light"]):not(.light)`。
 * 少了这个类，系统深色的用户手动选浅色时会被媒体查询打回深色。
 *
 * `dark` 这个类名同时被 Element Plus 的 theme-chalk/dark/css-vars.css 使用，
 * 所以一次切换，EP 组件与本规范令牌一起翻。
 *
 * 存储键与 index.html 里的防闪脚本共用，改这里要同步改那边。
 */
export const isDark = useDark({ valueLight: 'light' })

export const toggleTheme = useToggle(isDark)
