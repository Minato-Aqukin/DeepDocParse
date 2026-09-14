# DDP Resource Policy v1

This additive contract supersedes deployment-wide shared-read behavior. Content hashes
identify bytes, never ownership. `ResourceVersion.document_id` binds existing content.

Authenticated control API actor context is required on every route. Private/draft/withdrawn
resources are readable and writable only by the matching local organization and owner.
API keys use the verified `X-DDP-User` subject supplied by control-api; a key id is not a user id. Missing key subject context fails closed. Service/node credentials confer no private resource permission. Published resources are
readable by authenticated actors while their complete `copied_from` ancestry remains published; missing origins and cycles do not grant public access. Only the owner can change or delete them. Denied
reads return 404 with no resource metadata. Unknown publication states fail closed.
Legacy documents without any resource mapping remain accessible only to their original
organization's recorded uploaders. A mapped document must never fall back to this rule.
Legacy resolution with multiple authorized logical resources requires `resource_id` and
returns 409 `resource_context_required` rather than choosing a resource arbitrarily.

GET /api/v1/resources (alias /api/resources): scopes `mine` (default), `site_public`.
Returns items, offset, limit, has_more and coverage with scope, complete and local watermark;
no remote enumeration or global totals are implied. Temporary external parse submissions
are never listed in site_public.
GET /api/v1/resources/{resource_id}: resource metadata and immutable versions.
POST /api/v1/resources: create a private asset by copying an authorized document binding;
requires Idempotency-Key. Never accepts a bare hash as authorization. Optional copied_from
must identify an authorized resource. Same key and payload returns the same asset; same
key and different payload returns 409 idempotency_conflict. A new key creates a new asset.
GET /api/v1/resources/{resource_id}/versions and GET /api/v1/resources/{resource_id}/versions/{version_id}: authorized fixed version metadata.
POST /api/v1/resources/{resource_id}/versions: bind an authorized document as a new fixed
version, with Idempotency-Key. Ownership is checked before resolving any content.
PATCH /api/v1/resources/{resource_id}: owner may update display_name or publication;
only persistent web resources with original bytes and ready indexes may be published; a copy cannot be published while any source ancestor is private, missing or withdrawn.
DELETE /api/v1/resources/{resource_id}: tombstones this resource and its versions only.
The last live reference also marks the content row inactive and fences index workers; bytes, chunks and audit history remain until reference-aware GC. Existing document DELETE resolves the caller's asset and follows the same rule. Shared
content reclamation belongs to reference-aware GC, never to the resource endpoint.

Search authorization is applied before vector and keyword candidate limits, rerank or
model calls. Original files, crops, evidence, historical conversations and extraction
exports recheck current source permission; cache validators never precede authorization.

## Resource-scoped mutation (P1)

`reparse`, `current-job` and `reindex` require ownership of the resolved resource.
Public read permission never grants these writes. A shared content row can host separate
ParseJobs: changing one resource's job or index must leave another resource's fixed
versions, chunks and index generation unchanged. Selecting a new parse appends a fixed
ResourceVersion; it never rewrites an existing version's parse binding. ParseJob owns
index/compile readiness and fencing state; Document fields are compatibility mirrors.
A metadata copy pointing at its source's ParseJob cannot rebuild that borrowed parse:
`reindex` returns 409 `shared_parse_write_unsupported` until it has its own parse.

Human evidence verification still mutates shared review/assertion state. It requires
ownership and no other live resource referencing the Document; shared writes return
409 `shared_document_write_unsupported`. A caller must also be authorized for the
specific evidence parse. Missing ownership returns 404 before mutation.

Every metadata-only asset copy derives `copied_from` from the server-authorized source
binding. The optional client field is only a source-context assertion; it cannot remove
lineage. Only verified byte ingestion can create an independent publication root.
Metadata version additions apply the same lineage rule. The current single-parent model
can add one source dependency to a root, or retain its existing identical dependency;
a different additional parent returns 409 `resource_lineage_conflict`. Self/descendant
parents are rejected. Failed or conflicting operations leave the target lineage unchanged.
This is a bounded P1 restriction, not full multi-source version provenance support.

Parse attempts are scoped by `(document_id, resource_id, options_hash)`. Explicit uploads
create independent pending attempts and input grants, even when bytes are deduplicated.
The gateway cache identity is a hash of resource identity plus the verified byte digest;
`Document.doc_id` and `ResourceVersion.source_digest` retain the byte identity. Every
version records its actual parse job explicitly at submission, including stable retries.
Historical conversations/extractions with missing resource context cannot infer ownership
from a subsequently published same-content asset. Migration may bind an unambiguous
original owner; unresolved mapped histories fail closed on access and model dispatch.

Fixed parse authorization is applied before retrieval candidates, model reranking, crops,
evidence details and extraction exports. A readable Document hash cannot authorize a
parse from another asset. `version_id` can select a version only inside the resolved
resource; omitted version context chooses its latest fixed version. Metadata copies
preserve the authorized source version's filename and parse binding. Extraction runs
record the chosen versions and recheck their source before every model dispatch,
including retries and verification. Revocation cancels remaining extraction fields. A
fixed resource parse without a searchable index returns `resource_index_unavailable`;
it never borrows another asset's ready index.

Registration holds the content lock until the live asset/version/parse binding commits.
Redundant upload bytes are deleted only after this commit so concurrent GC cannot destroy
both the old deduplicated object and the newly verified source before registration.
