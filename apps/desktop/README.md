# DeepDocParse desktop host

The host loads the Web AppShell (`apps/web`) from `ddp://app`; there is no second
business UI. `HOST-CONTRACT.md` and `bridge.d.ts` define the preload/main boundary,
and `docs/refactor/DESKTOP-APPSHELL-PLAN.md` the product shape.

One **data source** is current at a time: an owned local workspace (the
`ddp_local` runtime, which serves the center content API subset with the same paths
and shapes — `packages/contracts/ddp/local-content-subset.md`) or a connected center.
The page talks to `ddp://app/api/**`; the host forwards it to the current source
with a host-held credential (local process token or center JWT), so the renderer
never holds a token or a presigned object URL (center object URLs are rewritten to
`ddp://app/_object/<id>`). A center is a **read-only mirror**: requests other than GET/HEAD are
refused in the host with `approved_plan_required` and no network I/O; writes go
through a local federated-task plan approved in a native dialog. Switching sources
reloads the page.

Unpackaged builds accept `http://127.0.0.1` / `http://[::1]` center endpoints for
the local stack (`loopbackCenters`); packaged builds require https. The endpoint
must be the center's public base URL — the one its node proof signs.

## Build and run

Electron is pinned to **44.3.0** with a standalone npm lock. From this directory:

```sh
npm ci --workspaces=false --registry=https://registry.npmjs.org/
# npm 12 may skip lifecycle scripts under an allow-scripts policy.
# Run only the pinned Electron installer after reviewing that package:
node node_modules/electron/install.js
npm test
npm start
```

Build the existing Vue UI at the repository root first (`npm run web:build`). The
default host reads that build from `apps/web/dist` using `ddp://app`. An unpackaged
developer may explicitly set `DDP_DESKTOP_DEV_URL=http://127.0.0.1:5173`; other
hosts, authentication, paths and query parameters are rejected. Packaged hosts
ignore this override. Development uses the repository `.venv`; directory packages
contain their own minimal Python dependency set and declare Arch's Python ABI.

For an actual desktop smoke, select the graphical session environment and run
`npm run smoke`. This opens and captures a real Electron window, checks isolation
and IPC rejection, starts the real local runtime, suspends/resumes it and quits.
There is no `--no-sandbox` fallback. No display means the test fails explicitly.
The script stores a screenshot and a non-secret report in ignored `artifacts/`;
raw Electron diagnostics remain in its private temporary directory.

## Lifecycle and secrets

Closing the window quits this first host. If owned local workspaces are running,
the native confirmation states that unfinished work stops and may need retry.
Only ChildProcess handles created by this host are signalled. Refreshing the UI
leaves the runtime running. Suspend stops owned runtimes; resume reconnects only
those previously owned, retaining the runtime's persistent workspace identity.
Linux parent-death signals stop the runtime when the host crashes; the local CPU
worker applies the same rule to its parent. There is no detached worker guarantee.

The credential broker uses actual Electron safeStorage backend detection. Linux
`basic_text`, unknown, unavailable and unvalidated platforms always use memory
only. Persistent ciphertext is bound to both environment and profile; replacing a
persistent secret with a session secret removes the old file. No secret getter,
arbitrary IPC, arbitrary file read, shell, URL fetch or PID channel is exposed.

The packaged UI CSP blocks remote fonts and all external content. The existing
Vue HTML's inline theme initializer is also blocked; Vue's own theme logic runs
from bundled JS and the system font fallback remains usable. Do not weaken script
CSP to preserve that optional first-paint optimization. PDF rendering is exercised in the actual Wayland smoke. Full multi-environment
product flows and approved remote dispatch still require independent acceptance.

Sources checked 2026-09-12: [Electron releases](https://releases.electronjs.org/release?channel=stable),
[security guidance](https://www.electronjs.org/docs/latest/tutorial/security),
[safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage).
