# Windows WSL runtime tarball (W3)

`deepdocparse-wsl-runtime-<version>-linux-x64.tar.gz` is the self-contained
Linux x86_64 runtime the Windows installer bundles for WSL2 local mode. It
needs no system Python and installs nothing at runtime: a pinned
python-build-standalone interpreter plus the unpacked pinned wheels, run with
`-S -P` and `PYTHONPATH=<root>/runtime/site-packages`. `pip` (and ensurepip's
bundled wheel) is stripped from the interpreter tree.

## Build and verify

```sh
# real build; downloads the pinned interpreter and wheels into dist/wsl-cache/
.venv/bin/python scripts/build_wsl_runtime.py

# offline rebuild from an already-populated, hash-verified cache
.venv/bin/python scripts/build_wsl_runtime.py --skip-download

# re-hash a directory or tarball, re-run the ABI probe, re-derive licenses
.venv/bin/python scripts/build_wsl_runtime.py --verify dist/wsl/deepdocparse-wsl-runtime-0.1.0-linux-x64
.venv/bin/python scripts/build_wsl_runtime.py --verify dist/wsl/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz

# offline guards (no network/docker); the real build is opt-in
.venv/bin/python -m pytest -q tests/test_wsl_runtime_build.py
env WSL_RUNTIME_BUILD=1 .venv/bin/python -m pytest -q tests/test_wsl_runtime_build.py
```

Outputs in `dist/wsl/` (gitignored): the stage directory, the tarball,
`<tarball>.sha256`, and the archive-level `wsl-runtime.json` (the same file
inside the bundle carries the per-file digests; the outer copy adds
`archive`/`size`/`sha256`).

## Packaging and package verification (W4)

`scripts/build_desktop.py --platform win32-x64` refuses to assemble unless all
of the following hold, and there is no flag to skip any of it:

- the outer `dist/wsl/wsl-runtime.json` has release identity
  (`format`/`name`/`version`/`platform`), archive name, byte size and SHA256
  matching the tarball, and the `.sha256` sidecar agrees;
- the tarball really is a W3 bundle:
  `<top>/runtime/python/bin/python3`, `<top>/wsl-runtime.json` and
  `<top>/runtime/wsl-runtime-lock.json` are present;
- the **inner** `<top>/wsl-runtime.json` is read back out of the archive and
  cross-checked: `format`/`name`/`version`/`platform`, `epoch`, the python ABI
  (identical to the outer manifest), a non-empty `files` map, and its
  `lock_sha256` equal to both the embedded
  `<top>/runtime/wsl-runtime-lock.json` and — whenever the reviewed lock is
  present, which it is for every in-repo build/verify —
  `packaging/windows/wsl-runtime-lock.json`;
- the full `files` digest map is recomputed against the archive: every listed
  member must exist and hash to the recorded digest (symlinks hash their
  target), and every packaged file must be listed;
- the archive clears the **minimum release shape** (floors owned by
  `build_wsl_runtime.py`): ≥ 100 000 000 unpacked bytes over ≥ 5 000 members,
  a resolved `runtime/python/bin/python3` of ≥ 5 MiB, and every required member
  derived from the pinned lock — stdlib (`encodings`/`socket`/`sqlite3` under
  `python<major>.<minor>`), the wheel roots (e.g. `fastapi`, `httpx`,
  `pypdfium2`, `PIL/Image.py`), the local packages plus `ddp_local/cli.py`,
  `runtime-info.json`, both `app/src/runtime-*.py` files and
  `wsl-runtime.json`. `build_wsl_runtime.py --verify` runs the same shape gate.

What this does and does not prove: a synthetic stand-in, a placeholder, or a
small **self-consistent** forgery (a shell-stub interpreter, padding, the real
in-repo lock copied in, and an inner manifest listing every member with the
digest the archive actually contains) fails closed. These are
**self-consistency checks against pinned public inputs, not cryptographic
provenance**: v1 ships unsigned (plan decision 6), so a determined forger with
build access can still fabricate a full-sized bundle whose bytes match its own
manifest. The shape floors raise the cost; they are not a signature and must
not be described as one. The same inner + shape checks run when a packaged
tree's embedded runtime is verified.

After electron-builder runs, re-hash the packaged output before shipping:

```sh
.venv/bin/python scripts/build_desktop.py --verify dist/desktop/windows/win-unpacked
# the same checks, standalone, plus installer payload floors:
.venv/bin/python scripts/verify_windows_package.py dist/desktop/windows/win-unpacked \
  --installer dist/desktop/windows/DeepDocParse-0.1.0-win-x64-setup.exe \
  --installer dist/desktop/windows/DeepDocParse-0.1.0-win-x64-portable.exe
```

This fails closed when `deepdocparse.exe`, the app sources (`resources/app/`
`package.json` · `src/main.mjs` · `ui/index.html`, plus
`resources/client-runtime/{index,sqlite-store,http-provider}.ts` and
`resources/runtime/runtime-info.json`) are missing, and when the bundled
`resources/deepdocparse-wsl-runtime-<version>-linux-x64.tar.gz` is not
byte-identical (exact size + SHA256) to the W3 release manifest
(`dist/wsl/wsl-runtime.json`/`resources/runtime/wsl-runtime.json`) — including
the full inner file map and the minimum release shape above. It also refuses
when the staged archive name or app version disagrees with the lock.

The payload diff anchors on an **external** source, in order: an explicitly
passed `--stage`, the assembled stage next to the output, or the repository
sources. A `BUILD-MANIFEST.json` found *inside* the directory being verified is
deliberately ignored — the packaged output is the artifact under suspicion, so
a manifest travelling with it proves nothing. Every `resources/app/**` and
`resources/client-runtime/**` entry the anchor records is compared by digest
(`preload.cjs`, `runtime-launcher.py`, `runtime-files.py` and `ui/**` included),
and `package.json` by its canonical content, since electron-builder rewrites
`scripts`/`devDependencies`/`author`. The first real Windows packaging run
shipped a 205-byte placeholder here because no such check existed.

### Build hosts

- `portable` builds on Linux without wine and embeds the full payload
  (`$PLUGINSDIR/app-64.7z` contains the real runtime tarball).
- `nsis` additionally needs `wine` on Linux: electron-builder runs the
  generated installer once to make it write its uninstaller
  (`NsisTarget.computeScriptAndSignUninstaller`). Without wine the run dies
  with `wine process failed ENOENT` after producing only the ~190 KB
  uninstaller stub plus the complete `@ddpdesktop-<version>-x64.nsis.7z` — the
  stub is not a shippable installer and must never be uploaded. The setup exe
  is therefore produced by the `windows-latest` CI job (W5), which is the
  primary path; Linux has no flag to skip this step, only a full custom NSIS
  script (not used here).

## Verified build (2026-09-14, this machine)

The 11:55 build was `ac418ea0…` (128 592 559 bytes). The cli.py increment that
added the stdout `pid` and `--token-file -` (decision 3 in
`WINDOWS-AC-PLAN-v1.md`) changed the packaged `ddp_local` sources, so the
runtime bundle was rebuilt at 13:22 and its pins changed — `dist/wsl` pins
change with the source tree. The table below is that rebuild:

| | |
|---|---|
| Archive | `dist/wsl/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz` |
| Size | 128 592 543 bytes |
| SHA256 | `12cfa2f3f9111d3a699cd96978ef77670ff80300fb920feffbb152f4a6c44252` |
| Files in manifest | 6033 (stdlib + third-party bytecode precompiled) |
| Interpreter | CPython 3.12.14, `cpython-312`, SOABI `cpython-312-x86_64-linux-gnu`, `x86_64` |
| Standalone input | 111 368 545 bytes / `936c246d…ffa22` |
| Wheels | 18, 15 383 217 bytes total (table below) |
| Tests | offline `17 passed, 1 skipped` (skip names `WSL_RUNTIME_BUILD`) |

Four guard mutations were confirmed to go red and were reverted: skip-download
refusal, modified-file detection, gzip header determinism, `compileall -d`
path independence.

## Determinism

Three builds with different `--output` directories produce the same digest
(the stage path is not embedded: `compileall -d` rewrites `co_filename`,
`PYTHONHASHSEED=0` stabilises frozenset marshalling, all mtimes are pinned to
`SOURCE_DATE_EPOCH` / 1789257600, tar entries are sorted, gzip is written with
`-n`):

```console
$ .venv/bin/python scripts/build_wsl_runtime.py --skip-download
$ .venv/bin/python scripts/build_wsl_runtime.py --skip-download --output dist/wsl-repro-a
$ .venv/bin/python scripts/build_wsl_runtime.py --skip-download --output dist/wsl-repro-b
$ sha256sum dist/wsl/*.tar.gz dist/wsl-repro-a/*.tar.gz dist/wsl-repro-b/*.tar.gz
ac418ea0d2a3404d38bf948a82de859f752c02614927afc8033e15e2ccd4b44e  dist/wsl/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz
ac418ea0d2a3404d38bf948a82de859f752c02614927afc8033e15e2ccd4b44e  dist/wsl-repro-a/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz
ac418ea0d2a3404d38bf948a82de859f752c02614927afc8033e15e2ccd4b44e  dist/wsl-repro-b/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz
```

That console block is the 11:55 pre-increment run (the staged `dist/wsl` copy
was still the same digest then). The cli.py increment happened afterwards, so
those two repro directories no longer describe the current tree; the
post-increment pins are the table above, and the three-build repro was not
re-run against it in this round.

## Container smoke (`ubuntu:24.04`, no system Python, `/dist` read-only)

```sh
docker run --rm -v "$PWD/dist/wsl:/dist:ro" ubuntu:24.04 bash -lc '
set -eu
command -v python3 || echo "no python3 in PATH"
mkdir -p /work && tar -xzf /dist/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz -C /work
cd /work/deepdocparse-wsl-runtime-0.1.0-linux-x64
! test -e runtime/python/bin/pip
! test -e runtime/python/lib/python3.12/site-packages/pip
! test -e runtime/python/lib/python3.12/ensurepip
export PYTHONPATH=$PWD/runtime/site-packages PYTHONDONTWRITEBYTECODE=1
time runtime/python/bin/python3 -S -P -c "import ddp_contracts, ddp_core, ddp_local, fastapi, httpx, pypdfium2, PIL; print(\"imports-ok\")"
mkdir -p /tmp/ws
runtime/python/bin/python3 -S -P -m ddp_local --workspace /tmp/ws init
runtime/python/bin/python3 -S -P -m ddp_local --workspace /tmp/ws capabilities
sh -c "runtime/python/bin/python3 -S -P app/src/runtime-launcher.py --workspace /tmp/ws2 capabilities; echo launcher-parent-ok"
set +e
printf "not-a-bundle" | runtime/python/bin/python3 -S -P app/src/runtime-files.py
test $? = 1
set -e
echo SMOKE-OK'
```

Real output (trimmed to the assertions):

```console
no python3 in PATH
imports-ok
real	0m0.249s
{"workspace_id": "3efe7baa716641d28ba4de724f7aab57", ..., "parse": {"available": true, "provider": "borndigital", ...}, "degraded": ["embedding_unavailable", "vision_unavailable"]}
---
launcher-parent-ok
runtime-files exit=1
SMOKE-OK
```

The launcher refuses a parent PID of 1 (`desktop runtime parent unavailable`),
which is exactly what docker/`bash -lc` gives it; the `sh -c '…; echo'` wrapper
keeps a live non-PID-1 parent, mirroring the real bridge. On WSL the bridge
process supplies that parent.

## Bundle layout and the W2W bridge contract

The tarball has one top-level directory
(`deepdocparse-wsl-runtime-<version>-linux-x64/`). Extract its **contents**
into `~/.deepdocparse/runtime` so the final tree is:

```text
~/.deepdocparse/runtime/            <- <root>, the launcher's parents[2]
├── app/src/runtime-launcher.py     copied verbatim from apps/desktop/src
├── app/src/runtime-files.py
├── runtime/python/bin/python3      standalone interpreter (no pip)
├── runtime/site-packages/          unpacked wheels + ddp_contracts/ddp_core/ddp_local
├── runtime/runtime-info.json       ABI anchor; launcher reads <root>/runtime/runtime-info.json
├── runtime/wsl-runtime-lock.json   pinned inputs, for provenance
├── LICENSE
└── wsl-runtime.json                per-file digests, python ABI, signature: null
```

- **Installer**: verify the tarball against the outer `wsl-runtime.json`
  (`sha256`/`size`) and the bundled manifest's `python` ABI before shipping.
- **W2W bridge**: extract (strip the top-level directory) to
  `~/.deepdocparse/runtime`. Start exactly as `OwnedRuntimeManager` does:
  `<root>/runtime/python/bin/python3 -S -P <root>/app/src/runtime-launcher.py
  --workspace <ws> serve --port 0 --token-file -` with
  `PYTHONPATH=<root>/runtime/site-packages`, `PYTHONDONTWRITEBYTECODE=1`,
  `PYTHONSAFEPATH=1`, `PYTHONNOUSERSITE=1` and a minimal `PATH`. `-` means
  "print the token on the stdout bootstrap line"; the `<file>` form is for the
  native Linux backend, which reads it from disk.
- **Bundle validation**: `runtime-files.py` (same env, bundle bytes on stdin)
  must print `validated`; it returns 1 on anything malformed.
- Do not create virtualenvs, run pip, or write inside `<root>` at runtime
  (workspace/SQLite/blob data belongs under `~/.deepdocparse/`).
- Upgrades replace `<root>` atomically from a newer tarball; `wsl-runtime.json`
  names the version and ABI for the mismatch check.

## Pinned inputs (`wsl-runtime-lock.json`)

Standalone:
`https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz`
— 111 368 545 bytes, `936c246dfdbbfa7cb22dd01814a21f582a892689fae96b06071a5e433baffa22`
(from the release's `SHA256SUMS`).

Wheels resolved with
`pip download --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --abi cp312`:

| name | version | bytes | sha256 |
|---|---|---|---|
| annotated-doc | 0.0.5 | 5302 | `117bac03a25ede5df5440e855b32d556049ca169ead221505badf432fed4b101` |
| annotated-types | 0.8.0 | 13427 | `f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0` |
| anyio | 4.15.1 | 132079 | `6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101` |
| certifi | 2026.7.22 | 136983 | `62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775` |
| click | 8.5.0 | 125251 | `255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360` |
| fastapi | 0.141.1 | 131954 | `bfb91aa2d334c61cb35ba9a116fc123b3d3df31640b801cf57a7a78ec3f603b3` |
| h11 | 0.16.0 | 37515 | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |
| httpcore | 1.0.9 | 78784 | `2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55` |
| httpx | 0.28.1 | 73517 | `d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad` |
| idna | 3.19 | 68550 | `815e7be7a7806d54abb586dc943addc79e8b2ee16915059658cbeff4b1b43bf4` |
| pillow | 12.2.0 | 8094744 | `b86024e52a1b269467a802258c25521e6d742349d760728092e1bc2d135b4d76` |
| pydantic | 2.13.5 | 472589 | `346a034f080da3755d8e9cb5e00e8b07de1d39e4f6e2c87d8ab7cafa0b269a73` |
| pydantic-core | 2.46.5 | 2066284 | `0fc5be0abd4a407e200d844b404e33639a554e7bd0d448e7b9ae181be4789ac2` |
| pypdfium2 | 5.13.0 | 3730077 | `81df25c1ab4c13ff773102d3cbea1967511d079123b067fc077bd0c4d57d91d8` |
| starlette | 1.6.0 | 75969 | `a86dd39d14bb45f85a3d18525215a9ef0cfd1f192ac793220e72598c90335f0c` |
| typing-extensions | 4.16.0 | 45571 | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` |
| typing-inspection | 0.4.4 | 14750 | `65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147` |
| uvicorn | 0.52.4 | 79871 | `f86e41a149d7d05a9969337e3946a9c171c06a5d42680896daaba624aeac8da1` |

`ddp_contracts` / `ddp_core` / `ddp_local` are copied as source from
`python/`, not installed as wheels. The standalone tarball's SHA256 is
verified before extraction, then pip is stripped and every produced file is
recorded in `wsl-runtime.json` (so the post-strip tree has its own anchors).
