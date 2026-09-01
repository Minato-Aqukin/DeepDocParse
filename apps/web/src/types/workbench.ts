/** 工作台三栏之间传递的高亮区域。 */
export interface Highlight {
  pageIdx: number
  bbox: [number, number, number, number] | null
  pageSize: [number, number] | null
  /**
   * citation  出处（回答的依据）
   * selected  当前选中的块
   * chunk     分块边界的只读叠加层——**看得见，但不可编辑**
   *
   * 刻意不做 RAGFlow 那样的"人工编辑/合并/拆分块"：单人维护 + 有评测集的项目，
   * 分块不对应该去调分块器，而不是让用户一页页手工修（那还会逼着索引管线
   * 保留 edited 块，成本一路传导下去）。这里只保留"看得见"的那一半。
   */
  kind: 'citation' | 'selected' | 'chunk'
  label?: string
}
