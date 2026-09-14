"""Guards for the desktop release pipeline: update_check.py and build_desktop.py.

The update channel has no OS updater behind it (Electron ships no autoUpdater on
Linux), so these tests are the only thing that keeps checksum, signature,
version and backup semantics honest.
"""

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


update_check = load("update_check")
build_desktop = load("build_desktop")

# The real release-shape floors, captured before the autouse fixture below
# shrinks them for the synthetic fixtures.
REAL_WSL_SHAPE = {
    name: getattr(build_desktop.wsl_shape(), name)
    for name in ("MIN_UNPACKED_BYTES", "MIN_FILES", "MIN_INTERPRETER_BYTES")
}


@pytest.fixture(autouse=True)
def synthetic_runtime_shape(monkeypatch):
    """Synthetic W3 bundles are miniature on purpose.

    They clear the digest cross-checks but are not release-shaped; the real
    floors are asserted by test_wsl_runtime_pin_rejects_self_consistent_miniature
    (which restores them), and the derivation itself is unit-tested in
    tests/test_wsl_runtime_build.py.
    """
    shape = build_desktop.wsl_shape()
    monkeypatch.setattr(shape, "MIN_UNPACKED_BYTES", 1)
    monkeypatch.setattr(shape, "MIN_FILES", 1)
    monkeypatch.setattr(shape, "MIN_INTERPRETER_BYTES", 1)


ABI = {"major_minor": list(sys.version_info[:2]),
       "cache_tag": sys.implementation.cache_tag,
       "soabi": "test-soabi", "machine": os.uname().machine}

# The bundled WSL runtime is a Linux interpreter; a Windows release carries
# this ABI in its manifest while its platform says windows/amd64.
WSL_ABI = {"major_minor": [3, 12], "cache_tag": "cpython-312", "soabi": None,
           "machine": "x86_64"}


def marker(version: str) -> dict:
    return {"format": 1, "name": "deepdocparse-desktop", "version": version,
            "epoch": 1789257600, "platform": {"system": "linux", "machine": ABI["machine"]},
            "python": ABI, "electron": "44.3.0", "runtime_lock_sha256": "0" * 64}


def windows_marker(version: str, machine: str = "amd64") -> dict:
    return {"format": 1, "name": "deepdocparse-desktop", "version": version,
            "epoch": 1789257600, "platform": {"system": "windows", "machine": machine},
            "python": dict(WSL_ABI), "electron": "44.3.0",
            "runtime_lock_sha256": "0" * 64}


def make_tree(path: Path, version: str, body: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "deepdocparse").write_text(f"#!/bin/sh\necho deepdocparse {version}\n{body}")
    (path / "deepdocparse").chmod(0o755)
    (path / "data.txt").write_text(f"payload for {version}\n{body}")
    (path / "RELEASE-MANIFEST.json").write_text(json.dumps(marker(version)) + "\n")


def make_archive(tmp: Path, version: str, *, body: str = "", name: str | None = None,
                 files: dict | None = None) -> Path:
    stage = tmp / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    make_tree(stage, version, body)
    (stage / "LICENSE").write_text("test license\n")
    if files:
        for relative, content in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    manifest_files = {
        path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(stage.rglob("*")) if path.is_file()
    }
    build = {"format": 1, "epoch": 1789257600, "electron": "44.3.0",
             "runtime": {"distributions": {}},
             "license_manifest": {"electron": {"LICENSE": manifest_files["LICENSE"]},
                                  "distributions": {}},
             "files": manifest_files}
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    archive = tmp / (name or "deepdocparse-0.1.0-linux-x64.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    return archive


def make_windows_tree(path: Path, version: str, body: str = "") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "deepdocparse.exe").write_text(f"exe {version}\n{body}")
    (path / "deepdocparse.exe").chmod(0o755)
    (path / "data.txt").write_text(f"payload for {version}\n{body}")
    (path / "RELEASE-MANIFEST.json").write_text(
        json.dumps(windows_marker(version)) + "\n")


def make_windows_archive(tmp: Path, version: str, *, body: str = "",
                         name: str | None = None, files: dict | None = None) -> Path:
    stage = tmp / "stage-win"
    if stage.exists():
        shutil.rmtree(stage)
    make_windows_tree(stage, version, body)
    (stage / "LICENSE").write_text("test license\n")
    (stage / "LICENSES.chromium.html").write_text("chromium notices\n")
    if files:
        for relative, content in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    manifest_files = {
        path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(stage.rglob("*")) if path.is_file()
    }
    build = {"format": 1, "epoch": 1789257600, "electron": "44.3.0",
             "runtime": {"distributions": {}},
             "license_manifest": {
                 "electron": {"LICENSE": manifest_files["LICENSE"],
                              "LICENSES.chromium.html":
                                  manifest_files["LICENSES.chromium.html"]},
                 "distributions": {}},
             "files": manifest_files}
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    archive = tmp / (name or "deepdocparse-0.1.0-win32-x64.zip")
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(stage.rglob("*")):
            zf.write(path, arcname=f"{stage.name}/{path.relative_to(stage).as_posix()}")
    return archive


def make_manifest(tmp: Path, archive: Path, version: str, *, name: str | None = None,
                  system: str = "linux", machine: str | None = None,
                  python_abi: dict | None = None,
                  archive_name: str | None = None) -> Path:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            build_bytes = zf.read(
                next(n for n in zf.namelist() if n.endswith("BUILD-MANIFEST.json")))
    else:
        with tarfile.open(archive, "r:gz") as tar:
            build_bytes = tar.extractfile(
                next(m for m in tar if m.name.endswith("BUILD-MANIFEST.json"))).read()
    if system == "windows":
        platform_ = {"system": "windows", "machine": machine or "amd64"}
        abi = python_abi or dict(WSL_ABI)
    else:
        platform_ = {"system": "linux", "machine": machine or ABI["machine"]}
        abi = python_abi or ABI
    manifest = {
        "format": 1, "name": "deepdocparse-desktop", "version": version,
        "archive": archive_name or name or archive.name, "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "platform": platform_,
        "python": abi, "electron": "44.3.0",
        "build_manifest_sha256": hashlib.sha256(build_bytes).hexdigest(),
        "signature": None,
    }
    path = tmp / "release.json"
    path.write_text(json.dumps(manifest) + "\n")
    return path


def verify_args(tmp: Path, archive: Path, manifest: Path, root: Path | None = None):
    """Checksum/version/ABI tests do not exercise signing, so accept unsigned."""
    argv = ["verify", "--manifest", str(manifest), "--archive", str(archive),
            "--allow-unsigned"]
    if root is not None:
        argv += ["--root", str(root)]
    return argv


def raw_verify_args(archive: Path, manifest: Path) -> list[str]:
    return ["verify", "--manifest", str(manifest), "--archive", str(archive)]


# --------------------------------------------------------------- checksum

def test_bad_checksum_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    raw = bytearray(archive.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    archive.write_bytes(raw)
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "CHECKSUM MISMATCH" in capsys.readouterr().err


def test_size_mismatch_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    with archive.open("ab") as stream:
        stream.write(b"extra")
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "size" in capsys.readouterr().err


# --------------------------------------------------------------- versions

def test_downgrade_is_rejected_then_warns_with_flag(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.1.0")
    manifest = make_manifest(tmp_path, archive, "0.1.0")
    root = tmp_path / "installed"
    make_tree(root, "0.2.0")
    assert update_check.main(verify_args(tmp_path, archive, manifest, root)) == 1
    assert "DOWNGRADE" in capsys.readouterr().err
    assert update_check.main(verify_args(tmp_path, archive, manifest, root)
                             + ["--allow-downgrade"]) == 0
    assert "DOWNGRADE" in capsys.readouterr().err


def test_same_version_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.1.0")
    manifest = make_manifest(tmp_path, archive, "0.1.0")
    root = tmp_path / "installed"
    make_tree(root, "0.1.0")
    assert update_check.main(verify_args(tmp_path, archive, manifest, root)) == 1
    assert "already installed" in capsys.readouterr().err


def test_unknown_version_format_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    body = json.loads(manifest.read_text())
    body["version"] = "version-two"
    manifest.write_text(json.dumps(body))
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "unknown version" in capsys.readouterr().err


def test_archive_version_must_match_manifest(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.3.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "0.3.0" in capsys.readouterr().err


def test_abi_mismatch_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    body = json.loads(manifest.read_text())
    body["python"] = dict(ABI, cache_tag="cpython-313")
    manifest.write_text(json.dumps(body))
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "ABI" in capsys.readouterr().err


# --------------------------------------------------------------- signatures

def make_key(tmp_path: Path, name: str) -> tuple[Path, str]:
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True)
    return key, key.with_suffix(".pub").read_text().strip()


def allowed_signers_file(tmp_path: Path, identity: str, public: str) -> Path:
    path = tmp_path / "allowed_signers"
    path.write_text(f"{identity} {public}\n")
    return path


def test_signed_manifest_passes_and_wrong_signer_fails(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    key, public = make_key(tmp_path, "release-key")
    assert update_check.main(["sign", "--key", str(key), "--manifest", str(manifest),
                              "--identity", "release@ddp.test"]) == 0
    signers = allowed_signers_file(tmp_path, "release@ddp.test", public)
    assert update_check.main(verify_args(tmp_path, archive, manifest)
                             + ["--allowed-signers", str(signers)]) == 0

    other, other_public = make_key(tmp_path, "other-key")
    wrong = allowed_signers_file(tmp_path, "release@ddp.test", other_public)
    assert update_check.main(verify_args(tmp_path, archive, manifest)
                             + ["--allowed-signers", str(wrong)]) == 1
    assert "signature verification failed" in capsys.readouterr().err


def test_tampering_after_signature_is_rejected(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    key, public = make_key(tmp_path, "release-key")
    assert update_check.main(["sign", "--key", str(key), "--manifest", str(manifest),
                              "--identity", "release@ddp.test"]) == 0
    signers = allowed_signers_file(tmp_path, "release@ddp.test", public)
    body = json.loads(manifest.read_text())
    body["version"] = "0.2.1"
    manifest.write_text(json.dumps(body))
    assert update_check.main(verify_args(tmp_path, archive, manifest)
                             + ["--allowed-signers", str(signers)]) == 1
    assert "signature verification failed" in capsys.readouterr().err


def test_unsigned_manifest_needs_explicit_risk_flag(tmp_path, capsys):
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    assert update_check.main(raw_verify_args(archive, manifest)) == 1
    assert "unsigned" in capsys.readouterr().err
    assert update_check.main(raw_verify_args(archive, manifest)
                             + ["--allow-unsigned"]) == 0
    assert "UNSIGNED" in capsys.readouterr().err


# --------------------------------------------------------------- apply / rollback

def test_apply_keeps_previous_and_rollback_restores_it(tmp_path):
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    assert update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned"]) == 0
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.2.0"
    previous = root.with_name(root.name + ".previous")
    assert json.loads((previous / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert update_check.main(["rollback", "--root", str(root)]) == 0
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    rolled = list(root.parent.glob(root.name + ".rolledback-*"))
    assert rolled and json.loads(
        (rolled[0] / "RELEASE-MANIFEST.json").read_text())["version"] == "0.2.0"


def test_pending_previous_refuses_until_forced(tmp_path, capsys):
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    make_tree(root.with_name(root.name + ".previous"), "0.0.9")
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    argv = ["apply", "--root", str(root), "--manifest", str(manifest),
            "--archive", str(archive), "--allow-unsigned"]
    assert update_check.main(argv) == 1
    assert "interrupted update is pending" in capsys.readouterr().err


def test_interrupted_update_leaves_old_version_runnable(tmp_path, capsys):
    root = tmp_path / "opt" / "deepdocparse"
    previous = root.with_name(root.name + ".previous")
    make_tree(previous, "0.1.0")
    assert not root.exists()
    assert update_check.main(["status", "--root", str(root)]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["interrupted"] is True and state["runnable"] is True
    assert update_check.main(["rollback", "--root", str(root)]) == 0
    assert (root / "deepdocparse").is_file()
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert not previous.exists()


# --------------------------------------------------------------- models

def test_models_are_untouched_by_apply_and_rollback(tmp_path):
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    models = tmp_path / "var" / "models"
    models.mkdir(parents=True)
    (models / "bge-m3.bin").write_bytes(b"weights")
    (models / "config.json").write_text("{}\n")
    before = update_check.tree_digest(models)
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    assert update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned",
                              "--models", str(models)]) == 0
    assert update_check.tree_digest(models) == before
    assert update_check.main(["rollback", "--root", str(root)]) == 0
    assert update_check.tree_digest(models) == before


def test_apply_rejects_archive_swapped_after_verification(tmp_path, monkeypatch, capsys):
    """TOCTOU: the bytes extracted must be the bytes that were verified.

    A replacement archive that is *internally* self-consistent (its own
    BUILD-MANIFEST matches its own files, same version) used to be extracted
    and installed even though it failed the release manifest's sha256 — the
    verify/apply window re-opened the archive by path.
    """
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    evil = make_archive(tmp_path, "0.2.0", body="smuggled payload",
                        name="evil.tar.gz")
    evil_bytes = evil.read_bytes()

    real_compat = update_check.check_compatibility

    def swap_after_verify(*args, **kwargs):
        result = real_compat(*args, **kwargs)
        archive.write_bytes(evil_bytes)   # swapped in after verification
        return result

    monkeypatch.setattr(update_check, "check_compatibility", swap_after_verify)
    code = update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned"])
    assert code == 1
    assert "changed between verification and extraction" in capsys.readouterr().err
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert not root.with_name(root.name + ".previous").exists(), \
        "refusal must happen before anything is applied"


def test_apply_rejects_unmanifested_files_in_archive(tmp_path, capsys):
    """An archive can be self-consistent and still smuggle an unlisted file."""
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    archive = make_archive(tmp_path, "0.2.0")
    payload = b"not covered by any notice\n"
    # gzip tars cannot be appended to; rewrite with one extra member. The
    # BUILD-MANIFEST inside still lists only the original files.
    with tarfile.open(archive, "r:gz") as tar:
        members = [(member, tar.extractfile(member).read() if member.isfile() else None)
                   for member in tar.getmembers()]
    with tarfile.open(archive, "w:gz") as tar:
        for member, data in members:
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
        info = tarfile.TarInfo("stage/smuggled.bin")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    manifest = make_manifest(tmp_path, archive, "0.2.0")

    code = update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned"])
    assert code == 1
    assert "unmanifested" in capsys.readouterr().err
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert not root.with_name(root.name + ".previous").exists()


def test_apply_refuses_model_cache_change_before_swap(tmp_path, monkeypatch, capsys):
    """The model-cache check is a preflight: a refusal must not have applied."""
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    models = tmp_path / "var" / "models"
    models.mkdir(parents=True)
    (models / "bge-m3.bin").write_bytes(b"weights")
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")

    real_restore = update_check._restore_tree

    def restore_then_change_models(archive_path, target):
        real_restore(archive_path, target)
        (models / "late.bin").write_bytes(b"changed while updating")

    monkeypatch.setattr(update_check, "_restore_tree", restore_then_change_models)
    code = update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned",
                              "--models", str(models)])
    assert code == 1
    assert "model cache changed" in capsys.readouterr().err
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0", \
        "old code swapped first and only then noticed the model cache changed"
    assert not root.with_name(root.name + ".previous").exists()


def test_models_inside_root_are_rejected(tmp_path, capsys):
    root = tmp_path / "opt" / "deepdocparse"
    make_tree(root, "0.1.0")
    models = root / "models"
    models.mkdir()
    (models / "w.bin").write_bytes(b"weights")
    archive = make_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0")
    assert update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned",
                              "--models", str(models)]) == 1
    assert "model cache must live outside" in capsys.readouterr().err


# --------------------------------------------------------------- packaging

def make_package_dir(path: Path, version: str = "0.1.0") -> Path:
    make_tree(path, version)
    (path / "LICENSE").write_text("electron license\n")
    (path / "LICENSES.chromium.html").write_text("chromium notices\n")
    runtime = path / "resources/runtime"
    runtime.mkdir(parents=True)
    (runtime / "runtime-info.json").write_text(json.dumps({
        "python_major_minor": ABI["major_minor"], "cache_tag": ABI["cache_tag"],
        "soabi": ABI["soabi"], "machine": ABI["machine"]}) + "\n")
    runtime_lock = {"python_major_minor": ABI["major_minor"], "machine": ABI["machine"],
                    "distributions": {}}
    (runtime / "runtime-lock.json").write_text(json.dumps(runtime_lock) + "\n")
    (path / "BUILD-MANIFEST.json").write_text(json.dumps({
        "format": 1, "epoch": 1789257600, "electron": "44.3.0",
        "runtime": runtime_lock,
        "license_manifest": build_desktop.collect_license_manifest(path, {}),
        "files": {},
    }) + "\n")
    files = {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*")) if item.is_file()
        and item.name != "BUILD-MANIFEST.json"
    }
    build = json.loads((path / "BUILD-MANIFEST.json").read_text())
    build["files"] = files
    (path / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    return path


def test_verify_directory_detects_modified_file(tmp_path):
    tree = make_package_dir(tmp_path / "pkg")
    result = build_desktop.verify_directory(tree)
    assert result["files"] > 0
    (tree / "data.txt").write_text("tampered")
    with pytest.raises(SystemExit, match="integrity check failed"):
        build_desktop.verify_directory(tree)


def test_verify_directory_rejects_runtime_lock_drift(tmp_path):
    tree = make_package_dir(tmp_path / "pkg")
    relative = "resources/runtime/runtime-lock.json"
    (tree / relative).write_text(
        json.dumps({"python_major_minor": ABI["major_minor"], "machine": "x86_64"}) + "\n")
    build = json.loads((tree / "BUILD-MANIFEST.json").read_text())
    build["files"][relative] = hashlib.sha256((tree / relative).read_bytes()).hexdigest()
    (tree / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    with pytest.raises(SystemExit, match="runtime-lock.json differs"):
        build_desktop.verify_directory(tree)


def test_license_manifest_requires_electron_notices(tmp_path):
    tree = tmp_path / "pkg"
    (tree / "resources/runtime/site-packages").mkdir(parents=True)
    (tree / "LICENSE").write_text("electron license")
    with pytest.raises(SystemExit, match="LICENSES.chromium.html"):
        build_desktop.collect_license_manifest(tree, {})
    (tree / "LICENSES.chromium.html").write_text("chromium notices")
    result = build_desktop.collect_license_manifest(tree, {})
    assert "LICENSE" in result["electron"]


def test_license_manifest_requires_dependency_notices(tmp_path):
    tree = tmp_path / "pkg"
    (tree / "resources/runtime/site-packages/demo-1.0.dist-info").mkdir(parents=True)
    (tree / "LICENSE").write_text("electron license")
    (tree / "LICENSES.chromium.html").write_text("chromium notices")
    with pytest.raises(SystemExit, match="dependency license notice for demo"):
        build_desktop.collect_license_manifest(tree, {"demo": {}})
    license_dir = tree / "resources/runtime/site-packages/demo-1.0.dist-info/licenses"
    license_dir.mkdir()
    (license_dir / "LICENSE").write_text("demo license")
    result = build_desktop.collect_license_manifest(tree, {"demo": {}})
    assert "demo" in result["distributions"]


def test_electron_zip_hash_is_pinned(tmp_path):
    import zipfile
    zip_path = tmp_path / "electron-fake.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("LICENSE", "fake")
    payload = zip_path.read_bytes()
    lock = tmp_path / "electron-lock.json"
    lock.write_text(json.dumps({
        "format": 1, "version": "44.3.0", "platform": "linux-x64",
        "file": zip_path.name, "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }))
    assert build_desktop.verify_electron_zip(zip_path, lock)["size"] == len(payload)
    zip_path.write_bytes(payload + b"tamper")
    with pytest.raises(SystemExit, match="size"):
        build_desktop.verify_electron_zip(zip_path, lock)
    zip_path.write_bytes(payload)
    lock.write_text(lock.read_text().replace(hashlib.sha256(payload).hexdigest(), "0" * 64))
    with pytest.raises(SystemExit, match="SHA256"):
        build_desktop.verify_electron_zip(zip_path, lock)


# --------------------------------------------------------------- windows

def simulate_windows(monkeypatch):
    """Make host_system()/host_machine() behave like an AMD64 Windows host."""
    monkeypatch.setattr(update_check.platform, "system", lambda: "Windows")
    monkeypatch.setattr(update_check.platform, "machine", lambda: "AMD64")


def test_cross_system_manifests_are_rejected(tmp_path, capsys, monkeypatch):
    archive = make_windows_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0", system="windows")
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 1
    assert "built for windows, host is linux" in capsys.readouterr().err

    linux_archive = make_archive(tmp_path, "0.2.0")
    linux_manifest = make_manifest(tmp_path, linux_archive, "0.2.0")
    simulate_windows(monkeypatch)
    assert update_check.main(verify_args(tmp_path, linux_archive, linux_manifest)) == 1
    assert "built for linux, host is windows" in capsys.readouterr().err


def test_windows_manifest_accepted_on_windows_host(tmp_path, monkeypatch):
    simulate_windows(monkeypatch)
    assert update_check.host_abi()["machine"] == "amd64"
    assert update_check.host_machine("windows") == "amd64"
    archive = make_windows_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0", system="windows")
    assert update_check.main(verify_args(tmp_path, archive, manifest)) == 0


def test_windows_platform_normalization(tmp_path):
    assert update_check.normalize_windows_machine("AMD64") == "amd64"
    assert update_check.normalize_windows_machine("x86_64") == "amd64"
    assert update_check.normalize_windows_machine("ARM64") == "arm64"
    manifest_path = make_manifest(
        tmp_path, make_windows_archive(tmp_path, "0.2.0"), "0.2.0",
        system="windows", machine="AMD64")
    manifest = update_check.validate_manifest(
        update_check.load_json(manifest_path, "release manifest"))
    assert manifest["platform"]["machine"] == "amd64"

    arm = json.loads(manifest_path.read_text())
    arm["platform"]["machine"] = "arm64"
    with pytest.raises(update_check.Rejected, match="unsupported platform"):
        update_check.validate_manifest(arm)

    windows_python = json.loads(manifest_path.read_text())
    windows_python["platform"]["machine"] = "amd64"
    windows_python["python"] = dict(WSL_ABI, machine="amd64")
    with pytest.raises(update_check.Rejected, match="complete python ABI"):
        update_check.validate_manifest(windows_python)


def test_windows_zip_apply_and_rollback(tmp_path, monkeypatch):
    simulate_windows(monkeypatch)
    root = tmp_path / "opt" / "deepdocparse"
    make_windows_tree(root, "0.1.0")
    archive = make_windows_archive(tmp_path, "0.2.0")
    manifest = make_manifest(tmp_path, archive, "0.2.0", system="windows")
    assert update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned"]) == 0
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.2.0"
    assert (root / "deepdocparse.exe").is_file()
    assert update_check.main(["rollback", "--root", str(root)]) == 0
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert (root / "deepdocparse.exe").is_file()


def test_apply_rejects_unmanifested_files_in_windows_zip(tmp_path, monkeypatch, capsys):
    """A self-consistent zip can still smuggle a file no manifest covers."""
    simulate_windows(monkeypatch)
    root = tmp_path / "opt" / "deepdocparse"
    make_windows_tree(root, "0.1.0")
    archive = make_windows_archive(tmp_path, "0.2.0")
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("stage-win/smuggled.bin", b"not covered by any notice\n")
    manifest = make_manifest(tmp_path, archive, "0.2.0", system="windows")

    code = update_check.main(["apply", "--root", str(root), "--manifest", str(manifest),
                              "--archive", str(archive), "--allow-unsigned"])
    assert code == 1
    assert "unmanifested" in capsys.readouterr().err
    assert json.loads((root / "RELEASE-MANIFEST.json").read_text())["version"] == "0.1.0"
    assert not root.with_name(root.name + ".previous").exists()


def test_windows_status_checks_for_exe(tmp_path, monkeypatch, capsys):
    simulate_windows(monkeypatch)
    root = tmp_path / "opt" / "deepdocparse"
    root.mkdir(parents=True)
    (root / "deepdocparse").write_text("linux-style binary name\n")
    (root / "RELEASE-MANIFEST.json").write_text(
        json.dumps(windows_marker("0.1.0")) + "\n")
    assert update_check.main(["status", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["runnable"] is False
    binary = root / "deepdocparse.exe"
    binary.write_text("windows binary\n")
    binary.chmod(0o755)
    assert update_check.main(["status", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["runnable"] is True


def test_zip_restore_rejects_links_and_traversal(tmp_path):
    target = tmp_path / "out"
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as zf:
        zf.writestr("stage/../escape.txt", "escaped")
    with pytest.raises(update_check.Rejected, match="unsafe path"):
        update_check._restore_tree(traversal, target)

    link = tmp_path / "link.zip"
    info = zipfile.ZipInfo("stage/link")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(link, "w") as zf:
        zf.writestr(info, "target")
    with pytest.raises(update_check.Rejected, match="link or device"):
        update_check._restore_tree(link, tmp_path / "out-link")


def make_windows_package_dir(path: Path, version: str = "0.1.0") -> Path:
    make_windows_tree(path, version)
    (path / "LICENSE").write_text("electron license\n")
    (path / "LICENSES.chromium.html").write_text("chromium notices\n")
    runtime = path / "resources/runtime"
    runtime.mkdir(parents=True)
    release_dir = path.parent / (path.name + "-wsl")
    tarball = make_wsl_release(release_dir, version)
    wsl_lock = json.loads((release_dir / "wsl-runtime.json").read_text())
    shutil.copy2(tarball, path / "resources" / tarball.name)
    shutil.copy2(tarball.with_name(tarball.name + ".sha256"),
                 path / "resources" / (tarball.name + ".sha256"))
    (runtime / "wsl-runtime.json").write_text(json.dumps(wsl_lock) + "\n")
    (runtime / "runtime-info.json").write_text(json.dumps({
        "python_major_minor": WSL_ABI["major_minor"], "cache_tag": WSL_ABI["cache_tag"],
        "soabi": WSL_ABI["soabi"], "machine": WSL_ABI["machine"]}) + "\n")
    release = windows_marker(version)
    release["python"] = dict(wsl_lock["python"])
    release["runtime_lock_sha256"] = hashlib.sha256(
        (runtime / "wsl-runtime.json").read_bytes()).hexdigest()
    release["wsl_runtime"] = {"archive": tarball.name, "size": tarball.stat().st_size,
                              "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest()}
    (path / "RELEASE-MANIFEST.json").write_text(json.dumps(release) + "\n")
    (path / "BUILD-MANIFEST.json").write_text(json.dumps({
        "format": 1, "epoch": 1789257600, "electron": "44.3.0",
        "platform": {"system": "windows", "machine": "amd64"},
        "runtime": wsl_lock,
        "license_manifest": build_desktop.collect_license_manifest(path, {}),
        "files": {},
    }) + "\n")
    files = {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*")) if item.is_file()
        and item.name != "BUILD-MANIFEST.json"
    }
    build = json.loads((path / "BUILD-MANIFEST.json").read_text())
    build["files"] = files
    (path / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    return path


def test_verify_windows_directory(tmp_path):
    tree = make_windows_package_dir(tmp_path / "pkg")
    result = build_desktop.verify_directory(tree)
    assert result["platform"] == "win32-x64"
    assert result["distributions"] == 0
    (tree / "data.txt").write_text("tampered")
    with pytest.raises(SystemExit, match="integrity check failed"):
        build_desktop.verify_directory(tree)


def test_verify_windows_directory_rejects_wsl_runtime_drift(tmp_path):
    tree = make_windows_package_dir(tmp_path / "pkg")
    tarball = tree / "resources/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz"
    tarball.write_bytes(b"swapped WSL runtime\n")
    build = json.loads((tree / "BUILD-MANIFEST.json").read_text())
    build["files"][tarball.relative_to(tree).as_posix()] = hashlib.sha256(
        tarball.read_bytes()).hexdigest()
    (tree / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    with pytest.raises(SystemExit, match="bundled WSL runtime"):
        build_desktop.verify_directory(tree)


def test_windows_electron_lock_matches_platform_table():
    lock = json.loads((ROOT / "packaging/windows/electron-lock.json").read_text())
    assert lock["platform"] == "win32-x64"
    assert lock["file"] == "electron-v44.3.0-win32-x64.zip"
    assert lock["size"] == 158149320
    assert lock["sha256"] == \
        "26bf9a617d58d81772b3d68305d59ee48272969c15083c06db634a77358a8d9d"
    assert build_desktop.PLATFORMS["win32-x64"]["electron_lock"] == \
        ROOT / "packaging/windows/electron-lock.json"
    assert build_desktop.PLATFORM == "linux-x64"
    assert build_desktop.PLATFORMS["linux-x64"]["binary"] == "deepdocparse"
    assert build_desktop.PLATFORMS["win32-x64"]["binary"] == "deepdocparse.exe"


# The reviewed pinned-inputs lock the W3 build consumes. Bundles embed a copy
# as runtime/wsl-runtime-lock.json and the inner manifest's lock_sha256 is its
# digest, so the fixtures must carry the real bytes for verification to pass.
REPO_WSL_LOCK = ROOT / "packaging/windows/wsl-runtime-lock.json"


def wsl_python_block() -> dict:
    return {"python_major_minor": WSL_ABI["major_minor"],
            "cache_tag": WSL_ABI["cache_tag"],
            "machine": WSL_ABI["machine"],
            "soabi": "cpython-312-x86_64-linux-gnu"}


def make_wsl_release(directory: Path, version: str = "0.1.0", *,
                     placeholder: bool = False, lock_overrides: dict | None = None,
                     inner_overrides: dict | None = None):
    """A W3-shaped release input: tarball + sidecar + outer manifest.

    Non-placeholder bundles carry a real inner `wsl-runtime.json` whose full
    files map recomputes, because `verify_wsl_archive_layout` reads it back out
    of the archive, and include every member the pinned lock's shape derivation
    requires (at the fixture-shrunk floors; the real floors are asserted
    separately). A real-sized fixture would be ~400 MB per case.
    """
    directory.mkdir(parents=True, exist_ok=True)
    top = f"deepdocparse-wsl-runtime-{version}-linux-x64"
    tarball = directory / (top + ".tar.gz")
    build_lock = REPO_WSL_LOCK.read_bytes() if REPO_WSL_LOCK.is_file() else b"lock\n"
    if placeholder:
        # gzip of an empty tar: the shape of the 205-byte artifact the first
        # Windows packaging run shipped into win-unpacked.
        with tarball.open("wb") as raw:
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w"):
                    pass
        lock = {"format": 1, "name": "deepdocparse-wsl-runtime", "version": version,
                "epoch": 1789257600, "platform": "linux-x86_64",
                "python": wsl_python_block()}
    else:
        try:
            requirements = build_desktop.wsl_shape().shape_requirements(
                json.loads(build_lock))
        except (SystemExit, ValueError):
            requirements = {}
        members = {
            f"{top}/{relative}": b"x" * max(floor, 1)
            for relative, floor in requirements.items()
            if relative != "wsl-runtime.json"
        }
        members.update({
            f"{top}/runtime/wsl-runtime-lock.json": build_lock,
            f"{top}/LICENSE": b"license\n",
            f"{top}/runtime/padding.bin": os.urandom(build_desktop.MIN_WSL_RUNTIME_BYTES),
        })
        files = {name[len(top) + 1:]: hashlib.sha256(data).hexdigest()
                 for name, data in members.items()}
        inner = {"format": 1, "name": "deepdocparse-wsl-runtime", "version": version,
                 "epoch": 1789257600, "platform": "linux-x86_64",
                 "python": wsl_python_block(),
                 "lock_sha256": hashlib.sha256(build_lock).hexdigest(),
                 "license_manifest": {"python": {}, "distributions": {}},
                 "files": files, "signature": None}
        if inner_overrides:
            inner.update(inner_overrides)
        members[f"{top}/wsl-runtime.json"] = (
            json.dumps(inner, indent=2) + "\n").encode()
        with tarfile.open(tarball, "w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        lock = dict(inner)
    payload = tarball.read_bytes()
    lock.update({"archive": tarball.name, "size": len(payload),
                 "sha256": hashlib.sha256(payload).hexdigest()})
    if lock_overrides:
        lock.update(lock_overrides)
    (directory / "wsl-runtime.json").write_text(json.dumps(lock) + "\n")
    (directory / (tarball.name + ".sha256")).write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {tarball.name}\n")
    return tarball


def test_wsl_runtime_pin_accepts_a_release_input(tmp_path):
    tarball = make_wsl_release(tmp_path)
    verified = build_desktop.verify_wsl_runtime(tarball, expected_version="0.1.0")
    assert verified["sha256"] == hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert verified["size"] == tarball.stat().st_size
    assert verified["python"] == {"major_minor": WSL_ABI["major_minor"],
                                  "cache_tag": WSL_ABI["cache_tag"],
                                  "soabi": "cpython-312-x86_64-linux-gnu",
                                  "machine": "x86_64"}
    assert build_desktop.wsl_python_abi(
        {"python_major_minor": WSL_ABI["major_minor"],
         "cache_tag": WSL_ABI["cache_tag"], "machine": "x86_64"})["machine"] == "x86_64"

    sidecar = tmp_path / (tarball.name + ".sha256")
    sidecar.write_text("0" * 64 + f"  {tarball.name}\n")
    with pytest.raises(SystemExit, match="sidecar"):
        build_desktop.verify_wsl_runtime(tarball)

    sidecar.write_text(
        f"{hashlib.sha256(tarball.read_bytes()).hexdigest()}  {tarball.name}\n")
    (tmp_path / "wsl-runtime.json").write_text(
        json.dumps({"format": 1, "name": "deepdocparse-wsl-runtime",
                    "version": "0.1.0", "platform": "linux-x86_64",
                    "archive": tarball.name, "size": tarball.stat().st_size,
                    "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
                    "python": {}}) + "\n")
    with pytest.raises(SystemExit, match="major_minor"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_synthetic_stand_in(tmp_path):
    """The exact stand-in shipped by the first packaging run must be refused."""
    tarball = make_wsl_release(tmp_path, placeholder=True, lock_overrides={
        "name": None, "platform": {"system": "linux", "machine": "x86_64"},
        "stand_in": "SYNTHETIC W4 local build input"})
    with pytest.raises(SystemExit, match="stand-in|release manifest"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_placeholder_even_when_self_consistent(tmp_path):
    """A well-formed manifest pinning a 205-byte empty tar still fails."""
    tarball = make_wsl_release(tmp_path, placeholder=True)
    assert tarball.stat().st_size < build_desktop.MIN_WSL_RUNTIME_BYTES
    with pytest.raises(SystemExit, match="placeholder"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_corrupted_tarball(tmp_path):
    tarball = make_wsl_release(tmp_path)
    tarball.write_bytes(tarball.read_bytes() + b"corruption")
    with pytest.raises(SystemExit, match="size"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_same_size_corruption(tmp_path):
    tarball = make_wsl_release(tmp_path)
    payload = bytearray(tarball.read_bytes())
    payload[-1] ^= 0xFF
    tarball.write_bytes(payload)
    with pytest.raises(SystemExit, match="SHA256"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_wrong_version_and_archive(tmp_path):
    tarball = make_wsl_release(tmp_path)
    with pytest.raises(SystemExit, match="version"):
        build_desktop.verify_wsl_runtime(tarball, expected_version="9.9.9")
    lock = json.loads((tmp_path / "wsl-runtime.json").read_text())
    lock["archive"] = "other.tar.gz"
    (tmp_path / "wsl-runtime.json").write_text(json.dumps(lock) + "\n")
    with pytest.raises(SystemExit, match="archive name"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_non_bundle_layout(tmp_path):
    """A big tarball with a valid hash but no interpreter is not a runtime."""
    top = "deepdocparse-wsl-runtime-0.1.0-linux-x64"
    tarball = tmp_path / (top + ".tar.gz")
    with tarfile.open(tarball, "w:gz") as tar:
        data = os.urandom(build_desktop.MIN_WSL_RUNTIME_BYTES + 4096)
        info = tarfile.TarInfo(f"{top}/junk.bin")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    payload = tarball.read_bytes()
    lock = {"format": 1, "name": "deepdocparse-wsl-runtime", "version": "0.1.0",
            "platform": "linux-x86_64",
            "python": {"python_major_minor": WSL_ABI["major_minor"],
                       "cache_tag": WSL_ABI["cache_tag"], "machine": "x86_64"},
            "archive": tarball.name, "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}
    (tmp_path / "wsl-runtime.json").write_text(json.dumps(lock) + "\n")
    (tmp_path / (tarball.name + ".sha256")).write_text(
        f"{lock['sha256']}  {tarball.name}\n")
    with pytest.raises(SystemExit, match="not a W3 bundle"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_forged_1mib_runtime(tmp_path):
    """The first re-review's forge: ~1 MiB tar, shell-stub python, valid manifest.

    Every outer field is well formed and the archive layout is exactly what the
    pre-fix guard looked for, so this used to be assembled and shipped. The
    inner manifest lists its shell stub with a digest that cannot recompute, and
    that is what refuses it; the follow-up forgery whose digests *do* recompute
    is covered by test_wsl_runtime_pin_rejects_self_consistent_miniature.
    """
    build_lock = REPO_WSL_LOCK.read_bytes()
    top = "deepdocparse-wsl-runtime-0.1.0-linux-x64"
    tarball = tmp_path / (top + ".tar.gz")
    inner = {"format": 1, "name": "deepdocparse-wsl-runtime", "version": "0.1.0",
             "epoch": 1789257600, "platform": "linux-x86_64",
             "python": wsl_python_block(),
             "lock_sha256": hashlib.sha256(build_lock).hexdigest(),
             "license_manifest": {"python": {}, "distributions": {}},
             "files": {
                 "runtime/python/bin/python3": "0" * 64,
                 "runtime/wsl-runtime-lock.json":
                     hashlib.sha256(build_lock).hexdigest(),
             },
             "signature": None}
    members = {
        f"{top}/runtime/python/bin/python3": b"#!/bin/sh\necho stub\n",
        f"{top}/runtime/wsl-runtime-lock.json": build_lock,
        f"{top}/runtime/padding.bin": os.urandom(build_desktop.MIN_WSL_RUNTIME_BYTES),
        f"{top}/wsl-runtime.json": (json.dumps(inner) + "\n").encode(),
    }
    with tarfile.open(tarball, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    payload = tarball.read_bytes()
    lock = dict(inner, archive=tarball.name, size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest())
    (tmp_path / "wsl-runtime.json").write_text(json.dumps(lock) + "\n")
    (tmp_path / (tarball.name + ".sha256")).write_text(
        f"{lock['sha256']}  {tarball.name}\n")
    assert tarball.stat().st_size >= build_desktop.MIN_WSL_RUNTIME_BYTES
    with pytest.raises(SystemExit, match="does not match the archive files"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_self_consistent_miniature(tmp_path, monkeypatch):
    """The re-review's forge: every digest recomputes; only the shape refuses.

    Four members, a shell-stub interpreter, 1 MiB of padding, the real pinned
    lock copied in, and an inner manifest listing each member with the digest
    the archive actually contains. The previous cross-checks (identity, ABI,
    lock_sha256, full files map) all pass by construction, so removing the
    minimum-release-shape check makes this archive verify and package.
    """
    shape = build_desktop.wsl_shape()
    for name, value in REAL_WSL_SHAPE.items():
        monkeypatch.setattr(shape, name, value)
    build_lock = REPO_WSL_LOCK.read_bytes()
    top = "deepdocparse-wsl-runtime-0.1.0-linux-x64"
    tarball = tmp_path / (top + ".tar.gz")
    members = {
        f"{top}/runtime/python/bin/python3": b"#!/bin/sh\necho stub\n",
        f"{top}/runtime/wsl-runtime-lock.json": build_lock,
        f"{top}/runtime/padding.bin": os.urandom(build_desktop.MIN_WSL_RUNTIME_BYTES),
    }
    inner = {"format": 1, "name": "deepdocparse-wsl-runtime", "version": "0.1.0",
             "epoch": 1789257600, "platform": "linux-x86_64",
             "python": wsl_python_block(),
             "lock_sha256": hashlib.sha256(build_lock).hexdigest(),
             "license_manifest": {"python": {}, "distributions": {}},
             "files": {name[len(top) + 1:]: hashlib.sha256(data).hexdigest()
                       for name, data in members.items()},
             "signature": None}
    members[f"{top}/wsl-runtime.json"] = (json.dumps(inner) + "\n").encode()
    with tarfile.open(tarball, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    payload = tarball.read_bytes()
    lock = dict(inner, archive=tarball.name, size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest())
    (tmp_path / "wsl-runtime.json").write_text(json.dumps(lock) + "\n")
    (tmp_path / (tarball.name + ".sha256")).write_text(
        f"{lock['sha256']}  {tarball.name}\n")
    assert tarball.stat().st_size >= build_desktop.MIN_WSL_RUNTIME_BYTES
    with pytest.raises(SystemExit, match="minimum release shape"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_inner_manifest_with_foreign_lock(tmp_path):
    """A bundle whose inner manifest names a lock it does not embed is refused."""
    tarball = make_wsl_release(
        tmp_path, inner_overrides={"lock_sha256": "0" * 64})
    with pytest.raises(SystemExit, match="lock_sha256|embedded lock"):
        build_desktop.verify_wsl_runtime(tarball)


def test_wsl_runtime_pin_rejects_empty_inner_files(tmp_path):
    tarball = make_wsl_release(tmp_path, inner_overrides={"files": {}})
    with pytest.raises(SystemExit, match="empty files map"):
        build_desktop.verify_wsl_runtime(tarball)


def make_fake_electron_zip(tmp_path):
    zip_path = tmp_path / "electron-fake.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("electron.exe", b"exe")
        zf.writestr("LICENSE", "license\n")
        zf.writestr("LICENSES.chromium.html", "notices\n")
    payload = zip_path.read_bytes()
    lock = tmp_path / "electron-lock.json"
    lock.write_text(json.dumps({
        "format": 1, "version": "44.3.0", "platform": "win32-x64",
        "file": zip_path.name, "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest()}) + "\n")
    return zip_path, lock


def make_fake_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "apps/desktop/src").mkdir(parents=True)
    (root / "apps/desktop/package.json").write_text(
        json.dumps({"name": "@ddp/desktop", "version": "0.1.0", "private": True,
                    "type": "module", "main": "src/main.mjs"}) + "\n")
    (root / "apps/desktop/src/main.mjs").write_text("// main\n")
    (root / "packages/client-runtime/src").mkdir(parents=True)
    (root / "packages/client-runtime/src/index.ts").write_text("export {};\n")
    (root / "apps/web/dist").mkdir(parents=True)
    (root / "apps/web/dist/index.html").write_text("<html></html>\n")
    (root / "LICENSE").write_text("license\n")
    return root


def build_windows_args(tmp_path, tarball, electron_zip, output=None):
    return argparse.Namespace(lock_runtime=False, electron_zip=electron_zip,
                              wsl_runtime=tarball, wsl_lock=None,
                              output=output or (tmp_path / "out"), version="0.1.0")


def test_build_windows_refuses_corrupted_wsl_runtime(tmp_path, monkeypatch):
    """The assembler must fail closed, not trust a hash-less/sidecar match."""
    repository = make_fake_repo(tmp_path)
    monkeypatch.setattr(build_desktop, "ROOT", repository)
    zip_path, electron_lock = make_fake_electron_zip(tmp_path)
    monkeypatch.setattr(build_desktop, "WINDOWS_LOCK", electron_lock)
    tarball = make_wsl_release(tmp_path / "wsl")
    tarball.write_bytes(tarball.read_bytes() + b"corruption")
    args = build_windows_args(tmp_path, tarball, zip_path)
    with pytest.raises(SystemExit, match="size"):
        build_desktop.build_windows(args)
    assert not args.output.exists()


def test_build_windows_refuses_stand_in_wsl_runtime(tmp_path, monkeypatch):
    repository = make_fake_repo(tmp_path)
    monkeypatch.setattr(build_desktop, "ROOT", repository)
    zip_path, electron_lock = make_fake_electron_zip(tmp_path)
    monkeypatch.setattr(build_desktop, "WINDOWS_LOCK", electron_lock)
    tarball = make_wsl_release(tmp_path / "wsl", lock_overrides={
        "name": None, "platform": {"system": "linux", "machine": "x86_64"},
        "stand_in": "SYNTHETIC"})
    args = build_windows_args(tmp_path, tarball, zip_path)
    with pytest.raises(SystemExit, match="stand-in|release manifest"):
        build_desktop.build_windows(args)


def test_build_windows_embeds_verified_runtime(tmp_path, monkeypatch):
    repository = make_fake_repo(tmp_path)
    monkeypatch.setattr(build_desktop, "ROOT", repository)
    zip_path, electron_lock = make_fake_electron_zip(tmp_path)
    monkeypatch.setattr(build_desktop, "WINDOWS_LOCK", electron_lock)
    tarball = make_wsl_release(tmp_path / "wsl")
    args = build_windows_args(tmp_path, tarball, zip_path)
    build_desktop.build_windows(args)
    stage = args.output / "deepdocparse-0.1.0-win32-x64"
    assert (stage / "resources" / tarball.name).read_bytes() == tarball.read_bytes()
    assert (stage / "deepdocparse.exe").is_file()
    assert build_desktop.verify_directory(stage)["platform"] == "win32-x64"


def make_packaged_windows_dir(tmp_path, version="0.1.0", *, corrupt=False):
    """A stand-in for electron-builder's win-unpacked output.

    Includes the assembled stage it was built from (the sibling directory with
    a BUILD-MANIFEST), because the packaged payload is diffed against it.
    """
    lock_dir = tmp_path / "wsl"
    tarball = make_wsl_release(lock_dir, version)
    source_manifest = {"name": "@ddp/desktop", "version": version, "private": True,
                       "type": "module", "main": "src/main.mjs",
                       "description": "test host", "scripts": {"start": "electron ."},
                       "devDependencies": {"electron": "44.3.0"},
                       "engines": {"node": ">=24.12.0"}}
    packaged_manifest = {key: value for key, value in source_manifest.items()
                         if key not in {"scripts", "devDependencies"}}
    packaged_manifest["author"] = "DeepDocParse"
    sources = {
        "resources/app/src/main.mjs": "// main\n",
        "resources/app/src/client-host.mjs": "// client host\n",
        "resources/app/src/preload.cjs": "// preload\n",
        "resources/app/src/runtime-launcher.py": "# launcher\n",
        "resources/app/src/runtime-files.py": "# runtime files\n",
        "resources/client-runtime/index.ts": "export {};\n",
        "resources/client-runtime/sqlite-store.ts": "export {};\n",
        "resources/client-runtime/http-provider.ts": "export {};\n",
    }
    # Same relative layout as dist/desktop/windows/win-unpacked: the assembled
    # stage is a sibling of the electron-builder output directory.
    package = tmp_path / "windows/win-unpacked"
    stage = tmp_path / f"deepdocparse-{version}-win32-x64"
    for root in (package, stage):
        for relative in ("resources/app/src", "resources/app/ui",
                         "resources/client-runtime", "resources/runtime"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "deepdocparse.exe").write_bytes(
            b"E" * (build_desktop.MIN_WINDOWS_EXE_BYTES + 1))
        (root / "resources/app/ui/index.html").write_text("<html></html>\n")
        (root / "resources/runtime/runtime-info.json").write_text("{}\n")
        (root / "resources/client-runtime/package.json").write_text(
            '{"private":true,"type":"module"}\n')
        for relative, content in sources.items():
            (root / relative).write_text(content)
    (package / "resources/app/package.json").write_text(
        json.dumps(packaged_manifest) + "\n")
    (stage / "resources/app/package.json").write_text(
        json.dumps(source_manifest) + "\n")
    (package / "resources/runtime/wsl-runtime.json").write_text(
        (lock_dir / "wsl-runtime.json").read_text())
    payload = b"corrupted payload\n" if corrupt else tarball.read_bytes()
    (package / "resources" / tarball.name).write_bytes(payload)
    (package / "resources" / (tarball.name + ".sha256")).write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {tarball.name}\n")
    # The stage BUILD-MANIFEST records every app/client file, like the real
    # assembler; the packaged payload is diffed against all of them.
    build_files = {
        item.relative_to(stage).as_posix():
            hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(stage.rglob("*"))
        if item.is_file() and item.name != "BUILD-MANIFEST.json"
        and item.relative_to(stage).as_posix().startswith(
            ("resources/app/", "resources/client-runtime/"))
    }
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps({
        "format": 1, "epoch": 1789257600, "electron": "44.3.0",
        "platform": {"system": "windows", "machine": "amd64"},
        "runtime": json.loads((lock_dir / "wsl-runtime.json").read_text()),
        "license_manifest": {"electron": {}, "distributions": {}},
        "files": build_files,
    }) + "\n")
    return package, lock_dir / "wsl-runtime.json"


def test_verify_packaged_windows_accepts_good_layout(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path)
    result = build_desktop.verify_packaged_windows(package, lock)
    assert result["version"] == "0.1.0"
    tarball = package / "resources" / result["wsl_runtime"]["archive"]
    assert result["wsl_runtime"]["size"] == tarball.stat().st_size
    assert result["wsl_runtime"]["sha256"] == hashlib.sha256(
        tarball.read_bytes()).hexdigest()


def test_verify_packaged_windows_rejects_placeholder_payload(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path, corrupt=True)
    with pytest.raises(SystemExit, match="size|SHA256|placeholder"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_requires_exe_and_sources(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path)
    (package / "deepdocparse.exe").unlink()
    with pytest.raises(SystemExit, match="deepdocparse.exe not found"):
        build_desktop.verify_packaged_windows(package, lock)
    (package / "deepdocparse.exe").write_bytes(
        b"E" * (build_desktop.MIN_WINDOWS_EXE_BYTES + 1))
    (package / "resources/app/src/main.mjs").unlink()
    with pytest.raises(SystemExit, match="missing app source"):
        build_desktop.verify_packaged_windows(package, lock)
    (package / "resources/app/src/main.mjs").write_text("// main\n")
    (package / "resources/client-runtime/index.ts").unlink()
    with pytest.raises(SystemExit, match="missing client-runtime source"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_refuses_stand_in_lock(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path)
    stand_in = json.loads(lock.read_text())
    stand_in["stand_in"] = "SYNTHETIC"
    lock.write_text(json.dumps(stand_in) + "\n")
    with pytest.raises(SystemExit, match="stand-in"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_rejects_tampered_payload(tmp_path):
    """A copied win-unpacked with a smuggled client-runtime source must fail."""
    package, lock = make_packaged_windows_dir(tmp_path)
    tampered = package / "resources/client-runtime/index.ts"
    tampered.write_text("export const smuggled = true;\n")
    with pytest.raises(SystemExit, match="payload"):
        build_desktop.verify_packaged_windows(package, lock)
    (package / "resources/client-runtime/index.ts").write_text("export {};\n")
    (package / "resources/app/src/main.mjs").write_text("// smuggled\n")
    with pytest.raises(SystemExit, match="payload"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_ignores_in_dir_manifest(tmp_path):
    """A self-consistent BUILD-MANIFEST inside the output proves nothing.

    The packaged directory is the artifact under suspicion; a manifest that
    ships with it can bless its own bytes. The diff must run against the
    external anchor (here: the assembled stage next to it).
    """
    package, lock = make_packaged_windows_dir(tmp_path)
    (package / "resources/app/src/preload.cjs").write_text("// smuggled\n")
    files = {
        item.relative_to(package).as_posix():
            hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(package.rglob("*"))
        if item.is_file() and item.name != "BUILD-MANIFEST.json"
    }
    (package / "BUILD-MANIFEST.json").write_text(json.dumps({
        "format": 1, "epoch": 1789257600, "electron": "44.3.0",
        "platform": {"system": "windows", "machine": "amd64"},
        "runtime": {}, "license_manifest": {"electron": {}, "distributions": {}},
        "files": files,
    }) + "\n")
    with pytest.raises(SystemExit, match="payload"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_covers_all_app_files(tmp_path):
    """preload.cjs and ui/** are payload too, not only src/*.mjs + *.ts."""
    package, lock = make_packaged_windows_dir(tmp_path)
    (package / "resources/app/src/preload.cjs").write_text("// smuggled\n")
    with pytest.raises(SystemExit, match="payload"):
        build_desktop.verify_packaged_windows(package, lock)
    (package / "resources/app/src/preload.cjs").write_text("// preload\n")
    (package / "resources/app/ui/index.html").write_text("<html>smuggled</html>\n")
    with pytest.raises(SystemExit, match="payload"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_stage_must_have_manifest(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path)
    empty = tmp_path / "not-a-stage"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no BUILD-MANIFEST"):
        build_desktop.verify_packaged_windows(package, lock, stage=empty)


def test_verify_windows_package_stage_flag_is_honored(tmp_path, capsys):
    verifier = load("verify_windows_package")
    package, lock = make_packaged_windows_dir(tmp_path)
    stage = tmp_path / "deepdocparse-0.1.0-win32-x64"
    assert verifier.main([str(package), "--wsl-lock", str(lock),
                          "--stage", str(stage)]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"
    body = json.loads((stage / "BUILD-MANIFEST.json").read_text())
    body["files"]["resources/app/src/preload.cjs"] = "0" * 64
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(body) + "\n")
    with pytest.raises(SystemExit, match="payload"):
        verifier.main([str(package), "--wsl-lock", str(lock),
                       "--stage", str(stage)])


def test_verify_packaged_windows_rejects_package_json_drift(tmp_path):
    """The canonical package.json view catches a redirected entry point."""
    package, lock = make_packaged_windows_dir(tmp_path)
    body = json.loads((package / "resources/app/package.json").read_text())
    body["main"] = "src/evil.mjs"
    (package / "resources/app/package.json").write_text(json.dumps(body) + "\n")
    with pytest.raises(SystemExit, match="package.json"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_packaged_windows_rejects_lock_archive_and_version_drift(tmp_path):
    package, lock = make_packaged_windows_dir(tmp_path)
    body = json.loads(lock.read_text())
    body["archive"] = "deepdocparse-wsl-runtime-9.9.9-linux-x64.tar.gz"
    lock.write_text(json.dumps(body) + "\n")
    with pytest.raises(SystemExit, match="archive"):
        build_desktop.verify_packaged_windows(package, lock)
    body["archive"] = json.loads(
        (tmp_path / "wsl/wsl-runtime.json").read_text())["archive"]
    body["version"] = "9.9.9"
    lock.write_text(json.dumps(body) + "\n")
    with pytest.raises(SystemExit, match="version"):
        build_desktop.verify_packaged_windows(package, lock)


def test_verify_dispatches_electron_builder_output(tmp_path, capsys):
    package, lock = make_packaged_windows_dir(tmp_path)
    assert build_desktop.main(
        ["--verify", str(package), "--wsl-lock", str(lock)]) is None
    result = json.loads(capsys.readouterr().out)
    assert result["directory"] == str(package.resolve())
    assert result["wsl_runtime"]["size"] > build_desktop.MIN_WSL_RUNTIME_BYTES
    (package / "resources/app/src/main.mjs").unlink()
    with pytest.raises(SystemExit, match="missing app source"):
        build_desktop.main(["--verify", str(package), "--wsl-lock", str(lock)])


def test_verify_dispatch_prefers_assembled_stage(tmp_path, capsys):
    """The assembled stage has exe + resources/app too; BUILD-MANIFEST wins."""
    stage = make_windows_package_dir(tmp_path / "stage")
    assert build_desktop.main(["--verify", str(stage)]) is None
    result = json.loads(capsys.readouterr().out)
    assert result["platform"] == "win32-x64"
    assert "binary" not in result


def test_verify_windows_package_installer_payload_floor(tmp_path, capsys):
    verifier = load("verify_windows_package")
    package, lock = make_packaged_windows_dir(tmp_path)
    stub = tmp_path / "DeepDocParse-0.1.0-win-x64-setup.exe"
    stub.write_bytes(b"stub")
    with pytest.raises(SystemExit, match="payload is missing"):
        verifier.main([str(package), "--wsl-lock", str(lock),
                       "--installer", str(stub)])
    portable = tmp_path / "DeepDocParse-0.1.0-win-x64-portable.exe"
    portable.write_bytes(b"P" * (verifier.MIN_INSTALLER_BYTES + 1))
    assert verifier.main([str(package), "--wsl-lock", str(lock),
                          "--installer", str(portable)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["installers"][0]["size"] == portable.stat().st_size
