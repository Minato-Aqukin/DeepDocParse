import { ElMessageBox } from 'element-plus'

import { documentsApi } from '@/api'
import type { IndexValidation } from '@/types/api'

export async function validateAndReindex(documentId: string): Promise<IndexValidation> {
  const validation = (await documentsApi.validateIndex(documentId)).data
  if (!validation.safe_to_reindex) {
    await ElMessageBox.confirm(
      `新版编译能接回 ${validation.citation_reconnectable} 条历史出处，` +
      `${validation.citation_invalidations} 条会明确标为失效。继续重建？`,
      '确认出处失效',
      { type: 'warning', confirmButtonText: '确认重建', cancelButtonText: '取消' },
    )
  }
  await documentsApi.reindex(documentId, !validation.safe_to_reindex)
  return validation
}
