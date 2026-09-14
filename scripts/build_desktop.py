#!/usr/bin/env python3
"""Build a pinned directory package from verified build inputs.

`--platform linux-x64` (default) packages the shared UI, the Electron host and
the Arch system-Python runtime as a reproducible tar.gz. `--platform win32-x64`
packages the Electron host, the shared UI and the W3-built WSL runtime tarball
as a reproducible zip; it deliberately carries no native Windows Python
payload. No network, database, environment file, model cache or user data is
copied.
"""

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import sys
import sysconfig
import tarfile
import tempfile
import time
import zipfile
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging/arch/runtime-lock.json"
ELECTRON_LOCK = ROOT / "packaging/arch/electron-lock.json"
ELECTRON_ZIP = ROOT / "apps/desktop/artifacts/download/electron-v44.3.0-linux-x64.zip"
ROOTS = ["httpx", "pypdfium2", "pillow", "fastapi", "uvicorn"]
VERSION = "0.1.0"
PLATFORM = "linux-x64"
WINDOWS_PLATFORM = "win32-x64"
WINDOWS_LOCK = ROOT / "packaging/windows/electron-lock.json"
WINDOWS_ELECTRON_ZIP = (
    ROOT / "apps/desktop/artifacts/download/electron-v44.3.0-win32-x64.zip"
)
WSL_RUNTIME = "deepdocparse-wsl-runtime-{version}-linux-x64"
WSL_RUNTIME_NAME = "deepdocparse-wsl-runtime"
WSL_PLATFORM = "linux-x86_64"
# W3 writes all of its release outputs, including the outer manifest, into
# dist/wsl/. The manifest that pins the tarball by exact size + SHA256 is the
# only anchor this script trusts; never `tarball`/a sidecar alone.
WSL_RUNTIME_DIR = ROOT / "dist/wsl"
WSL_RUNTIME_LOCK = WSL_RUNTIME_DIR / "wsl-runtime.json"
# The real runtime is ~123 MiB. Anything smaller than this is a placeholder
# or a synthetic stand-in and must never be assembled or packaged.
MIN_WSL_RUNTIME_BYTES = 1 << 20
MIN_WINDOWS_EXE_BYTES = 1 << 20
# The W3 inner manifest lists ~6 000 files and is ~750 KiB; anything larger is
# not a manifest and must not be buffered.
MAX_WSL_MANIFEST_BYTES = 16 << 20
# The pinned-inputs lock the W3 build consumed. The bundle embeds a copy as
# runtime/wsl-runtime-lock.json; when this file is present it is also the
# out-of-band anchor the inner manifest's lock_sha256 must match.
WSL_BUILD_LOCK = Path("packaging/windows/wsl-runtime-lock.json")

# The platform table. Linux is the default and must keep the exact artifact
# names, archive format and messages it had before Windows support existed;
# Windows only swaps the Electron pin, the archive container and the binary
# name, and adds the WSL runtime input (W3-owned).
PLATFORMS = {
    "linux-x64": {
        "system": "linux",
        "machine": "x86_64",
        "electron_lock": ELECTRON_LOCK,
        "electron_zip": ELECTRON_ZIP,
        "binary": "deepdocparse",
    },
    WINDOWS_PLATFORM: {
        "system": "windows",
        "machine": "amd64",
        "electron_lock": WINDOWS_LOCK,
        "electron_zip": WINDOWS_ELECTRON_ZIP,
        "binary": "deepdocparse.exe",
    },
}


def digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


_WSL_SHAPE_MODULE = None
_WSL_SHAPE_NAME = "ddp_wsl_runtime_shape"


def wsl_shape():
    """The W3 bundle's shape rules, owned by scripts/build_wsl_runtime.py.

    The assembler must not keep a second inventory of what the runtime looks
    like; it asks the script that builds it. Loaded lazily so a missing sibling
    fails only when a WSL runtime is actually validated, and registered in
    `sys.modules` so every loader in this process (verify_windows_package.py
    loads its own copy of this script) shares one shape configuration.
    """
    global _WSL_SHAPE_MODULE
    if _WSL_SHAPE_MODULE is None:
        module = sys.modules.get(_WSL_SHAPE_NAME)
        if module is None:
            path = Path(__file__).resolve().with_name("build_wsl_runtime.py")
            if not path.is_file():
                raise SystemExit(
                    f"W3 shape rules not found (expected {path}); refusing to "
                    "validate the WSL runtime without them")
            spec = importlib.util.spec_from_file_location(_WSL_SHAPE_NAME, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[_WSL_SHAPE_NAME] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(_WSL_SHAPE_NAME, None)
                raise
        _WSL_SHAPE_MODULE = module
    return _WSL_SHAPE_MODULE


def verify_electron_zip(zip_path, lock_path=ELECTRON_LOCK):
    """Pin the Electron download by size and SHA256 before trusting its tree."""
    lock = json.loads(Path(lock_path).read_text())
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise SystemExit(f"Electron archive not found: {zip_path}")
    actual_size = zip_path.stat().st_size
    if actual_size != lock["size"]:
        raise SystemExit(
            f"Electron archive size {actual_size} != pinned {lock['size']} "
            f"({zip_path})")
    actual = digest(zip_path)
    if actual != lock["sha256"]:
        raise SystemExit(
            f"Electron archive SHA256 {actual} != pinned {lock['sha256']} ({zip_path})")
    if zip_path.name != lock["file"]:
        raise SystemExit(f"Electron archive name {zip_path.name} != pinned {lock['file']}")
    return {"version": lock["version"], "file": zip_path.name,
            "size": actual_size, "sha256": actual}


def collect_license_manifest(directory, distributions):
    """Electron notices + every locked distribution's license files.

    Raises when any locked distribution (or Electron) has no notice in the
    staged tree: an incomplete manifest must fail the build, not ship.
    """
    directory = Path(directory)
    result = {"electron": {}, "distributions": {}}
    missing = []
    for relative in ("LICENSE", "LICENSES.chromium.html"):
        path = directory / relative
        if path.is_file():
            result["electron"][relative] = digest(path)
        else:
            missing.append(f"Electron notice {relative}")
    site = directory / "resources/runtime/site-packages"
    for name in sorted(distributions):
        prefix = name.replace("-", "_")
        files = {}
        for dist_info in sorted(site.glob(f"{prefix}-*.dist-info")):
            for path in sorted(dist_info.rglob("*")):
                if path.is_file() and "licenses" in path.relative_to(dist_info).parts:
                    files[path.relative_to(directory).as_posix()] = digest(path)
        if files:
            result["distributions"][name] = files
        else:
            missing.append(f"dependency license notice for {name}")
    if missing:
        raise SystemExit("license manifest is incomplete: " + "; ".join(missing))
    return result


def dependencies():
    pending, result = list(ROOTS), {}
    while pending:
        installed = distribution(pending.pop())
        name = installed.metadata["Name"].lower().replace("_", "-")
        if name in result:
            continue
        result[name] = installed
        for raw in installed.requires or []:
            requirement = Requirement(raw)
            if not requirement.marker or requirement.marker.evaluate({"extra": ""}):
                if requirement.extras:
                    raise SystemExit("extra dependency needs explicit locking")
                pending.append(requirement.name)
    return dict(sorted(result.items()))


def payload(installed):
    base = Path(installed.locate_file("")).resolve()
    for entry in sorted(installed.files or []):
        relative = Path(str(entry))
        if ".." in relative.parts or relative.is_absolute():
            continue  # Console scripts are not executable inputs to the fixed module launcher.
        if "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        if relative.name in {"RECORD", "direct_url.json", "INSTALLER", "REQUESTED"}:
            continue
        if relative.suffix == ".pth":
            raise SystemExit(f"uncontrolled site hook: {installed.metadata['Name']}")
        original = (base / relative).resolve()
        if not original.is_relative_to(base) or not original.is_file():
            raise SystemExit("unsafe dependency payload")
        yield relative, original


def runtime_lock(installed):
    return {
        "format": 1,
        "python_major_minor": list(sys.version_info[:2]),
        "machine": platform.machine(),
        "roots": ROOTS,
        "distributions": {
            name: {
                "version": value.version,
                "payload_sha256": hashlib.sha256(
                    "".join(
                        f"{relative.as_posix()}:{digest(original)}\n"
                        for relative, original in payload(value)
                    ).encode()
                ).hexdigest(),
            }
            for name, value in installed.items()
        },
    }


def copy_tree(source, target):
    for original in sorted(source.rglob("*")):
        relative = original.relative_to(source)
        if "__pycache__" in relative.parts or original.suffix == ".pyc":
            continue
        if original.is_symlink():
            raise SystemExit(f"unreviewed build input symlink: {relative}")
        if original.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)


def extract_zip(archive, target):
    """Extract a checksum-pinned zip, rejecting links and unsafe paths."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts:
                raise SystemExit(f"unsafe path in archive: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SystemExit(f"link in archive is not allowed: {info.filename}")
            destination = (target / name).resolve()
            if not destination.is_relative_to(root):
                raise SystemExit(f"unsafe path in archive: {info.filename}")
            if info.is_dir() or stat.S_ISDIR(mode):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            permissions = mode & 0o7777
            if permissions:
                os.chmod(destination, permissions)


def write_deterministic_zip(stage, archive, epoch):
    """Zip a stage directory with pinned timestamps, mode bits and order."""
    stage = Path(stage)
    archive = Path(archive)
    date_time = tuple(time.gmtime(epoch)[:6])
    if date_time[0] < 1980:
        date_time = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for entry in [stage, *sorted(stage.rglob("*"))]:
            relative = entry.relative_to(stage).as_posix()
            name = stage.name if relative == "." else f"{stage.name}/{relative}"
            info = zipfile.ZipInfo(name + "/" if entry.is_dir() else name, date_time)
            info.create_system = 3  # deterministic across Linux and Windows builders
            if entry.is_dir():
                info.external_attr = (0o40755 << 16) | 0x10
                info.compress_type = zipfile.ZIP_STORED
                zf.writestr(info, b"")
            else:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with entry.open("rb") as source, zf.open(info, "w") as sink:
                    shutil.copyfileobj(source, sink)


def wsl_python_abi(lock):
    """Extract the Linux Python ABI from the W3-owned wsl-runtime.json.

    The Windows host runs no native Python; the only interpreter ships inside
    the WSL runtime tarball, so this ABI is a Linux ABI (`machine` is x86_64,
    never the Windows amd64 the release platform declares).
    """
    if not isinstance(lock, dict):
        raise SystemExit("wsl-runtime.json has an unsupported shape")
    nested = lock.get("python")
    if isinstance(nested, dict):
        major_minor = nested.get("major_minor")
        if major_minor is None:
            # W3's wsl-runtime.json makes the probe's key names explicit.
            major_minor = nested.get("python_major_minor")
        cache_tag = nested.get("cache_tag")
        machine = nested.get("machine")
        soabi = nested.get("soabi")
    else:
        major_minor = lock.get("python_major_minor")
        cache_tag = lock.get("cache_tag")
        machine = lock.get("machine")
        soabi = lock.get("soabi")
    if not isinstance(major_minor, list) or len(major_minor) != 2 \
            or not all(isinstance(part, int) for part in major_minor):
        raise SystemExit("wsl-runtime.json has no python major_minor ABI")
    if machine not in {"x86_64", "aarch64"}:
        raise SystemExit(
            f"wsl-runtime.json python machine {machine!r} is not a Linux ABI")
    if not isinstance(cache_tag, str) or not cache_tag:
        cache_tag = f"cpython-{major_minor[0]}{major_minor[1]}"
    return {"major_minor": major_minor, "cache_tag": cache_tag,
            "soabi": soabi, "machine": machine}


def wsl_release_lock(lock, path):
    """Validate the identity of a W3 release manifest before trusting its pins.

    `verify_wsl_runtime` below used to accept any JSON with a `sha256`/`size`
    pair, so a self-consistent *synthetic stand-in* (a 205-byte empty tar with
    a matching sidecar and manifest) was assembled and shipped into the
    electron-builder payload. A release manifest always declares its format,
    name, version, Linux platform and archive; anything else fails closed.
    """
    if not isinstance(lock, dict):
        raise SystemExit(f"wsl-runtime.json has an unsupported shape: {path}")
    if lock.get("format") != 1:
        raise SystemExit(f"wsl-runtime.json format is not 1: {path}")
    if lock.get("name") != WSL_RUNTIME_NAME:
        raise SystemExit(
            f"wsl-runtime.json is not a W3 release manifest "
            f"(name {lock.get('name')!r}): {path}. A synthetic stand-in must "
            "never be packaged.")
    if lock.get("platform") != WSL_PLATFORM:
        raise SystemExit(
            f"wsl-runtime.json platform {lock.get('platform')!r} != "
            f"{WSL_PLATFORM!r}: {path}")
    if "stand_in" in lock:
        raise SystemExit(
            f"wsl-runtime.json is a synthetic stand-in, not a W3 release "
            f"manifest: {path}")
    if not isinstance(lock.get("version"), str) or not lock["version"]:
        raise SystemExit(f"wsl-runtime.json has no release version: {path}")
    size = lock.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise SystemExit(f"wsl-runtime.json has no pinned size: {path}")
    sha = lock.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 \
            or any(character not in "0123456789abcdef" for character in sha.lower()):
        raise SystemExit(f"wsl-runtime.json has no pinned sha256: {path}")
    return size, sha.lower()


def wsl_build_lock_path():
    """Where the reviewed pinned-inputs lock lives for the current ROOT."""
    return ROOT / WSL_BUILD_LOCK


def read_wsl_archive(tarball):
    """One streaming pass over a W3 tarball: names, digests, links, payloads.

    The inner manifest records digests *as extracted* (a symlinked entry hashes
    its target's content), so the only way to recompute it without writing
    ~6 000 files to disk is to hash the tar members in place and resolve links
    lexically afterwards. The manifest and the embedded pinned lock are kept in
    memory (bounded); everything else is hashed and dropped.
    """
    names, digests, links, payloads, sizes = set(), {}, {}, {}, {}
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar:
                names.add(member.name)
                if member.issym():
                    links[member.name] = member.linkname
                    continue
                if not member.isreg():
                    continue
                keep = ([] if member.name.endswith(
                    ("/wsl-runtime.json", "/wsl-runtime-lock.json")) else None)
                source = tar.extractfile(member)
                if source is None:
                    raise SystemExit(
                        f"WSL runtime archive member is unreadable: {member.name}")
                hasher = hashlib.sha256()
                kept = 0
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    if keep is not None:
                        kept += len(chunk)
                        if kept > MAX_WSL_MANIFEST_BYTES:
                            raise SystemExit(
                                f"WSL runtime manifest {member.name} exceeds "
                                f"{MAX_WSL_MANIFEST_BYTES} bytes")
                        keep.append(chunk)
                digests[member.name] = hasher.hexdigest()
                sizes[member.name] = member.size
                if keep is not None:
                    payloads[member.name] = b"".join(keep)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemExit(
            f"WSL runtime archive is not a readable tar.gz: {tarball}: {exc}") from None
    return names, digests, links, payloads, sizes


def resolve_archive_member(name, links, digests, depth=0):
    """Digest of a member as extraction would see it, following tar symlinks.

    Tar member names and link targets are POSIX strings on every platform; path
    joining must use posixpath even on Windows or `bin/2to3` -> `2to3-3.12`
    resolves to a backslash name that never matches the archive map.
    """
    if depth > 32:
        return None
    if name in digests:
        return digests[name]
    target = links.get(name)
    if target is None or target.startswith("/"):
        return None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if relative == ".." or relative.startswith("../"):
        return None
    return resolve_archive_member(relative, links, digests, depth + 1)


def resolve_archive_size(name, links, sizes, depth=0):
    """Size of a member as extraction would see it, following tar symlinks.

    The bundled interpreter is a symlink to `python3.12`; its real size is the
    target's, which is exactly the property a shell-stub forgery cannot have.
    """
    if depth > 32:
        return None
    if name in sizes:
        return sizes[name]
    target = links.get(name)
    if target is None or target.startswith("/"):
        return None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if relative == ".." or relative.startswith("../"):
        return None
    return resolve_archive_size(relative, links, sizes, depth + 1)


def verify_wsl_inner_manifest(tarball, lock, top, names, digests, links, payloads,
                              sizes):
    """Cross-check the manifest W3 writes *inside* the archive.

    The outer `wsl-runtime.json` can be fabricated in seconds; a fully
    self-consistent inner manifest cannot be produced from nothing — but an
    attacker with the real pinned lock can still copy it and hand-write a
    miniature whose digests all recompute. That is why, after the file map
    matches, the bundle must also clear the minimum release shape owned by
    `build_wsl_runtime`: enough unpacked bytes, enough files, a real-sized
    interpreter and every required stdlib/site-packages member the pinned lock
    implies. The shape does not prove provenance; it refuses a small
    self-consistent forgery, and nothing here claims more than that.
    """
    path = f"{top}/wsl-runtime.json"
    if path not in names:
        raise SystemExit(
            f"WSL runtime archive is not a W3 bundle (missing {path}): {tarball}")
    raw = payloads.get(path)
    if raw is None:
        raise SystemExit(
            f"WSL runtime archive is not a W3 bundle (unreadable {path}): {tarball}")
    try:
        inner = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(
            f"WSL runtime inner manifest is not valid JSON: {tarball}: {exc}") from None
    if not isinstance(inner, dict):
        raise SystemExit(f"WSL runtime inner manifest has an unsupported shape: {tarball}")
    if inner.get("format") != 1:
        raise SystemExit(f"WSL runtime inner manifest format is not 1: {tarball}")
    if inner.get("name") != WSL_RUNTIME_NAME:
        raise SystemExit(
            f"WSL runtime inner manifest is not a W3 release manifest "
            f"(name {inner.get('name')!r}): {tarball}")
    if inner.get("version") != lock.get("version"):
        raise SystemExit(
            f"WSL runtime inner manifest version {inner.get('version')!r} != outer "
            f"{lock.get('version')!r}: {tarball}")
    if inner.get("platform") != WSL_PLATFORM:
        raise SystemExit(
            f"WSL runtime inner manifest platform {inner.get('platform')!r} != "
            f"{WSL_PLATFORM!r}: {tarball}")
    epoch = inner.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch <= 0:
        raise SystemExit(f"WSL runtime inner manifest has no epoch: {tarball}")
    outer_epoch = lock.get("epoch")
    if isinstance(outer_epoch, int) and not isinstance(outer_epoch, bool) \
            and epoch != outer_epoch:
        raise SystemExit(
            f"WSL runtime inner manifest epoch {epoch} != outer {outer_epoch}: "
            f"{tarball}")
    inner_abi = wsl_python_abi(inner)
    outer_abi = wsl_python_abi(lock)
    if inner_abi != outer_abi:
        raise SystemExit(
            f"WSL runtime inner manifest python ABI {inner_abi} != outer "
            f"{outer_abi}: {tarball}")
    lock_sha = inner.get("lock_sha256")
    if not isinstance(lock_sha, str) or len(lock_sha) != 64 \
            or any(character not in "0123456789abcdef" for character in lock_sha.lower()):
        raise SystemExit(
            f"WSL runtime inner manifest has no pinned lock_sha256: {tarball}")
    embedded_lock = f"{top}/runtime/wsl-runtime-lock.json"
    if embedded_lock not in digests:
        raise SystemExit(
            f"WSL runtime archive is missing {embedded_lock}: {tarball}")
    if digests[embedded_lock] != lock_sha.lower():
        raise SystemExit(
            f"WSL runtime embedded lock {digests[embedded_lock]} != inner manifest "
            f"lock_sha256 {lock_sha.lower()}: {tarball}")
    reviewed = wsl_build_lock_path()
    if reviewed.is_file() and digest(reviewed) != lock_sha.lower():
        raise SystemExit(
            f"WSL runtime was built against a different pinned lock "
            f"(inner {lock_sha.lower()} != {digest(reviewed)}): {tarball}")
    files = inner.get("files")
    if not isinstance(files, dict) or not files:
        raise SystemExit(
            f"WSL runtime inner manifest has an empty files map: {tarball}")
    missing, mismatched = [], []
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str) \
                or len(expected) != 64 \
                or any(character not in "0123456789abcdef" for character in expected.lower()):
            raise SystemExit(
                f"WSL runtime inner manifest has a malformed file entry: "
                f"{relative!r}: {tarball}")
        actual = resolve_archive_member(f"{top}/{relative}", links, digests)
        if actual is None:
            missing.append(relative)
        elif actual != expected.lower():
            mismatched.append(relative)
    unlisted = sorted(
        name[len(top) + 1:] for name in [*digests, *links]
        if name.startswith(top + "/") and name != path
        and name[len(top) + 1:] not in files)
    problems = []
    if missing:
        problems.append(f"listed but absent: {missing[:3]}")
    if mismatched:
        problems.append(f"digest mismatch: {mismatched[:3]}")
    if unlisted:
        problems.append(f"present but unlisted: {unlisted[:3]}")
    if problems:
        raise SystemExit(
            "WSL runtime inner manifest does not match the archive files: "
            + "; ".join(problems) + f": {tarball}")
    # Shape: derive the pinned lock's required members from the reviewed lock
    # when it is present, else from the lock the archive itself embeds.
    pinned_raw = (reviewed.read_text() if reviewed.is_file()
                  else payloads.get(embedded_lock))
    try:
        pinned = json.loads(pinned_raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"WSL runtime pinned lock is not valid JSON: {tarball}") from None
    shape_sizes = {}
    for relative in files:
        member_size = resolve_archive_size(f"{top}/{relative}", links, sizes)
        if member_size is not None:
            shape_sizes[relative] = member_size
    manifest_size = resolve_archive_size(path, links, sizes)
    if manifest_size is not None:
        shape_sizes["wsl-runtime.json"] = manifest_size
    violations = wsl_shape().shape_problems(shape_sizes, pinned)
    if violations:
        raise SystemExit(
            "WSL runtime archive fails the minimum release shape: "
            + "; ".join(violations[:5]) + f": {tarball}")
    return inner


def verify_wsl_archive_layout(tarball, lock):
    """The archive must really be a W3 bundle, not just bytes with a right hash.

    A placeholder can be pinned by a placeholder-shaped manifest; the member
    list, the inner manifest and the minimum release shape are the properties a
    manifest cannot fake into a usable runtime. The whole archive is hashed once
    and the inner manifest (identity, ABI, pinned-lock digest and full files
    map) is recomputed before the shape floors are applied.
    """
    top = WSL_RUNTIME.format(version=lock["version"])
    names, digests, links, payloads, sizes = read_wsl_archive(tarball)
    required = [f"{top}/runtime/python/bin/python3", f"{top}/wsl-runtime.json",
                f"{top}/runtime/wsl-runtime-lock.json"]
    missing = [name for name in required if name not in names]
    if missing:
        raise SystemExit(
            f"WSL runtime archive is not a W3 bundle (missing {missing[0]}): {tarball}")
    return verify_wsl_inner_manifest(
        tarball, lock, top, names, digests, links, payloads, sizes)


def verify_wsl_runtime(tarball, lock_path=None, expected_version=None):
    """Pin the W3-built WSL runtime by manifest and sidecar, or refuse.

    The anchor is the outer `wsl-runtime.json` (W3 writes it to
    `dist/wsl/wsl-runtime.json`): format/name/platform identity, the exact
    archive name, byte size and SHA256. The sidecar and the archive's own
    member layout must agree; a stand-in or placeholder fails closed.
    """
    tarball = Path(tarball)
    if not tarball.is_file():
        raise SystemExit(
            f"WSL runtime archive not found: {tarball} "
            "(build it with scripts/build_wsl_runtime.py)")
    if not tarball.name.endswith(".tar.gz"):
        raise SystemExit(f"WSL runtime archive must be a .tar.gz: {tarball}")
    if lock_path is None:
        lock_path = tarball.with_name("wsl-runtime.json")
    lock_path = Path(lock_path)
    if not lock_path.is_file():
        raise SystemExit(f"WSL runtime lock not found: {lock_path}")
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"wsl-runtime.json is not valid JSON: {lock_path}: {exc}") from None
    pinned_size, pinned_sha = wsl_release_lock(lock, lock_path)
    abi = wsl_python_abi(lock)
    if expected_version is not None and lock["version"] != expected_version:
        raise SystemExit(
            f"WSL runtime version {lock['version']!r} != release "
            f"{expected_version!r} ({lock_path})")
    if lock.get("archive") != tarball.name:
        raise SystemExit(
            f"WSL runtime archive name {tarball.name} != wsl-runtime.json "
            f"{lock.get('archive')!r}")
    size = tarball.stat().st_size
    if size < MIN_WSL_RUNTIME_BYTES:
        raise SystemExit(
            f"WSL runtime archive is only {size} bytes; a placeholder must "
            f"never be packaged ({tarball})")
    if size != pinned_size:
        raise SystemExit(
            f"WSL runtime size {size} != wsl-runtime.json {pinned_size} ({tarball})")
    actual = digest(tarball)
    if actual != pinned_sha:
        raise SystemExit(
            f"WSL runtime SHA256 {actual} != wsl-runtime.json {pinned_sha} ({tarball})")
    sidecar = tarball.with_name(tarball.name + ".sha256")
    if not sidecar.is_file():
        raise SystemExit(f"WSL runtime checksum file not found: {sidecar}")
    recorded = sidecar.read_text().split()[0].lower()
    if recorded != actual:
        raise SystemExit(
            f"WSL runtime SHA256 {actual} != sidecar {recorded} ({tarball})")
    verify_wsl_archive_layout(tarball, lock)
    return {"archive": tarball.name, "size": size, "sha256": actual,
            "python": abi, "lock": lock, "path": tarball,
            "sidecar": sidecar, "lock_path": lock_path}


def read_build_manifest(directory):
    manifest = json.loads((directory / "BUILD-MANIFEST.json").read_text())
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise SystemExit("BUILD-MANIFEST.json has an unsupported shape")
    return manifest


def verify_windows_directory(directory, manifest):
    """Anchor checks for the win32-x64 host package (no native Python payload)."""
    runtime = manifest.get("runtime")
    embedded = json.loads(
        (directory / "resources/runtime/wsl-runtime.json").read_text())
    if embedded != runtime:
        raise SystemExit(
            "embedded wsl-runtime.json differs from BUILD-MANIFEST runtime")
    info = json.loads(
        (directory / "resources/runtime/runtime-info.json").read_text())
    abi = wsl_python_abi(runtime)
    if list(info["python_major_minor"]) != abi["major_minor"] \
            or info.get("machine") != abi["machine"] \
            or info.get("cache_tag") != abi["cache_tag"]:
        raise SystemExit("runtime-info.json ABI differs from BUILD-MANIFEST runtime")
    expected = collect_license_manifest(directory, {})
    if manifest.get("license_manifest") != expected:
        raise SystemExit("license manifest does not match the files in the package")
    release = json.loads((directory / "RELEASE-MANIFEST.json").read_text())
    if (release.get("platform") or {}).get("system") != "windows":
        raise SystemExit("RELEASE-MANIFEST.json is not a Windows release")
    if release.get("runtime_lock_sha256") != digest(
            directory / "resources/runtime/wsl-runtime.json"):
        raise SystemExit(
            "RELEASE-MANIFEST runtime lock digest does not match the embedded lock")
    wsl = release.get("wsl_runtime") or {}
    tarball = directory / "resources" / str(wsl.get("archive", ""))
    if not tarball.is_file() or digest(tarball) != wsl.get("sha256") \
            or tarball.stat().st_size != wsl.get("size"):
        raise SystemExit(
            "bundled WSL runtime does not match RELEASE-MANIFEST wsl_runtime")
    # The embedded lock is the outer W3 manifest; re-derive the inner manifest
    # against the archive so a package assembled from a forged runtime cannot
    # pass just because its own BUILD-MANIFEST was generated by the same run.
    verify_wsl_archive_layout(tarball, embedded)
    return {"files": len(manifest["files"]), "distributions": 0,
            "electron": manifest.get("electron"), "version": release["version"],
            "platform": WINDOWS_PLATFORM}


def canonical_package_view(raw):
    """package.json as electron-builder preserves it, minus its own edits.

    electron-builder strips `scripts`/`devDependencies` and injects
    `extraMetadata.author`, so the packaged digest can never equal the source
    digest. Everything else must survive byte-for-byte; comparing the canonical
    JSON of the preserved fields still catches a tampered `main`/`type`/added
    payload fields.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    view = {key: value for key, value in data.items()
            if key not in {"scripts", "devDependencies", "author"}}
    return json.dumps(view, sort_keys=True, separators=(",", ":")).encode()


def verify_packaged_payload(directory, version, stage=None):
    """Compare the packaged app payload with a trusted build source, or refuse.

    The anchor is, in order: an explicitly passed `stage`, the assembled stage
    electron-builder was pointed at (the usual
    `dist/desktop/deepdocparse-<version>-win32-x64` next to `win-unpacked`),
    then the repository sources. A `BUILD-MANIFEST.json` shipped *inside*
    `directory` is never trusted: the packaged output is the artifact under
    suspicion, so a manifest travelling with it proves nothing about where it
    came from. Every `resources/app/**` / `resources/client-runtime/**` entry
    the anchor records is compared, not only the `src/*.mjs` + `*.ts` pair;
    `package.json` goes through `canonical_package_view`.
    """
    directory = Path(directory)
    packaged_app = directory / "resources/app"
    packaged_client = directory / "resources/client-runtime"
    problems = []
    if stage is not None:
        stage = Path(stage)
        if not (stage / "BUILD-MANIFEST.json").is_file():
            raise SystemExit(
                f"stage {stage} has no BUILD-MANIFEST.json; refusing to diff "
                "the packaged payload against an unverifiable source")
        root, label, manifest_files = stage, str(stage), \
            read_build_manifest(stage)["files"]
    else:
        root = label = manifest_files = None
        for candidate in (
            directory.parent.parent / f"deepdocparse-{version}-{WINDOWS_PLATFORM}",
            ROOT / "dist/desktop" / f"deepdocparse-{version}-{WINDOWS_PLATFORM}",
        ):
            if candidate.resolve() == directory.resolve():
                continue
            if (candidate / "BUILD-MANIFEST.json").is_file():
                root, label, manifest_files = candidate, str(candidate), \
                    read_build_manifest(candidate)["files"]
                break
        if manifest_files is None:
            root, label = ROOT, "repository sources"
    covered = ("resources/app/", "resources/client-runtime/")
    if manifest_files is not None:
        for relative, expected in sorted(manifest_files.items()):
            if not relative.startswith(covered) \
                    or relative == "resources/app/package.json":
                continue
            packaged = directory / relative
            if not packaged.is_file():
                problems.append(
                    f"{relative} is missing from the packaged output ({label})")
            elif digest(packaged) != expected:
                problems.append(f"{relative} differs from {label}")
        source_manifest = root / "resources/app/package.json"
    else:
        for source_root, packaged_root in (
            (root / "apps/desktop/src", packaged_app / "src"),
            (root / "apps/web/dist", packaged_app / "ui"),
        ):
            for source in sorted(source_root.rglob("*")):
                if not source.is_file():
                    continue
                packaged = packaged_root / source.relative_to(source_root)
                if not packaged.is_file():
                    problems.append(
                        f"packaged {packaged.relative_to(directory)} has no "
                        f"{source_root.relative_to(root)} counterpart")
                elif digest(packaged) != digest(source):
                    problems.append(
                        f"{packaged.relative_to(directory)} differs from "
                        f"{source_root.relative_to(root)}")
        for source in sorted((root / "packages/client-runtime/src").glob("*.ts")):
            packaged = packaged_client / source.name
            if not packaged.is_file():
                problems.append(
                    f"packaged client-runtime/{source.name} has no repository "
                    "counterpart")
            elif digest(packaged) != digest(source):
                problems.append(
                    f"client-runtime/{source.name} differs from repository "
                    "sources")
        if not (packaged_client / "package.json").is_file():
            problems.append("missing client-runtime/package.json")
        source_manifest = root / "apps/desktop/package.json"
    if not source_manifest.is_file():
        problems.append(f"no apps/desktop package.json under {label}")
    elif canonical_package_view((packaged_app / "package.json").read_bytes()) \
            != canonical_package_view(source_manifest.read_bytes()):
        problems.append(f"package.json payload differs from {label}")
    if problems:
        raise SystemExit(
            "packaged app payload does not match its build source: "
            + "; ".join(problems[:5]))


def verify_packaged_windows(directory, wsl_lock=None, expected_version=None,
                            stage=None):
    """Fail-closed checks for an electron-builder Windows output directory.

    This is the guard the first real packaging run lacked: the assembled stage
    had the right tarball, but electron-builder ran before it existed and
    silently embedded a 205-byte placeholder. Re-hash the packaged tree, compare
    the bundled runtime byte-for-byte with the W3 release manifest (outer pins,
    inner manifest, file map and minimum shape), compare the staged name/version
    with the lock, and diff the app payload against an explicitly passed
    `stage`, the assembled stage next to the output, or the repository sources —
    never against a manifest that ships inside `directory`.
    """
    directory = Path(directory).resolve()
    binary = directory / "deepdocparse.exe"
    if not binary.is_file():
        raise SystemExit(f"packaged deepdocparse.exe not found: {binary}")
    exe_size = binary.stat().st_size
    if exe_size < MIN_WINDOWS_EXE_BYTES:
        raise SystemExit(
            f"packaged deepdocparse.exe is only {exe_size} bytes: {binary}")
    app = directory / "resources/app"
    problems = []
    for relative in ("package.json", "src/main.mjs", "ui/index.html"):
        if not (app / relative).is_file():
            problems.append(f"missing app source: resources/app/{relative}")
    # shared-client.mjs dynamically imports exactly these three from
    # resources/client-runtime (extraResources copies packages/client-runtime/src).
    for relative in ("index.ts", "sqlite-store.ts", "http-provider.ts",
                     "package.json"):
        if not (directory / "resources/client-runtime" / relative).is_file():
            problems.append(f"missing client-runtime source: {relative}")
    if not (directory / "resources/runtime/runtime-info.json").is_file():
        problems.append("missing resources/runtime/runtime-info.json")
    if problems:
        raise SystemExit("packaged directory is incomplete: " + "; ".join(problems))
    app_version = json.loads((app / "package.json").read_text()).get("version")
    if expected_version is not None and app_version != expected_version:
        raise SystemExit(
            f"packaged app version {app_version!r} != expected {expected_version!r}")
    version = expected_version or app_version
    if not isinstance(version, str) or not version:
        raise SystemExit("packaged app package.json has no version")
    release_manifest = directory / "RELEASE-MANIFEST.json"
    if release_manifest.is_file():
        release = json.loads(release_manifest.read_text())
        if release.get("version") != version:
            raise SystemExit(
                f"packaged RELEASE-MANIFEST version {release.get('version')!r} != "
                f"staged {version!r}")
    tarballs = sorted(
        (directory / "resources").glob(f"{WSL_RUNTIME_NAME}-*-linux-x64.tar.gz"))
    if len(tarballs) != 1:
        raise SystemExit(
            "expected exactly one packaged WSL runtime tarball, found "
            f"{[path.name for path in tarballs]}: {directory / 'resources'}")
    tarball = tarballs[0]
    match = re.fullmatch(
        re.escape(WSL_RUNTIME_NAME) + r"-(?P<version>.+)-linux-x64\.tar\.gz",
        tarball.name)
    archive_version = match.group("version") if match else None
    if archive_version != version:
        raise SystemExit(
            f"packaged WSL runtime archive {tarball.name!r} does not carry the "
            f"staged version {version!r}")
    lock_path = Path(wsl_lock) if wsl_lock is not None else WSL_RUNTIME_LOCK
    if not lock_path.is_file():
        raise SystemExit(
            f"WSL runtime lock not found: {lock_path}; refusing to accept an "
            "unverifiable package")
    try:
        lock = json.loads(lock_path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"wsl-runtime.json is not valid JSON: {lock_path}: {exc}") from None
    pinned_size, pinned_sha = wsl_release_lock(lock, lock_path)
    if lock.get("version") != version:
        raise SystemExit(
            f"WSL runtime lock version {lock.get('version')!r} != staged "
            f"{version!r} ({lock_path})")
    if lock.get("archive") != tarball.name:
        raise SystemExit(
            f"WSL runtime lock archive {lock.get('archive')!r} != packaged "
            f"{tarball.name!r} ({lock_path})")
    size = tarball.stat().st_size
    if size < MIN_WSL_RUNTIME_BYTES:
        raise SystemExit(
            f"packaged WSL runtime is only {size} bytes; the payload is a "
            f"placeholder ({tarball})")
    if size != pinned_size:
        raise SystemExit(
            f"packaged WSL runtime size {size} != wsl-runtime.json "
            f"{pinned_size} ({tarball})")
    actual = digest(tarball)
    if actual != pinned_sha:
        raise SystemExit(
            f"packaged WSL runtime SHA256 {actual} != wsl-runtime.json "
            f"{pinned_sha} ({tarball})")
    sidecar = tarball.with_name(tarball.name + ".sha256")
    if not sidecar.is_file() or sidecar.read_text().split()[0].lower() != actual:
        raise SystemExit(
            f"packaged WSL runtime sidecar does not match {actual}: {sidecar}")
    embedded = directory / "resources/runtime/wsl-runtime.json"
    if not embedded.is_file():
        raise SystemExit(f"packaged WSL runtime lock not found: {embedded}")
    embedded_lock = json.loads(embedded.read_text())
    if embedded_lock.get("sha256") != actual or embedded_lock.get("size") != size:
        raise SystemExit(
            "packaged resources/runtime/wsl-runtime.json does not match the "
            "bundled WSL runtime tarball")
    verify_wsl_archive_layout(tarball, lock)
    verify_packaged_payload(directory, version, stage=stage)
    return {"directory": str(directory), "version": version,
            "binary": {"size": exe_size, "sha256": digest(binary)},
            "wsl_runtime": {"archive": tarball.name, "size": size,
                            "sha256": actual},
            "lock": str(lock_path)}


def verify_directory(directory):
    """Re-hash a built directory package against its own anchors."""
    directory = Path(directory).resolve()
    manifest = read_build_manifest(directory)
    problems = []
    for relative, expected in sorted(manifest["files"].items()):
        path = directory / relative
        if not path.is_file():
            problems.append(f"missing file: {relative}")
        elif digest(path) != expected:
            problems.append(f"modified file: {relative}")
    if problems:
        raise SystemExit("package integrity check failed: " + "; ".join(problems[:5]))
    if (manifest.get("platform") or {}).get("system") == "windows":
        return verify_windows_directory(directory, manifest)
    embedded = json.loads((directory / "resources/runtime/runtime-lock.json").read_text())
    if embedded != manifest.get("runtime"):
        raise SystemExit("embedded runtime-lock.json differs from BUILD-MANIFEST runtime")
    info = json.loads((directory / "resources/runtime/runtime-info.json").read_text())
    runtime = manifest.get("runtime") or {}
    if list(info["python_major_minor"]) != runtime.get("python_major_minor") \
            or info.get("machine") != runtime.get("machine"):
        raise SystemExit("runtime-info.json ABI differs from BUILD-MANIFEST runtime")
    distributions = (runtime.get("distributions") or {})
    expected = collect_license_manifest(directory, distributions)
    if manifest.get("license_manifest") != expected:
        raise SystemExit("license manifest does not match the files in the package")
    return {"files": len(manifest["files"]), "distributions": len(distributions),
            "electron": manifest.get("electron"), "version": json.loads(
                (directory / "RELEASE-MANIFEST.json").read_text())["version"]}


def build_linux(args):
    installed = dependencies()
    current = runtime_lock(installed)
    if args.lock_runtime:
        LOCK.write_text(json.dumps(current, indent=2) + "\n")
        print("Updated runtime lock; review version and payload changes before building.")
        return
    if current != json.loads(LOCK.read_text()):
        raise SystemExit("runtime build inputs differ from reviewed lock")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise SystemExit("only Linux x86_64 directory packages have been validated")
    electron = ROOT / "apps/desktop/node_modules/electron/dist"
    if (
        not (electron / "electron").is_file()
        or (electron / "version").read_text().strip().lstrip("v") != "44.3.0"
    ):
        raise SystemExit("install pinned Electron 44.3.0 with checksum verification first")
    electron_zip = args.electron_zip or ELECTRON_ZIP
    if electron_zip.is_file():
        verify_electron_zip(electron_zip)
    else:
        print("warning: pinned Electron archive not present; extracted tree is not "
              "checksum-verified against packaging/arch/electron-lock.json", file=sys.stderr)
    ui = ROOT / "apps/web/dist"
    if not (ui / "index.html").is_file():
        raise SystemExit("build the shared Vue UI first: npm run web:build")
    output = args.output.resolve()
    # A dedicated output directory avoids erasing unrelated workspace artifacts.
    output.mkdir(parents=True, exist_ok=True)
    stage = output / f"deepdocparse-{args.version}-{PLATFORM}"
    if stage.exists():
        if stage.is_symlink():
            raise SystemExit("unsafe output directory")
        shutil.rmtree(stage)
    stage.mkdir()
    copy_tree(electron, stage)
    (stage / "electron").rename(stage / "deepdocparse")
    # Electron default_app is sample code; the only packaged app is our shared workbench.
    (stage / "resources/default_app.asar").unlink(missing_ok=True)
    application = stage / "resources/app"
    application.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "apps/desktop/package.json", application / "package.json")
    copy_tree(ROOT / "apps/desktop/src", application / "src")
    copy_tree(ROOT / "packages/client-runtime/src", stage / "resources/client-runtime")
    (stage / "resources/client-runtime/package.json").write_text(
        '{"private":true,"type":"module"}\n'
    )
    copy_tree(ui, application / "ui")
    runtime = stage / "resources/runtime"
    site = runtime / "site-packages"
    site.mkdir(parents=True)
    for installed_distribution in installed.values():
        for relative, original in payload(installed_distribution):
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    for name in ["ddp_contracts", "ddp_core", "ddp_local"]:
        copy_tree(ROOT / "python" / name / name, site / name)
    python_abi = {
        "major_minor": list(sys.version_info[:2]),
        "cache_tag": sys.implementation.cache_tag,
        "soabi": sysconfig.get_config_var("SOABI"),
        "machine": platform.machine(),
    }
    # runtime-info.json keeps the key names the packaged launcher reads.
    (runtime / "runtime-info.json").write_text(json.dumps({
        "python_major_minor": python_abi["major_minor"],
        "cache_tag": python_abi["cache_tag"],
        "soabi": python_abi["soabi"],
        "machine": python_abi["machine"],
    }, indent=2) + "\n")
    shutil.copy2(LOCK, runtime / "runtime-lock.json")
    copy_tree(ROOT / "packaging/arch/licenses", stage / "runtime-licenses")
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE.DeepDocParse")
    epoch = int(
        os.environ.get("SOURCE_DATE_EPOCH", "1789257600")
    )  # 2026-09-13 UTC; caller may pin release time.
    release = {
        "format": 1,
        "name": "deepdocparse-desktop",
        "version": args.version,
        "epoch": epoch,
        "platform": {"system": "linux", "machine": platform.machine()},
        "python": python_abi,
        "electron": "44.3.0",
        "runtime_lock_sha256": digest(LOCK),
    }
    (stage / "RELEASE-MANIFEST.json").write_text(json.dumps(release, indent=2) + "\n")
    license_manifest = collect_license_manifest(stage, current["distributions"])
    manifest = {
        file.relative_to(stage).as_posix(): digest(file)
        for file in sorted(stage.rglob("*"))
        if file.is_file()
    }
    (stage / "BUILD-MANIFEST.json").write_text(
        json.dumps(
            {
                "format": 1,
                "epoch": epoch,
                "electron": "44.3.0",
                "runtime": current,
                "license_manifest": license_manifest,
                "files": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    for file in stage.rglob("*"):
        os.utime(file, (epoch, epoch))
    archive = output / (stage.name + ".tar.gz")
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for file in [stage, *sorted(stage.rglob("*"))]:
                entry = tar.gettarinfo(str(file), arcname=str(file.relative_to(output)))
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                entry.mtime = epoch
                with file.open("rb") if file.is_file() else open(os.devnull, "rb") as content:
                    tar.addfile(entry, content if file.is_file() else None)
    archive_sha = digest(archive)
    (output / (archive.name + ".sha256")).write_text(f"{archive_sha}  {archive.name}\n")
    release_manifest = dict(
        release,
        archive=archive.name,
        size=archive.stat().st_size,
        sha256=archive_sha,
        build_manifest_sha256=digest(stage / "BUILD-MANIFEST.json"),
        signature=None,
    )
    (output / (stage.name + ".release.json")).write_text(
        json.dumps(release_manifest, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "directory": str(stage),
                "archive": str(archive),
                "sha256": archive_sha,
                "release_manifest": str(output / (stage.name + ".release.json")),
                "files": len(manifest),
                "python_abi": python_abi,
                "licenses": {
                    "electron": sorted(license_manifest["electron"]),
                    "distributions": len(license_manifest["distributions"]),
                },
            }
        )
    )


def build_windows(args):
    """Assemble the win32-x64 host directory package plus a deterministic zip.

    The host carries no native Python: the only runtime payload is the
    W3-built WSL runtime tarball, pinned here by its `.sha256` sidecar and
    `wsl-runtime.json`. The release manifest therefore declares platform
    windows/amd64 while `python` stays the WSL runtime's Linux ABI.
    """
    if args.lock_runtime:
        raise SystemExit(
            "the Windows package has no native Python runtime lock; the WSL "
            "runtime is built by scripts/build_wsl_runtime.py")
    electron_zip = args.electron_zip or WINDOWS_ELECTRON_ZIP
    verify_electron_zip(electron_zip, WINDOWS_LOCK)
    wsl = verify_wsl_runtime(
        args.wsl_runtime or (
            WSL_RUNTIME_DIR / (WSL_RUNTIME.format(version=args.version) + ".tar.gz")),
        args.wsl_lock,
        expected_version=args.version)
    ui = ROOT / "apps/web/dist"
    if not (ui / "index.html").is_file():
        raise SystemExit("build the shared Vue UI first: npm run web:build")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = output / f"deepdocparse-{args.version}-{WINDOWS_PLATFORM}"
    if stage.exists():
        if stage.is_symlink():
            raise SystemExit("unsafe output directory")
        shutil.rmtree(stage)
    stage.mkdir()
    with tempfile.TemporaryDirectory(prefix="ddp-electron-win-") as work:
        extracted = Path(work) / "electron"
        extract_zip(electron_zip, extracted)
        copy_tree(extracted, stage)
    binary = stage / "electron.exe"
    if not binary.is_file():
        raise SystemExit("pinned Electron archive has no electron.exe")
    binary.rename(stage / "deepdocparse.exe")
    # Electron default_app is sample code; the only packaged app is our shared workbench.
    (stage / "resources/default_app.asar").unlink(missing_ok=True)
    application = stage / "resources/app"
    application.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "apps/desktop/package.json", application / "package.json")
    copy_tree(ROOT / "apps/desktop/src", application / "src")
    copy_tree(ROOT / "packages/client-runtime/src", stage / "resources/client-runtime")
    (stage / "resources/client-runtime/package.json").write_text(
        '{"private":true,"type":"module"}\n'
    )
    copy_tree(ui, application / "ui")
    runtime = stage / "resources/runtime"
    runtime.mkdir(parents=True)
    abi = wsl["python"]
    # runtime-info.json keeps the key names the packaged launcher reads; for a
    # Windows host this is the WSL runtime's Linux ABI, never a Windows one.
    (runtime / "runtime-info.json").write_text(json.dumps({
        "python_major_minor": abi["major_minor"],
        "cache_tag": abi["cache_tag"],
        "soabi": abi.get("soabi"),
        "machine": abi["machine"],
    }, indent=2) + "\n")
    (runtime / "wsl-runtime.json").write_text(
        json.dumps(wsl["lock"], indent=2) + "\n")
    shutil.copy2(wsl["path"], stage / "resources" / wsl["archive"])
    shutil.copy2(wsl["sidecar"], stage / "resources" / wsl["sidecar"].name)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE.DeepDocParse")
    epoch = int(
        os.environ.get("SOURCE_DATE_EPOCH", "1789257600")
    )  # 2026-09-13 UTC; caller may pin release time.
    release = {
        "format": 1,
        "name": "deepdocparse-desktop",
        "version": args.version,
        "epoch": epoch,
        "platform": {"system": "windows", "machine": "amd64"},
        "python": abi,
        "electron": "44.3.0",
        "runtime_lock_sha256": digest(runtime / "wsl-runtime.json"),
        "wsl_runtime": {"archive": wsl["archive"], "size": wsl["size"],
                        "sha256": wsl["sha256"]},
    }
    (stage / "RELEASE-MANIFEST.json").write_text(json.dumps(release, indent=2) + "\n")
    license_manifest = collect_license_manifest(stage, {})
    manifest = {
        file.relative_to(stage).as_posix(): digest(file)
        for file in sorted(stage.rglob("*"))
        if file.is_file()
    }
    (stage / "BUILD-MANIFEST.json").write_text(
        json.dumps(
            {
                "format": 1,
                "epoch": epoch,
                "electron": "44.3.0",
                "platform": {"system": "windows", "machine": "amd64"},
                "runtime": wsl["lock"],
                "license_manifest": license_manifest,
                "files": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    for file in stage.rglob("*"):
        os.utime(file, (epoch, epoch))
    archive = output / (stage.name + ".zip")
    write_deterministic_zip(stage, archive, epoch)
    archive_sha = digest(archive)
    (output / (archive.name + ".sha256")).write_text(f"{archive_sha}  {archive.name}\n")
    release_manifest = dict(
        release,
        archive=archive.name,
        size=archive.stat().st_size,
        sha256=archive_sha,
        build_manifest_sha256=digest(stage / "BUILD-MANIFEST.json"),
        signature=None,
    )
    (output / (stage.name + ".release.json")).write_text(
        json.dumps(release_manifest, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "directory": str(stage),
                "archive": str(archive),
                "sha256": archive_sha,
                "release_manifest": str(output / (stage.name + ".release.json")),
                "files": len(manifest),
                "python_abi": abi,
                "platform": WINDOWS_PLATFORM,
                "wsl_runtime": {"archive": wsl["archive"], "size": wsl["size"],
                                "sha256": wsl["sha256"]},
                "licenses": {
                    "electron": sorted(license_manifest["electron"]),
                    "distributions": len(license_manifest["distributions"]),
                },
            }
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock-runtime", action="store_true",
        help="explicitly review/refresh dependency lock (Linux packages only)"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "dist/desktop")
    parser.add_argument("--version", default=VERSION,
                        help="release version (drives stage, marker and manifest names)")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default=PLATFORM,
                        help="target package platform (default: linux-x64)")
    parser.add_argument("--electron-zip", type=Path, default=None,
                        help="pinned Electron download to verify (default: artifacts cache)")
    parser.add_argument("--wsl-runtime", type=Path, default=None,
                        help="W3-built WSL runtime tarball (win32-x64 only; default: "
                             "dist/wsl/deepdocparse-wsl-runtime-<version>-linux-x64.tar.gz)")
    parser.add_argument("--wsl-lock", type=Path, default=None,
                        help="W3 release manifest pinning the WSL runtime (default: "
                             "wsl-runtime.json next to the tarball, i.e. "
                             "dist/wsl/wsl-runtime.json)")
    parser.add_argument("--verify", type=Path, default=None,
                        help="verify an already-built directory package (or an "
                             "electron-builder win output directory such as "
                             "dist/desktop/windows/win-unpacked) and exit")
    parser.add_argument("--stage", type=Path, default=None,
                        help="assembled stage to diff a packaged output against "
                             "(default: the stage next to it, then repo sources)")
    parser.add_argument("--verify-electron", action="store_true",
                        help="verify only the Electron download and exit")
    args = parser.parse_args(argv)
    if args.verify_electron:
        target = PLATFORMS[args.platform]
        verified = verify_electron_zip(args.electron_zip or target["electron_zip"],
                                       target["electron_lock"])
        print(json.dumps({"electron": verified}, indent=2))
        return
    if args.verify is not None:
        target = Path(args.verify)
        # The assembled stage and electron-builder's win-unpacked share the
        # exe + resources/app layout; only the stage carries BUILD-MANIFEST.
        if not (target / "BUILD-MANIFEST.json").is_file() \
                and (target / "deepdocparse.exe").is_file() \
                and (target / "resources/app").is_dir():
            print(json.dumps(
                verify_packaged_windows(target, args.wsl_lock,
                                        stage=args.stage), indent=2))
        else:
            print(json.dumps(verify_directory(target), indent=2))
        return
    if args.platform == WINDOWS_PLATFORM:
        build_windows(args)
    else:
        build_linux(args)


if __name__ == "__main__":
    main()
