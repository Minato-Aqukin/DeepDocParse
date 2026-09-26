# DDP Resource Policy v1

This additive contract supersedes deployment-wide shared-read behavior. Content hashes
identify bytes, never ownership. `ResourceVersion.document_id` binds existing content.

Authenticated control API actor context is required on every route. Private/draft/withdrawn
resources are readable and writable only by the matching local organization and owner.
API keys use the verified `X-DDP-User` subject supplied by control-api; a key id is not a user id. Missing key subject context fails closed. Service/node credentials confer no private resource permission. Published resources are
readable by authenticated actors **of the resource's organization** while their complete `copied_from` ancestry remains published; missing origins and cycles do not grant public access. Only the owner can change or delete them. Denied
reads return 404 with no resource metadata. Unknown publication states fail closed.
A resource with a `copied_from` parent (a metadata-only copy, or a root that borrowed a
version) is readable by its own owner only while that parent is still readable to the owner:
owned by the same principal, or published through its complete ancestry. Withdrawing,
unpublishing or deleting a foreign ancestor therefore closes the copy's content — resource,
versions, search, evidence, crops, originals, conversations and Wikis — to the copier as
well; the owner can still list, rename, withdraw and delete the copy (writes do not depend
on lineage), and republishing the ancestor reopens it. Without this a copy made while the
source was public kept revoked content readable to the copier. Local resources are
tombstoned, never removed, so a `copied_from` with no local row at all is a placeholder (a
bundle import's `remote:` origin): it keeps publication closed but does not close the owner's
own copy — that revocation is carried by the replica ledger.
The organization predicate applies to every published read (resource and version reads, search,
evidence, crops, bundles, `site_public` listing). The first deployment form is single-organization,
where this is the whole site; enterprise boundary 8 keeps the predicate anyway so a multi-organization
deployment never serves one tenant's publications to another. Cross-node sharing is federation
(peer probes and admissions under the serving node's organization), never a cross-tenant read.
Legacy documents without any resource mapping remain accessible only to their original
organization's recorded uploaders. A mapped document must never fall back to this rule.
Legacy resolution with multiple authorized logical resources requires `resource_id` and
returns 409 `resource_context_required` rather than choosing a resource arbitrarily.

GET /api/v1/resources (alias /api/resources): scopes `mine` (default), `site_public`
(published resources of the caller's organization).
Returns items, offset, limit, has_more and coverage with scope, complete and local watermark;
no remote enumeration or global totals are implied. Temporary external parse submissions
are never listed in site_public.
GET /api/v1/resources/{resource_id}: resource metadata and immutable versions.
POST /api/v1/resources: create a private asset by copying an authorized document binding;
requires Idempotency-Key. Never accepts a bare hash as authorization. Optional copied_from
must identify an authorized resource. Same key and payload returns the same asset; same
key and different payload returns 409 idempotency_conflict. A new key creates a new asset.
GET /api/v1/resources/{resource_id}/versions and GET /api/v1/resources/{resource_id}/versions/{version_id}: authorized fixed version metadata.
Version read responses include `parse_status` and `index_status`, the current parse and
index states (enums `parse_status` / `index_status`) of that version's fixed
`parse_job_id` (both null when no parse exists). They are readiness projections, not a
mutation of the version binding. A non-null job ID alone does not establish successful
parsing; source choosers require `parse_status == "succeeded"`, and retrieval over a
version needs `index_status == "ready"`.
Every fixed version of a resource stays searchable, and versions usually share a filename.
`GET /api/search` groups and `GET /api/documents/{id}` (opened with a `version_id`) therefore
carry `source_version_no` next to `source_version_id` (null for legacy non-resource documents),
so a reader can tell which fixed version a hit or a conversation belongs to.
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
A conversation is bound to the resource it was opened in and answers from and cites that
resource's fixed parse. `GET /api/conversations?document=` with a resource context
(`resource_id`) therefore lists only conversations bound to that resource; without a context
it keeps listing every readable conversation on the document. Two resources holding the same
bytes share one Document, and listing the other resource's conversation made its citations
unreadable (404) in the resource being viewed.

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
