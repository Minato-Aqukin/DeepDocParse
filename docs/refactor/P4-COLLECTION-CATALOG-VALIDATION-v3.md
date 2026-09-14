# P4 explicit collection catalog validation

Implemented corpus-owned Collection metadata and fixed ResourceVersion members. New code:
`ddp_corpus/collection_models.py`, `catalog.py`, `routers/collections.py`; migration `0026`.
Contract: `packages/contracts/ddp/collection-catalog-format.md` and
`packages/contracts/openapi/collections-v1.yaml`. Strict CollectionDescriptor remains the
existing DDP-DISCOVERY schema; it never contains source IDs, content, embeddings or counts.

Publication is an explicit owner/admin operation after live public-source and ready-index
checks. Replacement returns to draft. Every write uses scoped canonical-body receipts and
CAS revisions. Private collections do not enter catalog totals or revision fingerprints.
Sources and ancestor permissions are rechecked on reads and every frozen page, including
the empty terminal page. Source publication epochs prevent withdrawal/republication from
reviving an older snapshot. Directory completeness is separate from index readiness and
never claims content-snapshot completeness.

## Completed checks

- 9 SQLite HTTP tests: explicit publication/privacy, third-user and cross-organization
  denial, strict descriptor schema, same-key/different-body conflicts, CAS/admin boundaries,
  fixed pages with new arrivals, caller/scope/cursor isolation, expiry, ancestor revocation,
  unavailable-index disclosure, fixed parse membership, true service/caller context, and
  source withdrawal/republication and visible-only descriptor byte bounds.
- 1 real PostgreSQL test: 10 concurrent identical creates produce one collection and one
  fixed member; 8 concurrent CAS publishes produce one success and seven conflicts;
  8 concurrent catalog snapshots share a visible revision; source withdrawal invalidates
  the original terminal proof and reports only the original denied collection IDs.
- Fresh scratch PostgreSQL database `ddp_v3_collections` on loopback port 15439 migrated
  0001→0026; successful 0026→0025→0026 round trip. These runs used Alembic, not create_all.
- 71 targeted corpus tests passed before the final added source-republication regression:
  collection, client projection, resource ACL/layer/migration tests. Final collection-only
  run including PostgreSQL: 10 passed. Ruff F/B (repository B008 exception) passed.
- Scope agent's `TestScopeRealCorpusPublishedCatalogAndWithdrawal` passed using
  `tests/scope_catalog_server.py` with the real Python router and migrated PG: Go creates a
  sealed scope with two explicit public collections and excludes a private decoy; withdrawal
  through the actual Go collection proxy marks the exact old target revoked and the remaining
  old snapshot target unreachable, preserves the denominator/digest, and a new scope seals
  with one target. No catalog or HTTP revocation endpoint was mocked.

Reproduction:

```bash
cd services/corpus-api
env COLLECTION_CATALOG_TEST_DATABASE_URL='<dedicated migrated PG URL>' ../../.venv/bin/python -m pytest -q tests/test_collection_catalog.py tests/test_collection_catalog_pg.py
```

The fixture server requires `SCOPE_CORPUS_DATABASE_URL`, `SERVICE_TOKEN`, `--port`,
`--organization` and `--owner`; optional `--fixture-file` emits seeded IDs. It has no admin
backdoor and does not run production storage/model lifespan. It exists only for real HTTP
boundary tests. There was no commit or push in this subtask.

Optional CLI review attempts did not produce a verdict: Claude reported an exhausted
session limit; the bounded 90-second OpenCode read-only review reached its timeout. Neither
is counted as an independent acceptance pass.
