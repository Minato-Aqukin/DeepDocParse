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
    status    --root DIR [--workspace DIR ...]
    verify    --root DIR --manifest FILE --archive FILE
             [--allowed-signers FILE] [--allow-unsigned] [--allow-downgrade]
    apply     (verify options) [--models DIR] [--backup-dir DIR] [--force-stale]
              [--workspace DIR ...] [--allow-active]
    rollback  --root DIR [--restore-workspaces]
    uninstall --root DIR [--workspace DIR ...] [--backup-dir DIR]
              [--allow-active] [--remove-backups]
    sign      --key FILE --manifest FILE [--output FILE]

Exit codes: 0 accepted, 1 rejected or failed, 2 usage error.  Downgrades are
rejected unless --allow-downgrade is passed, in which case a warning is printed
and the command succeeds.

Workspaces hold the local SQLite databases (`workspace.sqlite3`,
`consents.sqlite3`) outside the application tree. `apply` refuses while any
listed workspace has queued/running tasks, an unverifiable task table, or the
model cache holds partial (`.part`) downloads, unless `--allow-active` is
passed explicitly. With `--workspace`, `--backup-dir` is required: consistent
online copies of both databases land in
`<backup-dir>/<root>-<old-version>-workspaces/` and are recorded in
`UPDATE-STATE.json`. `rollback` restores the application tree only after the
pre-upgrade backup re-proves itself (BUILD-MANIFEST re-hash, versioned marker,
and the recorded `archive_sha256`): a planted or tampered `.previous` tree is
refused before the swap, and the version transition is logged. By default it
keeps the current workspace databases (post-upgrade writes survive a
same-schema rollback); `--restore-workspaces` restores the recorded
pre-upgrade copies after verifying their digests and integrity. `uninstall`
removes only the application tree (plus its own staging leftovers); workspace
databases, the model cache and any process it did not start are never touched.
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
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from contextlib import ExitStack
from functools import wraps
from pathlib import Path


def _workspace_expected_versions():
    """Installer-side readable set, kept in sync with ddp_local at build time.

    stdlib-only by design (it runs under the Arch system Python on the
    target machine), so it cannot import ddp_local here: `build_desktop.py`
    embeds the release-canonical values into RELEASE-MANIFEST.json
    (`workspace_schemas`) from `ddp_local.workspace_schemas` at build time,
    and this file carries the same literal for new-install preflights before
    any release is staged. KEEP IN SYNC with
    `python/ddp_local/ddp_local/workspace_schemas.py::WORKSPACE_SCHEMA_VERSIONS`
    and with the release marker's `workspace_schemas` (drift guard:
    tests/test_desktop_release.py::test_workspace_schema_declaration_matches_stores).
    """
    return {"workspace.sqlite3": {0, 1, 2, 3, 4}, "consents.sqlite3": {0, 1, 2, 3, 4}}

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

# ----------------------------------------------------------------- lifecycle
#
# Workspaces (local SQLite) live outside the application tree:
#   <workspace>/workspace.sqlite3          LocalStore (user_version 0..4, fresh writes 4)
#   <workspace>/consents.sqlite3           ConsentStore (user_version 0..4, fresh writes 4)
#   <workspace>/models/                    ModelInstaller (0700, fd-pinned)
# The installer never touches them except as below. Update flow for one
# workspace:
#   1. preflight: refuse on queued/running tasks (LocalStore.tasks rows with
#      status queued/running, or generation tasks running past lease), on an
#      unreadable schema, or on partial model downloads (`*.part`), unless
#      --allow-active is passed explicitly by the operator;
#   2. consistent online backup: SQLite backup API copies workspace.sqlite3
#      and consents.sqlite3 (WAL-safe; never a file copy) plus per-file
#      sha256 into <backup-dir>/<root>-<old-version>-workspaces/;
#   3. after the atomic tree swap, rollback keeps current workspace files by
#      default; --restore-workspaces restores the recorded copies after
#      digest + integrity verification.
#
# Old binaries never understand new schemas, so a downgrade-shaped rollback
# only restores workspace databases when their user_version pair matches the
# current on-disk pair; otherwise it keeps the current databases and says so.

WORKSPACE_DATABASES = ("workspace.sqlite3", "consents.sqlite3")
WORKSPACE_EXPECTED_VERSIONS = _workspace_expected_versions()


def _resolve_workspaces(values) -> list[Path]:
    seen: list[Path] = []
    for value in values or []:
        path = Path(value).absolute()
        if path.is_symlink():
            raise Rejected(f"workspace root is a symlink: {path}")
        path = path.resolve()
        if path not in seen:
            seen.append(path)
    return seen


def _workspace_mutation(command):
    """Hold the runtime's exclusive lease across preflight, backups and swaps."""
    @wraps(command)
    def guarded(args):
        root = Path(args.root).absolute()
        if root.is_symlink():
            raise Rejected(f"application root is a symlink: {root}")
        workspaces = _resolve_workspaces(getattr(args, "workspace", []))
        state_path = root / "UPDATE-STATE.json"
        if state_path.is_file():
            state = load_json(state_path, "UPDATE-STATE.json")
            recorded = (state.get("workspace_backup") or {}).get("workspaces", [])
            workspaces = _resolve_workspaces(
                [*workspaces, *(entry["workspace"] for entry in recorded)])
        args._guarded_workspaces = workspaces
        with ExitStack() as leases:
            if workspaces:
                try:
                    import fcntl
                except ImportError as exc:
                    raise Rejected("local workspace maintenance must run inside its POSIX/WSL runtime") from exc
                for workspace in sorted(workspaces):
                    try:
                        lease = leases.enter_context(os.fdopen(
                            os.open(workspace / ".runtime.lock",
                                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                            "r+b", buffering=0))
                        info = os.fstat(lease.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                            raise Rejected(f"workspace runtime lease is not private: {workspace}")
                        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise Rejected(f"workspace runtime is open: {workspace}; close the App/runtime before maintenance") from exc
                    except OSError as exc:
                        raise Rejected(f"cannot lock workspace {workspace}: {exc}") from exc
            return command(args)
    return guarded


def _sqlite_user_version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _table_exists(path: Path, table: str) -> bool:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        return row is not None
    finally:
        connection.close()


def workspace_preflight(workspace: Path) -> dict:
    """Read-only barrier for one workspace. Never writes to user data."""
    report: dict = {"workspace": str(workspace), "ok": True, "active": [],
                    "problems": [], "versions": {}}
    if not workspace.is_dir():
        report["ok"] = False
        report["problems"].append(f"workspace is not a directory: {workspace}")
        return report
    if workspace.is_symlink():
        report["ok"] = False
        report["problems"].append(f"workspace root is a symlink: {workspace}")
        return report
    for name in WORKSPACE_DATABASES:
        database = workspace / name
        if database.is_symlink():
            report["ok"] = False
            report["problems"].append(f"{name} is a symlink; refusing")
            continue
        if not database.is_file():
            continue
        try:
            version = _sqlite_user_version(database)
        except (sqlite3.Error, OSError, ValueError) as exc:
            report["ok"] = False
            report["problems"].append(f"{name} is not a readable SQLite database: {exc}")
            continue
        report["versions"][name] = version
        if version not in WORKSPACE_EXPECTED_VERSIONS[name]:
            report["ok"] = False
            report["problems"].append(
                f"{name} schema version {version} is not understood by this installer")
            continue
        if name != "workspace.sqlite3" or not _table_exists(database, "tasks"):
            continue
        try:
            connection = sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=10)
            try:
                rows = connection.execute(
                    "SELECT id, kind, status FROM tasks WHERE status IN ('queued','running')"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            report["ok"] = False
            report["problems"].append(f"tasks table is unreadable: {exc}")
            continue
        for task_id, kind, status in rows:
            report["active"].append(
                {"id": task_id, "kind": kind, "status": status})
    if report["active"]:
        report["ok"] = False
        report["problems"].append(
            f"{len(report['active'])} active task(s) "
            f"({', '.join(sorted({item['status'] for item in report['active']}))}); "
            "cancel or wait before updating")
    return report


def check_workspaces(workspaces: list[Path], *, allow_active: bool) -> list[dict]:
    reports = [workspace_preflight(workspace) for workspace in workspaces]
    blocked = [report for report in reports if not report["ok"]]
    if blocked and not allow_active:
        detail = "; ".join(
            f"{report['workspace']}: {', '.join(report['problems'][:2])}"
            for report in blocked)
        raise Rejected(
            f"pre-upgrade barrier: {detail} "
            "-- refusing; cancel/wait, or pass --allow-active to override explicitly")
    return reports


def _check_workspace_readers(marker: dict, reports: list[dict]) -> None:
    supported = marker.get("workspace_schemas") or {}
    for report in reports:
        for name, version in report["versions"].items():
            readers = supported.get(name)
            if not isinstance(readers, list) or any(type(item) is not int for item in readers) \
                    or version not in readers:
                raise Rejected(
                    f"application {marker.get('version')} cannot read {name} schema v{version}; "
                    "keeping the application and workspace unchanged")


def check_model_partials(models: Path | None) -> list[str]:
    """Partial downloads (`.part`) must never be mistaken for installed models."""
    if models is None:
        return []
    partials = sorted(path.name for path in models.glob("*.part")
                      if path.is_file() and not path.is_symlink())
    return partials


def _sqlite_backup_online(source: Path, destination: Path) -> None:
    """Consistent online copy via the SQLite backup API (WAL-safe)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(destination, timeout=30)
    try:
        origin.backup(target)
    finally:
        target.close()
        origin.close()


def backup_workspaces(workspaces: list[Path], backup_root: Path) -> dict:
    """Backup every workspace database; returns the UPDATE-STATE fragment."""
    fragment: dict = {"workspaces": []}
    used: set[str] = set()
    for workspace in workspaces:
        # Two workspaces may share a basename (e.g. two `default` dirs under
        # different homes); the backup subdir must stay unique per workspace.
        candidate = workspace.name
        suffix = 0
        while candidate in used:
            suffix += 1
            candidate = f"{workspace.name}-{suffix}"
        used.add(candidate)
        entry: dict = {"workspace": str(workspace), "backup": candidate, "files": []}
        destination_dir = backup_root / candidate
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name in WORKSPACE_DATABASES:
            source = workspace / name
            if not source.is_file() or source.is_symlink():
                continue
            destination = destination_dir / name
            if destination.exists():
                raise Rejected(f"workspace backup already exists: {destination}")
            _sqlite_backup_online(source, destination)
            entry["files"].append({"name": name, "sha256": digest(destination),
                                  "user_version": _sqlite_user_version(destination)})
            for sidecar_suffix in ("-wal", "-shm", "-journal"):
                sidecar = workspace / (name + sidecar_suffix)
                if sidecar.is_file() and not sidecar.is_symlink():
                    entry["files"].append({"name": name + sidecar_suffix,
                                          "note": "sidecar left in place; backup is the checkpointed copy"})
                    break
        fragment["workspaces"].append(entry)
    return fragment


def verify_workspace_backup_file(path: Path, expected_sha256: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise Rejected(f"workspace backup is missing: {path}")
    actual = digest(path)
    if actual != expected_sha256:
        raise Rejected(
            f"workspace backup changed: {path.name} is {actual}, "
            f"recorded {expected_sha256}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise Rejected(f"workspace backup failed integrity_check: {path}")
    finally:
        connection.close()


def _preverify_workspace_restore(state: dict, wanted: list[Path] | None) -> None:
    """Fail before the tree swap when a workspace restore would refuse."""
    selected = {str(path) for path in (wanted or [])}
    for entry in (state.get("workspace_backup") or {}).get("workspaces", []):
        if selected and entry["workspace"] not in selected:
            continue
        live_dir = Path(entry["workspace"])
        if live_dir.is_symlink() or (live_dir.exists() and not live_dir.is_dir()):
            raise Rejected(f"workspace root is unsafe to restore: {live_dir}")
        backup_dir = Path((state.get("workspace_backup") or {}).get("directory", ""))
        source_dir = backup_dir / entry.get("backup", live_dir.name)
        for item in entry["files"]:
            if item["name"] not in WORKSPACE_DATABASES:
                continue
            verify_workspace_backup_file(source_dir / item["name"], item["sha256"])
            recorded = int(item.get("user_version", -1))
            live = live_dir / item["name"]
            if live.is_file() and not live.is_symlink():
                try:
                    current = _sqlite_user_version(live)
                except (sqlite3.Error, OSError, ValueError):
                    current = -1
                if current != recorded:
                    raise Rejected(
                        f"workspace {item['name']} schema changed during the upgrade "
                        f"(backup v{recorded}, current v{current}); keeping current databases "
                        "-- refusing blind binary downgrade of user data")
            if live.is_symlink():
                raise Rejected(f"workspace database is a symlink; refusing: {live}")


def restore_workspace_backups(state: dict, *, workspaces: list[Path] | None = None) -> list[dict]:
    """Restore recorded pre-upgrade workspace copies. Returns per-file results."""
    wanted = {str(path): path for path in (workspaces or [])}
    restored: list[dict] = []
    for entry in (state.get("workspace_backup") or {}).get("workspaces", []):
        if wanted and entry["workspace"] not in wanted:
            continue
        live_dir = Path(entry["workspace"])
        if live_dir.is_symlink() or (live_dir.exists() and not live_dir.is_dir()):
            raise Rejected(f"workspace root is unsafe to restore: {live_dir}")
        live_dir.mkdir(parents=True, exist_ok=True)
        backup_dir = Path((state.get("workspace_backup") or {}).get("directory", ""))
        source_dir = backup_dir / entry.get("backup", live_dir.name)
        # Verify every recorded copy (digest + integrity) and every schema
        # comparison BEFORE restoring the first byte: a half-restored
        # workspace (one database old, one new) is worse than no restore.
        plan: list[tuple[Path, Path]] = []
        for item in entry["files"]:
            if item["name"] not in WORKSPACE_DATABASES:
                continue
            source = source_dir / item["name"]
            verify_workspace_backup_file(source, item["sha256"])
            recorded = int(item.get("user_version", -1))
            live = live_dir / item["name"]
            current: int | None = None
            if live.is_file() and not live.is_symlink():
                try:
                    current = _sqlite_user_version(live)
                except (sqlite3.Error, OSError, ValueError):
                    current = -1
            if current is not None and current != recorded:
                raise Rejected(
                    f"workspace {item['name']} schema changed during the upgrade "
                    f"(backup v{recorded}, current v{current}); keeping current databases "
                    "-- refusing blind binary downgrade of user data")
            if live.is_symlink():
                raise Rejected(f"workspace database is a symlink; refusing: {live}")
            plan.append((source, live))
        for source, live in plan:
            _sqlite_backup_online(source, live)
            restored.append({"workspace": entry["workspace"], "name": live.name,
                             "sha256": digest(live)})
    return restored

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


def _tree_digest_excluding(root: Path, excluded: set[str]) -> str:
    """`tree_digest` with installer-written state files excluded by name."""
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.relative_to(root).as_posix() in excluded:
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
    schemas = manifest.get("workspace_schemas")
    if not isinstance(schemas, dict) or set(schemas) != set(WORKSPACE_DATABASES):
        raise Rejected("release manifest has no workspace_schemas for the workspace databases")
    for name in WORKSPACE_DATABASES:
        readers = schemas[name]
        if not isinstance(readers, list) or not readers \
                or any(type(item) is not int for item in readers):
            raise Rejected(f"release manifest has no readable versions for {name}")
    manifest["workspace_schemas"] = {name: sorted(schemas[name]) for name in WORKSPACE_DATABASES}
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

def verify_tree(directory: Path, *, extra_allowed: tuple[str, ...] = ()) -> dict:
    """Re-hash an extracted package against its own BUILD-MANIFEST.json.

    **Extra files are rejected too.** The manifest lists every packaged file;
    anything else in the tree was smuggled in after the manifest was computed
    (an archive can be internally self-consistent and still carry a file no
    notice covers). The single documented allowance is `BUILD-MANIFEST.json`
    itself, which the builder deliberately keeps out of its own `files` map.
    Callers pass `extra_allowed` for files the installer itself writes after
    the swap — `apply` records `UPDATE-STATE.json` inside the live tree, so a
    pre-upgrade backup always carries one its BUILD-MANIFEST cannot list. The
    staging check passes nothing extra: an archive smuggling state is refused.
    """
    build = load_json(directory / BUILD_MANIFEST, BUILD_MANIFEST)
    if build.get("format") != 1 or not isinstance(build.get("files"), dict):
        raise Rejected("BUILD-MANIFEST.json has an unsupported shape")
    allowed = set(extra_allowed)
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
        if relative == BUILD_MANIFEST or relative in declared or relative in allowed:
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


def _verify_rollback_tree(previous: Path, state: dict) -> dict:
    """Pre-swap gate for `rollback`: the planted `.previous` tree must prove itself.

    `verify_tree` re-hashes the tree against its own BUILD-MANIFEST (tampered or
    smuggled files fail). The tree must carry a parseable RELEASE-MANIFEST
    version, and — when the live UPDATE-STATE.json records the pre-upgrade
    state — its BUILD-MANIFEST bytes plus its payload digest (excluding the
    installer-written UPDATE-STATE.json, which did not exist when the digest
    was recorded) must equal the recorded values: a planted replacement still
    fails even when self-consistent. With no update record anywhere
    (hand-installed tree restored into a missing root), there is no newer
    live tree to protect — BUILD-MANIFEST re-hash + marker are the whole
    gate. `UPDATE-STATE.json` is the one installer-written file the manifest
    cannot list, so it is allowed here (and only here — the apply staging
    check passes nothing extra).
    """
    has_build = (previous / BUILD_MANIFEST).is_file()
    candidate = marker_of(previous)
    parse_version(candidate.get("version"))  # missing/malformed versions refuse here
    build: dict = verify_tree(previous, extra_allowed=("UPDATE-STATE.json",)) if has_build else {}
    _ = build  # returned below; kept live so the re-hash is never optimized into a check-only call
    # Gate on the exact BUILD-MANIFEST bytes the apply path saw, when it saw
    # one: a rebuilt tree with identical payload regenerates identical
    # canonical bytes (the `_install_tree` fixtures prove it — same-version
    # rebuilds match), while any payload or file-set difference changes them.
    # Byte comparison, not a parsed `files` map: re-serializing JSON would
    # normalize away the whitespace/ordering differences a forgery could hide
    # behind. Trees installed before this gate existed record no manifest, so
    # the payload digest below is their only pin — still strictly stronger
    # than the old marker-only gate.
    recorded_raw = state.get("previous_build_manifest")
    recorded_sha = state.get("previous_build_sha256")
    live_recorded = state.get("previous_digest")
    if live_recorded is None and "previous_version" not in state:
        return build
    if not isinstance(live_recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", live_recorded):
        raise Rejected("no recorded pre-upgrade digest in UPDATE-STATE.json; refusing rollback")
    if isinstance(recorded_raw, str) and isinstance(recorded_sha, str):
        current_raw = (previous / BUILD_MANIFEST).read_bytes()
        if (hashlib.sha256(current_raw).hexdigest() != recorded_sha
                or current_raw.decode("utf-8") != recorded_raw):
            raise Rejected("pre-upgrade backup does not match the recorded manifest; refusing rollback")
    # `tree_digest` hashes every file including UPDATE-STATE.json, but the
    # apply path recorded `previous_digest` BEFORE that file existed in the
    # old tree — so digest with the installer-written state file excluded.
    # A planted replacement still fails: its payload bytes differ. Excluding
    # by name (not by recomputing around content) keeps the gate total: any
    # extra/missing file besides this one still changes the digest.
    if _tree_digest_excluding(previous, {"UPDATE-STATE.json"}) != live_recorded:
        raise Rejected("pre-upgrade backup does not match the recorded digest; refusing rollback")
    live_version = state.get("previous_version")
    if live_version is not None and live_version != candidate.get("version"):
        raise Rejected("pre-upgrade backup version does not match the recorded update; "
                       "refusing rollback")
    return build

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
    # UPDATE-STATE.json records the last apply (tree + workspace backups).
    state_path = root / "UPDATE-STATE.json"
    if state_path.is_file():
        try:
            result["update_state"] = load_json(state_path, "UPDATE-STATE.json")
        except Rejected as exc:
            result["update_state_error"] = str(exc)
    workspaces = _resolve_workspaces(getattr(args, "workspace", []) or [])
    if workspaces:
        result["workspaces"] = [workspace_preflight(workspace)
                                for workspace in workspaces]
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


@_workspace_mutation
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
    partials = check_model_partials(models)
    if partials and not args.allow_active:
        raise Rejected(
            f"model cache holds partial downloads ({', '.join(partials[:3])}); "
            "finish or drop them before updating, or pass --allow-active explicitly")

    workspaces = args._guarded_workspaces
    for workspace in workspaces:
        if models is not None and (workspace == models or models in workspace.parents):
            raise Rejected("workspace databases must not live inside the model cache")
        if workspace == root or root in workspace.parents or workspace in root.parents:
            raise Rejected("workspace must live outside the application tree")
    workspace_reports = check_workspaces(workspaces, allow_active=args.allow_active)
    active_blocked = [report for report in workspace_reports if not report["ok"]]
    if active_blocked and args.allow_active:
        for report in active_blocked:
            print(f"warning: ACTIVE workspace {report['workspace']}: "
                  f"{'; '.join(report['problems'][:2])}", file=sys.stderr)
    if workspaces and not args.backup_dir:
        raise Rejected("workspaces were listed but no --backup-dir was given; "
                       "workspace backups are required when workspaces are listed")
    if args.backup_dir:
        backup_root = Path(args.backup_dir).resolve()
        if backup_root == root or root in backup_root.parents or backup_root in root.parents:
            raise Rejected("the backup directory must live outside the application tree")
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

        # **All compatibility/model-cache/workspace checks run before the swap.**
        # A refusal after `os.replace` would leave the new tree installed while
        # the command reports failure — the user asked not to apply, and we did.
        if models is not None and tree_digest(models) != models_before:
            raise Rejected("model cache changed during update; refusing to continue")
        if check_model_partials(models):
            raise Rejected("model cache gained a partial download during update; "
                           "refusing to continue")
        live_reports = check_workspaces(workspaces, allow_active=args.allow_active)
        if any(not report["ok"] for report in live_reports) and not args.allow_active:
            raise Rejected("a workspace changed during update; refusing to continue")
        _check_workspace_readers(marker_of(staging), live_reports)

        # Pre-upgrade backup: the complete old tree stays next to the new one,
        # so a rollback never needs the network or the old archive.
        old_version = marker_of(root).get("version", "unknown")
        workspace_backup: dict = {}
        if args.backup_dir:
            backup = Path(args.backup_dir).resolve() / f"{root.name}-{old_version}"
            if backup.exists():
                raise Rejected(f"backup already exists: {backup}")
            if workspaces:
                workspace_dir = backup.with_name(backup.name + "-workspaces")
                if workspace_dir.exists():
                    raise Rejected(f"workspace backup already exists: {workspace_dir}")
                for workspace in workspaces:
                    if workspace == workspace_dir or workspace in workspace_dir.parents \
                            or workspace_dir in workspace.parents:
                        raise Rejected("workspace must live outside the backup directory")
                workspace_dir.mkdir(parents=True)
                workspace_backup = backup_workspaces(workspaces, workspace_dir)
                workspace_backup["directory"] = str(workspace_dir)
        previous_digest = tree_digest(root)
        previous_build_path = root / BUILD_MANIFEST
        previous_build_raw = previous_build_path.read_bytes() if previous_build_path.is_file() else None
        os.replace(root, previous)
        if args.backup_dir:
            shutil.copytree(previous, backup, symlinks=False)
        os.replace(staging, root)

        final_marker = marker_of(root)
        (root / "UPDATE-STATE.json").write_text(json.dumps({
            "format": 1, "name": NAME, "version": final_marker["version"],
            "python": final_marker.get("python"), "previous": str(previous),
            "previous_digest": previous_digest, "previous_version": old_version,
            **({"previous_build_manifest": previous_build_raw.decode("utf-8"),
                "previous_build_sha256": hashlib.sha256(previous_build_raw).hexdigest()}
               if previous_build_raw is not None else {}),
            "archive_sha256": manifest["sha256"], "installed_at": _now(),
            "licenses_checked": sorted(
                (build.get("license_manifest") or {}).get("distributions") or {}),
            "workspace_backup": workspace_backup,
            "application_backup": str(backup) if args.backup_dir else None,
            "models_digest": tree_digest(models) if models else None,
            "models_partials": check_model_partials(models),
            "preflight_overridden": bool(active_blocked and args.allow_active),
        }, indent=2) + "\n")

    print(json.dumps({"applied": True, "version": final_marker["version"],
                      "root": str(root), "previous": str(previous),
                      "archive_sha256": manifest["sha256"],
                      "workspace_backup": workspace_backup.get("directory")
                      if workspace_backup else None}, indent=2))
    return 0


@_workspace_mutation
def command_rollback(args) -> int:
    root = Path(args.root).resolve()
    previous = root.with_name(root.name + ".previous")
    if not previous.is_dir():
        raise Rejected(f"no pre-upgrade backup next to {root}; nothing to roll back to")
    restore = bool(getattr(args, "restore_workspaces", False))
    wanted = _resolve_workspaces(getattr(args, "workspace", []) or [])
    state: dict = {}
    state_path = root / "UPDATE-STATE.json"
    if state_path.is_file():
        state = load_json(state_path, "UPDATE-STATE.json")
    previous_state: dict = {}
    previous_state_path = previous / "UPDATE-STATE.json"
    if previous_state_path.is_file():
        try:
            previous_state = load_json(previous_state_path, "UPDATE-STATE.json")
        except Rejected:
            previous_state = {}
    # An interrupted update deleted `root` without installing anything: the
    # live UPDATE-STATE.json is gone with it, but `.previous` (the untouched
    # old tree) is still runnable. When `.previous` itself carries the record
    # of the last successful apply INTO it, gate against that record. A tree
    # installed by hand (no UPDATE-STATE.json anywhere) restores without a
    # recorded digest — there is no newer live tree to protect and no state
    # to be stale against; BUILD-MANIFEST re-hash + marker still apply.
    rollback_state = state or previous_state
    if restore and not (state.get("workspace_backup") or {}).get("workspaces"):
        raise Rejected("no workspace backup is recorded in UPDATE-STATE.json; "
                       "rollback keeps the current workspace databases")
    # Pre-swap gates, all BEFORE any `os.replace`: a refusal after the swap
    # would leave the wrong tree installed while reporting failure. First the
    # workspace copies (digest + integrity + schema match), then the planted
    # `.previous` tree itself (BUILD-MANIFEST re-hash + marker + recorded
    # update digest), so a tampered backup fails instead of going live.
    if restore:
        _preverify_workspace_restore(state, wanted or None)
    reports = (check_workspaces(args._guarded_workspaces, allow_active=False) if not restore
               else [workspace_preflight(workspace) for workspace in args._guarded_workspaces])
    _verify_rollback_tree(previous, rollback_state)
    _check_workspace_readers(marker_of(previous), reports)
    if root.is_dir():
        current_version = (marker_of(root).get("version", "unknown")
                           if (root / MARKER).is_file() else "unknown")
        print(f"rollback: {current_version} -> "
              f"{marker_of(previous).get('version', 'unknown')}",
              file=sys.stderr)
    else:
        print(f"rollback: restoring {marker_of(previous).get('version', 'unknown')} "
              f"into missing {root}", file=sys.stderr)
    kept = None
    if root.is_dir():
        live_marker = marker_of(root) if (root / MARKER).is_file() else {}
        kept = root.with_name(f"{root.name}.rolledback-{live_marker.get('version', 'unknown')}")
        if kept.exists():
            shutil.rmtree(kept)
        os.replace(root, kept)
    os.replace(previous, root)
    restored: list[dict] = []
    restore_note: str | None = None
    if restore:
        # Default rollback keeps post-upgrade workspace writes (same-schema
        # app downgrade must not silently discard user data). Explicit
        # --restore-workspaces brings back the recorded pre-upgrade copies,
        # but only when their schema still matches the live databases.
        restored = restore_workspace_backups(state, workspaces=wanted or None)
    else:
        restore_note = ("workspace databases were left in place; "
                        "pass --restore-workspaces to bring back the pre-upgrade copies")
    final = marker_of(root)
    print(json.dumps({"rolled_back": True, "version": final.get("version"),
                      "root": str(root), "kept_copy": str(kept) if kept else None,
                      "workspaces_restored": restored,
                      "workspace_note": restore_note,
                      "previous_update_state": previous_state.get("installed_at")
                      if previous_state else None},
                     indent=2))
    return 0


@_workspace_mutation
def command_uninstall(args) -> int:
    """Remove the application tree; user data is never deleted by default.

    Only the verified application tree is removed by default. User workspaces,
    model caches and recorded backups remain. ``--remove-backups`` also removes
    verified neighbouring application copies and this install's recorded backup
    directories. Unrecognised staging directories are never treated as ours.
    """
    root = Path(args.root).resolve()
    workspaces = args._guarded_workspaces
    check_workspaces(workspaces, allow_active=args.allow_active)
    if not root.is_dir() and not root.with_name(root.name + ".previous").is_dir():
        raise Rejected(f"nothing installed at {root}")
    models = Path(args.models).resolve() if getattr(args, "models", "") else None
    if models is not None and (models == root or root in models.parents
                               or models in root.parents):
        raise Rejected("the model cache must live outside the application tree")
    removed: list[str] = []
    deletions: list[Path] = []
    recorded_backups: set[Path] = set()
    candidates = [root]
    if getattr(args, "remove_backups", False):
        candidates.extend([root.with_name(root.name + ".previous"),
                           *sorted(root.parent.glob(root.name + ".staging-*")),
                           *sorted(root.parent.glob(root.name + ".rolledback-*"))])
    for candidate in candidates:
        if candidate.is_symlink():
            if candidate == root:
                raise Rejected(f"refusing to remove a symlink: {candidate}")
            continue
        if not candidate.is_dir():
            continue
        if not (candidate / MARKER).is_file() or marker_of(candidate).get("name") != NAME:
            if candidate == root:
                raise Rejected(f"refusing to remove an unrecognised application tree: {candidate}")
            continue
        deletions.append(candidate)
        state_path = candidate / "UPDATE-STATE.json"
        if state_path.is_file():
            state = load_json(state_path, "UPDATE-STATE.json")
            for value in (state.get("application_backup"),
                          (state.get("workspace_backup") or {}).get("directory")):
                if value:
                    recorded_backups.add(Path(value).absolute())
    if getattr(args, "backup_dir", "") and getattr(args, "remove_backups", False):
        backup_root = Path(args.backup_dir).resolve()
        for candidate in sorted(recorded_backups):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if candidate.parent != backup_root:
                raise Rejected(f"recorded backup is outside the selected backup root: {candidate}")
            deletions.append(candidate)
    protected = [*workspaces, *([models] if models else [])]
    for candidate in deletions:
        for data in protected:
            if data == candidate or candidate in data.parents or data in candidate.parents:
                raise Rejected(f"refusing to remove an application copy overlapping user data: {candidate}")
    for candidate in deletions:
        shutil.rmtree(candidate)
        removed.append(str(candidate))
    print(json.dumps({"uninstalled": True, "root": str(root), "removed": removed,
                      "workspaces_kept": [workspace_preflight(workspace) for workspace in workspaces],
                      "models_kept": str(models) if models else None,
                      "models_digest": tree_digest(models) if models else None,
                      "models_partials": check_model_partials(models)}, indent=2))
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
    status.add_argument("--workspace", action="append", default=[])
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
    apply_.add_argument("--workspace", action="append", default=[])
    apply_.add_argument("--allow-active", action="store_true")
    apply_.add_argument("--force-stale", action="store_true")
    apply_.set_defaults(func=command_apply)

    rollback = sub.add_parser("rollback", help="swap the pre-upgrade backup back in")
    rollback.add_argument("--root", required=True)
    rollback.add_argument("--workspace", action="append", default=[])
    rollback.add_argument("--restore-workspaces", action="store_true")
    rollback.set_defaults(func=command_rollback)

    uninstall = sub.add_parser("uninstall", help="remove the application tree, keep user data")
    uninstall.add_argument("--root", required=True)
    uninstall.add_argument("--workspace", action="append", default=[])
    uninstall.add_argument("--models", default="")
    uninstall.add_argument("--backup-dir", default="")
    uninstall.add_argument("--allow-active", action="store_true")
    uninstall.add_argument("--remove-backups", action="store_true")
    uninstall.set_defaults(func=command_uninstall)

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
