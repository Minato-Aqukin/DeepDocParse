#!/usr/bin/env bash
# Build the local Arch recipe for the pinned directory artifact.
#
#   scripts/build_desktop_arch.sh              # build artifact, verify, prepare recipe
#   scripts/build_desktop_arch.sh --build      # ... then run makepkg (no install)
#   scripts/build_desktop_arch.sh --reproduce  # run makepkg twice, compare hashes
#   scripts/build_desktop_arch.sh --build --version 0.1.2   # release version (default: build_desktop.py's)
#
# No mode installs a package, calls sudo, touches a workspace or publishes.
# makepkg is pinned to the artifact epoch so package builds are byte-identical.
set -euo pipefail
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python="$repository/.venv/bin/python"

mode=--prepare
version=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prepare|--build|--reproduce) mode="$1"; shift ;;
    --version) version="${2:?--version requires a value}"; shift 2 ;;
    *) printf 'usage: %s [--prepare|--build|--reproduce] [--version X.Y.Z]\n' "$0" >&2; exit 2 ;;
  esac
done
# One version for the directory artifact, its stage name and the PKGBUILD pkgver.
if [ -z "$version" ]; then
  version=$("$python" -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["VERSION"])' \
    "$repository/scripts/build_desktop.py")
fi
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'version must be X.Y.Z (pkgver cannot contain "-"): %s\n' "$version" >&2
  exit 2
fi
stage_name="deepdocparse-$version-linux-x64"
stage="$repository/dist/desktop/$stage_name"
recipe="$repository/dist/desktop/arch"

"$python" "$repository/scripts/build_desktop.py" --version "$version"
if [ -f "$repository/apps/desktop/artifacts/download/electron-v44.3.0-linux-x64.zip" ]; then
  "$python" "$repository/scripts/build_desktop.py" --verify-electron >/dev/null
  printf 'Electron download matches packaging/arch/electron-lock.json\n'
fi
"$python" "$repository/scripts/build_desktop.py" --verify "$stage"

mkdir -p "$recipe"
cp "$repository/packaging/arch/PKGBUILD" "$repository/packaging/arch/deepdocparse.desktop" \
  "$repository/packaging/arch/deepdocparse" "$recipe/"
cp "$repository/dist/desktop/$stage_name.tar.gz" "$recipe/"
"$python" - "$recipe" "$version" <<'PY'
import hashlib
from pathlib import Path
import sys
root, version = Path(sys.argv[1]), sys.argv[2]
recipe = root / 'PKGBUILD'
text = recipe.read_text()
if text.count('pkgver=BUILD_SCRIPT_MUST_SET_VERSION') != 1:
    raise SystemExit('PKGBUILD template must carry exactly one pkgver placeholder')
text = text.replace('pkgver=BUILD_SCRIPT_MUST_SET_VERSION', f'pkgver={version}', 1)
for name in [f'deepdocparse-{version}-linux-x64.tar.gz', 'deepdocparse.desktop', 'deepdocparse']:
    with (root / name).open('rb') as content:
        digest = hashlib.file_digest(content, 'sha256').hexdigest()
    text = text.replace('BUILD_SCRIPT_MUST_SET_SHA256', digest, 1)
recipe.write_text(text)
PY

epoch=$("$python" -c "import json,sys;print(json.load(open(sys.argv[1]))['epoch'])" \
  "$stage/BUILD-MANIFEST.json")
printf 'Prepared local makepkg recipe: %s (SOURCE_DATE_EPOCH=%s)\n' "$recipe" "$epoch"
printf 'Review it, then run: env SOURCE_DATE_EPOCH=%s makepkg -f --noconfirm\n' "$epoch"
printf 'Nothing is installed or published.\n'

build_package() {
  env SOURCE_DATE_EPOCH="$epoch" makepkg --verifysource -f --noconfirm
  env SOURCE_DATE_EPOCH="$epoch" makepkg -f --noconfirm
}
# Exactly this release's package: older packages in the recipe dir must not be hashed or compared.
package="$recipe/deepdocparse-desktop-spike-$version-1-x86_64.pkg.tar.zst"

case "$mode" in
  --build)
    ( cd "$recipe" && build_package )
    sha256sum "$package"
    ;;
  --reproduce)
    ( cd "$recipe" && build_package )
    first=$(sha256sum "$package" | cut -d' ' -f1)
    ( cd "$recipe" && build_package )
    second=$(sha256sum "$package" | cut -d' ' -f1)
    printf 'first  %s\nsecond %s\n' "$first" "$second"
    [ "$first" = "$second" ] || { printf 'NOT REPRODUCIBLE\n' >&2; exit 1; }
    printf 'REPRODUCIBLE\n'
    ;;
esac
