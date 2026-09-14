import { http } from './http'

export interface ResourceVersion {
  id: string
  resource_id: string
  version_no: number
  document_id: string
  source_digest: string
  filename: string
  size_bytes: number
  parse_job_id: string | null
}
export interface Resource {
  id: string
  organization_id: string
  owner_id: string
  uploader_ref: { issuer: string; subject: string }
  display_name: string
  publication: 'private' | 'draft' | 'published' | 'withdrawn'
  versions: ResourceVersion[]
}
export const resourcesApi = {
  list: (scope: 'mine' | 'site_public', offset = 0) => http.get<{ items: Resource[]; has_more: boolean }>(
    '/api/resources', { params: { scope, offset, limit: 50 } }),
  publish: (id: string, publication: Resource['publication']) => http.patch<Resource>(
    `/api/resources/${encodeURIComponent(id)}`, { publication }),
  remove: (id: string) => http.delete(`/api/resources/${encodeURIComponent(id)}`),
  bundleUrl: (resource: string, version: string) =>
    `/api/resources/${encodeURIComponent(resource)}/versions/${encodeURIComponent(version)}/bundle`,
}
