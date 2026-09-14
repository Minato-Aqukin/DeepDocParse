# DDP-CLIENT v1

`ddp-client/1` is the workbench projection and reconciliation protocol. It does not
replace the authoritative resource, task, approval or delivery APIs. A chat model
endpoint is insufficient to implement this protocol.

The shared runtime registers one connection per `(environment_id, profile_id)`.
An environment binds an authority node and a workspace; a profile binds its issuer
and authenticated subject. Local identities come from the owned launcher's bootstrap.
Remote pairing first reads `/api/v1/federation/node` without credentials and matches
the saved authority using a fresh key-possession proof. It then authenticates and verifies the workspace and subject.
Moving to a different authority or subject requires explicit pairing and a different
cache scope. An address change alone is not an identity change: `relocate` replaces
the transport, retains the identity-scoped cache, and performs a fresh handshake.

The client sends a cryptographically random 24-byte challenge encoded as unpadded
base64url in `?challenge=`. The response includes the 32-byte Ed25519 `public_key`
in standard base64 and `proof` with schema `ddp-node-proof/1`, `nonce`, `node_id`,
`endpoint`, `issued_at`, `expires_at`, and unpadded base64url `signature`. The node ID
is `node-` plus the first 48 hexadecimal characters of SHA-256 of the raw public key.
The signature covers the UTF-8 JSON array `[schema,nonce,node_id,endpoint,issued_at,expires_at]`.
The endpoint is the node's configured public base URL with no trailing slash, not
an incoming Host or Forwarded header. Its exact match prevents a relayed proof from
another endpoint being accepted. Validity is at most 60 seconds with 5 seconds of
clock skew tolerance. A copied public descriptor without the private key is insufficient.
Neither a failed proof nor a redirect may receive the saved credential.

`GET /api/v1/client/handshake` returns:

```json
{
  "protocol_version": "ddp-client/1",
  "identity": {"environment_id":"env", "authority_node_id":"node", "workspace_id":"workspace"},
  "profile": {"issuer":"node", "subject":"user"},
  "capabilities": ["client.snapshot", "client.events", "client.receipt"]
}
```

Capability IDs indicate implemented interfaces. Current engine/model readiness is
separate data in `state.capabilities`; an unavailable generator never becomes ready
because the Wiki route exists. Unknown protocol versions or missing required client
interfaces reject the connection before it applies new authoritative data.

`GET /api/v1/client/snapshot` returns `{cursor, sequence, state}` from one consistent
read. The opaque cursor is bound to the authenticated workspace and a consistent
source revision (local event watermark or center caller-scoped observation revision).
State includes authorized `resources`, durable `tasks` and current
`capabilities`. Credential values and heartbeat-only lease timestamps are excluded.

`GET /api/v1/client/events?after=<opaque-cursor>` returns `events`, each containing a
complete projection and `previous_sequence`. A frame consumes the complete source
range `(previous_sequence, sequence]`; the prior sequence must equal the client's
last applied sequence. An unchanged frame acknowledges the exact prior cursor and
sequence without applying different state. The response is not a promise of a
historical full-text snapshot. Invalid, expired, cross-workspace or future cursors
return HTTP 410 with code `cursor_expired`; the client obtains a fresh snapshot.
Changing runtime capabilities creates a durable event, so an old cursor cannot
acknowledge a new model configuration as if nothing changed.

State and cursor persist in one transaction, guarded by connection epoch and expected
cursor. A disposed connection cannot write a later epoch. Transport state and data
freshness are independent: an offline cached snapshot is `stale`, never `current`.
Cache disposal preserves drafts and command reconciliation records and does not delete assets.

`GET /api/v1/client/receipts/{operation_key}` reads an already accepted operation in
the authenticated scope. A key is an opaque nonsecret business identifier, never an
authorization token. Unknown keys return 404. Writes persist a canonical request
digest and intent before dispatch; uncertain results require explicit receipt lookup.
The original caller may discard an intent only when it knows no request was sent.
An interrupted process or a missing remote receipt cannot prove this. Confirmed
receipt bodies are compacted after 128 entries per scope; their request digest
tombstones still prevent replay and require an explicit remote receipt lookup.
The unresolved ledger has a separate 10,000-entry cap, and the SQLite cache has a
64 MiB database cap; reaching either produces a visible storage failure.
Reconnection restores reads and subscriptions and never replays writes. A receipt
records acceptance, not successful generation or verified delivery.

The desktop SQLite cache is main/utility-process-only. It uses private files, WAL,
full synchronous commits, bounded projection/receipt sizes, fenced epochs and CAS
draft revisions. It stores no provider credential. The web uses a separate adapter;
importing the shared connection code never opens SQLite or a network connection.

Read-only `Connection.query` supports `corpus.search`, `evidence.get`, local
`models.list`, and the center's `resource.page`/`task.page` windows. It uses the
existing authenticated session, never the write ledger,
and rejects late results after that connection generation is disposed. Its current
local HTTP adapter uses fixed endpoints; center reads use the fixed client query
endpoint below. Remote commands require an approved plan. The renderer cannot choose a URL.

The local adapter also exposes fixed Wiki reads: `wiki.list` with optional
`cursor`/`limit`, `wiki.get` with `wiki_id` and optional fixed `revision_id`, and
`wiki.revisions` with `wiki_id` and optional bounded metadata window. List items
are summaries; page bodies and dependency manifests come only from `wiki.get`.
`wiki.create` carries `{body}`, `wiki.rebuild` carries `{wiki_id,body}`, and
`wiki.edit` carries `{wiki_id,page_key,body}`. They map to fixed POST/POST/PATCH
routes documented in `python/ddp_local/docs/local-api.md`. Builds bind selected
resource/version pairs and local-only policy; edits require `base_revision_id`
and identified human paragraphs. Every write uses the shared durable intent and
receipt ledger; a receipt without a committed revision is not a successful edit.
The renderer has no arbitrary URL, HTTP method or remote-policy override.

## Center metadata windows (`client.windows`)

A center advertises the three required client interfaces only after its corpus
projection and receipt stores are available. It additionally advertises `client.query`
and `client.windows`. Read routes accept authenticated sessions and user keys with
`read` scope. The control service binds the internal caller scope to the freshly
validated organization, principal, credential kind/ID/scopes and role. Caller-supplied
scope values cannot override it. No corpus SQL is read by control.

Center snapshot/event `state` has this bounded shape:

```json
{
  "snapshot_id": "opaque-current-snapshot",
  "resources": [{"id":"resource", "resource_id":"resource", "version_id":"version", "version_no":1, "parse_revision":"parse-job", "document_id":"document", "name":"Manual", "filename":"manual.pdf", "publication":"private", "owner_id":"user", "source_digest":"sha256", "size_bytes":123, "index_status":"ready"}],
  "tasks": [{"id":"parse-job", "kind":"doc.parse", "status":"succeeded", "engine":"borndigital", "parse_revision":"parse-job", "version_ids":["version"], "index_status":"ready", "compile_status":"none", "error_code":null}],
  "capabilities": [], "capability_status":"unknown",
  "windows": {
    "resources":{"visible_total":201,"items_loaded":100,"has_more":true,"next_cursor":"opaque-resource-page"},
    "tasks":{"visible_total":1,"items_loaded":1,"has_more":false,"next_cursor":null}
  },
  "snapshot_complete":true, "cache_complete":false
}
```

Resources are fixed **resource-version rows**, so `visible_total` counts visible
versions, not globally deduplicated documents. Queue tasks with a proven visible parse
binding have IDs `queue:<id>` and expose kind/status/parse_revision/error_code/degraded;
worker IDs, lease timestamps, payloads, source text and raw exception messages are excluded.
`tasks` counts these plus fixed parse jobs. No other task types are implied present.
`state.projection_scope` explicitly identifies `resources: authorized_fixed_versions`,
`tasks: fixed_parse_and_job_bound_queue`, and `all_task_kinds: false`; it does not claim
that historical extraction/knowledge/GC tasks without a proven fixed parse binding
have been enumerated.

`snapshot_complete` means the server enumerated the authorized metadata consistently.
`cache_complete` means both initial windows contain all of that metadata. It never
asserts full-text completeness or that later pages are already loaded. Each window
contains at most 100 items and targets 512 KiB. Responses are limited to 3 MiB, beneath
the Provider/cache 4 MiB limit. A single oversized item, more than 20,000 authorized
version rows or job-bound queue tasks, or a projection above 32 MiB returns explicit
507 `projection_too_large`/`projection_item_too_large`; it is never silently truncated.

A UI loads a selected next page without concatenating every page into its persisted
projection. `POST /api/v1/client/query` takes `{name,payload}`. The fixed names are:

- `resource.page` or `task.page`: `{snapshot_id,cursor}`. Response is
  `{snapshot_id,sequence,kind,items,next_cursor,has_more,visible_total,items_loaded,page_index}`.
  `kind` is `resources` or `tasks`; `items_loaded` is the count in this page, not a cumulative
  count or a claim that previous pages are still cached. Page 0 is the initial window.
- `corpus.search`: `{query,limit?:1..50,version_ids?:string[]|null,resource_id?,version_id?}`.
  `null`/omitted means currently authorized fixed versions, `[]` means empty scope;
  lists accept at most 1,000 IDs and authorize every selected version before retrieval.
  A non-null list cannot be combined with the single resource/version context.
  A missing or unauthorized selected version returns the same 404. It reuses the corpus
  search/evidence implementation and applies authorized document **and parse-job** scopes
  before ranking/top-k, with permission checks again after external awaits. The response
  contains `hits`, `degraded`, and `scope`; each hit has current `version_id`, fixed
  `parse_job_id`/`parse_revision`, `text`, `evidence_id` and score. No unmapped legacy
  document is silently promoted to a fixed ResourceVersion.
- `evidence.get`: `{evidence_id,resource_id?,version_id?}`. It reuses corpus evidence
  authorization and returns `{id,version_id,excerpt,evidence,crop}`. `version_id` is the
  current center's readable copy; `evidence` is the DDP-EVIDENCE envelope with original
  `source_version_id`, source digest, node authority and locator. For native center
  parsing the two version IDs coincide. Imported bundles without a fixed local parse
  binding are not advertised as indexed evidence. Missing verified source digest or
  authority fails explicitly with 409 `evidence_provenance_unavailable`. Oversized
  responses fail explicitly rather than truncating evidence.

Unknown query names/fields fail. Callers cannot supply a URL. Queries never write a
command intent, accept a task or regenerate results. Remote commands remain
`approved_plan_required`.

Page cursors are opaque and bound to snapshot, caller scope and window kind. Each read
checks every frozen resource/version/parse binding against current authorization;
withdrawal, deletion or changed binding invalidates the old page with 410
`cursor_expired`, including revocation of an item on a previously loaded page. The UI
must discard that window and obtain a new projection. New resources do not enter old
pages. Different principals/credentials/workspaces cannot distinguish another scope's
cursor from an invalid one. Event cursors and pages expire after 15 minutes; at most 16
metadata snapshots and 64 MiB of history are retained per caller scope. Eviction returns
410. The durable per-scope sequence survives projection eviction and service restart.

Events compare the complete authorized metadata fingerprint inside the serialized
view transaction, including current capability/model configuration and readiness but
excluding observation/lease heartbeat timestamps. A change creates a new persistent
sequence/cursor and a complete replacement **window manifest**, not an assertion that
all metadata has been cached. An unchanged event returns the exact saved cursor,
sequence and saved state; no newly observed state is hidden behind an unchanged ACK.
Sequences are caller-scoped observation revisions and do not expose other users' writes.
`state.model_configuration` carries only model selectors and relevant enable flags,
never endpoint URLs or credentials. These selectors remain in the state when model
health is unknown, so changing an offline model still creates a new revision.

## Center accepted-upload receipts

The existing verified `DocumentSubmitted` path records a receipt in the same corpus
transaction as its ResourceVersion and ParseJob acceptance, before upstream dispatch.
The operation key is the control upload-session ID (`payload.upload_id`); events from
older producers use their existing event ID. Retries preserve the accepted resource,
version, parse job and canonical request digest. A missing key is 404, including when
another principal owns it or the accepted resource/version is no longer authorized.
Keys are scoped by organization and original principal; that principal may reconcile
using a newly authenticated credential with read permission.

`GET /api/v1/client/receipts/{operation_key}` returns
`{operation_key,accepted:true,operation:"document.upload",resource_id,version_id,parse_revision,task_id,request_digest,accepted_at}`.
It proves the corpus accepted that upload/parse operation, not successful parsing or
verified delivery. Lookup never dispatches a task; a 404 while the upload outbox is
still in transit is uncertain and must not trigger resubmission. Other business writes
are not yet advertised as reconcilable remote commands.
