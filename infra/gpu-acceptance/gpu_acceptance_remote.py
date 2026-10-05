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
# `.venv/bin/pip list` when the repo's floors move).
PINS = [
    "fastapi==0.141.1", "uvicorn==0.52.4", "httpx==0.28.1",
    "fakeredis==2.37.1", "redis==5.3.1",
    "sqlalchemy[asyncio]==2.0.52", "aiosqlite==0.22.1",
    "pydantic==2.13.5", "pydantic-settings==2.15.0", "pyyaml==6.0.3",
    "pytest==9.1.1", "pytest-asyncio==1.4.0",
]


def run(*args, cwd=ROOT):
    print(f"+ {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=cwd, check=True)


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
    run("apt-get", "update")
    run("apt-get", "install", "-y", "python3-venv", "vulkan-tools")
    if not (ROOT / ".venv").exists():
        run("python3", "-m", "venv", ".venv")
    run(".venv/bin/pip", "install", "-q", "-e", "python/ddp_contracts",
        "-e", "python/ddp_core", "-e", "python/ddp_local",
        "-e", "services/model-gateway", "-e", "services/corpus-api",
        "-e", "services/corpus-worker")
    run(".venv/bin/pip", "install", "-q", *PINS)
    proc = subprocess.run([".venv/bin/python", "scripts/gpu_acceptance.py", "--mode", "gpu"],
                          cwd=ROOT)
    print(f"[gpu-acceptance-remote] done rc={proc.returncode} "
          f"(instance ttl {args.ttl}); pull the artifact, then release.", flush=True)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
