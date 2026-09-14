# P4 persistent scope validation

Verified on 2026-09-13 against the current uncommitted worktree. This is a local-directory scope implementation; remote collection expansion still needs authenticated downstream delegation and a real producer.

## Implemented behavior

- `POST /api/v1/federation/scopes` freezes the authenticated caller's persistent member snapshot together with the actual corpus collection snapshot. Both source revisions and snapshot IDs remain in the revision vector. User input cannot assert targets, caller identity, source revisions or completeness.
- The collector reads the stable catalog through its separate terminal cursor. A data page, empty/incomplete response, mixed revision, missing cursor, cursor cycle, expiry, unknown source, redirect or exhausted budget cannot produce a sealed scope. Consistent targets already observed remain in partial scopes.
- Targets use `(origin_node_id, collection_id, operation)`, sort deterministically and deduplicate. Capability readiness is not an exclusion input. Registered remote members without a verified collection producer remain named unknown subtrees, restricted to the original caller's authorized view.
- Scope manifests, target pages and opaque cursors persist in control SQL migrations 0007/0008. Current credentials and permission scope are checked on every read. New members or a new catalog require a new scope; the original denominator and digest remain stable.
- Reads revalidate the original corpus snapshot. Identified withdrawn collections become persistently `revoked`, while other targets in that invalid snapshot remain `unreachable`. Later source success or expiry cannot erase a recorded revocation. Retained expired history exposes `expired:true`; metadata sealing does not freeze historical full text or imply successful retrieval.

## Executed checks

Control migrations 0001 through 0008 were applied to the isolated PostgreSQL database `ddp_v3_scope` in the existing scratch container on port 15439. The checks use synthetic test accounts and collections. Existing migrations 0005/0006 were not changed. Migration copies and the migration checksum ledger match.

`CONTROL_TEST_DATABASE_URL=<scratch control DSN> go test ./...` and `go vet ./...` passed. Targeted scope tests also passed with the actual corpus DSN supplied through `SCOPE_CORPUS_DATABASE_URL`:

| Check | Evidence |
|---|---|
| T71 stable pages and unique denominator | `TestScopePGFrozenDeduplicatedPagesIsolationAndDurableRevocation`, digest round-trip test, separate empty terminal page |
| T72 unknown subdomains | `TestScopePGDirectMembersAreUnknownNotInventedCollections`, hidden member excluded without leaking a count |
| T73 snapshot inconsistency and truncation | `TestScopeHTTPPartialForInvalidEnumerationAndBudget`: changed revision, missing terminal, early complete, cycle, redirect, budget; PG missing-page and expired-source cases |
| T74 new scope and revocation retention | PG Store reconstruction plus actual HTTP withdrawal; old denominator/digest retained, specific revoked target recorded, new scope excludes withdrawal |
| T75 metadata versus content snapshots | Both envelope and corpus producer explicitly deny a full-content snapshot guarantee; index/metadata changes remain incomplete rather than success or inferred revocation |
| Credential isolation | HTTP 404 for another user or another key belonging to the same user; forged caller fields rejected; fixed service producer receives server-derived identity |

`TestScopeRealCorpusPublishedCatalogAndWithdrawal` starts the real Python collection router over loopback HTTP against the separately migrated `ddp_v3_collections` PostgreSQL database, using `tests/scope_catalog_server.py`. It does not mock catalog behavior or expose a test-only mutation route. The fixture contains two explicitly published ready collections and one private decoy. The real Go scope endpoint obtains exactly two targets and seals only after catalog termination. The test withdraws one collection through the normal Go `/api/v1/collections/{id}/withdraw` proxy. The original scope retains both targets and its digest, marks precisely the withdrawn collection revoked, and a new scope contains one authorized target. The other original target is incomplete, not falsely reported revoked.

Federation schema/fixture validation, enum usage validation and `git diff --check` passed. The new optional directory/snapshot revision references were included in the regenerated resolved schema bundle.

## Explicit limits

No registered remote node is contacted automatically, and no user credential is forwarded downstream. Remote subtree enumeration, Probe dispatch, retrieval receipts, root-wide request/byte billing, historical full-text snapshots and complete-coverage percentages are not implemented by this P4 slice. Remote nodes remain `partial` even when their configuration declares enumeration support. `total_targets` is the number of known targets; with unknown subtrees it is not the full scope denominator. The bounded metadata collector is synchronous (10-second deadline and a finite page/target budget), not a background all-network crawler. GPU retrieval or model execution was not part of these checks.
