/** PDF /PageLabels are display aliases; navigation must retain the physical page index. */
export function formatPageLocator(
  pageIdx: number,
  printedPageLabel: string | null | undefined,
  physicalOnlyPrefix: '第' | 'PDF 第' = '第',
): string {
  return printedPageLabel
    ? `印刷页 ${printedPageLabel} · PDF 第 ${pageIdx + 1} 页`
    : `${physicalOnlyPrefix} ${pageIdx + 1} 页`
}
