# DDP-Wiki v1: versioned, permission-bound drafts

The `/api/wikis` workflow is separate from legacy `/api/wiki` entity pages.
All operations require the trusted actor context. A Wiki belongs to one actor
and organization. Drafts and their fixed history are visible only to that actor;
published content still requires every source Resource to remain published.
A service actor has no automatic access.

## Requests

- `POST /api/wikis`: `{title, sources: [{resource_id, source_version_id}],
  max_pages: 1..12 (default 4), max_evidence: 1..200 (default 50),
  max_output_tokens: 256..16384 (default 4096), max_input_chars: 1000..200000
  (default 50000)}`. `Idempotency-Key` is required (1..128 chars).
- `POST /api/wikis/{wiki_id}/revisions`: same fields and `base_revision_id`.
  Select exact versions; a new version is never silently substituted. Rebuilds
  preserve the previous revision's human paragraphs; absent generated pages
  with manual edits remain as explicit merge conflicts.
- `GET /api/wikis`, `GET /api/wikis/{wiki_id}`,
  `GET /api/wikis/{wiki_id}/revisions/{revision_id}`.
- `PATCH /api/wikis/{wiki_id}/pages/{page_key}`: `{base_revision_id,
  paragraphs: [{id, text}]}` plus `Idempotency-Key`. Creates a new revision and
  human edit audit; never mutates generated text or the base revision. User text
  is labelled human/unsupported and cannot invent evidence bindings.
- `POST /api/wikis/{wiki_id}/publish`: `{base_revision_id}`. Publication compares
  the current revision atomically and fails when sources are private/revoked,
  unavailable, stale, unsupported, or contain unresolved merge conflicts.

Writes use compare-and-swap on `current_revision_id`; 409 `revision_conflict`
means the caller must reload. Scoped `(organization, actor, Idempotency-Key)`
keys bind the canonical request body AND operation. Same key/different request
returns 409 `idempotency_conflict`; replay returns the original fixed revision.
Failed transactions do not consume a key or leave partial drafts.

## Fixed outputs

`Wiki` has `id`, `title`, `current_revision_id`, `published_revision_id`.
`WikiRevision` has `id`, `wiki_id`, `base_revision_id`, `kind` (`generated` or
`human_edit`), `created_by`, `provider`, `limits`, `merge_conflicts`, `pages`,
`dependency_manifest`, `stale` and `stale_reasons`. The stored revision,
pages, bindings and manifest are append-only. `stale` is evaluated against
current source state on read, independent from fixed historical metadata.

Each page has a stable `page_key`, title, generated sections of claims, and a
separate `human_paragraphs` array. Each generated claim has `id`, `text`,
`evidence_ids`, `unsupported`, and optional `conflict_group`. Claims with no
valid original evidence remain visibly unsupported. Bindings link claim IDs
to stable original evidence IDs and excerpt digests; they do not imply that a
semantic verifier proved the claim correct.

`DependencyManifest` records original evidence actually sent to each writing call (plus
retained human-page dependencies), conservatively including uncited context so a
model cannot launder a private input by citing a different public input, including `resource_id`, fixed
`source_version_id`, `source_digest`, `document_id`, `parse_revision`,
`evidence_id`, `excerpt_digest`, and original locator. Access authorization
checks the explicit resource binding; a different public resource with the same
bytes cannot grant publication permission for a private binding.

Source deletion/revocation removes access to derived content. A changed latest
resource version, parse revision, or original digest marks only dependent pages
stale and leaves the old revision intact. Owner responses may read the old
revision while the exact original source remains authorized. Publication is
rechecked on every read; changing a source back to private stops public access.

## Work bounds and current scope

Planning receives only authorized original Evidence (`derived_from IS NULL`),
never generated Wiki pages. One bounded planning call and at most `max_pages`
writing calls are allowed. Total completion allowance is split between calls
using the model protocol's `max_tokens`; aggregate source text is bounded by
`max_input_chars`; evidence count, page count, and response body size are checked.
Exhausting any bound fails visibly rather than silently publishing a partial Wiki.
Original-only sources have dependency expansion depth 0, preventing self-citation
cycles. This implementation accepts local ResourceVersions only; remote evidence
requires the separately validated federation retrieval and receipt adapter.
