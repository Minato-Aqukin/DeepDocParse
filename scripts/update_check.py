#!/usr/bin/env python3
"""Update/rollback for the DeepDocParse desktop directory package.

Electron has no autoUpdater on Linux (the Squirrel/Windows and Mac paths do not
exist here), so this script is the whole update channel: a versioned release
manifest, a checksum + optional detached-signature check, a pre-upgrade backup
of the installed tree, an atomic swap, and an explicit rollback command.

It runs on both Linux (tar.gz packages, native Python ABI) and Windows (zip
packages, no native Python: the release manifest's `python` field describes the
bundled WSL runtime and the readiness binary is `deepdocparse.exe`). A manifest
built for one system is refused on the other.

It is deliberately stdlib-only so it runs under the Arch system Python that the
Linux package itself targets.

Commands
--------
    status   --root DIR
    verify   --root DIR --manifest FILE --archive FILE
             [--allowed-signers FILE] [--allow-unsigned] [--allow-downgrade]
    apply    (verify options) [--models DIR] [--backup-dir DIR] [--force-stale]
    rollback --root DIR
    sign     --key FILE --manifest FILE [--output FILE]

Exit codes: 0 accepted, 1 rejected or failed, 2 usage error.  Downgrades are
rejected unless --allow-downgrade is passed, in which case a warning is printed
and the command succeeds.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

FORMAT = 1
NAME = "deepdocparse-desktop"
SIGN_NAMESPACE = "ddp-desktop-release"
MARKER = "RELEASE-MANIFEST.json"
BUILD_MANIFEST = "BUILD-MANIFEST.json"
VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")

# `platform.machine()` is `AMD64` on Windows and `x86_64`/`aarch64` elsewhere.
# A Windows release runs no native Python: its manifest `python` block is the
# Linux ABI of the bundled WSL runtime, so it maps to the host architecture
# through WINDOWS_TO_LINUX instead of comparing interpreter ABIs.
WINDOWS_MACHINES = {"amd64"}
LINUX_MACHINES = {"x86_64", "aarch64"}
WINDOWS_TO_LINUX = {"amd64": "x86_64", "arm64": "aarch64"}


class Rejected(Exception):
    """The candidate is refused.  The message is user-facing."""


# --------------------------------------------------------------------- helpers

def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_version(value) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise Rejected(f"unknown version {value!r}: expected MAJOR.MINOR.PATCH")
    match = VERSION_RE.match(value.strip())
    if not match:
        raise Rejected(f"unknown version {value!r}: expected MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def load_json(path: Path, what: str) -> dict:
    try:
        body = json.loads(path.read_text())
    except FileNotFoundError:
        raise Rejected(f"{what} not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise Rejected(f"{what} is not valid JSON: {path}: {exc}") from None
    if not isinstance(body, dict):
        raise Rejected(f"{what} must be a JSON object: {path}")
    return body


def tree_digest(root: Path) -> str:
    """Deterministic digest of a directory (sorted paths + file contents)."""
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        entries.append(f"{path.relative_to(root).as_posix()}:{digest(path)}\n")
    return hashlib.sha256("".join(entries).encode()).hexdigest()


def marker_of(tree: Path) -> dict:
    return load_json(tree / MARKER, f"installed {MARKER}")


def host_system() -> str:
    """Canonical OS name: `linux` or `windows`."""
    return platform.system().lower()


def normalize_windows_machine(value) -> str:
    """Normalize a Windows architecture string (`AMD64` -> `amd64`)."""
    machine = str(value).strip().lower()
    return {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64",
            "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)


def host_machine(system: str | None = None) -> str:
    """Canonical host machine; Windows strings are normalized to `amd64`."""
    machine = platform.machine()
    if (system or host_system()) == "windows":
        return normalize_windows_machine(machine)
    return machine


def host_abi() -> dict:
    return {"major_minor": list(sys.version_info[:2]),
            "cache_tag": sys.implementation.cache_tag,
            "machine": host_machine()}


def binary_name() -> str:
    """The installed host binary: `deepdocparse.exe` on Windows."""
    return "deepdocparse.exe" if host_system() == "windows" else "deepdocparse"


# ------------------------------------------------------------------- manifest

def validate_manifest(manifest: dict) -> dict:
    if manifest.get("format") != FORMAT:
        raise Rejected(f"unsupported release manifest format: {manifest.get('format')!r}")
    if manifest.get("name") != NAME:
        raise Rejected(f"release manifest is for {manifest.get('name')!r}, expected {NAME!r}")
    parse_version(manifest.get("version"))
    archive = manifest.get("archive")
    if not isinstance(archive, str) or not archive or "/" in archive or archive in {".", ".."}:
        raise Rejected("release manifest has no plain archive file name")
    if not isinstance(manifest.get("sha256"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise Rejected("release manifest has no sha256 of the archive")
    if type(manifest.get("size")) is not int or manifest["size"] <= 0:
        raise Rejected("release manifest has no archive size")
    platform_ = manifest.get("platform")
    if not isinstance(platform_, dict) or not isinstance(platform_.get("machine"), str):
        raise Rejected("release manifest declares an unsupported platform")
    system = platform_.get("system")
    if system == "linux":
        if platform_["machine"] not in LINUX_MACHINES:
            raise Rejected("release manifest declares an unsupported platform")
    elif system == "windows":
        platform_["machine"] = normalize_windows_machine(platform_["machine"])
        if platform_["machine"] not in WINDOWS_MACHINES:
            raise Rejected("release manifest declares an unsupported platform")
    else:
        raise Rejected("release manifest declares an unsupported platform")
    python_ = manifest.get("python")
    if not isinstance(python_, dict) or not isinstance(python_.get("major_minor"), list) \
            or len(python_["major_minor"]) != 2 or python_.get("machine") not in LINUX_MACHINES:
        raise Rejected("release manifest has no complete python ABI")
    if system == "linux":
        if python_["machine"] != platform_["machine"]:
            raise Rejected("release manifest platform and python ABI disagree on the machine")
    elif WINDOWS_TO_LINUX.get(platform_["machine"]) != python_["machine"]:
        raise Rejected("release manifest platform and python ABI disagree on the machine")
    if not isinstance(manifest.get("electron"), str):
        raise Rejected("release manifest has no Electron version")
    if not isinstance(manifest.get("build_manifest_sha256"), str) \
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["build_manifest_sha256"]):
        raise Rejected("release manifest has no BUILD-MANIFEST.json digest")
    return manifest


def signature_payload(manifest_path: Path) -> bytes:
    """Canonical bytes that the detached signature covers.

    The manifest carries the signature block, so the payload must exclude it;
    otherwise verifying would re-sign the field we just added.
    """
    body = load_json(manifest_path, "release manifest")
    body.pop("signature", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def check_signature(manifest_path: Path, allowed_signers: Path | None,
                    allow_unsigned: bool) -> list[str]:
    warnings: list[str] = []
    payload = signature_payload(manifest_path)
    signature = load_json(manifest_path, "release manifest").get("signature")
    if signature is None:
        if allowed_signers is not None:
            raise Rejected("a signer allowlist was given but the manifest carries no signature")
        if not allow_unsigned:
            raise Rejected(
                "release manifest is unsigned; pass --allowed-signers with a signed "
                "manifest, or --allow-unsigned to accept the risk explicitly")
        warnings.append("UNSIGNED release manifest accepted because --allow-unsigned was given")
        return warnings
    if allowed_signers is None:
        raise Rejected("signed release manifest requires --allowed-signers")
    if not isinstance(signature, dict) \
            or signature.get("scheme") != "ssh-ed25519" \
            or signature.get("namespace") != SIGN_NAMESPACE \
            or not isinstance(signature.get("signer"), str) or not signature["signer"]:
        raise Rejected("release manifest signature block is malformed")
    signature_file = Path(signature.get("file") or f"{manifest_path.name}.sig")
    if not signature_file.is_absolute():
        signature_file = manifest_path.parent / signature_file
    if not signature_file.is_file():
        raise Rejected(f"detached signature not found: {signature_file}")
    if not shutil.which("ssh-keygen"):
        raise Rejected("ssh-keygen is required to verify ssh-ed25519 signatures")
    process = subprocess.run(
        ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers),
         "-I", signature["signer"], "-n", SIGN_NAMESPACE, "-s", str(signature_file)],
        input=payload, capture_output=True)
    if process.returncode != 0:
        detail = process.stderr.decode(errors="replace").strip() or "signature mismatch"
        raise Rejected(f"release manifest signature verification failed: {detail}")
    return warnings


def archive_kind(archive: Path) -> str:
    """`zip` for the Windows portable archive, `tar` for the Linux one."""
    name = archive.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar"
    raise Rejected(f"unsupported archive type: {archive.name}")


def archive_members(archive: Path):
    """Yield `(name, kind, read)` for every archive member.

    `kind` is `file`, `dir`, `link` or `device`; `read()` returns the bytes of
    a regular file. Links and devices are surfaced, never followed.
    """
    archive = Path(archive)
    if archive_kind(archive) == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    member_kind = "link"
                elif stat.S_ISDIR(mode) or info.is_dir():
                    member_kind = "dir"
                else:
                    member_kind = "file"
                yield (info.filename.replace("\\", "/"), member_kind,
                       lambda info=info: zf.read(info))
    else:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if member.isfile():
                    member_kind = "file"
                elif member.isdir():
                    member_kind = "dir"
                elif member.issym() or member.islnk():
                    member_kind = "link"
                else:
                    member_kind = "device" if member.isdev() else "other"

                def read(member=member, tar=tar):
                    stream = tar.extractfile(member)
                    return stream.read() if stream is not None else b""

                yield member.name, member_kind, read


def extract_anchor(archive: Path, name: str) -> tuple[bytes, dict]:
    """Read one anchor file from the archive without unpacking the tree."""
    for member_name, member_kind, read in archive_members(archive):
        if member_kind != "file":
            continue
        parts = Path(member_name).parts
        if parts and parts[-1] == name and len(parts) <= 3:
            body = read()
            try:
                return body, json.loads(body)
            except ValueError:
                raise Rejected(f"{name} inside the archive is not valid JSON") from None
    raise Rejected(f"{name} is missing from the archive")


def check_license_manifest(archive: Path, build: dict) -> None:
    """Every file in the manifest's license section must exist, unchanged."""
    section = build.get("license_manifest")
    if not isinstance(section, dict):
        raise Rejected("BUILD-MANIFEST.json has no license_manifest section")
    expected: dict[str, str] = {}
    for relative, sha in (section.get("electron") or {}).items():
        expected[relative] = sha
    for name, files in (section.get("distributions") or {}).items():
        if not isinstance(files, dict) or not files:
            raise Rejected(f"license_manifest has no notice files for {name}")
        expected.update(files)
    distributions = (build.get("runtime") or {}).get("distributions") or {}
    for name in distributions:
        if name not in (section.get("distributions") or {}):
            raise Rejected(f"license_manifest is missing dependency {name}")
    found: dict[str, str] = {}
    for member_name, member_kind, read in archive_members(archive):
        if member_kind != "file":
            continue
        parts = Path(member_name).parts
        if len(parts) < 2:
            continue
        relative = "/".join(parts[1:])
        if relative not in expected:
            continue
        found[relative] = hashlib.sha256(read()).hexdigest()
    missing = sorted(set(expected) - set(found))
    if missing:
        raise Rejected("license manifest is incomplete: missing " + ", ".join(missing[:5]))
    changed = sorted(relative for relative, sha in expected.items() if found[relative] != sha)
    if changed:
        raise Rejected("license notices changed: " + ", ".join(changed[:5]))


def check_archive(manifest: dict, archive: Path) -> tuple[dict, dict]:
    if not archive.is_file():
        raise Rejected(f"archive not found: {archive}")
    if archive.name != manifest["archive"]:
        raise Rejected(
            f"archive name {archive.name!r} does not match the manifest {manifest['archive']!r}")
    if archive.stat().st_size != manifest["size"]:
        raise Rejected(
            f"archive size {archive.stat().st_size} does not match the manifest "
            f"{manifest['size']}")
    actual = digest(archive)
    if actual != manifest["sha256"]:
        raise Rejected(
            f"CHECKSUM MISMATCH for {archive.name}: archive is {actual}, "
            f"manifest says {manifest['sha256']}")
    build_bytes, build = extract_anchor(archive, BUILD_MANIFEST)
    if hashlib.sha256(build_bytes).hexdigest() != manifest["build_manifest_sha256"]:
        raise Rejected("BUILD-MANIFEST.json inside the archive does not match the manifest digest")
    if build.get("electron") != manifest["electron"]:
        raise Rejected("archive and manifest disagree on the Electron version")
    _, embedded = extract_anchor(archive, MARKER)
    if embedded.get("version") != manifest["version"]:
        raise Rejected(
            f"archive contains version {embedded.get('version')!r} but the manifest says "
            f"{manifest['version']!r}")
    if embedded.get("python") != manifest["python"]:
        raise Rejected("archive and manifest disagree on the python ABI")
    check_license_manifest(archive, build)
    return build, embedded


def check_compatibility(manifest: dict, root: Path | None) -> list[str]:
    warnings: list[str] = []
    host = host_abi()
    system = manifest["platform"]["system"]
    if system != host_system():
        raise Rejected(f"candidate is built for {system}, host is {host_system()}")
    if manifest["platform"]["machine"] != host["machine"]:
        raise Rejected(
            f"candidate is built for {manifest['platform']['machine']}, "
            f"host is {host['machine']}")
    if system == "windows":
        # The host is Windows: the manifest python block describes the bundled
        # WSL runtime, so only the architecture mapping is meaningful here.
        if WINDOWS_TO_LINUX.get(host["machine"]) != manifest["python"]["machine"]:
            raise Rejected(
                f"candidate WSL runtime is built for {manifest['python']['machine']}, "
                f"host is {manifest['platform']['machine']}")
    elif manifest["python"]["major_minor"] != host["major_minor"] \
            or manifest["python"].get("cache_tag") != host["cache_tag"]:
        raise Rejected(
            f"candidate Python ABI {manifest['python']} is not compatible with this host "
            f"{host}; rebuild the artifact for this interpreter")
    if root is not None and (root / MARKER).is_file():
        current = marker_of(root)
        if current.get("python") != manifest["python"]:
            raise Rejected("candidate Python ABI differs from the installed release")
        current_version = parse_version(current.get("version"))
        candidate_version = parse_version(manifest["version"])
        if candidate_version == current_version:
            raise Rejected(f"version {manifest['version']} is already installed")
        if candidate_version < current_version:
            warnings.append(
                f"DOWNGRADE: {current['version']} -> {manifest['version']} "
                "(allowed explicitly with --allow-downgrade)")
    return warnings


def read_candidate(manifest_path: Path, archive: Path, root: Path | None,
                   allowed_signers: Path | None, allow_unsigned: bool,
                   allow_downgrade: bool) -> tuple[dict, list[str]]:
    warnings = check_signature(manifest_path, allowed_signers, allow_unsigned)
    manifest = validate_manifest(load_json(manifest_path, "release manifest"))
    check_archive(manifest, archive)
    warnings += check_compatibility(manifest, root)
    if any(warning.startswith("DOWNGRADE") for warning in warnings) and not allow_downgrade:
        raise Rejected(warnings[-1] + " -- refusing; pass --allow-downgrade to override")
    return manifest, warnings


# --------------------------------------------------------------- tree checks

def verify_tree(directory: Path) -> dict:
    """Re-hash an extracted package against its own BUILD-MANIFEST.json.

    **Extra files are rejected too.** The manifest lists every packaged file;
    anything else in the tree was smuggled in after the manifest was computed
    (an archive can be internally self-consistent and still carry a file no
    notice covers). The single documented allowance is `BUILD-MANIFEST.json`
    itself, which the builder deliberately keeps out of its own `files` map.
    """
    build = load_json(directory / BUILD_MANIFEST, BUILD_MANIFEST)
    if build.get("format") != 1 or not isinstance(build.get("files"), dict):
        raise Rejected("BUILD-MANIFEST.json has an unsupported shape")
    problems = []
    declared = set(build["files"])
    for relative, expected in sorted(build["files"].items()):
        path = directory / relative
        if not path.is_file():
            problems.append(f"missing file: {relative}")
        elif digest(path) != expected:
            problems.append(f"modified file: {relative}")
    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative == BUILD_MANIFEST or relative in declared:
            continue
        problems.append(f"unmanifested {'symlink' if path.is_symlink() else 'file'}: "
                        f"{relative}")
    if problems:
        raise Rejected("package integrity check failed: " + "; ".join(problems[:5]))
    return build


def _copy_verified_archive(archive: Path, manifest: dict, directory: Path) -> Path:
    """Copy the archive into a private directory while hashing it.

    The release archive was hashed once in `check_archive`; extraction must not
    re-open the path, or bytes swapped in between verification and extraction
    would be installed under the verified manifest (TOCTOU). The copy made here
    is what `_restore_tree` extracts, and its digest must equal the manifest's
    sha256/size — a self-consistent replacement archive fails on the hash even
    though its own BUILD-MANIFEST would validate.
    """
    target = directory / manifest["archive"]
    hasher = hashlib.sha256()
    size = 0
    with archive.open("rb") as source, target.open("wb") as sink:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
            sink.write(chunk)
            size += len(chunk)
    if size != manifest["size"] or hasher.hexdigest() != manifest["sha256"]:
        raise Rejected(
            "archive changed between verification and extraction; refusing to apply")
    return target


# ------------------------------------------------------------------- commands

def command_status(args) -> int:
    root = Path(args.root).resolve()
    previous = root.with_name(root.name + ".previous")
    rolled = sorted(root.parent.glob(root.name + ".rolledback-*"))
    result: dict = {"root": str(root), "exists": root.is_dir(), "runnable": False,
                    "interrupted": False, "current": None, "previous": None,
                    "rollback_copies": [str(path) for path in rolled],
                    "staging": [str(path) for path in
                                sorted(root.parent.glob(root.name + ".staging-*"))]}
    active = root if root.is_dir() else (previous if previous.is_dir() else None)
    if active is not None:
        binary = active / binary_name()
        result["runnable"] = binary.is_file() and os.access(binary, os.X_OK)
        if (active / MARKER).is_file():
            marker = marker_of(active)
            result["current" if active == root else "previous"] = {
                "version": marker.get("version"), "python": marker.get("python")}
    if previous.is_dir():
        if root.is_dir():
            if (previous / MARKER).is_file():
                result["previous"] = {"version": marker_of(previous).get("version")}
        else:
            result["interrupted"] = True
    if not root.is_dir() and previous.is_dir():
        result["runnable"] = (previous / binary_name()).is_file() \
            and os.access(previous / binary_name(), os.X_OK)
    print(json.dumps(result, indent=2))
    return 0 if active is not None else 1


def command_verify(args) -> int:
    root = Path(args.root).resolve() if args.root else None
    manifest, warnings = read_candidate(
        Path(args.manifest).resolve(), Path(args.archive).resolve(), root,
        Path(args.allowed_signers).resolve() if args.allowed_signers else None,
        args.allow_unsigned, args.allow_downgrade)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(json.dumps({"accepted": True, "version": manifest["version"],
                      "archive": str(Path(args.archive).resolve()),
                      "warnings": warnings}, indent=2))
    return 0


def _strip_top(parts: tuple[str, ...], strip: bool) -> str | None:
    """Drop the single wrapper directory; None means the member is that root."""
    if strip:
        if len(parts) == 1:
            return None
        return "/".join(parts[1:])
    return "/".join(parts)


def _restore_zip(archive: Path, target: Path) -> None:
    """Safe zip extraction: no links, no traversal, one wrapper directory."""
    root = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        tops = {Path(info.filename.replace("\\", "/")).parts[0]
                for info in infos if info.filename}
        strip = len(tops) == 1
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if info.filename.startswith("/") or ".." in parts:
                raise Rejected(f"unsafe path in archive: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise Rejected(
                    f"link or device in archive is not allowed: {info.filename}")
            name = _strip_top(parts, strip)
            if name is None:
                continue
            destination = (target / name).resolve()
            if not destination.is_relative_to(root):
                raise Rejected(f"unsafe path in archive: {info.filename}")
            if info.is_dir() or stat.S_ISDIR(mode):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            permissions = mode & 0o7777
            if permissions:
                os.chmod(destination, permissions)


def _restore_tree(archive: Path, target: Path) -> None:
    """Extract the package, stripping the single top-level stage directory.

    The release archive wraps everything in ``deepdocparse-<version>-<platform>/``
    exactly like the directory artifact; the installed root holds its contents.
    Windows releases are zip archives, Linux releases tar.gz.
    """
    target.mkdir(parents=True)
    if archive_kind(archive) == "zip":
        _restore_zip(archive, target)
        return
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        tops = {Path(member.name).parts[0] for member in members if member.name}
        strip = len(tops) == 1
        kept = []
        for member in members:
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise Rejected(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise Rejected(f"link or device in archive is not allowed: {member.name}")
            if strip:
                if len(parts) == 1:
                    continue
                member.name = "/".join(parts[1:])
            kept.append(member)
        tar.extractall(target, members=kept, filter="data")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def command_apply(args) -> int:
    root = Path(args.root).resolve()
    manifest, warnings = read_candidate(
        Path(args.manifest).resolve(), Path(args.archive).resolve(), root,
        Path(args.allowed_signers).resolve() if args.allowed_signers else None,
        args.allow_unsigned, args.allow_downgrade)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    models = Path(args.models).resolve() if args.models else None
    if models is not None:
        if models == root or root in models.parents or models in root.parents:
            raise Rejected("the model cache must live outside the application tree")
        if not models.is_dir():
            raise Rejected(f"model cache does not exist: {models}")
    models_before = tree_digest(models) if models else None

    staging = root.with_name(f"{root.name}.staging-{os.getpid()}")
    previous = root.with_name(root.name + ".previous")
    if staging.exists():
        shutil.rmtree(staging)
    if previous.exists():
        if not args.force_stale:
            raise Rejected(
                f"an interrupted update is pending ({previous}); run `rollback` first "
                "or pass --force-stale to discard it")
        shutil.rmtree(previous)
    if not root.is_dir():
        raise Rejected(f"installed tree not found: {root}")

    # Extract **the bytes verified in `read_candidate`**, not the archive path:
    # copy + hash into a private temp dir first, and re-hash after extraction.
    # A self-consistent archive swapped in after verification fails the hash
    # here instead of being installed.
    with tempfile.TemporaryDirectory(prefix="ddp-update-") as work:
        verified_archive = _copy_verified_archive(
            Path(args.archive).resolve(), manifest, Path(work))
        _restore_tree(verified_archive, staging)
        build = verify_tree(staging)
        if (staging / MARKER).is_file():
            if marker_of(staging).get("version") != manifest["version"]:
                raise Rejected("staged tree version does not match the manifest")
        else:
            raise Rejected(f"staged tree has no {MARKER}")
        if digest(verified_archive) != manifest["sha256"]:
            raise Rejected("archive changed while extracting; refusing to apply")

        # **All compatibility/model-cache checks run before the swap.** A
        # refusal after `os.replace` would leave the new tree installed while
        # the command reports failure — the user asked not to apply, and we did.
        if models is not None and tree_digest(models) != models_before:
            raise Rejected("model cache changed during update; refusing to continue")

        # Pre-upgrade backup: the complete old tree stays next to the new one,
        # so a rollback never needs the network or the old archive.
        old_version = marker_of(root).get("version", "unknown")
        if args.backup_dir:
            backup = Path(args.backup_dir).resolve() / f"{root.name}-{old_version}"
            if backup.exists():
                raise Rejected(f"backup already exists: {backup}")
        os.replace(root, previous)
        if args.backup_dir:
            shutil.copytree(previous, backup, symlinks=False)
        os.replace(staging, root)

        final_marker = marker_of(root)
        (root / "UPDATE-STATE.json").write_text(json.dumps({
            "format": 1, "name": NAME, "version": final_marker["version"],
            "python": final_marker.get("python"), "previous": str(previous),
            "archive_sha256": manifest["sha256"], "installed_at": _now(),
            "licenses_checked": sorted(
                (build.get("license_manifest") or {}).get("distributions") or {}),
        }, indent=2) + "\n")

    print(json.dumps({"applied": True, "version": final_marker["version"],
                      "root": str(root), "previous": str(previous),
                      "archive_sha256": manifest["sha256"]}, indent=2))
    return 0


def command_rollback(args) -> int:
    root = Path(args.root).resolve()
    previous = root.with_name(root.name + ".previous")
    if not previous.is_dir():
        raise Rejected(f"no pre-upgrade backup next to {root}; nothing to roll back to")
    kept = None
    if root.is_dir():
        marker = marker_of(root) if (root / MARKER).is_file() else {}
        kept = root.with_name(f"{root.name}.rolledback-{marker.get('version', 'unknown')}")
        if kept.exists():
            shutil.rmtree(kept)
        os.replace(root, kept)
    os.replace(previous, root)
    marker = marker_of(root)
    print(json.dumps({"rolled_back": True, "version": marker.get("version"),
                      "root": str(root), "kept_copy": str(kept) if kept else None},
                     indent=2))
    return 0


def command_sign(args) -> int:
    manifest = Path(args.manifest).resolve()
    key = Path(args.key).resolve()
    output = Path(args.output).resolve() if args.output \
        else manifest.with_name(manifest.name + ".sig")
    with tempfile.TemporaryDirectory() as directory:
        payload_file = Path(directory) / "payload.json"
        payload_file.write_bytes(signature_payload(manifest))
        process = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", SIGN_NAMESPACE,
             str(payload_file)],
            capture_output=True)
        if process.returncode != 0:
            print(process.stderr.decode(errors="replace"), file=sys.stderr)
            return 1
        shutil.move(str(payload_file) + ".sig", output)
    body = json.loads(manifest.read_text())
    body["signature"] = {"scheme": "ssh-ed25519",
                         "signer": args.identity or key.name,
                         "namespace": SIGN_NAMESPACE, "file": output.name}
    manifest.write_text(json.dumps(body, indent=2) + "\n")
    print(json.dumps({"signed": str(manifest), "signature": str(output)}))
    return 0


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show installed / previous / interrupted state")
    status.add_argument("--root", required=True)
    status.set_defaults(func=command_status)

    def verify_options(target):
        target.add_argument("--manifest", required=True)
        target.add_argument("--archive", required=True)
        target.add_argument("--allowed-signers", default="")
        target.add_argument("--allow-unsigned", action="store_true")
        target.add_argument("--allow-downgrade", action="store_true")

    verify = sub.add_parser("verify", help="check checksum, signature, version and ABI")
    verify.add_argument("--root", default="")
    verify_options(verify)
    verify.set_defaults(func=command_verify)

    apply_ = sub.add_parser("apply", help="back up the old tree, then swap in the new one")
    apply_.add_argument("--root", required=True)
    verify_options(apply_)
    apply_.add_argument("--models", default="")
    apply_.add_argument("--backup-dir", default="")
    apply_.add_argument("--force-stale", action="store_true")
    apply_.set_defaults(func=command_apply)

    rollback = sub.add_parser("rollback", help="swap the pre-upgrade backup back in")
    rollback.add_argument("--root", required=True)
    rollback.set_defaults(func=command_rollback)

    sign = sub.add_parser("sign", help="create a detached ssh-ed25519 signature")
    sign.add_argument("--key", required=True)
    sign.add_argument("--manifest", required=True)
    sign.add_argument("--identity", default="")
    sign.add_argument("--output", default="")
    sign.set_defaults(func=command_sign)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Rejected as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
