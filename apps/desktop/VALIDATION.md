# Desktop host + shared client validation — 2026-09-13

Author self-validation only. Independent acceptance of the final diff remains
required. This is a bounded P3 implementation, not a claim that all P3/P7 tests pass.

| Check | Actual result |
| --- | --- |
| Node host suite | **18 passed**, no skip; actual Python runtime and SQLite client cache |
| Security mutation | Temporarily allowing `basic_text` made its regression test fail; restored code passes |
| Actual GUI | **PASS** Electron 44.3.0, niri/Wayland (`wayland-1`), Linux x86_64/Radeon 780M host |
| Workbench | Existing shared Vue `/workspaces` rendered a real imported resource; selecting it rendered the actual PDF.js canvas and page text |
| Renderer | `require`/`process` undefined; sandbox/contextIsolation true; Node integration false |
| IPC | Forged frame/window, wrong origin, extra path/shell/scope fields, generic HTTP and implicit remote generation rejected |
| Shared client | Real OwnedRuntimeManager → HttpProvider → ConnectionRegistry → SqliteProjectionStore; reload/unsubscribe/disconnect do not stop the owned runtime |
| Reconnection | Controlled suspend/resume advances process generation, retains identity, reconnects the changed random port; actual window close stops its runtime |
| Projection/draft | Restart restores identity-bound projection as stale; draft survives with CAS conflict rejection |
| Native files | Chinese PDF filename preserved; one bounded FD snapshot is hashed and uploaded; fixed-version source bytes match original; complete bundle verified before atomic export |
| Receipt safety | Lost upload response was simulated after real server acceptance; a second import made no HTTP write, receipt lookup reconciled it, repeated key remained one dispatch |
| Remote proof | Unproven node pairing blocks before credential access and applies no new projection |
| Credentials | Actual Linux backend `basic_text`; persistentAvailable false. Session-only mode, ciphertext identity binding and stale persistent-copy deletion tested |
| External process | Independently spawned Python remained alive through owned runtime shutdown |
| Directory runtime | From `/tmp`, system Python with only packaged dependencies: shared client/SQLite, parse, search, Evidence bbox, source bytes, receipt, draft and validated bundle all passed |
| Python/style | Ruff check + format check passed for launcher, fixed bundle validator and build helper |

The actual screenshot and nonsecret report are in ignored
`apps/desktop/artifacts/{desktop.png,smoke-report.json}`. The PDF test is a 439-byte
repository fixture; the exported bundle is 3896 bytes. Test workspace UUIDs contain
no credentials. Raw Electron diagnostics remain in private temporary directories.

The PDF canvas test initially failed because `connect-src 'self'` did not permit
its `blob:ddp://app/...` request. Adding only `blob:` to connect-src fixed the real
render. External fonts/images/scripts/HTTP remain blocked by both CSP and the host
request filter. The existing HTML inline theme initializer stays blocked.

Reproduction from the repository root:

```sh
node --test --test-timeout=30000 apps/desktop/test/*.test.mjs
env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
  XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=niri \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  npm run smoke --workspace @ddp/desktop
.venv/bin/python scripts/build_desktop.py --output dist/desktop-client-final
node apps/desktop/scripts/package-smoke.mjs \
  dist/desktop-client-final/deepdocparse-0.1.0-linux-x64 tests/fixtures/sample.pdf
```

Use the actual graphical session values on another machine. The GUI smoke uses
trusted development-only dialog fixtures; real OS dialogs are wired, while no
claim is made that a fresh user manually completed an installer wizard.

Packaging history: the earlier host-only spike was built twice to identical
SHA256 `0d431130fa5f110e062ab0c88e709450f791f320ee300c68bb4f43dfdaea42f9`.
`makepkg --verifysource` and `makepkg --noconfirm` then produced a 115 MiB package,
without installation. The shared-client upgrade copies its TypeScript modules and
explicit module metadata inside resources; Electron's embedded Node 24 loads them
without a checkout or separate Node installation. Current archive hashes are in
the build's `.sha256` and `BUILD-MANIFEST.json`; rebuilding after another agent's
Vue/runtime edits produces a new expected input hash.

Electron download: 122830582 bytes, SHA256
`8b49b9efdd73c0f467edc3c1cd5678392c384ccf224f34ff54179f736e2f384b` matched the exact
official npm package's checksum. GitHub transfers repeatedly ended in TLS EOF;
the successful transfer used npmmirror, followed by full verification. This is a
checksum verification, not a detached-signature claim.

Remaining: independent cross-environment/security review, remote approved-plan
business routing and relocation UX, actual OS sleep during active parsing (the
smoke invokes the same lifecycle path without sleeping the user's computer),
GNOME/KWallet secure persistence, X11/other GPUs, fresh-machine installation and
upgrade/rollback/workspace-backup tests. No AUR publication or complete T20/T65/T88
acceptance is claimed. Imported-bundle evidence navigation was reported to root:
its local `version_id` must locate the local copy while the immutable envelope
keeps its original `source_version_id`; root owns that UI correction.
