# DDP-Wiki v1: versioned, permission-bound drafts

The `/api/wikis` workflow is separate from legacy `/api/wiki` entity pages.
All operations require the trusted actor context. A Wiki belongs to one actor
and organization. Drafts and their fixed history are visible only to that actor;
published content still requires every source Resource to remain published.
A service actor has no automatic access.

## Requests

- `POST /api/wikis`: `{title, sources: [{resource_id, source_version_id}],
  max_pages: 1..12 (default 1), max_evidence: 1..200 (default 50),
  max_output_tokens: 256..16384 (default 4096), max_input_chars: 1000..200000
  (default 24000)}`. `Idempotency-Key` is required (1..128 chars).
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
`evidence_ids`, `support`, `support_check`, `unsupported`, and optional
`conflict_group`. A center writer claim with an explicit empty `support` remains
visibly unsupported and carries no citations. The planner uses 1-based original
references; the writer instead receives a bounded catalog of original spans
(`q<reference>.<span>`), preserving every non-whitespace character, including PDF
extraction artifacts. Each supported writer claim selects 1..3 catalog IDs in
`support`; this is its **only source pointer**. The producer must not also emit
`evidence_ids` or `references`. Unknown sentence fields, missing selections,
unknown or duplicate span IDs fail before a revision is committed.
The server materializes selected spans into canonical `{reference, evidence_id,
quote}` records and derives the claim's citation IDs from those exact originals.
It never infers a source from claim text, fills in an unselected source, or
repairs a contradictory producer ID. This removes the redundant producer ID
channel, not the original-identity invariant.
Persisted and federated quotes retain their explicit references and are still
checked against the exact cited source set and verbatim original text
(whitespace-collapsed, case kept — `K` and `k` may differ); short atoms under 8
characters are quoted only in full. Invalid selections fail
`502 wiki_generation_failed` before any draft, revision or idempotency receipt is
committed. Bindings link claim IDs
to stable original evidence IDs and excerpt digests; a passing `support_check`
(`verbatim_present`) is a mechanical quote-presence verdict only and does not
imply that a semantic verifier proved the claim correct. A planner output with more raw entries
than `max_pages` fails `409 wiki_budget_exceeded`. Within that bound, entries with
the same stable `page_key` merge into one writing plan: first title retained,
valid reference numbers and section headings unioned in encounter order. Invalid
references still fail; conflicting literal source anchors become null, never a
fabricated relationship anchor. More than 40 merged headings exceeds the budget.
Other rejected planner outputs fail `502 wiki_generation_failed` with the actual fault.

For a federated quote, a supplied `evidence_id` must be a string identifying an
original admitted for this commit. Unknown or malformed explicit IDs are rejected,
never discarded in favor of a numeric `reference`. Known IDs are remapped to the
current frozen evidence order before the shared quote checks.

When an authorized indexed original has native `table_html`, center planning and
writing receive that structure alongside its original text/span catalog. The
whole selected snapshot, including HTML, is charged to `max_input_chars`; an
oversized table is not silently flattened to bypass the limit. Quote checking
still uses the unchanged original text and excerpt digest, not generated HTML.
`limits.table_contexts` freezes each selected table's `evidence_id`, `table_html`
and UTF-8 SHA-256 `table_html_digest` for that generation. Historical revisions
never read a replacement structure from a rebuilt index. The selection profile
is `original-text-and-table-html/1`; none of these checks establishes entailment.

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
original evidence fails with `409 wiki_source_unavailable`.
- Candidates are ranked by the shared facet retrieval (the title plus up to three
  bounded retrieval queries — the same seam search and QA use) with the same similarity
  floor: each focused query keeps its own fused order (hybrid pool fused per query
  with the shared cross-encoder order via RRF whenever reranking is enabled
  and actually ran). This is Wiki's ranking policy; QA and fields retain their
  separately measured selection policies. Focused queues take turns
  before the generic title queue; the title and then original document order
  (including evidence below the floor) fill remaining slots. One head per
  distinct focused-query result seeds the shared QA/extraction neighbor helper
  with its real index chunk identity; title-only fallback uses one head.
  These facet seeds all precede their qualifying bounded page-local context: a
  neighbor jumps the queue only when it continues its parent's own block
  (identical page geometry) or the ranker independently placed it within
  `max_evidence` + facet-head merged positions, capped by the number of actual
  retrieved candidates before document-order fallback is appended. Fallback
  position alone is never independent rank corroboration. Other page-local neighbors
  keep their own ranked tail positions instead of evicting higher-ranked
  facet hits. Immediate neighbors precede second-hop continuations, with
  original parent identities retained before their children. Thus an early
  facet's neighbors cannot take every count-budget slot before a later facet
  is represented. Qualifying context is promoted ahead of the remaining
  ranked tail without widening either limit.
  Sources also take turns by rank so one source cannot fill the budget.
When reranking is enabled the per-query pool
widens to `rerank_candidates` (the QA/extraction seam); reranking reorders the
already-authorized bounded retrieval pool only: it admits no new atoms and
never widens the final evidence/input/output limits.
Before each reranker request, candidates are restricted to the original atoms of
the requested fixed source version and that source's live access/publication
state is rechecked. Withdrawal during query planning or retrieval therefore
blocks text egress rather than waiting for the later writer gate.
The retrieval queries come from one bounded query-planning call over the title
alone (never source content). Profile `wiki-query-planning/2-focused-facets`
requests separate concise English expressions for requested concepts, without
repeating the document title unless needed to distinguish subjects. It does not
guess values or add requested goals. `applied` records valid query shape, not
proof that the model covered every topic. Planner failure falls back to
title-only retrieval without inventing queries or retrying. Its completion-token cap
(`min(256, max(64, max_output_tokens // 8))`) is charged against
`max_output_tokens` even when planning fails, and the outline/writing calls are
sized from the remainder; recorded caps bound every request, not measured usage.
A bounded subset fits `max_evidence` and
`max_input_chars` (the serialized frozen evidence envelope). `max_input_chars`
caps only that final frozen payload sent to the writing model, not the upstream
ranked candidate texts the reranker scores. Ranked budget selection, including
pulled page-local continuations, is fixed before generation; continuations share
the same `max_evidence`/`max_input_chars` caps and the every-source
representation gate instead of appending beyond them. The planner and writer
then see the frozen evidence in source reading order (the QA `build_messages`
seam), so a sentence's subject and its continuation are not separated by other
hits.
Every requested source must contribute at least one original block within the
budget; otherwise generation fails visibly. This also keeps coverage metadata
subject to the same publication policy as the selected source context. Otherwise
`limits.evidence_selection` reports `total_original_evidence`, `selected_evidence`,
`omitted_evidence`, `complete`, `ranking_degraded` (`null`, `embedding_unavailable`
when only the keyword path ranked the candidates, or `rerank_unavailable` when
the cross-encoder did not run and the hybrid pool order stands), and per-source
counts keyed by `resource_id` and `source_version_id`. It also reports `query_planning`
(`status` `applied` or `unavailable` with `reserved_output_tokens` and, on
failure, a short `reason`) and, when planning applied, the bounded
`retrieval_queries` actually used. These fields are absent on older revisions, and
`ranking_degraded` is absent on revisions built before it existed. Omitted original
evidence is not claimed as covered; the dependency manifest records selected
context, not the entire source. Generation sees only that bounded subset: the
writer must never claim the whole document lacks a fact, a value, or a section,
and an unstatable requested subtopic stays visible as unsupported rather than
becoming an absence verdict about the whole document.

The reader displays unavailable query planning and its recorded reason as a
title-only retrieval warning, independently of `ranking_degraded`. Missing planning
metadata on historical revisions is not reported as a new failure or success.

One bounded query-planning call, one bounded outline-planning call, at most
`max_pages` writing calls, and at most one
relation-selection call are allowed. The last call is made only when original
statements co-mention two planned source anchors. Total completion allowance,
including relation selection, is split between calls using the model protocol's
`max_tokens`. Page count and response body size are checked. Exhausting a
generation bound fails visibly rather than publishing truncated model output.
New revisions record `limits.generation_observations` (per-stage allowances and
per-claim mechanical verdicts), `limits.protocol` (`ddp-wiki-generation/9`) and
`limits.decoder_revision` (`wiki-json/6-authoritative-support-spans`); these fields are
absent on older revisions, which stay readable with their original bindings.
Original-only sources have dependency expansion depth 0, preventing self-citation
cycles. This implementation accepts local ResourceVersions only; remote evidence
requires the separately validated federation retrieval and receipt adapter.
