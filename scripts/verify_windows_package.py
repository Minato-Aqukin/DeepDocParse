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
the directory being verified is never trusted. Each `--installer` exe must
additionally match the SHA256SUMS/sidecar anchors recorded in the `windows/`
directory by the checksum step (`release_publication.py verify-package` checks
the same anchors): same-size tampering or a missing anchor fails closed.
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


def _installer_anchor_dirs(installer: Path, directory: Path) -> list[Path]:
    """Candidate dirs holding the SHA256SUMS/sidecar anchors, nearest first.

    The `directory` argument is the win-unpacked output dir (e.g.
    `dist/desktop/windows/win-unpacked`), while `verify-package` records the
    anchors next to the installers in the real `windows/` package directory
    (`locate_package` resolution). Neither path alone finds the other, so walk
    upward from both the unpacked dir and the installer location: the nearest
    `windows/`-style dir wins, and a bare anchor dir (holding SHA256SUMS for
    the installer name) is accepted too. Symlinked anchor dirs are skipped so
    a redirected anchor cannot bless a trojaned exe.
    """
    seen: list[Path] = []
    for base in (Path(directory), installer.parent):
        node = base.resolve()
        for _ in range(6):
            if node.is_symlink():
                break
            if node not in seen:
                seen.append(node)
            win = node / "windows"
            if win.is_dir() and not win.is_symlink() and win not in seen:
                seen.append(win)
            if node.parent == node:
                break
            node = node.parent
    return seen


def check_installer_against_anchors(installer: Path, build_desktop, directory: Path) -> dict:
    """Fail closed unless the exe matches the release's recorded SHA256SUMS/sidecar.

    The Windows build records one SHA256SUMS listing every installer plus one
    `<name>.sha256` sidecar per installer next to the installers (the same
    files `release_publication.py write-receipt`/`verify-package` read later).
    A same-size trojaned exe passes the payload floor, so the digest is
    compared against those anchors — never trusted on its own — and any
    missing, mismatched, malformed or duplicated anchor refuses. The shared
    `parse_hash_line`/`read_sidecar` raise `PublicationError` (not
    `SystemExit`), so only that failure type is translated into a closed
    refusal — unexpected errors still surface.
    """
    scripts_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts_dir))
    try:
        from release_publication import PublicationError as _PublicationError  # noqa: PLC0415
        from release_publication import parse_hash_line as _parse_hash_line  # noqa: PLC0415
        from release_publication import read_sidecar as _read_sidecar  # noqa: PLC0415
    finally:
        sys.path.remove(str(scripts_dir))
    if not installer.is_file() or installer.is_symlink():
        raise SystemExit(f"installer not found: {installer}")
    size = installer.stat().st_size
    if size < MIN_INSTALLER_BYTES:
        raise SystemExit(
            f"installer {installer} is only {size} bytes; the payload "
            "is missing (an electron-builder uninstaller stub?)")
    actual = build_desktop.digest(installer)
    anchors: list[str] = []
    anchor_dir: Path | None = None
    for candidate in _installer_anchor_dirs(installer, Path(directory)):
        sums_path = candidate / "SHA256SUMS"
        if sums_path.is_file() and not sums_path.is_symlink():
            # The build lists setup and portable in one SHA256SUMS while each
            # installer is checked on its own call, so look up this exe's line
            # instead of demanding exact coverage. A malformed or duplicated
            # list is not an anchor anyone can trust: refuse rather than skip.
            recorded: dict[str, str] = {}
            try:
                for raw in sums_path.read_text(encoding="utf-8").splitlines():
                    if not raw.strip():
                        continue
                    digest, name = _parse_hash_line(raw.strip(), sums_path.name)
                    if name in recorded:
                        raise SystemExit(
                            f"SHA256SUMS in {candidate} lists {name} twice; refusing")
                    recorded[name] = digest
            except (OSError, _PublicationError) as exc:
                raise SystemExit(
                    f"SHA256SUMS in {candidate} is unreadable: {exc}; refusing") from exc
            if installer.name in recorded:
                if recorded[installer.name] != actual:
                    raise SystemExit(
                        f"installer {installer.name} sha256 {actual} does not match "
                        f"SHA256SUMS {recorded[installer.name]} in {candidate}; refusing")
                anchors.append(f"{candidate.name}/SHA256SUMS" if anchor_dir else "SHA256SUMS")
                anchor_dir = anchor_dir or candidate
        sidecar = candidate / (installer.name + ".sha256")
        if sidecar.is_file() and not sidecar.is_symlink():
            try:
                expected = _read_sidecar(sidecar, installer.name)
            except _PublicationError as exc:
                raise SystemExit(
                    f"installer {installer.name} has an unreadable {sidecar.name} "
                    f"anchor in {candidate}: {exc}; refusing") from exc
            if expected != actual:
                raise SystemExit(
                    f"installer {installer.name} sha256 {actual} does not match "
                    f"{sidecar.name} {expected} in {candidate}; refusing")
            anchors.append(f"{candidate.name}/{sidecar.name}" if anchor_dir is not None and anchor_dir != candidate else sidecar.name)
            anchor_dir = anchor_dir or candidate
    if not anchors:
        raise SystemExit(
            f"installer {installer.name} has no SHA256SUMS/sidecar anchor near "
            f"{directory} or {installer.parent}; refusing (run the checksum step "
            "that writes SHA256SUMS + sidecars first)")
    return {"path": str(installer), "size": size, "sha256": actual,
            "anchors": sorted(anchors)}


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
            result["installers"].append(
                check_installer_against_anchors(installer, build_desktop,
                                              Path(args.directory)))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
