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

A single-instance lock keeps one host per application data directory.
Closing the last window exits this first host and interrupts its owned local runtimes.
There is no detached background mode. Before close/quit, a native dialog explains the
interruption and restart reconciliation; it does not promise checkpoints or success.
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

## Shared connection implementation (2026-09-13)

`bridge.d.ts` is the renderer-facing contract. `ClientHost` now owns one
`ConnectionRegistry` reference per environment/profile, a private
`SqliteProjectionStore`, and `HttpProvider` transports. `clientList` includes no
endpoint or credential. Listener events carry subscriptionId, connectionId and a
monotonic revision; the synchronous subscription response repairs subscribe races.
Reload removes view listeners only. Explicit disconnect retains runtime assets,
drafts and the reconciliation ledger. Restart restores cached projections as stale;
only a freshly verified handshake and event acknowledgement make them current.

The command allowlist is task.cancel/answer.generate/wiki.build plus
models.install/models.start/models.stop, with local-only execution. Model install
and start accept only a registry model_id, stop accepts an empty object; no
endpoint, engine arguments or implicit download operation is exposed. Queries are corpus.search/evidence.get/models.list and the capability-gated
resource.page/task.page windows. Window requests accept only a bounded
snapshot_id/cursor pair within the selected connection. Native file import
snapshots one bounded FD, verifies its before/after metadata, hashes those exact
bytes and stores an intent before posting those same bytes to a fixed endpoint.
A previous unknown intent cannot be replayed by selecting the file again. Receipts
are explicit reconciliation. No renderer file path or endpoint is accepted.

Source PDFs and bundles are read only from fixed local version endpoints after
current-connection verification. Replies are bounded to 32 MiB and fenced against
connection replacement. Export validates the complete archive through the shared
DDP bundle verifier before writing an atomic file in the user-selected directory.
Remote file/command dispatch awaits approved-plan support and returns an explicit
failure instead of using an unchecked HTTP route.

Remote HTTP authentication uses the shared Ed25519 challenge inspector before
accessing CredentialBroker. Pairing keeps authority/workspace/actor bindings;
changing an existing endpoint currently requires a separate explicit relocation
flow, which is not implemented by this bridge. Saved local directory reuse cannot
silently substitute a different runtime identity for the cached connection.
