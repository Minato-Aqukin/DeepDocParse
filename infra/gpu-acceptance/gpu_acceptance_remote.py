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


def glibc_version():
    """System glibc ``(major, minor)``; ``None`` when it cannot be read."""
    import platform as _platform
    try:
        name, version = _platform.libc_ver()
    except Exception:
        return None
    if name != "glibc" or not version:
        return None
    try:
        major, _, rest = version.partition(".")
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
    verifies with ``vulkaninfo --summary`` that an NVIDIA device is
    listed. Writes ``/root/gpu-acceptance/.icd-provisioned.json`` for the
    artifact. Any failure exits 3 with an explicit reason (fail fast,
    never a silent ``model_start_failed`` later).
    """
    import json as _json
    import tempfile as _tempfile
    icd_dir = Path("/usr/share/vulkan/icd.d")
    if any(icd_dir.glob("*nvidia*.json")):
        print("[gpu-acceptance-remote] NVIDIA ICD already present; skipping provision",
              flush=True)
        return None
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
    libdir = Path("/usr/lib/x86_64-linux-gnu")
    for name in ICD_LIBS:
        src = extract / name
        if not src.is_file():
            print(f"host_incompatible: driver package {driver} has no {name}; "
                  "cannot provision the Vulkan ICD", file=sys.stderr)
            sys.exit(3)
        subprocess.run(["install", "-m", "0755", str(src), str(libdir / name)], check=True)
        for link in ICD_SONAMES.get(name, []):
            link_path = libdir / link
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(name)
    subprocess.run(["ldconfig"], check=True)
    subprocess.run(["apt-get", "install", "-y", "libvulkan1"], cwd=ROOT, check=True)
    template = (extract / "nvidia_icd.json").read_text()
    icd_dir.mkdir(parents=True, exist_ok=True)
    (icd_dir / "nvidia_icd.json").write_text(template)
    info = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True)
    if info.returncode != 0 or "NVIDIA" not in (info.stdout or ""):
        print("host_incompatible: vulkaninfo --summary lists no NVIDIA device "
              "after ICD provisioning; refusing to run gpu mode", file=sys.stderr)
        print((info.stdout + info.stderr)[-2000:], file=sys.stderr)
        sys.exit(3)
    device = next((line.strip() for line in info.stdout.splitlines()
                   if "NVIDIA" in line), "unknown")
    marker = {"provisioned_by_kit": True, "driver_version": driver,
              "vulkan_device": device}
    (ROOT / ".icd-provisioned.json").write_text(_json.dumps(marker, indent=2))
    print(f"[gpu-acceptance-remote] ICD provisioned for driver {driver}: {device}",
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
