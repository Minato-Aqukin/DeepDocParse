import DOMPurify from 'dompurify'
import katex from 'katex'
import MarkdownIt from 'markdown-it'

import { http } from '@/api/http'

const md = new MarkdownIt({ html: false, linkify: true, breaks: false })

/**
 * 渲染解析结果的 markdown。
 *
 * 安全前提：这段 markdown 来自被解析的文档，是**不可信输入**。
 * html:false 关掉原始 HTML，再过一遍 DOMPurify —— 少一层都等于给上传的 PDF 开 XSS 后门。
 */
export function renderMarkdown(source: string): string {
  // 公式必须"先抽成占位符 -> 渲染 markdown -> 再填回 KaTeX HTML"：
  // markdown-it 开着 html:false（不可信输入必须的），直接把 KaTeX 的 HTML 喂进去
  // 会被整体转义成 &lt;span class=&quot;katex&quot;… 满屏乱码。
  const { text, formulas } = extractMath(source)
  const rendered = md
    .render(text)
    .replace(/KATEXPLACEHOLDER(\d+)ENDKATEX/g, (_m, i) => formulas[Number(i)] ?? '')
  return DOMPurify.sanitize(rendered, { ADD_ATTR: ['target'] })
}

/** mineru 输出里带 LaTeX（$...$ / $$...$$）。占位符不含 markdown 语义字符，渲染时不会被改写。 */
function extractMath(source: string): { text: string; formulas: string[] } {
  const formulas: string[] = []
  const keep = (html: string) => {
    formulas.push(html)
    return `KATEXPLACEHOLDER${formulas.length - 1}ENDKATEX`
  }
  const block = source.replace(/\$\$([\s\S]+?)\$\$/g, (raw, expr) =>
    keep(tryKatex(raw, expr, true)),
  )
  const text = block.replace(/(?<!\\)\$([^$\n]+?)\$/g, (raw, expr) =>
    keep(tryKatex(raw, expr, false)),
  )
  return { text, formulas }
}

function tryKatex(raw: string, expr: string, displayMode: boolean): string {
  try {
    return katex.renderToString(expr, { displayMode, throwOnError: false })
  } catch {
    // 公式写坏了就原样显示，不能让整页渲染失败。
    // 注意要转义：这段是在 md.render 之后拼回去的，绕过了 markdown-it 的转义
    return escapeHtml(raw)
  }
}

function escapeHtml(text: string): string {
  return text.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  )
}

/**
 * 把归档 markdown 里的图片引用（/api/documents/{doc}/jobs/{job}/images/{name}）换成 blob URL。
 *
 * 这些端点受 JWT 保护，而 <img src> 发不出 Authorization 头 ——
 * 所以渲染完再把图片按需取回来。好处是归档产物里不用埋任何凭证。
 */
/**
 * 单张受保护图片 -> blob URL。出处缩略图用它。
 *
 * 和 resolveAuthedImages 同一个道理：`<img src>` 发不出 Authorization 头，
 * 而 crop 端点是 JWT 保护的，直接绑 src 必然 401。
 * 调用方要在卸载时 revoke 返回的 URL。
 */
export async function fetchAuthedImage(url: string): Promise<string | null> {
  try {
    const { data } = await http.get<Blob>(url, { responseType: 'blob' })
    return URL.createObjectURL(data)
  } catch {
    return null
  }
}

export async function resolveAuthedImages(container: HTMLElement): Promise<() => void> {
  const urls: string[] = []
  const images = Array.from(container.querySelectorAll<HTMLImageElement>('img'))

  await Promise.all(
    images.map(async (img) => {
      const src = img.getAttribute('src') || ''
      if (!src.startsWith('/api/')) return
      try {
        const { data } = await http.get<Blob>(src, { responseType: 'blob' })
        const objectUrl = URL.createObjectURL(data)
        urls.push(objectUrl)
        img.src = objectUrl
      } catch {
        img.alt = `[图片加载失败: ${src}]`
      }
    }),
  )

  return () => urls.forEach((u) => URL.revokeObjectURL(u))
}
