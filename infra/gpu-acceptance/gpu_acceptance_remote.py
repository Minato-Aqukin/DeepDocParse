#!/usr/bin/env python3
"""On-host GPU steps: system deps, repo venv, kit in gpu mode.

Runs INSIDE the fresh NVIDIA instance (called by run.sh via `autodl exec`).
Dependency pins match this repo's venv (see REVISION for the exact commit);
the kit exit code propagates so run.sh still pulls the artifact and still
releases the instance. Exits nonzero on any failure.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path("/root/gpu-acceptance")

# Pinned to the repo venv at the pushed REVISION (refresh with
# `.venv/bin/pip list` when the repo's floors move). The tail entries feed
# the T59 corpus regression subsets (worker handlers, respx/jsonschema per
# services/corpus-api pyproject dev deps).
PINS = [
    "fastapi==0.141.1", "uvicorn==0.52.4", "httpx==0.28.1",
    "fakeredis==2.37.1", "redis==5.3.1",
    "sqlalchemy[asyncio]==2.0.52", "aiosqlite==0.22.1",
    "pydantic==2.13.5", "pydantic-settings==2.15.0", "pyyaml==6.0.3",
    "pytest==9.1.1", "pytest-asyncio==1.4.0",
    "asyncpg==0.31.0", "alembic==1.19.1", "minio==7.2.20",
    "prometheus-fastapi-instrumentator==8.1.0",
    "respx==0.23.1", "jsonschema==4.26.0",
]

def run(*args, cwd=ROOT):
    print(f"+ {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def dpkg_version(name):
    """Installed version of a dpkg package; ``None`` when not installed."""
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", name],
            capture_output=True, text=True)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def glibc_version():
    """System glibc ``(major, minor)``; ``None`` when it cannot be read.

    Asks a fresh process (``getconf GNU_LIBC_VERSION``): this script may have
    started before the jammy upgrade, so its own loaded libc is stale, and
    ``platform.libc_ver()`` reports what the Python binary was linked against,
    not the system C library.
    """
    try:
        out = subprocess.run(["getconf", "GNU_LIBC_VERSION"], capture_output=True,
                             text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) != 2 or out[0] != "glibc":
        return None
    try:
        major, _, rest = out[1].partition(".")
        return (int(major), int(rest.split(".")[0]))
    except (ValueError, IndexError):
        return None


# User-space libraries the NVIDIA Vulkan ICD loads (verified 2026-10-06 by
# extracting NVIDIA-Linux-x86_64-580.105.08.run and reading NEEDED with
# objdump: libGLX_nvidia needs glsi+tls+glcore; glcore needs tls+gpucomp).
# libcuda/libnvidia-ml are intentionally NOT here: the host kernel driver
# provides them, and overwriting them from the .run would risk a
# user/kernel version skew.
ICD_LIBS = [
    "libGLX_nvidia.so.580.105.08",
    "libnvidia-glcore.so.580.105.08",
    "libnvidia-glvkspirv.so.580.105.08",
    "libnvidia-gpucomp.so.580.105.08",
    "libnvidia-glsi.so.580.105.08",
    "libnvidia-tls.so.580.105.08",
    "libnvidia-allocator.so.580.105.08",
]

# Soname links the loader resolves (libGLX_nvidia.so.0 is what the ICD
# template names; the rest are NEEDED entries of the ICD closure).
ICD_SONAMES = {
    "libGLX_nvidia.so.580.105.08": ["libGLX_nvidia.so.0"],
    "libnvidia-glcore.so.580.105.08": ["libnvidia-glcore.so"],
    "libnvidia-glvkspirv.so.580.105.08": ["libnvidia-glvkspirv.so"],
    "libnvidia-gpucomp.so.580.105.08": ["libnvidia-gpucomp.so"],
    "libnvidia-glsi.so.580.105.08": ["libnvidia-glsi.so.580.105.08".replace(".580.105.08", "")],
    "libnvidia-tls.so.580.105.08": ["libnvidia-tls.so.580.105.08".replace(".580.105.08", "")],
    "libnvidia-allocator.so.580.105.08": ["libnvidia-allocator.so.1", "libnvidia-allocator.so"],
}

DRIVER_URLS = [
    "https://cn.download.nvidia.com/XFree86/Linux-x86_64/{ver}/NVIDIA-Linux-x86_64-{ver}.run",
    "https://download.nvidia.com/XFree86/Linux-x86_64/{ver}/NVIDIA-Linux-x86_64-{ver}.run",
]


def nvidia_vulkan_devices():
    """``deviceName`` values of NVIDIA Vulkan devices, plus the raw output.

    Plain ``vulkaninfo``: focal's vulkan-tools 1.2.131 has no ``--summary``
    (it prints usage), which made an earlier check fail on a working ICD.
    """
    info = subprocess.run(["vulkaninfo"], capture_output=True, text=True)
    text = (info.stdout or "") + (info.stderr or "")
    names = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "deviceName" and "NVIDIA" in value:
            names.append(value.strip())
    return sorted(set(names)), text


def provision_nvidia_icd():
    """Install the NVIDIA Vulkan ICD from the matching driver .run.

    Only when ``/usr/share/vulkan/icd.d`` has no NVIDIA ICD: reads the
    driver version from ``nvidia-smi``, fetches
    ``NVIDIA-Linux-x86_64-<ver>.run`` (China mirror first, both verified
    2026-10-06 for 580.105.08: HTTP 200, 396635119 bytes), extracts with
    ``--extract-only`` into a temp dir, copies ONLY the user-space
    GL/Vulkan libraries the ICD loads (never the kernel module, never
    libcuda), creates the soname symlinks, installs ``libvulkan1`` from
    apt, writes ``nvidia_icd.json`` from the package template, and
    verifies with ``vulkaninfo`` that an NVIDIA device is
    listed. Writes ``/root/gpu-acceptance/.icd-provisioned.json`` for the
    artifact. Any failure exits 3 with an explicit reason (fail fast,
    never a silent ``model_start_failed`` later).
    """
    import json as _json
    import tempfile as _tempfile
    icd_dir = Path("/usr/share/vulkan/icd.d")
    devices, _ = nvidia_vulkan_devices()
    if devices:
        marker = {"provisioned_by_kit": False, "method": "already_working",
                  "vulkan_device": devices[0]}
        (ROOT / ".icd-provisioned.json").write_text(_json.dumps(marker, indent=2))
        print(f"[gpu-acceptance-remote] Vulkan already sees {devices[0]}", flush=True)
        return marker
    smi = subprocess.run(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
        capture_output=True, text=True)
    driver = (smi.stdout.strip().splitlines() or [""])[0].strip()
    if smi.returncode != 0 or not driver:
        print("host_incompatible: nvidia-smi did not report a driver version; "
              "cannot provision the Vulkan ICD", file=sys.stderr)
        sys.exit(3)
    print(f"[gpu-acceptance-remote] no NVIDIA ICD; provisioning for driver {driver}",
          flush=True)
    libdir = Path("/usr/lib/x86_64-linux-gnu")
    # File names carry the host's driver version (the lists above are written
    # for 580.105.08, the version seen on AutoDL 4090 D hosts).
    icd_libs = [name.replace("580.105.08", driver) for name in ICD_LIBS]
    icd_sonames = {name.replace("580.105.08", driver): links
                   for name, links in ICD_SONAMES.items()}
    # The container runtime injects the driver's GL/Vulkan libraries
    # (bind-mounted, read-only) and an ICD manifest naming libGLX_nvidia.so.0.
    # Headless, that ICD returns no vkCreateInstance (seen 2026-10-06 on a
    # 4090 D: "Could not get 'vkCreateInstance' via vk_icdGetInstanceProcAddr"),
    # while the same driver's libEGL_nvidia.so.0 works (vulkaninfo then lists
    # the RTX 4090 D). Add an EGL manifest; the loader skips the broken one.
    egl = libdir / "libEGL_nvidia.so.0"
    if egl.exists():
        icd_dir.mkdir(parents=True, exist_ok=True)
        (icd_dir / "nvidia_egl_icd.json").write_text(_json.dumps(
            {"file_format_version": "1.0.1",
             "ICD": {"library_path": "libEGL_nvidia.so.0", "api_version": "1.3.0"}},
            indent=2) + "\n")
        devices, _ = nvidia_vulkan_devices()
        if devices:
            device = devices[0]
            marker = {"provisioned_by_kit": True, "method": "egl_icd_for_injected_libs",
                      "driver_version": driver, "vulkan_device": device}
            (ROOT / ".icd-provisioned.json").write_text(_json.dumps(marker, indent=2))
            print(f"[gpu-acceptance-remote] EGL ICD manifest for injected driver "
                  f"{driver} libraries: {device}", flush=True)
            return marker
        print("[gpu-acceptance-remote] EGL ICD for the injected libraries lists no "
              "NVIDIA device; falling back to the driver package", flush=True)
    tmp = Path(_tempfile.mkdtemp(prefix="nvidia-driver-"))
    pkg = tmp / f"NVIDIA-Linux-x86_64-{driver}.run"
    fetched = False
    for template in DRIVER_URLS:
        url = template.format(ver=driver)
        print(f"[gpu-acceptance-remote] fetching {url}", flush=True)
        proc = subprocess.run(
            ["curl", "-fL", "-C", "-", "--retry", "10",
             "--connect-timeout", "30", "--speed-time", "60", "--speed-limit", "10240",
             "-o", str(pkg), url], capture_output=True, text=True)
        if proc.returncode == 0 and pkg.is_file():
            fetched = True
            break
        print(f"[gpu-acceptance-remote] driver fetch failed rc={proc.returncode}: "
              f"{(proc.stderr.strip().splitlines() or [''])[-1][:160]}", flush=True)
    if not fetched:
        print(f"host_incompatible: could not fetch NVIDIA driver {driver} "
              "from download.nvidia.com or the .cn mirror; cannot provision "
              "the Vulkan ICD", file=sys.stderr)
        sys.exit(3)
    extract = tmp / "pkg"
    proc = subprocess.run(
        ["sh", str(pkg), "--extract-only", "--target", str(extract)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"host_incompatible: driver package failed integrity check: "
              f"{(proc.stdout + proc.stderr)[-200:]}", file=sys.stderr)
        sys.exit(3)
    for name in icd_libs:
        src = extract / name
        if not src.is_file():
            print(f"host_incompatible: driver package {driver} has no {name}; "
                  "cannot provision the Vulkan ICD", file=sys.stderr)
            sys.exit(3)
        if (libdir / name).exists():
            # Injected by the container runtime (often read-only); keep it.
            continue
        subprocess.run(["install", "-m", "0755", str(src), str(libdir / name)], check=True)
        for link in icd_sonames.get(name, []):
            link_path = libdir / link
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(name)
    subprocess.run(["ldconfig"], check=True)
    subprocess.run(["apt-get", "install", "-y", "libvulkan1"], cwd=ROOT, check=True)
    template = (extract / "nvidia_icd.json").read_text()
    icd_dir.mkdir(parents=True, exist_ok=True)
    (icd_dir / "nvidia_icd.json").write_text(template)
    devices, text = nvidia_vulkan_devices()
    if not devices:
        print("host_incompatible: vulkaninfo lists no NVIDIA device "
              "after ICD provisioning; refusing to run gpu mode", file=sys.stderr)
        print(text[-2000:], file=sys.stderr)
        sys.exit(3)
    device = devices[0]
    marker = {"provisioned_by_kit": True, "driver_version": driver,
              "vulkan_device": device}
    (ROOT / ".icd-provisioned.json").write_text(_json.dumps(marker, indent=2))
    print(f"[gpu-acceptance-remote] ICD provisioned for driver {driver}: {device}",
          flush=True)
    return marker


# Only these ever come from jammy (verified 2026-10-06 in a local
# ubuntu:20.04 container against the real b10809 tarballs: after this step
# `ldd llama-server` shows nothing missing for both builds,
# `llama-server --version` prints 0.4.0-dev build 10809, and the real
# ModelProcess CPU path starts Qwen3-1.7B and answers a completion).
# libc6/libc-bin provide GLIBC_2.32-2.34; libstdc++6 provides
# GLIBCXX_3.4.29/3.4.30 + CXXABI_1.3.13; libgomp1/libssl3 are the other two
# `not found` sonames on focal; libvulkan1 is what the Vulkan build
# dlopens plus what provision_nvidia_icd installs. gcc-12-base arrives as
# an automatic dependency of libgcc-s1; --no-install-recommends keeps out
# mesa-vulkan-drivers/libllvm15/python3.10 (recommends of libvulkan1),
# which would otherwise replace system python3 and break python3-venv.
TOOLCHAIN_PKGS = ["libc6", "libc-bin", "libstdc++6", "libgomp1",
                   "libgcc-s1", "libssl3", "libvulkan1"]

# Reachable jammy mirror for the AutoDL host (apt mirrors
# repo.huaweicloud.com there; huaweicloud carries jammy/jammy-updates AND
# jammy-security, verified 2026-10-06 from inside ubuntu:20.04).
TOOLCHAIN_MIRROR = "http://repo.huaweicloud.com/ubuntu"


def upgrade_toolchain_from_jammy():
    """Bring in just enough Ubuntu 22.04 userland for the b10809 binaries.

    Idempotent: when glibc already meets the llama.cpp floor (2.34), only
    records ``/root/gpu-acceptance/.toolchain-upgraded.json`` (still with
    before/after versions) and returns. Otherwise adds jammy sources with
    a priority-100 pin (focal stays the default for everything else),
    installs ONLY ``TOOLCHAIN_PKGS`` with ``--no-install-recommends`` from
    jammy-updates — except libvulkan1, which installs from jammy proper
    (under ``-t jammy-updates`` it resolves to 1.2.131.2-1; jammy proper
    has 1.3.204.1-2), and records the before/after versions in the marker
    for the artifact's host facts. Any failure here leaves the old glibc
    in place, so the preflight below still fails as host_incompatible.
    """
    import json as _json
    marker = ROOT / ".toolchain-upgraded.json"
    before = {name: dpkg_version(name) for name in TOOLCHAIN_PKGS + ["gcc-12-base"]}
    have = glibc_version()
    if have is not None and have >= (2, 34):
        print(f"[gpu-acceptance-remote] glibc {have[0]}.{have[1]} already meets "
              "the llama.cpp floor; skipping jammy toolchain upgrade", flush=True)
        keep = False
        if marker.is_file():
            try:
                keep = _json.loads(marker.read_text()).get("upgraded_by_kit") is True
            except Exception:
                keep = False
        if not keep:
            marker.write_text(_json.dumps(
                {"upgraded_by_kit": False, "mirror": None,
                 "before": before, "after": before}, indent=2))
        return None
    print("[gpu-acceptance-remote] focal glibc below the llama.cpp floor; "
          f"upgrading {', '.join(TOOLCHAIN_PKGS)} from jammy", flush=True)
    mirror = TOOLCHAIN_MIRROR
    Path("/etc/apt/sources.list.d/jammy-toolchain.list").write_text(
        f"deb {mirror} jammy main restricted universe\n"
        f"deb {mirror} jammy-updates main restricted universe\n"
        f"deb {mirror} jammy-security main restricted universe\n")
    Path("/etc/apt/preferences.d/jammy-toolchain").write_text(
        "Package: *\nPin: release n=jammy*\nPin-Priority: 100\n")
    run("apt-get", "update")
    # libvulkan1 rides its own `-t jammy` line on purpose (see docstring):
    # folding it into the updates line would land 1.2.131.2-1 first and
    # need an immediate upgrade, so each line converges in one step.
    run("apt-get", "install", "-y", "--no-install-recommends",
        "-t", "jammy-updates",
        *(name for name in TOOLCHAIN_PKGS if name != "libvulkan1"))
    run("apt-get", "install", "-y", "--no-install-recommends",
        "-t", "jammy", "libvulkan1")
    after = {name: dpkg_version(name) for name in TOOLCHAIN_PKGS + ["gcc-12-base"]}
    marker.write_text(_json.dumps(
        {"upgraded_by_kit": True, "mirror": mirror,
         "before": before, "after": after}, indent=2))
    print("[gpu-acceptance-remote] toolchain now: "
          + ", ".join(f"{name}={after.get(name)}" for name in TOOLCHAIN_PKGS),
          flush=True)
    return marker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ttl", default="90m")
    args = parser.parse_args()
    revision = (ROOT / "REVISION").read_text().strip() if (ROOT / "REVISION").is_file() else "?"
    print(f"[gpu-acceptance-remote] repo revision: {revision}", flush=True)
    out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        print("no NVIDIA GPU visible; refusing to run gpu mode", file=sys.stderr)
        sys.exit(2)
    print(out.stdout.strip(), flush=True)
    # Base images ship system Python 3.8, but every repo package needs >=3.11.
    # Same answer as infra/autodl: uv fetches its own CPython 3.12 without
    # touching system Python or conda. pip comes from the Aliyun mirror
    # (uv ignores /etc/pip.conf and PIP_INDEX_URL, so pass --index-url
    # explicitly each time).
    run("apt-get", "update")
    run("apt-get", "install", "-y", "python3-venv", "vulkan-tools", "curl", "ca-certificates",
        "libvulkan1")
    # The catalog's llama.cpp b10809 binaries need GLIBC_2.34 (Ubuntu
    # 22.04+); focal ships 2.31. Upgrade just the toolchain libraries from
    # jammy first — the host_incompatible preflight below still fires when
    # the upgrade fails.
    upgrade_toolchain_from_jammy()
    # Fail fast BEFORE any download: the catalog's llama.cpp b10809 binaries
    # need GLIBC_2.34 (Ubuntu 22.04+); focal ships 2.31, so every model start
    # would die as model_start_failed. Exit 3 with host_incompatible instead.
    have = glibc_version()
    if have is not None and have < (2, 34):
        print(f"host_incompatible: system glibc {have[0]}.{have[1]} is below "
              "the llama.cpp floor 2.34 (needs Ubuntu 22.04+); refusing to run "
              "gpu mode", file=sys.stderr)
        sys.exit(3)
    provision_nvidia_icd()
    # uv itself: system pip + Aliyun mirror first (infra/autodl/bootstrap.bash
    # pattern; astral.sh is outside the academic-proxy host list, so the
    # astral install script is only a fallback). `uv python install` pulls
    # CPython from GitHub releases, which IS proxy-covered, so that step
    # sources /etc/network_turbo when present (guarded: a missing file must
    # not abort the install). Aliyun pip steps stay direct.
    run("bash", "-c",
        "command -v uv >/dev/null || python3 -m pip install -q "
        "--index-url https://mirrors.aliyun.com/pypi/simple "
        "--trusted-host mirrors.aliyun.com uv || "
        "curl -fsSL https://astral.sh/uv/install.sh | sh")
    run("bash", "-c",
        "export PATH=\"$HOME/.local/bin:$PATH\"; "
        "if [ -f /etc/network_turbo ]; then source /etc/network_turbo >/dev/null 2>&1; fi; "
        "command -v uv >/dev/null || { echo 'uv install failed' >&2; exit 1; }; "
        "uv python install 3.12 && uv venv --python 3.12 /root/gpu-acceptance/.venv")
    run(".venv/bin/python", "--version")
    run("bash", "-c", "export PATH=\"$HOME/.local/bin:$PATH\" && "
        "VIRTUAL_ENV=/root/gpu-acceptance/.venv uv pip install --index-url "
        "https://mirrors.aliyun.com/pypi/simple "
        "-e python/ddp_contracts -e python/ddp_core -e python/ddp_local "
        "-e services/model-gateway -e services/corpus-api -e services/corpus-worker")
    run("bash", "-c", "export PATH=\"$HOME/.local/bin:$PATH\" && "
        "VIRTUAL_ENV=/root/gpu-acceptance/.venv uv pip install --index-url "
        "https://mirrors.aliyun.com/pypi/simple " + " ".join(PINS))
    # Model/runtime downloads come from huggingface.co + github releases, both
    # proxy-covered: source network_turbo so the kit's proxy-aware downloader
    # rides it. (Apt/pip steps above stay direct — the proxy slows them.)
    proc = subprocess.run(
        ["bash", "-c",
         "if [ -f /etc/network_turbo ]; then source /etc/network_turbo >/dev/null 2>&1; fi; "
         "exec .venv/bin/python scripts/gpu_acceptance.py --mode gpu"],
        cwd=ROOT)
    print(f"[gpu-acceptance-remote] done rc={proc.returncode} "
          f"(instance ttl {args.ttl}); pull the artifact, then release.", flush=True)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
