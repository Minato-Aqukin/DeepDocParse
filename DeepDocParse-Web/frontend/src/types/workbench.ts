/** 工作台三栏之间传递的高亮区域。 */
export interface Highlight {
  pageIdx: number
  bbox: [number, number, number, number] | null
  pageSize: [number, number] | null
  kind: 'citation' | 'selected'
  label?: string
}
