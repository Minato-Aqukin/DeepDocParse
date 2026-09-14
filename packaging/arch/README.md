# Local Arch directory package

This is a reproducible **directory artifact and local makepkg recipe**, not a
published AUR package, supported release channel or completed T88 acceptance.
There is no Electron autoUpdater integration on Linux. Updates are owned by
`scripts/update_check.py` (versioned manifest + checksum/signature + pre-upgrade
backup + explicit rollback); a future signed repository or reviewed AUR recipe
will own package updates. See `docs/refactor/RELEASE-MANUAL-v3.md`.

## Verified build (2026-09-13, Arch x86_64, Python 3.14)

```sh
npm run web:build                       # shared Vue UI
.venv/bin/python scripts/build_desktop.py
.venv/bin/python scripts/build_desktop.py --verify dist/desktop/deepdocparse-0.1.0-linux-x64
node apps/desktop/scripts/package-smoke.mjs \
  dist/desktop/deepdocparse-0.1.0-linux-x64 tests/fixtures/sample.pdf
scripts/build_desktop_arch.sh --reproduce
```

- `--verify` re-hashes every file against `BUILD-MANIFEST.json`, compares the
  embedded `runtime-lock.json`, checks the interpreter ABI and re-derives the
  license manifest.
- `--reproduce` runs makepkg twice and fails unless both `.pkg.tar.zst` hashes
  match. The first run without `SOURCE_DATE_EPOCH` produced different packages
  (`builddate` was wall-clock); the script now exports the artifact epoch, and
  two runs produced the same `c9353b42…`.
- `makepkg --verifysource` verifies all three sources; nothing is installed.

## Inputs and integrity

- Electron is pinned by `apps/desktop/package-lock.json` and independently by
  `electron-lock.json` (122 830 582 bytes, SHA256 `8b49b9ef…`). When the
  downloaded `electron-v44.3.0-linux-x64.zip` is present,
  `build_desktop.py --verify-electron` checks size and hash; the normal build
  also checks it when the archive is found. This is a checksum pin, not a
  detached signature.
- `runtime-lock.json` pins every Python distribution by version and payload
  digest; the build refuses to run when installed inputs drift from the
  reviewed lock. `runtime-info.json` pins the interpreter ABI the launcher
  reads.
- `BUILD-MANIFEST.json` records SHA256 for every copied file plus a
  `license_manifest` (Electron `LICENSE` + `LICENSES.chromium.html`, and each
  locked distribution's `dist-info/licenses` files). A missing notice fails the
  build.
- `deepdocparse-0.1.0-linux-x64.release.json` is the update-channel manifest:
  version, platform, CPU ABI, archive size/SHA256 and the
  `BUILD-MANIFEST.json` digest. `update_check.py sign` adds an optional
  detached `ssh-ed25519` signature.

## Package contents / limits

The artifact contains Electron, the shared Vue build, three DDP Python
packages and the minimal fixed HTTP/CPU dependency closure. No PostgreSQL,
Redis, MinIO, container, remote model, credentials or prestarted service is
required. Local CPU PDF parsing uses bundled PDFium; generation still reports
provider-unavailable unless a provider is deliberately selected.

All archive entries are sorted, uid/gid are zero, timestamps and gzip metadata
are fixed by `SOURCE_DATE_EPOCH` (default 1789257600). No broad checkout copy
is used: environment files, node_modules, caches, workspaces, raw traces and
model weights are outside the copy allowlist. Chromium user namespaces must be
available; this recipe never disables sandboxing or installs a setuid helper.

Validated platform claims belong in `apps/desktop/VALIDATION.md`; X11, other
desktop sessions, GPU families, secret-service backends, package upgrades and
fresh-machine installation must not be claimed without an actual test.
