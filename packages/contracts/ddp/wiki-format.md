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
`dependency_manifest`, `relations`, `stale` and `stale_reasons`. The stored revision,
pages, bindings and manifest are append-only. `stale` is evaluated against
current source state on read, independent from fixed historical metadata.

Each page has a stable `page_key`, title, generated sections of claims, and a
separate `human_paragraphs` array. Each generated claim has `id`, `text`,
`evidence_ids`, `unsupported`, and optional `conflict_group`. Claims with no
valid original evidence remain visibly unsupported. The model sees every
supplied evidence with its `evidence_id` and the 1-based `reference` number the
planner also uses; a claim may cite either, and the reference is resolved to that
evidence's ID. Anything else is dropped. Bindings link claim IDs
to stable original evidence IDs and excerpt digests; they do not imply that a
semantic verifier proved the claim correct. A planner output with more raw entries
than `max_pages` fails `409 wiki_budget_exceeded`. Within that bound, entries with
the same stable `page_key` merge into one writing plan: first title retained,
valid reference numbers and section headings unioned in encounter order. Invalid
references still fail; conflicting literal source anchors become null, never a
fabricated relationship anchor. More than 40 merged headings exceeds the budget.
Other rejected planner outputs fail `502 wiki_generation_failed` with the actual fault.

For a multi-source Wiki, both the plan and the generated claims must cite at least
one original from **every selected source binding** (origin, resource, fixed version).
The model receives these source identities with its evidence. Missing source
coverage fails `502 wiki_generation_failed`, before a new draft/revision or
idempotency receipt is committed; a failed rebuild leaves the previous revision
unchanged. Relations, retained human paragraphs, and the dependency manifest do not
substitute for generated claim coverage. Single-source unsupported drafts retain
their existing review/publication rules. Coverage is a structural check, not proof
that every requested fact was stated or that cited text supports its claim.

`relations` contains source-grounded statements connecting two planned pages.
Endpoints are page keys; each relation retains its original evidence IDs and
provider metadata. The predicate must occur in its cited original evidence.
Direction follows source mention order, not inferred causality. No co-mentioned
source anchors, or an explicit empty model selection, produces no relations.
Invalid selections fail generation instead of masquerading as an empty graph.

`DependencyManifest` records original evidence actually sent to each writing call (plus
retained human-page dependencies), conservatively including uncited context so a
model cannot launder a private input by citing a different public input, including `resource_id`, fixed
`source_version_id`, `source_digest`, `document_id`, `parse_revision`,
`evidence_id`, `excerpt_digest`, and original locator. Access authorization
checks the explicit resource binding; a different public resource with the same
bytes cannot grant publication permission for a private binding.

Source deletion/revocation removes access to derived content. A changed latest
resource version, parse revision, or original digest marks only dependent pages
stale and leaves the old revision intact. A newer resource version stales a page
only when none of that page's dependencies on the resource is at the latest version:
a rebuild carries the previous page's dependencies with its human paragraphs (so
publication still checks them), and a Wiki may cite several fixed versions on purpose.
Owner responses may read the old
revision while the exact original source remains authorized. Publication is
rechecked on every read; changing a source back to private stops public access.

## Work bounds and current scope

Planning receives only authorized original Evidence (`derived_from IS NULL`),
never generated Wiki pages. Every requested fixed source is authorized before
its evidence is considered, and its fixed parse revision must have
`status == succeeded` (otherwise `409 wiki_source_unavailable`, before any model
call). Candidates are the original Evidence behind the version's **current index**
(rows referenced by its chunks); Evidence superseded by an index rebuild stays readable
for historical citations but is never selected again. A version without indexed
original evidence fails with `409 wiki_source_unavailable`. Candidates are ranked by
the same retrieval as search and QA (vector + keyword fusion with the same similarity
floor, the title as query), then the rest of each source — including evidence below the
floor — in document order; sources take turns by rank so one
source cannot fill the budget. A bounded subset fits `max_evidence` and
`max_input_chars` (the serialized frozen evidence envelope).
Every requested source must contribute at least one original block within the
budget; otherwise generation fails visibly. This also keeps coverage metadata
subject to the same publication policy as the selected source context. Otherwise
`limits.evidence_selection` reports `total_original_evidence`, `selected_evidence`,
`omitted_evidence`, `complete`, `ranking_degraded` (`null`, or `embedding_unavailable`
when only the keyword path ranked the candidates), and per-source counts keyed by
`resource_id` and `source_version_id`. This field is absent on older revisions, and
`ranking_degraded` is absent on revisions built before it existed. Omitted original
evidence is not claimed as covered; the dependency manifest records selected
context, not the entire source.

One bounded planning call, at most `max_pages` writing calls, and at most one
relation-selection call are allowed. The last call is made only when original
statements co-mention two planned source anchors. Total completion allowance,
including relation selection, is split between calls using the model protocol's
`max_tokens`. Page count and response body size are checked. Exhausting a
generation bound fails visibly rather than publishing truncated model output.
Original-only sources have dependency expansion depth 0, preventing self-citation
cycles. This implementation accepts local ResourceVersions only; remote evidence
requires the separately validated federation retrieval and receipt adapter.
