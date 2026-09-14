#!/usr/bin/env bash
# Build the local Arch recipe for the pinned directory artifact.
#
#   scripts/build_desktop_arch.sh              # build artifact, verify, prepare recipe
#   scripts/build_desktop_arch.sh --build      # ... then run makepkg (no install)
#   scripts/build_desktop_arch.sh --reproduce  # run makepkg twice, compare hashes
#
# No mode installs a package, calls sudo, touches a workspace or publishes.
# makepkg is pinned to the artifact epoch so package builds are byte-identical.
set -euo pipefail
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python="$repository/.venv/bin/python"
stage_name="deepdocparse-0.1.0-linux-x64"
stage="$repository/dist/desktop/$stage_name"
recipe="$repository/dist/desktop/arch"

mode="${1:---prepare}"
case "$mode" in
  --prepare|--build|--reproduce) ;;
  *) printf 'usage: %s [--prepare|--build|--reproduce]\n' "$0" >&2; exit 2 ;;
esac

"$python" "$repository/scripts/build_desktop.py"
if [ -f "$repository/apps/desktop/artifacts/download/electron-v44.3.0-linux-x64.zip" ]; then
  "$python" "$repository/scripts/build_desktop.py" --verify-electron >/dev/null
  printf 'Electron download matches packaging/arch/electron-lock.json\n'
fi
"$python" "$repository/scripts/build_desktop.py" --verify "$stage"

mkdir -p "$recipe"
cp "$repository/packaging/arch/PKGBUILD" "$repository/packaging/arch/deepdocparse.desktop" \
  "$repository/packaging/arch/deepdocparse" "$recipe/"
cp "$repository/dist/desktop/$stage_name.tar.gz" "$recipe/"
"$python" - "$recipe" <<'PY'
import hashlib
from pathlib import Path
import sys
root = Path(sys.argv[1])
recipe = root / 'PKGBUILD'
text = recipe.read_text()
for name in ['deepdocparse-0.1.0-linux-x64.tar.gz', 'deepdocparse.desktop', 'deepdocparse']:
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

case "$mode" in
  --build)
    ( cd "$recipe" && build_package )
    sha256sum "$recipe"/deepdocparse-desktop-spike-*.pkg.tar.zst
    ;;
  --reproduce)
    ( cd "$recipe" && build_package )
    first=$(sha256sum "$recipe"/deepdocparse-desktop-spike-*.pkg.tar.zst | cut -d' ' -f1)
    ( cd "$recipe" && build_package )
    second=$(sha256sum "$recipe"/deepdocparse-desktop-spike-*.pkg.tar.zst | cut -d' ' -f1)
    printf 'first  %s\nsecond %s\n' "$first" "$second"
    [ "$first" = "$second" ] || { printf 'NOT REPRODUCIBLE\n' >&2; exit 1; }
    printf 'REPRODUCIBLE\n'
    ;;
esac
