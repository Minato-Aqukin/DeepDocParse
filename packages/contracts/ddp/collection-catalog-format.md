# DDP collection catalog v1

Corpus is the only writer of collections and their fixed resource-version members. Control
calls corpus over its authenticated HTTP boundary and never reads corpus SQL. There is no
automatic collection for a resource, user, organization or private corpus inventory.

`POST /api/v1/collections` creates an owner-bound private draft. The body contains `name`,
`licence`, explicit `languages` and `topics`, optional `time_range`, optional
`onward_recipients`, and 1–100 distinct `version_ids`. Metadata is supplied by the caller;
neither summaries nor counts are inferred from private content. `PUT /api/v1/collections/{id}`
replaces the same fields (omitting `onward_recipients` removes the policy) and requires
`expected_revision`; replacement returns the collection to draft. `POST .../{id}/publish`
and `POST .../{id}/withdraw` require `expected_revision`. Every write requires an
`Idempotency-Key` and a user/API-key principal who owns the collection or is an administrator
in that organization. Creation requires contributor. Idempotency binds organization, actor,
operation and canonical body; same key/different body returns 409 `idempotency_conflict`.
Stale expected revisions return 409 `collection_revision_conflict`. Replays recheck current
access, and never reinstate old publication or expose superseded membership.

Members pin ResourceVersion identity, its resource, document, parse binding and verified
source digest. Publication is an explicit second action and requires every source to be
currently public (including all copy ancestors), persistent, and successfully indexed with
chunks. Private-source publication returns a generic `collection_not_publishable` conflict.
There is no implicit sharing of a source. `GET .../{id}` rechecks all sources, returning 404
if the caller cannot currently read the collection and every fixed member. Owner/admin can
manage their draft metadata; administration does not grant access to another user's private
resource. Withdraw is always possible for the collection owner/admin.

`GET /internal/federation/collections` is a service-only producer. It requires the service
Bearer and standard `X-DDP-*` service actor headers, plus control-generated
`X-DDP-Caller-Actor`, `X-DDP-Caller-Kind` (`user`/`api_key`), `X-DDP-Caller-Role`,
`X-DDP-Caller-User` for API keys, `X-DDP-Caller-Scope` (`sha256:<hex>`), and
`X-DDP-Authority-Node`. Caller organization is the authenticated service organization.
Control strips these headers from inbound clients. The producer must not be proxied as an
unfiltered public route. Missing/malformed caller context is 401.

First request: `?scope_id=<1..128 character scope>&limit=<1..100, default 100>`.
Continuation: also supply `snapshot_id` and `cursor` from that snapshot. A snapshot persists
five minutes, binds scope_id, caller digest, actor identity/role, organization and node;
continuation cannot change its page size. Its response is:

```
{snapshot_id, scope_id, caller_scope_hash, origin_node_id, registry_revision,
 created_at, valid_until, first_cursor, terminal_cursor, total,
 collections: [CollectionDescriptor], index_readiness: {collection_id: ready|unavailable},
 next_cursor, complete, content_snapshot_complete: false}
```

`CollectionDescriptor` is the strict existing DDP-DISCOVERY v1 schema: origin_node_id,
collection_id, explicit licence/languages/topics/time_range/onward_recipients, revision,
index_revision and
valid_until only. Source IDs, members, filenames, counts, text, embeddings and storage keys
never enter descriptors. Only explicitly published, currently public-authorized collections
owned by the caller's organization enter the directory or its total/revision fingerprint.
A private collection write cannot change another caller's registry_revision.

`onward_recipients` is the source's onward-transfer policy (T83). Absent means the source
adds no restriction beyond the user's execution consent. A list (possibly empty) names the
only nodes, besides the plan's root coordinator, that evidence excerpts from this collection
and anything derived from them may reach along the plan's data edges, relays included. The
executor enforces it at admission over the whole approved plan: if any downstream recipient
of its outbound edges is not the root coordinator and not listed, a `retrieve` step on this
collection is refused with 403 `egress_denied` and nothing is persisted or executed. A
`retrieve` with no collection target is checked against every published collection of the
organization that carries a policy (fail closed). The coordinator also reads the field from
descriptors at planning and never selects a delegated generator a selected source forbids;
if that leaves no generator the result says `answer_reason=source_policy_denied`. Changing
the policy is a `PUT`, so it bumps the revision and returns the collection to draft.

Pages are fixed at creation. `complete=true` occurs only on the separate empty terminal
page, with next_cursor=null; an empty catalog still has that terminal proof. New collections
do not enter old snapshots. Every page rechecks every frozen collection's current publication,
revision, all source permissions and index fingerprint. Source withdrawal and collection revocation return 410 `catalog_snapshot_invalid`; only
a matching unexpired snapshot caller receives `revoked_collection_ids`, restricted to
identities already in that snapshot, and that bound 410 also repeats the snapshot's own
`snapshot_id`, `scope_id`, `caller_scope_hash` and `origin_node_id` so the reader can verify
which snapshot was revoked before trusting the identities. Wrong scope/caller/cursor returns
the generic 410 without identities or binding. Expiry returns 410 `catalog_snapshot_expired`;
changed index or metadata revision returns 409 `catalog_snapshot_changed`. None silently
replaces a page or its scope.
Snapshots are bounded to 10,000 collections, 8 MiB of authorized descriptors and 32 live
snapshots per caller binding. Exceeding a bound fails explicitly, never truncates to complete.

`index_revision` hashes the actual fixed member parse identities and observed index generation,
status, compile fingerprint and source/ancestor publication epochs. Revoking then
republishing a source cannot revive an older snapshot, even when nobody read it during
the revoked interval. Ancestry is bounded to 100 resources; unsupported longer chains fail closed. Index readiness is separate from enumeration. Once published,
a temporarily rebuilding/failed index remains enumerable with `index_readiness=unavailable`.
`complete` is solely a directory pagination proof. It does not freeze all content or prove
retrieval completeness: `content_snapshot_complete` is always false, and retrieval receipts
must independently bind the actual index and evidence versions.
