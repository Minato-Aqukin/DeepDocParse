# Wiki v3 implementation verification

Implemented API contract: `packages/contracts/openapi/wiki-v1.yaml` and
`packages/contracts/ddp/wiki-format.md`.

- Append-only WikiRevision, WikiPage, ClaimEvidenceBinding, DependencyManifest,
  WikiHumanEdit and actor-scoped WikiWriteKey; migration 0016.
- Exact ResourceVersion.parse_job_id selection, source digests and locators.
  Checks source authorization before each planning/writing request and save.
- Atomic current_revision_id compare-and-swap; failed writes roll back all children.
- Same-key request body conflicts; retry returns the original fixed revision.
- Human paragraphs stored separately and preserved on rebuild; missing edited
  pages are retained with merge conflicts. Page limits include preserved pages.
- Staleness checked against source version/parse changes without mutating history.
- Public reads expose the published revision's title and contents only; current
  source policy is rechecked on reads and publication. All model context counts
  as a publication dependency, including uncited private input.
- Original-evidence-only generation; page, evidence, input text, model completion
  and response byte bounds. Generated Wiki is never accepted as original evidence.
- Legacy knowledge projection is private to its recorded author and organization;
  exact source bindings prevent same-byte alternate-resource authorization.
  Unattributed historical projections are quarantined. Entity scope isolation is
  migration 0021 (depends on 0020), and is compatible with 0022's multiple parses
  for a single document/resource by selecting fixed parse bindings explicitly.
- Legacy graph/Wiki reviews require the generated artifact's owner; assertion and
  extraction backlinks/reviews require originating conversation/run ownership
  plus original resource context.

## Verification performed

`services/corpus-api`: `pytest tests/test_wiki_revisions.py tests/test_knowledge.py`
completed with **16 passed**. The 9 new tests cover fixed manifests, immutable
history, human edits/rebuilds, stale CAS, retry body conflicts, stale dependencies,
missing parse bindings, published-vs-private title isolation, revoked sources,
private-context publication laundering, inter-call revocation, generation bounds,
separate authors, exact-resource closure, private backlinks/reviews, and legacy
unattributed data quarantine.

`scripts/check_wiki_pg.py` executed against the explicitly provided scratch
PostgreSQL database. Two independent sessions read the same base then write
concurrently: exactly one commits; the other receives `revision_conflict` (409).
Only base + winner revisions and one write key remain. The script creates a random
isolated schema and removed it afterward.

Ruff F/B checks passed for changed Wiki files, migrations and the PostgreSQL check.

## Scope and remaining integration

The workflow currently consumes authorized local ResourceVersions. Cross-node
receipt-backed evidence acquisition, desktop Wiki UX, background build scheduling,
semantic entailment verification and conflict-resolution UI remain integration
work. Provider metadata explicitly records semantic verification as not performed;
model outputs and GPU validation are not fabricated. Source state is rechecked
before every external request and save, but no durable in-flight input lease has
been added to prevent GC during a long model call; a removed source fails the
build rather than producing a published revision.

Latest combined run with `test_mcp_tools.py`: 30 passed, 3 failed during concurrent
MCP scope changes (two strict payload comparisons now include `scope`, one old
citation fixture lacks the new parse binding). These are outside the Wiki files;
the dedicated Wiki/legacy suite above passes.
