"""Offline guards for scripts/build_wsl_runtime.py and its pinned lock.

Default runs never touch the network or docker: the build is exercised with a
fake cache containing a tiny shell "interpreter" and two hand-written wheels.
The real 120 MB build + ubuntu:24.04 smoke is opt-in via WSL_RUNTIME_BUILD=1.
"""

import hashlib
import importlib.util
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
SCRIPT = ROOT / "scripts/build_wsl_runtime.py"
FAKE_ABI = {"python_major_minor": [3, 12], "cache_tag": "cpython-312",
            "soabi": "cpython-312-x86_64-linux-gnu", "machine": "x86_64"}
FAKE_INTERPRETER = "#!/bin/sh\necho '" + json.dumps(FAKE_ABI, sort_keys=True) + "'\n"


def load(name="build_wsl_runtime"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_wsl_runtime = load()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def entry(path):
    return {"file": Path(path).name, "url": "https://example.invalid/" + Path(path).name,
            "size": Path(path).stat().st_size, "sha256": sha256(path)}


def make_fake_wheel(path, name="fakepkg", version="1.0.0"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "VALUE = 1\n")
        archive.writestr(f"{name}/__pycache__/stale.pyc", b"stale")
        archive.writestr(f"{name}-{version}.dist-info/METADATA", "Metadata-Version: 2.1\n")
        archive.writestr(f"{name}-{version}.dist-info/licenses/LICENSE", "test license\n")
        archive.writestr(f"{name}-{version}.dist-info/RECORD", f"{name}/__init__.py,,\n")
        archive.writestr(f"{name}-{version}.dist-info/INSTALLER", "pip\n")


def make_fake_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "standalone"
    (tree / "python/bin").mkdir(parents=True, exist_ok=True)
    interpreter = tree / "python/bin/python3"
    interpreter.write_text(FAKE_INTERPRETER)
    interpreter.chmod(0o755)
    (tree / "python/bin/python").unlink(missing_ok=True)
    (tree / "python/bin/python").symlink_to("python3")
    (tree / "python/lib/python3.12").mkdir(parents=True, exist_ok=True)
    (tree / "python/lib/python3.12/LICENSE.txt").write_text("PSF license\n")
    (tree / "python/lib/python3.12/fake_module.py").write_text("VALUE = 1\n")
    standalone = cache / "cpython-3.12.0+fake-x86_64-unknown-linux-gnu-install_only.tar.gz"
    with tarfile.open(standalone, "w:gz") as archive:
        archive.add(tree / "python", arcname="python")
    wheel = cache / "fakepkg-1.0.0-py3-none-any.whl"
    make_fake_wheel(wheel)
    lock = {
        "format": 1,
        "platform": "linux-x86_64",
        "python": {"implementation": "cpython", "major_minor": [3, 12],
                   "version": "3.12.0", "release": "fake"},
        "standalone": entry(standalone),
        "roots": ["fakepkg"],
        "local_packages": ["ddp_contracts", "ddp_core", "ddp_local"],
        "wheels": [dict(entry(wheel), name="fakepkg", version="1.0.0")],
    }
    lock_path = tmp_path / "wsl-runtime-lock.json"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    return lock_path, cache


def run_build(lock, cache, output, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--lock", str(lock), "--cache", str(cache),
         "--output", str(output), "--skip-download", *extra],
        capture_output=True, text=True,
    )


def build_fake_once(tmp_path, name="out"):
    lock, cache = make_fake_cache(tmp_path)
    output = tmp_path / name
    result = run_build(lock, cache, output)
    assert result.returncode == 0, result.stderr
    stage = output / "deepdocparse-wsl-runtime-0.1.0-linux-x64"
    archive = output / (stage.name + ".tar.gz")
    return lock, cache, stage, archive


# --------------------------------------------------------------- committed lock

def test_committed_lock_pins_every_input():
    lock = build_wsl_runtime.load_lock()
    assert lock["format"] == 1
    assert lock["platform"] == build_wsl_runtime.PLATFORM == "linux-x86_64"
    assert lock["python"]["implementation"] == "cpython"
    assert lock["python"]["major_minor"] == [3, 12], "CI parity: workflows pin 3.12"
    standalone = lock["standalone"]
    assert standalone["url"].endswith("/" + standalone["file"])
    assert standalone["size"] > 0
    files = [wheel["file"] for wheel in lock["wheels"]]
    assert len(files) == len(set(files))
    assert set(lock["roots"]) <= {wheel["name"] for wheel in lock["wheels"]}
    for wheel in lock["wheels"]:
        assert wheel["file"].endswith(".whl")
        assert wheel["url"].startswith("https://files.pythonhosted.org/")
        assert wheel["size"] > 0
        assert len(wheel["sha256"]) == 64
    for package in lock["local_packages"]:
        assert (ROOT / "python" / package / package).is_dir(), package


def test_lock_rejects_wheel_without_digest(tmp_path):
    lock = build_wsl_runtime.load_lock()
    lock["wheels"][0].pop("sha256")
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(lock))
    with pytest.raises(SystemExit, match="sha256"):
        build_wsl_runtime.load_lock(broken)


def test_lock_rejects_duplicate_wheel(tmp_path):
    lock = build_wsl_runtime.load_lock()
    lock["wheels"].append(dict(lock["wheels"][0]))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(lock))
    with pytest.raises(SystemExit, match="duplicate"):
        build_wsl_runtime.load_lock(broken)


def test_skip_download_fails_loudly_without_cache(tmp_path):
    lock, _cache = make_fake_cache(tmp_path)
    result = run_build(lock, tmp_path / "empty-cache", tmp_path / "out")
    assert result.returncode != 0
    assert "--skip-download" in result.stderr
    assert "populate the cache" in result.stderr


# --------------------------------------------------------------- fake assembly

def test_fake_build_is_deterministic(tmp_path):
    # One cache + one lock: the pinned inputs must not be regenerated between
    # the two builds, or the embedded lock (and its digests) legitimately differ.
    lock, cache = make_fake_cache(tmp_path)
    archives = []
    for name in ("out-a", "out-b"):
        result = run_build(lock, cache, tmp_path / name)
        assert result.returncode == 0, result.stderr
        archives.append(tmp_path / name / "deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz")
    assert sha256(archives[0]) == sha256(archives[1])
    assert archives[0].name == archives[1].name == "deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz"


def test_fake_build_manifest_and_abi(tmp_path):
    _lock, _cache, stage, archive = build_fake_once(tmp_path)
    manifest = json.loads((stage / "wsl-runtime.json").read_text())
    assert manifest["platform"] == "linux-x86_64"
    assert manifest["python"] == FAKE_ABI
    assert manifest["signature"] is None
    assert manifest["lock_sha256"] == sha256(tmp_path / "wsl-runtime-lock.json")
    assert "app/src/runtime-launcher.py" in manifest["files"]
    assert "runtime/python/bin/python3" in manifest["files"]
    assert "runtime/site-packages/fakepkg/__init__.py" in manifest["files"]
    assert not any("__pycache__" in name and "runtime/python" not in name
                   for name in manifest["files"])
    assert (stage / "runtime/site-packages/fakepkg/__pycache__/stale.pyc").exists() is False
    outer = json.loads((stage.parent / "wsl-runtime.json").read_text())
    assert outer["archive"] == archive.name
    assert outer["size"] == archive.stat().st_size
    assert outer["sha256"] == sha256(archive)
    assert (stage.parent / (archive.name + ".sha256")).read_text().split()[0] == sha256(archive)
    summary = build_wsl_runtime.verify_archive(archive)
    assert summary["files"] == len(manifest["files"])
    assert summary["sha256"] == sha256(archive)


def test_verify_directory_detects_modified_file(tmp_path):
    _lock, _cache, stage, _archive = build_fake_once(tmp_path)
    target = stage / "runtime/python/lib/python3.12/fake_module.py"
    target.write_text("VALUE = 2\n")
    with pytest.raises(SystemExit, match="modified file"):
        build_wsl_runtime.verify_directory(stage)


def test_verify_directory_detects_unlisted_file(tmp_path):
    _lock, _cache, stage, _archive = build_fake_once(tmp_path)
    (stage / "runtime/extra.txt").write_text("smuggled\n")
    with pytest.raises(SystemExit, match="unlisted file"):
        build_wsl_runtime.verify_directory(stage)


def test_precompile_is_path_independent(tmp_path):
    """The shipped pyc must not embed the staging path or hash-order.

    Runs the repo interpreter (not the bundled one) against a fake lib tree:
    co_filename is rewritten by ``compileall -d``, and PYTHONHASHSEED=0 keeps
    frozenset marshalling stable.
    """
    def prepare(root):
        package = root / "python/lib/python3.14/fake_pkg"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "constants.py").write_text('NAMES = frozenset({"alpha", "beta", "gamma"})\n')
        site = root / "site"
        site.mkdir()
        return root / "python", site

    runs = []
    for name in ("stage-a", "stage-b"):
        python_dir, site = prepare(tmp_path / name)
        build_wsl_runtime.set_epoch(tmp_path, 1789257600)
        build_wsl_runtime.precompile(sys.executable, python_dir, site)
        cache = sorted((python_dir / "lib/python3.14/fake_pkg/__pycache__").glob("*.pyc"))
        assert cache, "compileall wrote no bytecode"
        runs.append({path.name: path.read_bytes() for path in cache})
    assert runs[0] == runs[1]


def test_validate_abi_rejects_mismatched_interpreter():
    lock = build_wsl_runtime.load_lock()
    wrong = dict(FAKE_ABI, cache_tag="cpython-311")
    with pytest.raises(SystemExit, match="cache_tag"):
        build_wsl_runtime.validate_abi(wrong, lock)


# --------------------------------------------------------------- release shape

def test_shape_requirements_track_the_committed_lock():
    lock = build_wsl_runtime.load_lock()
    requirements = build_wsl_runtime.shape_requirements(lock)
    assert requirements["runtime/python/bin/python3"] == \
        build_wsl_runtime.MIN_INTERPRETER_BYTES
    major, minor = lock["python"]["major_minor"]
    for relative in ("encodings/__init__.py", "socket.py", "sqlite3/__init__.py"):
        assert f"runtime/python/lib/python{major}.{minor}/{relative}" in requirements
    for name in lock["local_packages"]:
        assert f"runtime/site-packages/{name}/__init__.py" in requirements
    assert "runtime/site-packages/ddp_local/cli.py" in requirements
    # the pillow wheel's import package is PIL, and its probe is Image.py
    assert "runtime/site-packages/PIL/Image.py" in requirements
    assert "runtime/site-packages/pillow/__init__.py" not in requirements
    for name in lock["roots"]:
        if name == "pillow":
            continue
        assert f"runtime/site-packages/{name}/__init__.py" in requirements


def test_shape_problems_refuses_a_miniature():
    lock = build_wsl_runtime.load_lock()
    problems = build_wsl_runtime.shape_problems(
        {"runtime/python/bin/python3": 20, "wsl-runtime.json": 2}, lock)
    assert any("unpacked bytes" in problem for problem in problems)
    assert any("members" in problem for problem in problems)
    assert any("missing required member" in problem for problem in problems)


def test_shape_problems_accepts_a_release_shaped_bundle():
    lock = build_wsl_runtime.load_lock()
    sizes = {relative: max(floor, 1) for relative, floor in
             build_wsl_runtime.shape_requirements(lock).items()}
    sizes["runtime/python/bin/python3"] = build_wsl_runtime.MIN_INTERPRETER_BYTES
    sizes["runtime/python/lib/python3.12/big.bin"] = \
        build_wsl_runtime.MIN_UNPACKED_BYTES
    index = 0
    while len(sizes) < build_wsl_runtime.MIN_FILES:
        sizes[f"runtime/padding/{index:05d}.bin"] = 1
        index += 1
    assert build_wsl_runtime.shape_problems(sizes, lock) == []
    del sizes["runtime/python/lib/python3.12/big.bin"]
    assert any("unpacked bytes" in problem
               for problem in build_wsl_runtime.shape_problems(sizes, lock))


def test_cli_shape_check_refuses_the_offline_fixture(tmp_path):
    """`--verify` demands the release shape; the library API stays neutral.

    The offline fixture is a handful of members, so the CLI's shape gate must
    refuse it while verify_directory() itself still serves offline fixtures.
    """
    _lock, _cache, stage, _archive = build_fake_once(tmp_path)
    build_wsl_runtime.verify_directory(stage, probe=False)
    with pytest.raises(SystemExit, match="minimum release shape"):
        build_wsl_runtime.main(["--verify", str(stage)])


def test_verify_archive_detects_tampered_tarball(tmp_path):
    _lock, _cache, stage, archive = build_fake_once(tmp_path)
    extracted = tmp_path / "extracted"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extracted, filter="data")
    target = extracted / stage.name / "runtime/python/lib/python3.12/fake_module.py"
    target.write_text("VALUE = 2\n")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(extracted / stage.name, arcname=stage.name)
    with pytest.raises(SystemExit, match="modified file"):
        build_wsl_runtime.verify_archive(archive)


def test_verify_rejects_truncated_archive_without_traceback(tmp_path):
    """A truncated gzip tarball must be a clean refusal, not an EOFError."""
    _lock, _cache, _stage, archive = build_fake_once(tmp_path)
    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(truncated)],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "not a readable tar.gz" in result.stderr


def test_verify_rejects_a_non_archive_without_traceback(tmp_path):
    _lock, _cache, _stage, _archive = build_fake_once(tmp_path)
    bogus = tmp_path / "not-a-tarball.tar.gz"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("x", "y")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(bogus)],
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "not a readable tar.gz" in result.stderr


# --------------------------------------------------------------- real build

@pytest.mark.skipif(
    os.environ.get("WSL_RUNTIME_BUILD") != "1",
    reason="real WSL runtime build is opt-in: set WSL_RUNTIME_BUILD=1 "
           "(downloads the pinned interpreter and wheels, then runs the docker smoke)",
)
def test_real_build_and_container_smoke(tmp_path):
    archives, payload = [], None
    for name in ("out", "out-repro"):
        output = tmp_path / name
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(output)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        archives.append(Path(payload["archive"]))
    assert sha256(archives[0]) == sha256(archives[1]), "two builds must be byte-identical"
    archive = archives[0]
    assert archive.stat().st_size == payload["size"]
    assert payload["python"]["cache_tag"] == "cpython-312"
    verify = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(archive)],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0, verify.stderr
    if shutil.which("docker") is None:
        pytest.skip("WSL_RUNTIME_BUILD=1 set but docker is unavailable; smoke not run")
    command = (
        "set -eu; mkdir -p /work; tar -xzf /dist/deepdocparse-wsl-runtime-0.1.0-linux-x64.tar.gz -C /work; "
        "cd /work/deepdocparse-wsl-runtime-0.1.0-linux-x64; "
        "test ! -e runtime/python/bin/pip; "
        "export PYTHONPATH=$PWD/runtime/site-packages PYTHONDONTWRITEBYTECODE=1; "
        "runtime/python/bin/python3 -S -P -c 'import ddp_contracts, ddp_core, "
        "ddp_local, fastapi, httpx, pypdfium2, PIL'; "
        "sh -c 'runtime/python/bin/python3 -S -P app/src/runtime-launcher.py "
        "--workspace /tmp/ws capabilities; echo launcher-parent-ok'; true"
        # trailing true stops bash -c from exec'ing the last command, so the
        # launcher's parent is a live non-PID-1 shell (the real bridge does this)
    )
    smoke = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{output}:/dist:ro", "ubuntu:24.04", "bash", "-lc", command],
        capture_output=True, text=True,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    assert "launcher-parent-ok" in smoke.stdout
    assert "workspace_id" in smoke.stdout
