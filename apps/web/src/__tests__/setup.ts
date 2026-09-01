import { config, enableAutoUnmount } from '@vue/test-utils'
import { afterEach, vi } from 'vitest'

// 每个用例跑完自动 unmount。**这一批用例里有好几条专门测"卸载之后会发生什么"**，
// 上一条用例残留的组件会让下一条的断言变成谎话（尤其 blob URL 那两条共用一本账）。
// 之前只在 vitest.config.ts 的注释里"声称"有这个安全网，实际没调用过 —— 现在真的有了。
enableAutoUnmount(afterEach)

// jsdom 没有实现这几个，而 Element Plus 与我们自己的代码都会碰它们。
// 不打桩的话报错发生在**组件挂载**阶段，看起来像"组件坏了"，
// 实际只是环境缺件 —— 会把真 bug 淹掉。
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

globalThis.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => false,
})) as unknown as typeof matchMedia

// **localStorage 得自己给。** jsdom 30 在 vitest 4 里即使给了合法 url
// （已确认 window.location.href 是 http://localhost:5173/，不是不透明源）
// 依然不暴露 window.localStorage。而 `stores/auth.ts` 在 setup 里就读 token ——
// 缺了它，**每个碰到 auth 的用例都会以一个跟 auth 毫无关系的 TypeError 挂掉**，
// 看起来像 store 坏了，实际只是环境缺件。给一个最小实现，行为与浏览器一致。
function makeStorage(): Storage {
  let map = new Map<string, string>()
  return {
    get length() { return map.size },
    key: (i: number) => [...map.keys()][i] ?? null,
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => { map = new Map() },
  } as Storage
}

if (!globalThis.localStorage) {
  const storage = makeStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true })
  Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
}
if (!globalThis.sessionStorage) {
  const storage = makeStorage()
  Object.defineProperty(globalThis, 'sessionStorage', { value: storage, configurable: true })
  Object.defineProperty(window, 'sessionStorage', { value: storage, configurable: true })
}

// jsdom 不实现 blob URL。这里给一个**能记账的**实现：
// 「卸载后 blob URL 已 revoke」是本批用例要钉的两件事之一，
// 而要断言它就必须能看见"发出去多少、收回来多少"。
let blobSeq = 0
const liveObjectUrls = new Set<string>()

URL.createObjectURL = vi.fn(() => {
  const url = `blob:mock/${++blobSeq}`
  liveObjectUrls.add(url)
  return url
}) as unknown as typeof URL.createObjectURL

URL.revokeObjectURL = vi.fn((url: string) => {
  liveObjectUrls.delete(url)
}) as unknown as typeof URL.revokeObjectURL

/** 还没被 revoke 的 blob URL。用例断言它为空 = 没有泄漏。 */
export function leakedObjectUrls(): string[] {
  return [...liveObjectUrls]
}

export function resetObjectUrls(): void {
  liveObjectUrls.clear()
}

// **Element Plus 在单测里没有注册**（`main.ts` 的 `app.use(ElementPlus)`
// 在这里没有对应动作）。于是挂载含 `el-*` 的组件时，Vue 会打一串
// `Failed to resolve component: el-empty` 之类的告警，那些标签渲染成空壳。
//
// 这是**有意的**：注册整个 EP 会让每个用例都慢一截，而目前没有任何单测
// 断言 EP 的渲染结果 —— 「界面上有没有显示降级原因」那类断言全在 e2e 层，
// 那里跑的是真应用、EP 真注册着。
//
// **所以：需要断言 Element Plus 渲染结果的用例，请写到 e2e，别写在这里。**
// 硬写在这里会静默断在空处（尤其 `el-alert` 的 `title` 是 prop 不是 slot，
// 组件没解析时它压根不出现在 DOM 里），看起来像"功能没实现"。
// 真要在单测里断言 EP，就在这里 `config.global.plugins = [ElementPlus]`。
config.global.stubs = {
  // el-dialog / el-message 会 teleport 到 body 外，断言时找不到
  teleport: true,
}

afterEach(() => {
  resetObjectUrls()
})
