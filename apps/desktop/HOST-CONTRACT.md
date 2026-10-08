# Desktop host boundary v1

Renderer privileges are limited to the named preload methods below. No generic IPC,
shell, file-read, fetch-with-credentials, credential-get, PID, or token endpoint is exposed.
All calls require this window's current main frame and an approved packaged/dev UI URL.
Unknown fields, malformed values, remote frames, and other windows are rejected.

| Method | Input | Result |
| --- | --- | --- |
| `hostStatus` | none | Host/build/platform, runtime backend and isolation, credential storage and lifecycle policy |
| `selectWorkspace` | none | Native directory dialog; opaque workspace handle and display name |
| `startLocal` | `{workspaceId}` | Public runtime identity and status; never a URL/token capability |
| `stopLocal` | `{workspaceId}` | Stops only the child this host launched for that handle |
| `runtimeStatus` | `{workspaceId}` | Current public owned-process status |
| `setCredential` | `{environmentId, profileId, secret, persist}` | Actual persistence mode and reason |
| `credentialStatus` | `{environmentId, profileId}` | Presence, persistence mode and backend |
| `clearCredential` | `{environmentId, profileId}` | Deletes that identity pair's stored credential |

The main-process `OwnedRuntimeManager.connection(workspaceId)` and
`CredentialBroker.withCredential(identity, callback)` are internal integration ports.
The shared client-runtime/provider connects through the typed domain operations in
`bridge.d.ts`; the renderer does not receive these privileged ports. Local business
queries, projections, drafts, receipts and native files are integrated below.

Renderer draft saves use `saveDesktopDraft` in `apps/web/src/platform/desktop.ts`:
the JSON content is copied to a plain snapshot before `clientSaveDraft` crosses IPC.
This includes Vue-reactive selection arrays; edits must not send their proxies.
Non-JSON values are rejected before IPC as `invalid_arguments`, not a disk/cache error.
Host-side, `clientSaveDraft` additionally requires a non-negative safe-integer
`expectedRevision` and caps the value at the 1 MiB store limit before touching the
store — malformed saves fail as `invalid_arguments`, never as `cache_failure`.

A single-instance lock keeps one host per application data directory.
Closing the last window exits this first host and interrupts its owned local runtimes.
There is no detached background mode. Before close/quit, a native dialog explains the
interruption and restart reconciliation; it does not promise checkpoints or success.
Shutdown waits are bounded: SIGTERM, then SIGKILL, then the host continues with or
without the child's `close` event — a wedged runtime never blocks quit.
Quitting (or stopping) during WSL startup, before the first bootstrap line, kills the
relay AND reaps the inner Linux runtime by workspace argv, and a bootstrap line that
arrives late is still recorded (pid file) and killed at once — no session is ever left
without a reaping record for `cleanupOrphans`.
Renderer refresh and a remote disconnection do not stop local runtimes or remote tasks.
Suspend stops owned runtimes; resume may restart only those previously owned instances.

Linux credentials persist only when safeStorage reports an approved secret-service
backend and encryption is available. `basic_text`, `unknown`, and encryption failures
produce explicit session-only status. Windows credentials persist only through DPAPI
when safeStorage reports encryption available; otherwise they are session-only with
reason `session_only_dpapi_unavailable`. Plain secrets never enter configuration or logs.

On Windows, host-private directories have no mode to enforce (NTFS ignores mkdir
modes), so their privacy derives from the user-profile NTFS ACL; the platform layer
rejects symlink/reparse targets and reports backend `ntfs_acl` rather than claiming a
mode. `hostStatus.isolation` is `wsl_vm` only when the WSL local runtime was actually
constructed; `ntfs_acl` when it is unavailable, and `posix_mode` on POSIX hosts.
Starting local mode without a WSL backend fails with `wsl_backend_unavailable` through
the normal error channel while remote connections keep working.
WSL workspace handles are virtual and distro-bound: the path is lexically
canonicalized (duplicate slashes collapsed, `.`/`..` segments rejected so
`~/a/../b` can never alias `~/b` into a second handle) and the handle identity is
`wsl:<distro>:<canonical>` using the backend's resolved distribution. The same
virtual path on another distro is a different workspace — connecting it against a
registration bound to another distro fails closed (`identity_mismatch`), and the
distro rides in `connections.json` (`workspaceDistro`; pre-binding entries adopt the
current distribution once on load).

## Shared connection implementation (2026-09-13)

`bridge.d.ts` is the renderer-facing contract. `ClientHost` owns one
`ConnectionRegistry` reference per environment/profile, a private
`SqliteProjectionStore`, and `HttpProvider` transports. `clientList` includes no
endpoint or credential. The removed view-subscription and file-transfer IPC
(`clientSubscribe`, `clientUnsubscribe`, `onClientView`, `clientImportFile`,
`clientExportBundle`, `clientReadOriginal`, `clientConnectLocal`,
`clientPairRemote`, `clientWake`, `clientDisconnect`) have no renderer callers
left (verified by grep over `apps/web/src`); their host-internal counterparts
(`connectLocal`, the private center registration, `wake`/`disconnect`,
`importFile`/`exportBundle`/`readOriginal`) stay for the source registry, the
plan ledger and suspend/resume. Restart restores cached projections as stale;
only a freshly verified handshake and event acknowledgement make them current.

The renderer command allowlist is models.install/models.start/models.stop, with
local-only execution. Model install and start accept only a registry model_id,
stop accepts an empty object; no endpoint, engine arguments or implicit download
operation is exposed. Every content write (uploads, Wiki create/rebuild/edit,
answers, withdraw/delete, task cancel) goes through the same-origin `/api` proxy
instead, so any other command name fails with `unsupported_operation`. The only
named `clientQuery` read is `models.list` (LocalModelsView), LOCAL connections
only; any other query name — and any query against a center connection — fails
with `unsupported_operation` / `approved_plan_required`. Centers are read
through the GET-only `/api` proxy (see below), never through the connection
query path. Receipts are explicit reconciliation. No renderer file path or
endpoint is accepted.

Remote HTTP authentication uses the shared Ed25519 challenge inspector before
accessing CredentialBroker. Pairing keeps authority/workspace/actor bindings;
changing an existing endpoint currently requires a separate explicit relocation
flow, which is not implemented by this bridge. Saved local directory reuse cannot
silently substitute a different runtime identity for the cached connection.
## Source registry and Host /api proxy

The renderer calls same-origin `ddp://app/api/**` (GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS) and
`ddp://app/_object/<opaque>` (GET). Web code builds URLs with `apiUrl(path)`;
CSP `connect-src` allows `ddp://app` (packaged and dev). `bridge.d.ts`
`DesktopSourceBridge` is the renderer-facing contract; `sourceId` IS the
`ConnectionSummary.connectionId` verbatim.

| Method | Input | Result |
| --- | --- | --- |
| `sourceList` | none | All registered sources (local + centers) with state/features/active |
| `sourceActivate` | `{sourceId}` | Reconnects/wakes that source and stays `connecting` until its snapshot is current or the connection gives up (`blocked`, 20 s cap; `backoff` is still inside the retry budget and keeps waiting), then records it active (persisted) |
| `sourceReconnect` | `{sourceId}` | Explicitly wakes a registered center with the same bounded readiness wait as activation (20 s cap, `backoff` keeps waiting), emits source updates and returns its summary without changing/persisting the active source; local sources reject with `invalid_arguments` |
| `sourceRemove` | `{sourceId}` | Disconnects, deletes the registration, clears a stored center JWT; never deletes local workspace data (T65) |
| `workspaceOpen` | none | Native directory dialog → start runtime → connect → activate; `null` = cancelled |
| `centerConnect` | `{endpoint, username, password, persist, storageOrigin?}` | Node challenge proof → login → handshake → register → store JWT → activate; returns the summary plus `credential: {mode, reason}` reporting the ACTUAL persistence (`persistent` vs `session`) so the UI never believes a session-only JWT was persisted |
| `onSourceChange` | listener | Pushes the source list on activate/remove/`signed_out` transitions |

After a successful `sourceActivate`/`workspaceOpen`/`centerConnect` the
RENDERER calls `location.reload()`; the host only records the active source
(persisted in userData as `active-source.json`, restored on restart).
`sourceReconnect` never reloads the renderer or switches its active source. The
数据源 page offers “重新连接” for both active and non-active centers that are not
ready; unsuccessful readiness is shown without extending the bounded retry budget.
When reconnecting the active center succeeds, the renderer refreshes its
boot-source state and clears the stale source-page reason so navigation and
authentication become ready again without switching sources or reloading.
`hostStatus()` additionally returns `version: app.getVersion()`.

Proxy rules: no active source → `503 no_active_source`. Local source: those
methods, path (capped 4096, `..`/decoded-escape rejected), query (capped 4096,
control characters rejected as `invalid_arguments`) and body (streamed with an
incremental 64 MiB cap — `Content-Length` pre-checked, then byte-counted while
reading so a lying length cannot OOM the host; SSE flows incrementally)
forwarded to the owned loopback runtime with its process Bearer token
(renderer `Authorization`/`Cookie`/`Origin` stripped, Host set by the runtime).
Center source: only GET/HEAD, else `403 approved_plan_required` with ZERO
network I/O; `Authorization: Bearer <JWT from CredentialBroker>`,
`redirect: 'error'`. Upstream 401 → marks the source `signed_out`, returns
`401 source_signed_out`. Every proxied response carries
`X-DDP-Source: <sourceId>` — including host error responses while a source is
active, so the renderer's fail-closed fence (missing/mismatched header →
`source_changed`, never rendered under the wrong egress label) still surfaces
the real error for the current source; `Set-Cookie` stripped; only safe headers
forwarded (`content-type/-length/-disposition`, `cache-control`, `etag`,
`last-modified`, `x-ddp-*`). Success responses additionally carry the static
`Content-Security-Policy` + `nosniff` + `no-store` posture. Center JSON
responses are parsed and every string value's absolute URL whose origin is the
center endpoint origin or the registered storage origin (e.g.
`GET /api/documents/{id}/download-url`) is rewritten to
`ddp://app/_object/<opaque>`; the rewrite works on decoded strings, never raw
text (Go writes `&` as `\u0026`, which would split a presigned URL),
unparsable JSON fails closed, rewrite input is capped at 16 MiB
(`protocol_incompatible` above it — an unbounded center body never enters host
memory), and the upstream `Content-Length` is dropped.
`_object` streams the object from the allowed origin with no `Authorization`
(presigned URLs are self-authenticating).
Opaque ids are 128-bit crypto-random (`crypto.randomBytes`), short-lived
(10 min), per source, capped at 256.
The renderer never receives any token (local process token, center JWT,
presigned URLs).

Center connect: endpoint must be `https:` (`http://127.0.0.1|[::1]` only
when the build is unpackaged, for testing against the local stack; main passes
`loopbackCenters: !app.isPackaged` so the shared provider accepts the same
endpoint). The endpoint must equal the center's public base URL signed in its
node proof →
`GET /api/v1/federation/node?challenge=` Ed25519 proof check (same rules as
the shared provider) → `POST /api/auth/login {username, password}` → JWT
(the password is used once, never stored/logged/returned) →
`GET /api/v1/client/handshake` (Bearer JWT) → identity
`{environment_id, authority_node_id, workspace_id}` + profile
`{issuer, subject}` → register via the existing pairRemote internals
(`uploadOrigin` = storageOrigin ?? endpoint origin) → store JWT via
CredentialBroker honoring `persist` and backend policy → activate. Center
`SourceSummary.features` is fixed
`['resources','documents','search','wiki','federation_tasks']` with
`readOnly: true`; local features come from the runtime
