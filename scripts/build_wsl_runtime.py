#!/usr/bin/env python3
"""Build the self-contained Linux x86_64 runtime tarball bundled with the Windows installer.

The Windows host never runs Python: the WSL bridge extracts this tarball to
``~/.deepdocparse/runtime`` and starts
``runtime/python/bin/python3 -S -P app/src/runtime-launcher.py`` with
``PYTHONPATH=runtime/site-packages``. The standalone interpreter and every wheel
come from ``packaging/windows/wsl-runtime-lock.json``, pinned by size and
SHA256, and nothing is resolved or installed on the target machine.

Bundle layout (paths as the WSL bridge sees them after extraction)::

    app/src/runtime-launcher.py    copied verbatim from apps/desktop/src
    app/src/runtime-files.py       copied verbatim from apps/desktop/src
    runtime/python/                python-build-standalone install_only tree
    runtime/site-packages/         unpacked wheels + ddp_contracts/ddp_core/ddp_local
    runtime/runtime-info.json      ABI anchor; runtime-launcher.py resolves it as
                                   parents[2] / "runtime" / "runtime-info.json"
    runtime/wsl-runtime-lock.json  pinned inputs, for provenance
    LICENSE                        repository license
    wsl-runtime.json               build manifest (all file digests, written last)

``pip`` (and ensurepip's bundled wheel) is removed from the interpreter after
extraction: the runtime is sealed and never installs anything. The upstream
standalone archive is still verified by its full SHA256 before extraction.

Output is a deterministic tar.gz (sorted entries, fixed mtime/uid/gid, gzip -n)
plus ``.sha256`` and an archive-level ``wsl-runtime.json``. ``--verify``
re-hashes a built directory or tarball, re-runs the ABI probe with the bundled
interpreter and re-derives the license manifest. ``--skip-download`` builds
from an existing verified cache (CI offline).
"""

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging/windows/wsl-runtime-lock.json"
DESKTOP_SOURCE = ROOT / "apps/desktop/src"
VERSION = "0.1.0"
PLATFORM = "linux-x86_64"
DIRECTORY_PLATFORM = "linux-x64"
ARCHIVE_PREFIX = "deepdocparse-wsl-runtime"
EPOCH_DEFAULT = 1789257600  # 2026-09-13 UTC, same release epoch as build_desktop.py
PROBE = (
    "import json,platform,sys,sysconfig;"
    "print(json.dumps({'python_major_minor': list(sys.version_info[:2]),"
    "'cache_tag': sys.implementation.cache_tag,"
    "'soabi': sysconfig.get_config_var('SOABI'),"
    "'machine': platform.machine()}, sort_keys=True))"
)
ABI_KEYS = ("python_major_minor", "cache_tag", "soabi", "machine")
SKIPPED_NAMES = {"RECORD", "direct_url.json", "INSTALLER", "REQUESTED"}
COPIES = ("runtime-launcher.py", "runtime-files.py")

# Minimum shape of the real release bundle. The 2026-09-14 build is a
# 128 592 543-byte tar whose inner manifest names 6 033 files over ~400 MB of
# unpacked payload, with a 102 MB interpreter. A miniature archive (a shell
# stub, some padding, a copied lock and an inner manifest whose digests all
# recompute) is still self-consistent, so consistency alone cannot refuse it:
# these floors can. They raise the fabrication bar, they do not prove
# provenance — v1 is deliberately unsigned.
MIN_UNPACKED_BYTES = 100_000_000
MIN_FILES = 5_000
MIN_INTERPRETER_BYTES = 5 << 20
# Distribution names whose import package is not the dash-normalised name, and
# the probe file required inside that package (PIL/__init__.py is thin, the
# imaging code lives next to it).
IMPORT_ALIASES = {"pillow": "PIL"}
IMPORT_PROBES = {"PIL": "Image.py"}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path, size, sha256):
    path = Path(path)
    return path.is_file() and path.stat().st_size == size and digest(path) == sha256


def load_lock(path=LOCK):
    """Parse and structurally validate the pinned build inputs."""
    lock = json.loads(Path(path).read_text())
    if lock.get("format") != 1:
        raise SystemExit("wsl-runtime-lock.json has an unsupported format")
    if lock.get("platform") != PLATFORM:
        raise SystemExit(f"lock platform {lock.get('platform')!r} != {PLATFORM!r}")
    python = lock.get("python") or {}
    if python.get("implementation") != "cpython":
        raise SystemExit("lock must pin a CPython standalone build")
    if not (isinstance(python.get("major_minor"), list) and len(python["major_minor"]) == 2):
        raise SystemExit("lock python.major_minor must be [major, minor]")
    standalone = lock.get("standalone") or {}
    for key in ("file", "url", "size", "sha256"):
        if not standalone.get(key):
            raise SystemExit(f"lock standalone is missing {key}")
    if len(standalone["sha256"]) != 64:
        raise SystemExit("lock standalone sha256 is not a hex digest")
    wheels = lock.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise SystemExit("lock must pin at least one wheel")
    seen = set()
    for wheel in wheels:
        for key in ("name", "version", "file", "url", "size", "sha256"):
            if not wheel.get(key):
                raise SystemExit(f"lock wheel is missing {key}: {wheel.get('file')}")
        if len(wheel["sha256"]) != 64:
            raise SystemExit(f"lock wheel sha256 is not a hex digest: {wheel['file']}")
        if wheel["file"] in seen:
            raise SystemExit(f"duplicate wheel in lock: {wheel['file']}")
        seen.add(wheel["file"])
    local_packages = lock.get("local_packages")
    if not isinstance(local_packages, list) or not local_packages:
        raise SystemExit("lock must list the local packages copied as source")
    return lock


def shape_requirements(lock, min_interpreter=None):
    """Bundle-relative path -> minimum byte size, derived from the pinned lock.

    The stdlib version and the local packages / wheel roots come from the lock,
    so re-pinning the runtime moves these checks with it instead of leaving a
    second hardcoded inventory behind. The interpreter floor and the bridge /
    launcher paths are the parts the lock cannot express.
    """
    python = lock.get("python") if isinstance(lock, dict) else None
    major_minor = (python or {}).get("major_minor")
    if not (isinstance(major_minor, list) and len(major_minor) == 2
            and all(isinstance(part, int) for part in major_minor)):
        raise SystemExit(
            "pinned lock has no python major_minor; cannot derive the bundle shape")
    stdlib = f"runtime/python/lib/python{major_minor[0]}.{major_minor[1]}"
    requirements = {
        "runtime/python/bin/python3":
            MIN_INTERPRETER_BYTES if min_interpreter is None else min_interpreter,
        f"{stdlib}/encodings/__init__.py": 0,
        f"{stdlib}/socket.py": 0,
        f"{stdlib}/sqlite3/__init__.py": 0,
        "runtime/runtime-info.json": 0,
        "app/src/runtime-launcher.py": 0,
        "app/src/runtime-files.py": 0,
        "wsl-runtime.json": 0,
        # The WSL bridge starts exactly this module; a tree without it is not
        # a runtime, whatever its manifest says.
        "runtime/site-packages/ddp_local/cli.py": 0,
    }
    for name in lock.get("local_packages") or []:
        if isinstance(name, str):
            requirements[f"runtime/site-packages/{name}/__init__.py"] = 0
    for name in lock.get("roots") or []:
        if not isinstance(name, str):
            continue
        module = IMPORT_ALIASES.get(name, name.replace("-", "_"))
        probe = IMPORT_PROBES.get(module, "__init__.py")
        requirements[f"runtime/site-packages/{module}/{probe}"] = 0
    return requirements


def shape_problems(sizes, lock, min_unpacked=None, min_files=None,
                   min_interpreter=None):
    """Minimum-shape violations for a bundle; empty means plausible.

    `sizes` maps bundle-relative member paths to byte sizes (symlinks resolved
    to their targets) and drives both the total and the required-member checks.
    This cannot establish provenance — anyone with build access can fabricate a
    self-consistent archive — it only refuses members too few and too small to
    be the pinned runtime.
    """
    min_unpacked = MIN_UNPACKED_BYTES if min_unpacked is None else min_unpacked
    min_files = MIN_FILES if min_files is None else min_files
    problems = []
    total = sum(sizes.values())
    if total < min_unpacked:
        problems.append(
            f"{total} unpacked bytes < {min_unpacked} (placeholder-shaped)")
    if len(sizes) < min_files:
        problems.append(f"{len(sizes)} members < {min_files} (placeholder-shaped)")
    for relative, floor in sorted(shape_requirements(
            lock, min_interpreter=min_interpreter).items()):
        if relative not in sizes:
            problems.append(f"missing required member: {relative}")
        elif floor and sizes[relative] < floor:
            problems.append(f"{relative} is {sizes[relative]} bytes < {floor}")
    return problems


def bundle_shape_problems(directory, lock=None):
    """Minimum-shape violations for an extracted bundle."""
    directory = Path(directory)
    if lock is None:
        lock_path = directory / "runtime/wsl-runtime-lock.json"
        if not lock_path.is_file():
            raise SystemExit(
                f"bundle has no pinned lock to derive its shape from: {lock_path}")
        lock = json.loads(lock_path.read_text())
    sizes = {}
    for path in directory.rglob("*"):
        if path.is_file():
            sizes[path.relative_to(directory).as_posix()] = path.stat().st_size
    return shape_problems(sizes, lock)


def fetch(entry, cache_dir, skip_download):
    """Return a cache path that matches the pinned size and SHA256.

    ``curl -C -`` resumes interrupted transfers; a completed file that fails
    verification is discarded before the next attempt. Nothing is trusted until
    both the byte size and the digest match the lock.
    """
    target = Path(cache_dir) / entry["file"]
    for attempt in range(4):
        if matches(target, entry["size"], entry["sha256"]):
            return target
        if skip_download:
            raise SystemExit(
                f"--skip-download: {target} is missing or fails its pinned size/sha256; "
                "run once without --skip-download to populate the cache"
            )
        try:
            if attempt:
                print(f"retrying {entry['file']} (attempt {attempt + 1})", file=sys.stderr)
            subprocess.run(
                ["curl", "--fail", "--location", "--retry", "5", "--retry-all-errors",
                 "--retry-delay", "2", "--connect-timeout", "30", "--max-time", "3600",
                 "-C", "-", "-o", str(target), entry["url"]],
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            if isinstance(error, FileNotFoundError):
                raise SystemExit("curl is required to download the pinned inputs") from error
            continue
        if matches(target, entry["size"], entry["sha256"]):
            return target
        target.unlink(missing_ok=True)  # completed but corrupt: never resume onto it
    raise SystemExit(f"failed to download and verify {entry['file']}")


def check_link(member_name, linkname, relative_to_member):
    """Reject links that lexically escape the extraction root.

    Relative symlinks may legitimately use ``..`` (terminfo does); what matters
    is that the normalized result stays inside the tree.
    """
    link = Path(linkname)
    if link.is_absolute():
        raise SystemExit(f"unsafe link target: {member_name} -> {linkname}")
    base = Path(member_name).parent if relative_to_member else Path()
    normalized = os.path.normpath(str(base / link))
    if normalized == ".." or normalized.startswith(".." + os.sep):
        raise SystemExit(f"unsafe link target: {member_name} -> {linkname}")


def extract_tar(archive, destination):
    """Extract a tar.gz with explicit path, link and mode handling."""
    destination = Path(destination)
    hardlinks = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit(f"unsafe tar member: {member.name}")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o777)
            elif member.issym():
                check_link(member.name, member.linkname, relative_to_member=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                os.symlink(member.linkname, target)
            elif member.islnk():
                check_link(member.name, member.linkname, relative_to_member=False)
                hardlinks.append((member.linkname, target))
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, member.mode & 0o777)
            else:
                raise SystemExit(f"unsupported tar member type: {member.name}")
    for linkname, target in hardlinks:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(destination / linkname, target)


def pip_leftovers(root):
    result = []
    for path in sorted(Path(root).rglob("*")):
        name = path.name.lower()
        if name in {"pip", "pip3", "ensurepip"} or name.startswith("pip-") or name.startswith("pip3."):
            result.append(path)
    return result


def strip_pip(python_dir):
    """The sealed runtime installs nothing: drop pip and ensurepip's bundled wheel."""
    python_dir = Path(python_dir)
    removed = []
    for pattern in ("bin/pip", "bin/pip3", "bin/pip3.*"):
        for path in sorted(python_dir.glob(pattern)):
            path.unlink()
            removed.append(path)
    for pattern in ("lib/python3.*/site-packages/pip", "lib/python3.*/site-packages/pip-*.dist-info",
                    "lib/python3.*/ensurepip"):
        for path in sorted(python_dir.glob(pattern)):
            shutil.rmtree(path)
            removed.append(path)
    leftovers = pip_leftovers(python_dir)
    if leftovers:
        raise SystemExit(
            "runtime still contains pip payloads: "
            + ", ".join(str(path.relative_to(python_dir)) for path in leftovers[:5])
        )
    return [str(path.relative_to(python_dir)) for path in removed]


def unpack_wheel(wheel, site):
    """Unpack one verified wheel, mirroring build_desktop.py's payload policy."""
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            relative = Path(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit(f"unsafe wheel member {info.filename} in {wheel.name}")
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            if relative.name in SKIPPED_NAMES:
                continue
            if relative.suffix == ".pth":
                raise SystemExit(f"uncontrolled site hook in {wheel.name}: {relative}")
            target = site / relative
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                target.unlink(missing_ok=True)
                os.symlink(archive.read(info).decode(), target)
                continue
            with archive.open(info) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            os.chmod(target, mode & 0o777 or 0o644)


def copy_tree(source, target):
    for original in sorted(Path(source).rglob("*")):
        relative = original.relative_to(source)
        if "__pycache__" in relative.parts or original.suffix == ".pyc":
            continue
        if original.is_symlink():
            raise SystemExit(f"unreviewed local package symlink: {relative}")
        if original.is_file():
            destination = Path(target) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)


def probe_abi(python_bin):
    """Ask the bundled interpreter for the ABI runtime-launcher.py compares.

    ``-B`` is a command-line flag, so it survives ``-I`` (which ignores the
    PYTHONDONTWRITEBYTECODE environment variable); the probe never writes into
    the tree it is validating.
    """
    result = subprocess.run(
        [str(python_bin), "-B", "-S", "-I", "-c", PROBE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def set_epoch(root, epoch):
    for path in [Path(root), *sorted(Path(root).rglob("*"))]:
        os.utime(path, (epoch, epoch), follow_symlinks=False)


def precompile(python_bin, python_dir, site):
    """Ship bytecode so the host's PYTHONDONTWRITEBYTECODE=1 costs no startup time.

    Sources are stamped to the release epoch first, so the timestamp-based pyc
    headers (source mtime + size) are identical on every build.
    """
    lib = sorted((python_dir / "lib").glob("python3.*"))
    if len(lib) != 1 or not lib[0].is_dir():
        raise SystemExit("standalone python tree has no lib/python3.* directory")
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    for source, label in ((lib[0], f"runtime/python/lib/{lib[0].name}"),
                          (site, "runtime/site-packages")):
        # No -I here: it would ignore PYTHONHASHSEED, and marshalling a
        # frozenset constant embeds hash-order, making the pyc unstable.
        # -d rewrites co_filename to the extraction-relative path so the build
        # does not depend on where the staging directory lives.
        result = subprocess.run(
            [str(python_bin), "-B", "-S", "-m", "compileall", "-q", "-f", "-j", "0",
             "-d", label, str(source)],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            raise SystemExit(f"compileall failed for {source}: {result.stdout}{result.stderr}")


def validate_abi(abi, lock):
    python = lock["python"]
    major, minor = python["major_minor"]
    expected_machine = lock["platform"].rsplit("-", 1)[-1]
    problems = []
    if list(abi["python_major_minor"]) != list(python["major_minor"]):
        problems.append(f"python_major_minor {abi['python_major_minor']} != {python['major_minor']}")
    if abi["cache_tag"] != f"cpython-{major}{minor}":
        problems.append(f"cache_tag {abi['cache_tag']!r} != 'cpython-{major}{minor}'")
    if abi["machine"] != expected_machine:
        problems.append(f"machine {abi['machine']!r} != {expected_machine!r}")
    if not abi.get("soabi"):
        problems.append("empty SOABI")
    if problems:
        raise SystemExit("bundled interpreter does not match the lock: " + "; ".join(problems))
    return abi


def collect_license_manifest(directory):
    """Python's license plus every unlocked distribution's dist-info/licenses tree."""
    directory = Path(directory)
    result = {"python": {}, "distributions": {}}
    missing = []
    python_licenses = sorted((directory / "runtime/python").glob("lib/python3.*/LICENSE.txt"))
    if not python_licenses:
        missing.append("Python license")
    for path in python_licenses:
        result["python"][path.relative_to(directory).as_posix()] = digest(path)
    for dist_info in sorted((directory / "runtime/site-packages").glob("*.dist-info")):
        name = dist_info.name.split("-")[0].replace("_", "-").lower()
        notices = {}
        for path in sorted(dist_info.rglob("*")):
            if path.is_file() and "licenses" in path.relative_to(dist_info).parts:
                notices[path.relative_to(directory).as_posix()] = digest(path)
        if notices:
            result["distributions"][name] = notices
        else:
            missing.append(f"license notice for {name}")
    if missing:
        raise SystemExit("license manifest is incomplete: " + "; ".join(missing))
    return result


def build(lock, lock_path, cache_dir, output, version, epoch, skip_download):
    stage = output / f"{ARCHIVE_PREFIX}-{version}-{DIRECTORY_PLATFORM}"
    if stage.exists():
        if stage.is_symlink():
            raise SystemExit("unsafe output directory")
        shutil.rmtree(stage)
    application = stage / "app/src"
    runtime = stage / "runtime"
    application.mkdir(parents=True, mode=0o755)
    runtime.mkdir(parents=True, mode=0o755)
    for directory in (stage, stage / "app", application, runtime):
        os.chmod(directory, 0o755)

    standalone = fetch(lock["standalone"], cache_dir, skip_download)
    extract_tar(standalone, runtime)  # the archive's own top level is python/
    python_dir = runtime / "python"
    python_bin = python_dir / "bin/python3"
    if not python_bin.is_file():
        raise SystemExit(f"standalone archive has no {python_bin.relative_to(stage)}")
    removed = strip_pip(python_dir)

    site = runtime / "site-packages"
    site.mkdir(mode=0o755)
    os.chmod(site, 0o755)
    for wheel in lock["wheels"]:
        unpack_wheel(fetch(wheel, cache_dir, skip_download), site)
    for name in lock["local_packages"]:
        source = ROOT / "python" / name / name
        if not source.is_dir():
            raise SystemExit(f"local package source not found: {source}")
        copy_tree(source, site / name)

    for name in COPIES:
        shutil.copy2(DESKTOP_SOURCE / name, application / name)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    shutil.copy2(lock_path, runtime / "wsl-runtime-lock.json")
    os.chmod(runtime / "wsl-runtime-lock.json", 0o644)
    os.chmod(stage / "LICENSE", 0o644)

    set_epoch(stage, epoch)
    precompile(python_bin, python_dir, site)
    abi = validate_abi(probe_abi(python_bin), lock)
    (runtime / "runtime-info.json").write_text(json.dumps(abi, indent=2, sort_keys=True) + "\n")
    os.chmod(runtime / "runtime-info.json", 0o644)

    license_manifest = collect_license_manifest(stage)
    files = {
        path.relative_to(stage).as_posix(): digest(path)
        for path in sorted(stage.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "format": 1,
        "name": ARCHIVE_PREFIX,
        "version": version,
        "epoch": epoch,
        "platform": PLATFORM,
        "python": abi,
        "lock_sha256": digest(lock_path),
        "license_manifest": license_manifest,
        "files": files,
        "signature": None,
    }
    (stage / "wsl-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(stage / "wsl-runtime.json", 0o644)
    set_epoch(stage, epoch)
    return {
        "stage": stage,
        "manifest": manifest,
        "python": abi,
        "wheels": len(lock["wheels"]),
        "removed": removed,
    }


def make_archive(stage, output, epoch):
    archive = output / (stage.name + ".tar.gz")
    entries = [stage, *sorted(stage.rglob("*"), key=lambda path: path.relative_to(output).as_posix())]
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in entries:
            entry = tar.gettarinfo(str(path), arcname=str(path.relative_to(output)))
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            entry.mtime = epoch
            if entry.isreg():
                with path.open("rb") as stream:
                    tar.addfile(entry, stream)
            else:
                tar.addfile(entry)
    return archive, digest(archive)


def read_manifest(directory):
    manifest = json.loads((Path(directory) / "wsl-runtime.json").read_text())
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise SystemExit("wsl-runtime.json has an unsupported shape")
    if manifest.get("platform") != PLATFORM:
        raise SystemExit(f"manifest platform {manifest.get('platform')!r} != {PLATFORM!r}")
    return manifest


def verify_directory(directory, probe=True, shape=False):
    """Re-hash a built directory against its own manifest and ABI anchor.

    `shape=True` (the CLI `--verify` path) additionally refuses a bundle that
    cannot be the pinned release: a small self-consistent tree with a shell
    stub interpreter passes every digest check by construction.
    """
    directory = Path(directory).resolve()
    manifest = read_manifest(directory)
    if shape:
        violations = bundle_shape_problems(directory)
        if violations:
            raise SystemExit(
                "runtime bundle fails the minimum release shape: "
                + "; ".join(violations[:5]))
    problems = []
    for relative, expected in sorted(manifest["files"].items()):
        path = directory / relative
        if not path.is_file():
            problems.append(f"missing file: {relative}")
        elif digest(path) != expected:
            problems.append(f"modified file: {relative}")
    listed = set(manifest["files"]) | {"wsl-runtime.json"}
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    for extra in sorted(actual - listed):
        problems.append(f"unlisted file: {extra}")
    if problems:
        raise SystemExit("runtime integrity check failed: " + "; ".join(problems[:5]))

    info_path = directory / "runtime/runtime-info.json"
    if not info_path.is_file():
        raise SystemExit("runtime-info.json is missing from the bundle")
    info = json.loads(info_path.read_text())
    if {key: info.get(key) for key in ABI_KEYS} != manifest["python"]:
        raise SystemExit("runtime-info.json ABI differs from wsl-runtime.json")
    launcher = directory / "app/src/runtime-launcher.py"
    if not launcher.is_file():
        raise SystemExit("runtime-launcher.py is missing from the bundle")
    expected_info = launcher.resolve().parents[2] / "runtime" / "runtime-info.json"
    if expected_info.resolve() != info_path.resolve():
        raise SystemExit(
            "runtime-launcher.py cannot resolve runtime/runtime-info.json at its packaged path"
        )
    lock_path = directory / "runtime/wsl-runtime-lock.json"
    if not lock_path.is_file() or digest(lock_path) != manifest.get("lock_sha256"):
        raise SystemExit("embedded wsl-runtime-lock.json differs from the manifest lock digest")
    if collect_license_manifest(directory) != manifest.get("license_manifest"):
        raise SystemExit("license manifest does not match the files in the bundle")
    if probe:
        python_bin = directory / "runtime/python/bin/python3"
        if not python_bin.is_file():
            raise SystemExit("the bundle has no runtime/python/bin/python3")
        abi = probe_abi(python_bin)
        if abi != manifest["python"]:
            raise SystemExit(f"bundled interpreter ABI {abi} differs from the manifest")
    return {
        "directory": str(directory),
        "files": len(manifest["files"]),
        "distributions": len(manifest["license_manifest"]["distributions"]),
        "python": manifest["python"],
        "version": manifest["version"],
    }


def verify_archive(archive, shape=False):
    archive = Path(archive).resolve()
    if not archive.is_file():
        raise SystemExit(f"archive not found: {archive}")
    with tempfile.TemporaryDirectory() as scratch:
        try:
            extract_tar(archive, scratch)
        except (tarfile.TarError, EOFError, zipfile.BadZipFile, OSError) as exc:
            raise SystemExit(
                f"WSL runtime archive is not a readable tar.gz: {archive}: {exc}"
            ) from None
        roots = [path for path in Path(scratch).iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise SystemExit("archive does not contain exactly one top-level directory")
        summary = verify_directory(roots[0], shape=shape)
    checksum_path = Path(str(archive) + ".sha256")
    if checksum_path.is_file():
        if checksum_path.read_text().split()[0] != digest(archive):
            raise SystemExit(f"{checksum_path.name} does not match {archive.name}")
    sibling = archive.parent / "wsl-runtime.json"
    if sibling.is_file():
        outer = json.loads(sibling.read_text())
        if outer.get("archive") != archive.name or outer.get("sha256") != digest(archive) \
                or outer.get("size") != archive.stat().st_size:
            raise SystemExit(f"{sibling.name} does not match {archive.name}")
    summary["archive"] = str(archive)
    summary["size"] = archive.stat().st_size
    summary["sha256"] = digest(archive)
    summary.pop("directory")
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--lock", type=Path, default=LOCK)
    result.add_argument("--output", type=Path, default=ROOT / "dist/wsl")
    result.add_argument("--cache", type=Path, default=ROOT / "dist/wsl-cache")
    result.add_argument("--version", default=VERSION)
    result.add_argument("--skip-download", action="store_true",
                        help="fail instead of downloading when the cache is incomplete")
    result.add_argument("--verify", type=Path, default=None,
                        help="verify an already-built directory or tar.gz and exit")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    lock = load_lock(args.lock)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", str(EPOCH_DEFAULT)))
    if args.verify is not None:
        target = args.verify
        # The CLI verifies a release artifact, so the release shape applies;
        # the library entry points stay shape-neutral for offline fixtures.
        summary = (verify_directory(target, shape=True) if target.is_dir()
                   else verify_archive(target, shape=True))
        print(json.dumps(summary, indent=2))
        return 0
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise SystemExit("only Linux x86_64 WSL runtimes have been validated")
    args.output.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    built = build(lock, args.lock, args.cache, args.output, args.version, epoch, args.skip_download)
    archive, archive_sha = make_archive(built["stage"], args.output, epoch)
    (args.output / (archive.name + ".sha256")).write_text(f"{archive_sha}  {archive.name}\n")
    outer = dict(built["manifest"], archive=archive.name,
                 size=archive.stat().st_size, sha256=archive_sha)
    (args.output / "wsl-runtime.json").write_text(json.dumps(outer, indent=2) + "\n")
    print(json.dumps(
        {
            "directory": str(built["stage"]),
            "archive": str(archive),
            "size": archive.stat().st_size,
            "sha256": archive_sha,
            "files": len(built["manifest"]["files"]),
            "wheels": built["wheels"],
            "python": built["python"],
            "removed_from_python_tree": built["removed"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
