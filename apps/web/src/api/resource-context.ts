/** A resource selected in the URL is explicit UI context, never an authorization claim.
 * Attach it only to reads of that document (or its evidence), never another document
 * or a remote URL. The server always checks the binding again.
 */
export function selectedResource(url: string, hash: string, params?: Record<string, unknown>): string | undefined {
  const current = /^#\/documents\/([^/?]+)(?:\/[^?]*)?(?:\?(.*))?$/.exec(hash)
  if (!current || !url.startsWith('/api/')) return undefined
  const resource = new URLSearchParams(current[2] || '').get('resource_id')
  if (!resource) return undefined
  const document = /^\/api\/documents\/([^/?]+)/.exec(url)
  if (document && document[1] === current[1]) return resource
  if (/^\/api\/evidence\/[^/?]+/.test(url)) return resource
  if (url === '/api/conversations' && params?.document === current[1]) return resource
  return undefined
}

export function selectedVersion(url: string, hash: string, params?: Record<string, unknown>): string | undefined {
  if (!selectedResource(url, hash, params)) return undefined
  return new URLSearchParams(hash.split('?')[1] || '').get('version_id') || undefined
}
