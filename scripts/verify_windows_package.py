#!/usr/bin/env python3
"""Verify an electron-builder Windows output directory against its anchors.

Run after electron-builder (or in CI after downloading the artifacts):

    .venv/bin/python scripts/verify_windows_package.py \
        dist/desktop/windows/win-unpacked \
        --wsl-lock dist/wsl/wsl-runtime.json \
        --installer dist/desktop/windows/DeepDocParse-0.1.0-win-x64-setup.exe \
        --installer dist/desktop/windows/DeepDocParse-0.1.0-win-x64-portable.exe

It fails closed when `deepdocparse.exe`, the app sources or the bundled WSL
runtime tarball are missing; when the bundled tarball is not byte-identical
(exact size + SHA256) to the W3 release manifest, or its inner manifest does not
recompute, or the archive fails the minimum release shape (a small
self-consistent forgery is refused even though every digest matches); when the
lock's `archive`/`version` disagree with the staged tarball/app; and when any
`resources/app/**` or `resources/client-runtime/**` entry recorded by the
payload anchor (`--stage`, else the assembled stage next to the output, else the
repository sources) disagrees with the packaged tree. A BUILD-MANIFEST inside
the directory being verified is never trusted.
`scripts/build_desktop.py --verify <directory>` runs the same directory checks.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

# An installer that embeds the WSL runtime payload is hundreds of MB. The
# ~190 KB artifact a wine-less electron-builder run leaves behind is the
# uninstaller stub, never a shippable installer.
MIN_INSTALLER_BYTES = 100 << 20


def load_build_desktop():
    path = Path(__file__).resolve().with_name("build_desktop.py")
    spec = importlib.util.spec_from_file_location("build_desktop", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path,
                        help="electron-builder win output directory "
                             "(e.g. dist/desktop/windows/win-unpacked)")
    parser.add_argument("--wsl-lock", type=Path, default=None,
                        help="W3 release manifest (default: dist/wsl/wsl-runtime.json)")
    parser.add_argument("--version", default=None,
                        help="expected release version (default: app package.json)")
    parser.add_argument("--stage", type=Path, default=None,
                        help="assembled stage whose BUILD-MANIFEST anchors the "
                             "payload diff (default: the stage next to the "
                             "output, then repository sources; a manifest "
                             "inside the output directory is never trusted)")
    parser.add_argument("--installer", action="append", type=Path, default=[],
                        help="installer/portable exe to check for an embedded "
                             "payload (repeatable)")
    args = parser.parse_args(argv)
    build_desktop = load_build_desktop()
    result = build_desktop.verify_packaged_windows(
        args.directory, args.wsl_lock, args.version, stage=args.stage)
    if args.installer:
        result["installers"] = []
        for installer in args.installer:
            if not installer.is_file():
                raise SystemExit(f"installer not found: {installer}")
            size = installer.stat().st_size
            if size < MIN_INSTALLER_BYTES:
                raise SystemExit(
                    f"installer {installer} is only {size} bytes; the payload "
                    "is missing (an electron-builder uninstaller stub?)")
            result["installers"].append({
                "path": str(installer), "size": size,
                "sha256": build_desktop.digest(installer)})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
