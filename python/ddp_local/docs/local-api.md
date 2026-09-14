# Local Runtime API v1

This private API runs on literal loopback only. All routes require a per-process random
`Authorization: Bearer <session-token>`; Host must exactly match the listener and Origin
must be absent or explicitly approved. No cookies, filesystem path parameters, implicit
remote fallback or control-database access. The token stays in Electron main / CLI and is
never handed to remote browser content. Errors are `{error:{code,message}}`.

| Method | Path | Input / result |
| --- | --- | --- |
| GET | `/api/v1/capabilities` | Workspace identity and truthful provider availability |
| GET | `/api/v1/client/handshake` | `ddp-client/1`, persistent identity, profile and supported operation IDs |
| GET | `/api/v1/client/snapshot` | `{cursor,sequence,state:{resources,tasks,capabilities}}` in one SQLite read transaction |
| GET | `/api/v1/client/events?after=CURSOR` | One complete projection batch, including `previous_sequence`; unchanged sequence is an ACK |
| GET | `/api/v1/client/receipts/{key}` | Existing task for the exact operation key; 404 does not create work |
| GET | `/api/v1/models` | Reviewed catalog, installed/partial/failed artifacts and owned runtime state |
| POST | `/api/v1/models/{id}/install` | Explicit download from catalog URL; resume and verify before atomic publication |
| POST | `/api/v1/models/{id}/verify` | Full artifact SHA-256, length and format verification |
| POST | `/api/v1/models/{id}/start` | Verify and start the compatible owned CPU runtime |
| POST | `/api/v1/models/stop` | Stop only the model process owned by this local runtime |
| GET | `/api/v1/resources` | Immutable local resource versions |
| POST | `/api/v1/resources/upload` | PDF bytes; `X-Filename`, `Idempotency-Key`; durable parse task |
| GET | `/api/v1/tasks` | Recent durable tasks |
| GET | `/api/v1/tasks/{id}` | Durable task state and explicit error |
| POST | `/api/v1/tasks/{id}/cancel` | Cancel and fence the current execution generation |
| GET | `/api/v1/events?after=N` | Persisted ordered cursor events |
| POST | `/api/v1/search` | `{query,version_ids?,limit?}`; keyword hits, bbox evidence IDs and degradation |
| GET | `/api/v1/evidence/{id}` | Original excerpt and stable DDP evidence envelope |
| GET | `/api/v1/versions/{id}/bundle` | Validated `application/zip` DDP-Bundle v1 |
| GET | `/api/v1/versions/{id}/source` | Fixed ready version's verified PDF bytes; no filesystem path input |
| POST | `/api/v1/bundles/import` | ZIP bytes; `Idempotency-Key`; validate all before READY |
| POST | `/api/v1/answer` | `{query,version_ids?,execution_policy?,allow_remote?}` |
| POST | `/api/v1/wiki` | Same request, persists a generated Wiki draft with evidence bindings |

Upload is capped at 32 MiB / 500 PDF pages; archives at 64 MiB with the shared Bundle
expanded-size, compression-ratio and path rules. Model selection is runtime configuration,
not a request-controlled URL. Default `local_only` never makes a remote request. Selecting
a remote model still requires `remote_allowed` and `allow_remote:true` for each generation;
the UI must disclose that question and selected evidence are sent before that action.
Keyword retrieval is available without a model; answer/Wiki reports `model_unavailable`.
For Unicode filenames, send `X-Filename-Encoded` containing percent-encoded UTF-8 instead
of `X-Filename`. Invalid escapes/UTF-8, duplicate filename headers, and simultaneous raw
and encoded headers are rejected. Decoded names still obey the plain-filename boundary.
Generation uses the common citation assertion projector; citations are structural bindings,
with `semantic_review:needs_review`, not a claim that the model's statements are verified.

`generation.status=configured_unverified` reports configuration only; it is not a health
probe or a successful model run. A request idempotency key records generation intent before
dispatch. Recovery does not automatically resend an uncertain completion. Real model
profile quality and the desktop Wiki editor remain separate P2/P3 acceptance;
local durable Wiki revision interfaces are specified below, and installation/ownership use the reviewed catalog endpoints above; see the package [README](../README.md) for current limits and commands.

Client cursors are opaque to consumers. This adapter uses `local.<workspace_id>.<event_seq>`;
the sequence highwater survives event retention and restart. A cursor for a different
workspace, malformed/future cursor, or unavailable retained interval returns HTTP 410
with `error.code=cursor_expired`, requiring a fresh snapshot. Event responses fold the
whole interval from `previous_sequence` to `sequence` into one complete projection;
they do not claim that skipped individual event sequence numbers were delivered.

The projection contains all resource versions and tasks. Task heartbeats do not advance
the user-visible event stream, so `lease_until` and heartbeat `updated_at` are omitted
from the client task projection; the existing task/receipt endpoints retain these fields.
Starting the listener appends `runtime.started` to the same durable event ledger, so a
restart with different provider configuration cannot acknowledge an old capability view
as current. No separate persisted projection or event numbering is introduced.

The bootstrap line printed by `serve` consists of `{url,pid,protocol_version,identity,
profile,capabilities}` plus exactly one token channel: `token_file` (default/file mode) or
`token` (`--token-file -`). `pid` is always present and identifies the listening process.
Its identity is `{environment_id,workspace_id,authority_node_id}`;
the profile is `{issuer:environment_id,subject:"workspace:"+workspace_id}`.

With the default token file or an explicit `--token-file <path>`, the line carries
`token_file` and the session token stays only in that 0600 file; no token is printed. With
`--token-file -` the line carries `token` instead and **no file is written anywhere**
(neither a literal `-` nor a `session-*.json` under the workspace); the token lives only in
the stdout pipe. File mode is otherwise unchanged: ASGI shutdown removes the file before the
HTTP server replays a termination signal; a forced kill still requires owner-side stale-file
cleanup.

The WSL2 desktop bridge consumes the stdout form. It starts

```text
python3 -S -P <root>/app/src/runtime-launcher.py --workspace <ws> serve --port 0 --token-file -
```

with `PYTHONPATH=<root>/runtime/site-packages`, `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1`,
`PYTHONDONTWRITEBYTECODE=1` and a minimal `PATH`, parses `url`/`token`/`pid` from the single
stdout line, and stops the runtime by the **inner pid**
(`wsl.exe -d <distro> -- kill -TERM <pid>`), never by terminating the distribution. Bundle
layout and pinned inputs are in `packaging/windows/README.md`; the user-facing flow is in
`docs/refactor/RELEASE-MANUAL-v3.md` §9.8.


## Durable Wiki revisions and explicit model command receipts

`POST /api/v1/wikis` requires `Idempotency-Key` and accepts:

```json
{"title":"Topic","sources":[{"resource_id":"local resource id","source_version_id":"local version id"}],
 "max_pages":4,"max_evidence":40,"max_output_tokens":4096,"max_input_chars":16000,
 "execution_policy":"local_only","allow_remote":false}
```

Limits: pages 1..12; evidence 1..200; total completion tokens 512..8192; original input
context 1000..50000 characters; 1..50 explicitly selected source versions. Unsupported,
over-budget or ungrounded generation fails visibly and does not publish a revision.

Successful writes return `{id,task_id,wiki,revision}`. `wiki` contains
`id,title,current_revision_id,published_revision_id:null`. `revision` contains
`id,wiki_id,base_revision_id,kind,title,created_by,created_at,provider,limits,pages,relations,
dependency_manifest,merge_conflicts,semantic_review,source_type,protocol,decoder_revision,
stale,stale_reasons`. Pages have stable `page_key`, `title`, `generated_sections` with
shared WikiSentence claims plus IDs, separate `human_paragraphs`, and computed `stale`.
Relations use shared Edge fields `subject_id,object_id,predicate,evidence_ids,unsupported,
review_state,provider`. Current relation profile is model-selected original statements;
direction means source mention order and is not a claim of causal inference.

- `GET /api/v1/wikis?limit=50&cursor=...`: metadata only; page/relationship counts are
  included in revision summaries, full pages/dependencies/stale checks require GET detail.
- `GET /api/v1/wikis/{id}`: current complete revision.
- `GET /api/v1/wikis/{id}/revisions?limit=50&cursor=...`: fixed revision ID summaries.
- `GET /api/v1/wikis/{id}/revisions/{revision_id}`: exact historical revision.
- `POST /api/v1/wikis/{id}/revisions`: same build body plus `base_revision_id`, and a new key.
- `PATCH /api/v1/wikis/{id}/pages/{page_key}`: `{base_revision_id,paragraphs:[{id,text}]}`
  plus key. Replaces only that page's human paragraph array in a new immutable revision.
- `GET /api/v1/tasks/{task_id}/wiki-attempts`: stage/protocol/token allowance/error/timestamps;
  raw model requests and outputs remain in the private local database.

Both lists return `{items,visible_total,has_more,next_cursor}`. Limit is 1..100. Cursors
bind the workspace and list kind, with an anchor excluding assets created after the first
page. `visible_total` counts that bounded workspace window. Current-revision summaries
may change between pages; they are not a global simultaneous snapshot. Full revisions
are capped at 2 MiB and outputs at 4 MiB, with explicit failure instead of truncation.

CAS compares the base revision again at transaction publication. A stale editor gets
409 `revision_conflict`; retrying the original successful key returns its original result.
Unknown/cross-workspace/cross-Wiki revisions return 404. Models cannot label output as
original source. Human paragraphs are `kind:human,source_type:generated,unsupported:true,
evidence_ids:[],review_state:unreviewed`. Generated text is not rewritten by manual edits.
Rebuild retains manual paragraphs or reports a merge conflict; original evidence is retained
by SQLite foreign keys. No local publication, resource deletion or Wiki deletion API exists.

The legacy `POST /api/v1/wiki` keeps its existing request and answer fields. Its successful
cited draft also creates a Wiki/Revision and adds `wiki_id`/`revision_id`; invalid citations
still fail. It does not claim the structured multipage generation protocol.

The existing model install/start/stop POST endpoints now require `Idempotency-Key`, accept
no caller-defined URL or command, and return their existing status fields plus `task_id`.
Their receipts use `/api/v1/client/receipts/{key}` and `/api/v1/tasks/{id}`. Replay never
redownloads or restarts; a receipt is historical, so read `/models` for current readiness.
Cancellation and `.part`/installed bytes remain separate truths. Expired model operations
become `execution_interrupted` and are never automatically reissued after reconnect.
