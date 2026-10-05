#!/usr/bin/env python3
"""GPU acceptance kit: T19 / T59 / T60 checks with redacted artifact output.

One runner for two modes::

    python scripts/gpu_acceptance.py --mode dry-run   # CPU-only machine: every
        # code path that does not need CUDA is exercised; GPU-required checks
        # come out ``skipped`` (never ``pass``).
    python scripts/gpu_acceptance.py --mode gpu       # fresh NVIDIA Linux host:
        # performs the real GPU profile (llama.cpp Vulkan), real checksums on
        # the downloaded catalog artifacts, and the live gateway/load checks.

    python scripts/gpu_acceptance.py --summarize <artifact.json>
        # print proposed ledger evidence/gap lines for the integrator.

The runner never downloads models in dry-run mode and never touches the live
named services. ``--base-port`` is 15490 or 15491 (the kit binds base..base+3;
15494 stays reserved for the local HTTP probe).

Artifact schema ``ddp-gpu-acceptance/1``: host facts (redacted), one record per
check (pass/fail/skipped with raw evidence), per-row verdicts. A row passes
only when EVERY check mapped to it passed; any skipped check makes its row
NOT pass.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS_API = ROOT / "services" / "corpus-api"

CHECKS_T19 = (
    "t19-backend-unsupported",
    "t19-oom-visible",
    "t19-explicit-cpu-recovery",
    "t19-gpu-oom",
    "t19-gpu-offload-evidence",
)
CHECKS_T59 = (
    "t59-gateway-concurrency",
    "t59-generation-budget",
    "t59-queue-and-field-caps",
    "t59-upload-search-caps",
    "t59-cache-caps-gc",
    "t59-federated-budget",
    "t59-gpu-concurrency",
    "t59-gpu-generation-budget",
)
CHECKS_T60 = (
    "t60-model-checksums",
    "t60-release-verify",
    "t60-host-report",
)
ROW_OF = {}
for _row, _ids in (("T19", CHECKS_T19), ("T59", CHECKS_T59), ("T60", CHECKS_T60)):
    for _cid in _ids:
        ROW_OF[_cid] = _row

HOME = os.path.expanduser("~")


def redact(value):
    """Strip hostname/user paths; the artifact must be safe to archive."""
    if isinstance(value, str):
        out = value.replace(HOME, "<HOME>")
        return re.sub(r"/(root|home)/[^/\s:]+", "/<USER>", out)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(check_id, status, evidence=None, reason=""):
    if status not in {"pass", "fail", "skipped"}:
        raise ValueError(f"unknown check status {status!r}")
    return {"id": check_id, "row": ROW_OF[check_id], "status": status,
            "reason": reason, "evidence": redact(evidence or {})}


def repo_rev():
    # On the GPU host the tree arrives via git archive (no .git); run.sh
    # writes the pushed commit into REVISION next to the kit.
    for candidate in (ROOT / "REVISION", Path.cwd() / "REVISION"):
        try:
            text = candidate.read_text().strip()
            if text:
                return text
        except OSError:
            pass
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def package_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


def host_facts():
    from ddp_local.model_runtime.catalog import catalog
    data = catalog()
    return {
        "python": platform.python_version(),
        "platform": f"{sys.platform}-{platform.machine()}",
        "catalog_revision": data["revision"],
        "catalog_artifacts": {a["id"]: a["sha256"][:16] for a in data["artifacts"]},
        "repo_rev": repo_rev(),
        "packages": {name: package_version(name) for name in
                     ("fastapi", "uvicorn", "httpx", "sqlalchemy", "pydantic")},
    }


def gpu_facts():
    """Best-effort NVIDIA/Vulkan facts; None fields mean 'not observed'."""
    facts = {"present": False, "driver": None, "cuda": None,
             "devices": [], "vulkan_icds": [], "nvidia_smi": None}
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return facts
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"], capture_output=True, text=True,
            timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 4:
                    facts["devices"].append({
                        "name": parts[0], "memory_total_mib": parts[1],
                        "memory_free_mib": parts[2], "driver": parts[3]})
                    facts["driver"] = facts["driver"] or parts[3]
            facts["present"] = True
            facts["nvidia_smi"] = "ok"
        head = subprocess.run([smi], capture_output=True, text=True, timeout=30)
        match = re.search(r"CUDA Version:\s*([0-9.]+)", head.stdout)
        if match:
            facts["cuda"] = match.group(1)
    except Exception as exc:
        facts["nvidia_smi"] = f"error: {type(exc).__name__}"
    icd = Path("/usr/share/vulkan/icd.d")
    if icd.is_dir():
        facts["vulkan_icds"] = sorted(p.name for p in icd.glob("*.json"))
    return facts


FIXTURE_SERVER = b'''#!/usr/bin/python3
import http.server,json,sys
args=sys.argv[1:]
def value(name): return args[args.index(name)+1]
key=open(value('--api-key-file')).read()
alias=value('--alias')
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.headers.get('Authorization') != 'Bearer '+key:
   self.send_response(401);self.end_headers();return
  body=json.dumps({'data':[{'id':alias}]} if self.path=='/v1/models' else {'status':'ok'}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
http.server.HTTPServer((value('--host'),int(value('--port'))),Handler).serve_forever()
'''


def fixture_archive(body, filename="runtime/server"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        entry = tarfile.TarInfo(filename)
        entry.size, entry.mode = len(body), 0o700
        archive.addfile(entry, io.BytesIO(body))
    return stream.getvalue()


def make_fixture_installer(directory, *, oom=False):
    """Tiny installer mirroring tests/test_model_process.py shapes (CPU only)."""
    from ddp_local.model_runtime.install import ModelInstaller
    model = b"GGUF\x03\0\0\0fixture-only"
    if oom:
        body = b"#!/bin/sh\necho 'Cannot allocate memory' >&2\nexit 1\n"
    else:
        body = FIXTURE_SERVER
    backend = fixture_archive(body)
    common = {"version": "protocol-test", "license": "MIT",
              "license_url": "https://fixture.example/license",
              "backend": "llama.cpp", "device": "cpu"}
    definitions = {
        "schema": "ddp-model-catalog/1", "revision": "gpu-acceptance-fixture",
        "artifacts": [
            {**common, "id": "fixture-model", "kind": "model", "format": "gguf-v3",
             "filename": "fixture.gguf", "url": "https://fixture.example/model",
             "bytes": len(model), "sha256": hashlib.sha256(model).hexdigest(),
             "runtime_id": "fixture-runtime", "architecture": "test"},
            {**common, "id": "fixture-runtime", "kind": "runtime", "format": "tar-gz",
             "filename": "runtime.tar.gz", "url": "https://fixture.example/runtime",
             "bytes": len(backend), "sha256": hashlib.sha256(backend).hexdigest(),
             "architectures": ["test"], "platform": f"{sys.platform}-{platform.machine()}",
             "executable": "runtime/server", "library_dirs": [], "unpacked_bytes": 65536},
        ],
    }
    installer = ModelInstaller(directory / "models", definitions=definitions)
    for identifier, payload in (("fixture-model", model), ("fixture-runtime", backend)):
        path = directory / identifier
        path.write_bytes(payload)
        installer.import_file(identifier, path)
    return installer


# ------------------------------------------------------------- T19 checks

async def check_t19_backend_unsupported(workdir):
    """Unknown runtime / unknown model fail before any process launch."""
    from ddp_core.application.ports import ApplicationError
    from ddp_local.model_runtime.process import ModelProcess
    installer = make_fixture_installer(workdir / "t19-backend")
    owned = ModelProcess(installer)
    evidence = {}
    try:
        try:
            await owned.start("fixture-model", runtime_id="not-a-reviewed-runtime", timeout=5)
            return record("t19-backend-unsupported", "fail", {}, "unknown runtime was accepted")
        except ApplicationError as exc:
            evidence["unknown_runtime_code"] = exc.code
        try:
            await owned.start("no-such-model", timeout=5)
            return record("t19-backend-unsupported", "fail", {}, "unknown model was accepted")
        except ApplicationError as exc:
            evidence["unknown_model_code"] = exc.code
        evidence["process_launched"] = owned.process is not None
        evidence["selection"] = owned.selection
        if (evidence["unknown_runtime_code"] == "model_backend_incompatible"
                and evidence["unknown_model_code"] == "model_not_found"
                and owned.process is None and owned.selection is None):
            return record("t19-backend-unsupported", "pass", evidence)
        return record("t19-backend-unsupported", "fail", evidence, "unexpected codes or side effect")
    finally:
        await owned.stop()
        installer.close()


async def check_t19_oom_visible(workdir):
    """CPU-side OOM path: visible failure, failed status, no silent fallback."""
    from ddp_core.application.ports import ApplicationError
    from ddp_local.model_runtime.process import ModelProcess
    installer = make_fixture_installer(workdir / "t19-oom", oom=True)
    owned = ModelProcess(installer)
    try:
        try:
            await owned.start("fixture-model", timeout=10)
            return record("t19-oom-visible", "fail", {}, "OOM runtime became ready")
        except ApplicationError as exc:
            code = exc.code
        status = owned.status()
        evidence = {"code": code, "status": status["status"], "error": status["error"],
                    "selection": status.get("backend")}
        if code == "out_of_memory" and status["status"] == "failed" and owned.selection is None:
            return record("t19-oom-visible", "pass", evidence)
        return record("t19-oom-visible", "fail", evidence, "OOM was not visible as out_of_memory")
    finally:
        await owned.stop()
        installer.close()


async def check_t19_explicit_cpu_recovery(workdir):
    """Recovery is an explicit CPU selection reporting the actual engine."""
    from ddp_local.model_runtime.process import ModelProcess
    installer = make_fixture_installer(workdir / "t19-recover")
    model_id, runtime_id = "fixture-model", "fixture-runtime"
    owned = ModelProcess(installer)
    try:
        try:
            selected = await owned.start(model_id, runtime_id=runtime_id, timeout=120)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return record("t19-explicit-cpu-recovery", "fail", {"error": code},
                          "explicit CPU start failed")
        provenance = selected.provenance or {}
        evidence = {"device": provenance.get("device"),
                    "model_id": provenance.get("model_id"),
                    "runtime_id": provenance.get("runtime_id"),
                    "offloaded_layers": provenance.get("offloaded_layers")}
        if provenance.get("device") == "cpu" and owned.selection is selected:
            return record("t19-explicit-cpu-recovery", "pass", evidence)
        return record("t19-explicit-cpu-recovery", "fail", evidence,
                      "CPU selection does not report the actual engine")
    finally:
        await owned.stop()
        installer.close()


async def check_t19_gpu_oom(workdir, installer, model_id, gpu_runtime_id, *, free_before=None):
    """Real GPU OOM: a card-sized oversized ctx must fail as OOM, not a refusal.

    The probe ctx is computed from the VRAM observed on the host
    (``_total_mib``) and the model's per-token KV size (``_kv_bytes_per_token``,
    read from the GGUF itself): ``ctx = ceil(1.5 * vram_bytes / kv_per_token)``.
    1.5x keeps the verdict honest on any card — 24G or 96G alike must produce
    a real ``out_of_memory``. A start refusal that is not an OOM (e.g. a ctx
    above a hard cap, a bad argument, a missing device) fails the check with
    its own code: it proves nothing about OOM surfacing.
    """
    from ddp_core.application.ports import ApplicationError
    from ddp_local.model_runtime.process import ModelProcess
    import math as _math
    artifact = installer.artifact(model_id)
    original_ctx = artifact.get("context_tokens")
    vram_mib = _total_mib()
    if vram_mib is None:
        return record("t19-gpu-oom", "fail", {"free_mib_before": free_before},
                      "nvidia-smi did not report total VRAM; refusing to guess a probe size")
    try:
        kv_per_token = _kv_bytes_per_token(installer.path(artifact))
    except Exception as exc:
        return record("t19-gpu-oom", "fail",
                      {"vram_mib": vram_mib, "error": f"{type(exc).__name__}: {exc}"},
                      "could not read KV geometry from the model file")
    probe_ctx = _math.ceil(1.5 * vram_mib * 2 ** 20 / kv_per_token)
    artifact["context_tokens"] = probe_ctx
    owned = ModelProcess(installer)
    try:
        try:
            await owned.start(model_id, runtime_id=gpu_runtime_id, timeout=300)
            status = owned.status()
            return record("t19-gpu-oom", "fail",
                          {"status": status, "context_tokens": probe_ctx,
                           "vram_mib": vram_mib, "kv_bytes_per_token": kv_per_token},
                          "oversized GPU context became ready; no OOM observed")
        except ApplicationError as exc:
            code = exc.code
        status = owned.status()
        evidence = {"code": code, "status": status["status"], "error": status["error"],
                    "context_tokens": probe_ctx, "vram_mib": vram_mib,
                    "kv_bytes_per_token": kv_per_token, "selection": None,
                    "free_mib_before": free_before,
                    "free_mib_after": _free_mib()}
        if (code == "out_of_memory" and status["status"] == "failed"
                and owned.selection is None and owned.process is None):
            return record("t19-gpu-oom", "pass", evidence)
        evidence["code_note"] = (
            "visible failure is not enough: an OOM check passes only on "
            "out_of_memory; a refusal (bad ctx cap, bad argument) fails. "
            "Extend process.py OOM markers if Vulkan logs differ.")
        return record("t19-gpu-oom", "fail", evidence,
                      f"GPU failure code was {code}, not out_of_memory")
    finally:
        artifact["context_tokens"] = original_ctx
        await owned.stop()


def _total_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip().splitlines()[0].strip()) if out.returncode == 0 else None
    except Exception:
        return None


def _free_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30)
        return out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 else None
    except Exception:
        return None


def _gguf_u32_kv_params(path):
    """Read Qwen3 KV-cache geometry straight from the GGUF metadata."""
    import struct as _struct

    def read_value(stream, vtype):
        if vtype in (0, 1, 7):
            return _struct.unpack("B", stream.read(1))[0]
        if vtype in (2, 3):
            return _struct.unpack("<H", stream.read(2))[0]
        if vtype in (4, 5):
            return _struct.unpack("<I", stream.read(4))[0]
        if vtype == 6:
            return _struct.unpack("<f", stream.read(4))[0]
        if vtype == 8:
            length = _struct.unpack("<Q", stream.read(8))[0]
            return stream.read(length).decode()
        if vtype in (10, 11):
            return _struct.unpack("<Q", stream.read(8))[0]
        if vtype == 12:
            return _struct.unpack("<d", stream.read(8))[0]
        if vtype == 9:
            elem = _struct.unpack("<I", stream.read(4))[0]
            return [read_value(stream, elem)
                    for _ in range(_struct.unpack("<Q", stream.read(8))[0])]
        raise ValueError(f"unknown GGUF metadata type {vtype}")

    want = {"general.architecture", "qwen3.block_count",
            "qwen3.attention.head_count_kv", "qwen3.attention.key_length",
            "qwen3.attention.value_length"}
    got: dict = {}
    with open(path, "rb") as stream:
        _magic, _ver = _struct.unpack("<II", stream.read(8))
        _n_tensors, n_kv = _struct.unpack("<QQ", stream.read(16))
        for _ in range(n_kv):
            klen = _struct.unpack("<Q", stream.read(8))[0]
            key = stream.read(klen).decode()
            vtype = _struct.unpack("<I", stream.read(4))[0]
            value = read_value(stream, vtype)
            if key in want:
                got[key] = value
    missing = want - set(got)
    if got.get("general.architecture") != "qwen3" or missing:
        raise ValueError(f"GGUF at {path} is not a Qwen3 model "
                         f"(architecture={got.get('general.architecture')!r}, missing={sorted(missing)})")
    return {k: got[k] for k in sorted(want - {"general.architecture"})}


def _kv_bytes_per_token(model_path):
    """KV-cache bytes per context token for the Qwen3 GGUF at ``model_path``.

    Derivation: each of ``block_count`` layers stores one key and one value
    vector per token, each ``head_count_kv * key_length`` / ``* value_length``
    fp16 elements (2 bytes). llama.cpp keeps the cache in the native
    precision for these GGUFs, so ``layers * kv_heads * (k_len + v_len) * 2``.
    For Qwen3-1.7B-Q8_0 that is ``28 * 8 * (128 + 128) * 2 = 114688``
    bytes/token (0.109375 MiB). Geometry comes from the file itself, never
    from a second constant that could drift from the pinned model.
    """
    params = _gguf_u32_kv_params(model_path)
    return (params["qwen3.block_count"] * params["qwen3.attention.head_count_kv"]
            * (params["qwen3.attention.key_length"] + params["qwen3.attention.value_length"]) * 2)


async def check_t19_gpu_offload_evidence(workdir, installer, model_id, gpu_runtime_id):
    """Real GPU profile: observed offload, devices and buffers in the log."""
    from ddp_local.model_runtime.process import ModelProcess
    owned = ModelProcess(installer)
    try:
        try:
            selected = await owned.start(model_id, runtime_id=gpu_runtime_id, timeout=300)
        except Exception as exc:
            return record("t19-gpu-offload-evidence", "fail",
                          {"error": getattr(exc, "code", type(exc).__name__)},
                          "real GPU start failed")
        provenance = selected.provenance or {}
        log_path = owned.workdir / "model.log" if owned.workdir else None
        tail = ""
        if log_path and log_path.is_file():
            tail = log_path.read_bytes()[-4096:].decode("utf-8", errors="replace")
        evidence = {"device": provenance.get("device"),
                    "gpu_devices": provenance.get("gpu_devices"),
                    "offloaded_layers": provenance.get("offloaded_layers"),
                    "total_layers": provenance.get("total_layers"),
                    "gpu_model_buffer_mib": provenance.get("gpu_model_buffer_mib"),
                    "runtime_id": provenance.get("runtime_id"),
                    "log_tail": tail[-1500:]}
        if (provenance.get("device") == "gpu"
                and (provenance.get("offloaded_layers") or 0) > 0
                and provenance.get("gpu_devices")
                and (provenance.get("gpu_model_buffer_mib") or 0) > 0):
            return record("t19-gpu-offload-evidence", "pass", evidence)
        return record("t19-gpu-offload-evidence", "fail", evidence,
                      "GPU offload was not observed in provenance")
    finally:
        await owned.stop()


# ------------------------------------------------------------- T59 checks

class _SlowUpstream(BaseHTTPRequestHandler):
    delay = 1.5

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(self.delay)
        body = json.dumps(
            {"id": "kit", "choices": [{"message": {"content": "ok"},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BudgetUpstream(BaseHTTPRequestHandler):
    """Over-budget for tiny max_tokens, honest for generous ones."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        want = int(request.get("max_tokens", 0))
        if want <= 16:
            content, finish, usage = "word " * (want + 50), "length", want + 50
        else:
            content, finish, usage = "fine.", "stop", 3
        response = {"choices": [{"message": {"content": content},
                                 "finish_reason": finish}],
                    "usage": {"completion_tokens": usage}}
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _serve(handler, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def _run_gateway(port, stub_port, *, vqa_cap, queue_max,
                       model_name="kit-slow", endpoint=None, timeout=15.0):
    """Real uvicorn gateway with a fakeredis-backed TaskStore (no live deps)."""
    import httpx
    import fakeredis.aioredis
    from ddp_gateway.config import ModelEntry, Registry, settings
    from ddp_gateway.main import app
    from ddp_gateway.services.task_store import TaskStore

    settings.parse_queue_max = queue_max
    app.state.registry = Registry(
        vqa_models={model_name: ModelEntry(
            endpoint=endpoint or f"http://127.0.0.1:{stub_port}", default=True,
            capabilities=["vision"])},
        parse_engines={"kit-engine": ModelEntry(
            endpoint="http://127.0.0.1:9/", default=True,
            capabilities=["parse"])},
    )
    app.state.http = httpx.AsyncClient(timeout=timeout, trust_env=False)
    import asyncio as _asyncio
    app.state.vqa_semaphore = _asyncio.Semaphore(vqa_cap)
    app.state.redis = fakeredis.aioredis.FakeRedis()
    app.state.task_store = TaskStore(app.state.redis, 600, 600.0)

    class _ArqStub:
        def __init__(self):
            self.jobs = []

        async def enqueue_job(self, name, *args):
            self.jobs.append((name, *args))

    app.state.arq = _ArqStub()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def _run_gateway_for_endpoint(port, endpoint, model_name, *, vqa_cap):
    return await _run_gateway(port, 0, vqa_cap=vqa_cap, queue_max=200,
                              model_name=model_name, endpoint=endpoint, timeout=120.0)


def _patch_gateway_engine_key(selection):
    """Authorize the gateway→engine hop with the owned engine's API key.

    The gateway registry has no API-key field and the kit must not modify
    gateway code for the test. Instead this wraps ``httpx.AsyncClient.send``
    process-wide for the leg: when the gateway forwards a chat request for
    the owned model to the owned endpoint, the wrapper sets the upstream
    ``Authorization`` from ``selection.api_key``. Client auth is untouched
    (gateway service token). Keys never enter the URL or the artifact.
    Installed before the gateway starts; returns a restore callable.
    """
    import httpx

    original_send = httpx.AsyncClient.send
    prefix = selection.endpoint.rsplit("/v1", 1)[0]
    engine_key = selection.api_key or ""

    async def patched_send(self, request, **kwargs):
        try:
            body = json.loads(request.content or b"{}")
        except Exception:
            body = {}
        if (engine_key and isinstance(body, dict)
                and body.get("model") == selection.model
                and str(request.url).startswith(prefix)):
            request.headers["Authorization"] = "Bearer " + engine_key
        return await original_send(self, request, **kwargs)

    httpx.AsyncClient.send = patched_send
    return lambda: setattr(httpx.AsyncClient, "send", original_send)


async def check_t59_gateway_concurrency(base_port):
    stub = _serve(_SlowUpstream, base_port)
    server = task = None
    try:
        import httpx
        from ddp_gateway.config import settings
        server, task = await _run_gateway(base_port + 1, base_port, vqa_cap=2, queue_max=200)
        headers = {"Authorization": f"Bearer {settings.service_token}"}

        async def one(client, index):
            response = await client.post(
                f"http://127.0.0.1:{base_port + 1}/v1/chat/completions",
                json={"model": "kit-slow",
                      "messages": [{"role": "user", "content": "hi"}]})
            code = (response.json().get("error", {}).get("code")
                    if response.status_code != 200 else "ok")
            return (index, response.status_code, code)

        async with httpx.AsyncClient(headers=headers, timeout=30.0,
                                     trust_env=False) as client:
            results = await asyncio.gather(*(one(client, i) for i in range(8)))
        counts: dict = {}
        for _, status, code in results:
            counts[(status, code)] = counts.get((status, code), 0) + 1
        evidence = {"parallel": 8, "vqa_cap": 2,
                    "outcomes": {f"{s}/{c}": n for (s, c), n in counts.items()}}
        ok200 = sum(n for (s, _), n in counts.items() if s == 200)
        limited = sum(n for (s, c), n in counts.items()
                      if s == 429 and c == "vqa_overloaded")
        if ok200 >= 1 and limited >= 1:
            return record("t59-gateway-concurrency", "pass", evidence)
        return record("t59-gateway-concurrency", "fail", evidence,
                      "parallel load did not show both success and visible 429")
    finally:
        if server is not None:
            server.should_exit = True
            await task
        stub.shutdown()
        stub.server_close()


async def check_t59_generation_budget(base_port):
    stub = _serve(_BudgetUpstream, base_port + 2)
    try:
        from ddp_core.application.ports import ApplicationError
        from ddp_local.providers import LocalExecutionProvider, ModelSelection
        provider = LocalExecutionProvider(ModelSelection(
            f"http://127.0.0.1:{base_port + 2}/v1", "kit-stub", "local"))
        evidence = {}
        try:
            await provider.generate([{"role": "user", "content": "say much"}],
                                    execution_policy="local_only",
                                    allow_remote=False, max_tokens=16)
            return record("t59-generation-budget", "fail", {},
                          "over-budget completion was accepted")
        except ApplicationError as exc:
            evidence["over_budget_code"] = exc.code
        output, _ = await provider.generate(
            [{"role": "user", "content": "say little"}],
            execution_policy="local_only", allow_remote=False, max_tokens=64)
        evidence["within_budget_output"] = output
        if evidence["over_budget_code"] == "generation_budget_exceeded" and output == "fine.":
            return record("t59-generation-budget", "pass", evidence)
        return record("t59-generation-budget", "fail", evidence,
                      "budget gate is imprecise")
    finally:
        stub.shutdown()
        stub.server_close()


def check_t59_federated_budget():
    """Repo regression: federated root budgets refuse over-spend visibly."""
    selection = (
        "tests/test_federation_metering.py::"
        "test_physical_attempts_are_counted_but_metering_stays_once",
        "tests/test_federation_tasks.py::"
        "test_expired_scope_is_410_and_probe_budget_exhaustion_is_visible",
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selection],
            cwd=str(CORPUS_API), capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return record("t59-federated-budget", "fail", {}, "regression subset timed out")
    tail = (proc.stdout + proc.stderr)[-2000:]
    evidence = {"returncode": proc.returncode,
                "seconds": round(time.monotonic() - started, 1),
                "tail": tail}
    if proc.returncode == 0:
        return record("t59-federated-budget", "pass", evidence)
    return record("t59-federated-budget", "fail", evidence,
                 "budget regression subset is red")


async def check_t59_engine_concurrency(base_port, selection):
    """Concurrency leg on the owned engine: gateway 429s under parallel load.

    ``selection`` is a live ``ModelSelection`` for the owned llama-server
    (GPU host: real Vulkan engine started by the kit; CPU proof: local CPU
    llama-server, labelled SIMULATED and kept out of the artifact).
    The *gateway* ``chat_completions`` semaphore (``vqa_cap=2``) is the gate
    under test: 6 parallel posts must show both forwarded 200s and visible
    429 ``vqa_overloaded``. The engine hop is authorized by a kit wrapper
    around the gateway's outbound send (registry has no key field); the key
    never enters the URL or the artifact.
    """
    import httpx
    from ddp_gateway.config import settings
    server = task = None
    restore = None
    try:
        # Patch FIRST: _run_gateway creates app.state.http; patching after
        # would wrap a client the gateway never uses again.
        restore = _patch_gateway_engine_key(selection)
        server, task = await _run_gateway_for_endpoint(
            base_port + 3, selection.endpoint.rsplit("/v1", 1)[0],
            selection.model, vqa_cap=2)
        headers = {"Authorization": "Bearer " + settings.service_token}

        async def one(client, index):
            response = await client.post(
                f"http://127.0.0.1:{base_port + 3}/v1/chat/completions",
                headers=headers,
                json={"model": selection.model,
                      "messages": [{"role": "user", "content": "hi"}]})
            code = (response.json().get("error", {}).get("code")
                    if response.status_code != 200 else "ok")
            return (index, response.status_code, code)

        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            results = await asyncio.gather(*(one(client, i) for i in range(6)))
        counts: dict = {}
        for _, status, code in results:
            counts[(status, code)] = counts.get((status, code), 0) + 1
        evidence = {"parallel": 6, "vqa_cap": 2, "owned_engine": True,
                    "provenance": redact(selection.provenance or {}),
                    "outcomes": {f"{s}/{c}": n for (s, c), n in counts.items()}}
        ok200 = sum(n for (s, _), n in counts.items() if s == 200)
        limited = sum(n for (s, c), n in counts.items()
                      if s == 429 and c == "vqa_overloaded")
        if ok200 >= 1 and limited >= 1:
            return record("t59-gpu-concurrency", "pass", evidence)
        return record("t59-gpu-concurrency", "fail", evidence,
                      "owned-engine parallel load did not show success + visible 429")
    finally:
        if restore is not None:
            restore()
        if server is not None:
            server.should_exit = True
            await task


async def check_t59_engine_generation_budget(selection, *, probe=None):
    """Budget leg on the owned engine: requires the budget-specific refusal.

    Passes only on ``generation_budget_exceeded`` raised by
    ``LocalExecutionProvider.generate`` against the owned engine. Any other
    code (including ``model_unavailable`` from a 401/404/500) is a fail with
    the raw outcome recorded — a miswired engine must never read as budget
    enforcement. ``probe`` overrides the tiny-budget probe for the CPU proof.
    """
    from ddp_core.application.ports import ApplicationError
    from ddp_local.providers import LocalExecutionProvider
    provider = LocalExecutionProvider(selection)
    tiny = dict(probe) if probe else {"max_tokens": 8}
    try:
        await provider.generate([{"role": "user", "content": "say much"}],
                                execution_policy="local_only",
                                allow_remote=False, **tiny)
        return record("t59-gpu-generation-budget", "fail",
                      {"owned_engine": True,
                       "provenance": redact(selection.provenance or {})},
                      "tiny-budget request succeeded; no budget pressure observed")
    except ApplicationError as exc:
        evidence = {"owned_engine": True, "code": exc.code,
                    "provenance": redact(selection.provenance or {})}
        if exc.code == "generation_budget_exceeded":
            return record("t59-gpu-generation-budget", "pass", evidence)
        return record("t59-gpu-generation-budget", "fail", evidence,
                      f"budget leg raised {exc.code}, not generation_budget_exceeded")


async def check_t59_queue_and_field_caps(base_port):
    stub = _serve(_SlowUpstream, base_port)
    server = task = None
    try:
        import httpx
        import uuid as _uuid
        from ddp_gateway.config import settings
        server, task = await _run_gateway(base_port + 1, base_port, vqa_cap=8, queue_max=3)
        from ddp_gateway.main import app as _app
        headers = {"Authorization": f"Bearer {settings.service_token}"}
        evidence = {}
        async with httpx.AsyncClient(headers=headers, timeout=15.0,
                                     trust_env=False) as client:
            base = f"http://127.0.0.1:{base_port + 1}"
            schema = {"type": "object",
                      "properties": {f"f{i}": {"type": "string",
                                               "description": f"field {i} query"}
                                     for i in range(65)}}
            response = await client.post(
                f"{base}/v1/extract",
                json={"file_url": "https://example.com/x.pdf", "schema": schema})
            evidence["too_many_fields"] = [response.status_code,
                                           response.json().get("error", {}).get("code")]
            for _ in range(3):
                await _app.state.task_store.create(
                    _uuid.uuid4().hex, "native", "kit-engine", None,
                    _uuid.uuid4().hex)
            response = await client.post(
                f"{base}/v1/parse", json={"file_url": "https://example.com/y.pdf"})
            evidence["queue_full"] = [response.status_code,
                                      response.json().get("error", {}).get("code")]
        ok = (evidence["too_many_fields"] == [400, "too_many_fields"]
              and evidence["queue_full"] == [429, "queue_full"])
        if ok:
            return record("t59-queue-and-field-caps", "pass", evidence)
        return record("t59-queue-and-field-caps", "fail", evidence,
                      "field or queue cap did not refuse visibly")
    finally:
        if server is not None:
            server.should_exit = True
            await task
        stub.shutdown()
        stub.server_close()


async def check_t59_upload_search_caps(workdir):
    import secrets as _secrets
    import httpx
    from ddp_local.http import create_app
    from ddp_local.runtime import LocalRuntime
    runtime = LocalRuntime(workdir / "t59-http")
    try:
        token = _secrets.token_urlsafe(48)
        host = "127.0.0.1:15494"
        app = create_app(runtime, session_token=token,
                         allowed_hosts={host}, start_worker=False)
        transport = httpx.ASGITransport(app=app)
        evidence = {}
        async with httpx.AsyncClient(
                transport=transport, base_url=f"http://{host}",
                headers={"Authorization": "Bearer " + token},
                trust_env=False) as client:
            big = await client.post(
                "/api/v1/resources/upload",
                headers={"Idempotency-Key": "kit-big", "X-Filename": "big.bin"},
                content=b"x" * (33 * 1024 * 1024))
            evidence["oversize_upload"] = [big.status_code,
                                           big.json().get("error", {}).get("code")]
            small = await client.post(
                "/api/v1/resources/upload",
                headers={"Idempotency-Key": "kit-small", "X-Filename": "small.txt"},
                content=b"hello")
            evidence["small_upload"] = small.status_code
            over = await client.post("/api/v1/search",
                                     json={"query": "hi", "limit": 101})
            evidence["over_limit_search"] = over.status_code
            fine = await client.post("/api/v1/search",
                                     json={"query": "hi", "limit": 5})
            evidence["normal_search"] = fine.status_code
            unknown = await client.post("/api/v1/models/no-such-model/start",
                                        headers={"Idempotency-Key": "kit-unknown"})
            body = unknown.json()
            evidence["unknown_model"] = [unknown.status_code,
                                         body.get("error", {}).get("code")]
            bad_backend = await client.post(
                "/api/v1/models/qwen3-1.7b-q8_0/start",
                headers={"Idempotency-Key": "kit-bad-backend"},
                json={"runtime_id": "not-a-reviewed-runtime"})
            evidence["bad_backend"] = [bad_backend.status_code,
                                       bad_backend.json().get("error", {}).get("code")]
        pending = [p.name for p in (workdir / "t59-http" / "blobs").iterdir()
                   if p.name.startswith(".pending-")]
        evidence["pending_leftovers"] = pending
        ok = (evidence["oversize_upload"] == [400, "input_too_large"]
              and evidence["small_upload"] == 202
              and evidence["over_limit_search"] == 422
              and evidence["normal_search"] == 200
              and evidence["unknown_model"] == [404, "model_not_found"]
              and evidence["bad_backend"] == [400, "model_backend_incompatible"]
              and pending == [])
        if ok:
            return record("t59-upload-search-caps", "pass", evidence)
        return record("t59-upload-search-caps", "fail", evidence,
                      "a budget cap did not refuse visibly or temp files leaked")
    finally:
        runtime.close()


def check_t59_cache_caps_gc():
    """Repo regression subset: cache caps under concurrency + GC keeps live refs."""
    selection = (
        "tests/test_cache.py", "tests/test_bundles.py", "-k",
        ("entries_cap or per_scope or bytes_cap or larger_than or concurrent_puts"
         " or t03_delete_first_copy or t03_running_task_retains"
         " or t03_durable_citation or t03_wiki_revision_references"),
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selection],
            cwd=str(CORPUS_API), capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return record("t59-cache-caps-gc", "fail", {}, "regression subset timed out")
    tail = (proc.stdout + proc.stderr)[-2000:]
    evidence = {"returncode": proc.returncode,
                "seconds": round(time.monotonic() - started, 1),
                "tail": tail}
    if proc.returncode == 0:
        return record("t59-cache-caps-gc", "pass", evidence)
    return record("t59-cache-caps-gc", "fail", evidence,
                 "cache/GC regression subset is red")


# ------------------------------------------------------------- T60 checks

def check_t60_model_checksums(workdir, *, real_installer=None):
    """Synthetic digest/size matrix through the real installer code (no network)."""
    import hashlib as _hashlib
    from ddp_core.application.ports import ApplicationError
    from ddp_local.model_runtime.install import ModelInstaller
    payload = b"GGUF\x03\0\0\0" + b"acceptance-fixture" * 64
    definitions = {
        "schema": "ddp-model-catalog/1", "revision": "gpu-acceptance-checksum",
        "artifacts": [{"id": "ck-model", "kind": "model", "name": "fixture",
                       "version": "fixed", "filename": "ck.gguf",
                       "bytes": len(payload),
                       "sha256": _hashlib.sha256(payload).hexdigest(),
                       "license": "Apache-2.0",
                       "license_url": "https://model.example/license",
                       "url": "https://model.example/fixed.gguf",
                       "format": "gguf-v3", "backend": "llama.cpp", "device": "cpu"}],
    }
    installer = ModelInstaller(workdir / "t60-ck", definitions=definitions)
    evidence = {}
    try:
        source = workdir / "ck-good.gguf"
        source.write_bytes(payload)
        evidence["import_good"] = installer.import_file("ck-model", source)["status"]
        name = installer.name(installer.artifact("ck-model"))
        with open(installer.directory / name, "r+b") as out:
            out.seek(9)
            out.write(b"!")
        try:
            installer.verify("ck-model")
            return record("t60-model-checksums", "fail", evidence,
                          "tampered artifact verified")
        except ApplicationError as exc:
            evidence["tamper_code"] = exc.code
        evidence["after_tamper"] = installer.status("ck-model")["status"]
        if real_installer is not None:
            real = {}
            for item in real_installer.definitions["artifacts"]:
                status = real_installer.status(item["id"])
                real[item["id"]] = {
                    "status": status["status"],
                    "digest_matches_catalog": status.get("manifest", {}).get("sha256") == item["sha256"],
                    "bytes": status.get("downloaded_bytes")}
            evidence["real_artifacts"] = real
            if any(v["status"] != "installed" for v in real.values()):
                return record("t60-model-checksums", "fail", evidence,
                              "a real catalog artifact is not installed+verified")
        if (evidence["import_good"] == "installed"
                and evidence.get("tamper_code") == "model_digest_mismatch"
                and evidence["after_tamper"] == "verification_required"):
            return record("t60-model-checksums", "pass", evidence)
        return record("t60-model-checksums", "fail", evidence,
                      "checksum matrix did not behave")
    finally:
        installer.close()


def _synthetic_release(stage, *, version="0.9.0", machine=None, cache_tag=None,
                       tamper=False):
    """Minimal release tree+manifest+archive in the update_check shape."""
    import sys as _sys
    machine = machine or platform.machine()
    cache_tag = cache_tag or _sys.implementation.cache_tag
    (stage / "deepdocparse").write_text("#!/bin/sh\necho kit\n")
    (stage / "LICENSE").write_text("test license\n")
    try:
        from ddp_local.workspace_schemas import WORKSPACE_SCHEMA_VERSIONS as live
        schemas = {name: sorted(versions) for name, versions in live.items()}
    except Exception:
        schemas = {"workspace.sqlite3": [0, 1, 2, 3], "consents.sqlite3": [0, 1, 2, 3, 4]}
    files = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(stage.rglob("*")) if p.is_file()}
    build = {"format": 1, "epoch": 1789257600, "electron": "44.3.0",
             "runtime": {"distributions": {}},
             "license_manifest": {"electron": {"LICENSE": files["LICENSE"]},
                                  "distributions": {}},
             "files": files}
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    # Re-list now that BUILD-MANIFEST.json itself exists; the builder keeps it
    # out of its own files map (update_check.verify_tree allows that one).
    files = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(stage.rglob("*"))
             if p.is_file() and p.name != "BUILD-MANIFEST.json"}
    build["files"] = files
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(build) + "\n")
    marker = {"format": 1, "name": "deepdocparse-desktop", "version": version,
              "epoch": 1789257600,
              "platform": {"system": "linux", "machine": machine},
              "python": {"major_minor": list(_sys.version_info[:2]),
                         "cache_tag": cache_tag, "soabi": "test-soabi",
                         "machine": machine},
              "electron": "44.3.0", "runtime_lock_sha256": "0" * 64,
              "workspace_schemas": schemas}
    (stage / "RELEASE-MANIFEST.json").write_text(json.dumps(marker) + "\n")
    archive = stage.parent / "kit-release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    if tamper:
        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        archive.write_bytes(bytes(raw))
    manifest = {
        "format": 1, "name": "deepdocparse-desktop", "version": version,
        "archive": archive.name, "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "platform": {"system": "linux", "machine": machine},
        "python": {"major_minor": list(_sys.version_info[:2]),
                   "cache_tag": cache_tag, "soabi": "test-soabi",
                   "machine": machine},
        "electron": "44.3.0",
        "build_manifest_sha256": hashlib.sha256(
            (stage / "BUILD-MANIFEST.json").read_bytes()).hexdigest(),
        "workspace_schemas": schemas, "signature": None,
    }
    if tamper:
        # Manifest keeps the pre-tamper digest: recompute from untampered bytes.
        manifest["sha256"] = "0" * 64
    manifest_path = stage.parent / "release.json"
    manifest_path.write_text(json.dumps(manifest) + "\n")
    return archive, manifest_path


def check_t60_release_verify(workdir):
    """Checksum/ABI/platform matrix through scripts/update_check.py."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import update_check
    finally:
        sys.path.pop(0)
    evidence: dict = {}

    def verify(stage_name, **kwargs):
        stage = workdir / stage_name
        stage.mkdir(parents=True, exist_ok=True)
        archive, manifest = _synthetic_release(stage, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = update_check.main(
                ["verify", "--manifest", str(manifest), "--archive", str(archive),
                 "--allow-unsigned"])
        return code, err.getvalue()[-500:]

    code, _ = verify("t60-good")
    evidence["good_rc"] = code
    code, err = verify("t60-tampered", tamper=True)
    evidence["tampered_rc"] = code
    evidence["tampered_says_checksum"] = "CHECKSUM MISMATCH" in err
    code, err = verify("t60-abi", cache_tag="cpython-000")
    evidence["abi_rc"] = code
    evidence["abi_says_abi"] = "ABI" in err
    other_machine = "aarch64" if platform.machine() == "x86_64" else "x86_64"
    code, err = verify("t60-platform", machine=other_machine)
    evidence["platform_rc"] = code
    if (evidence["good_rc"] == 0 and evidence["tampered_rc"] == 1
            and evidence["tampered_says_checksum"] and evidence["abi_rc"] == 1
            and evidence["abi_says_abi"] and evidence["platform_rc"] == 1):
        return record("t60-release-verify", "pass", evidence)
    return record("t60-release-verify", "fail", evidence,
                  "checksum/ABI/platform matrix did not refuse precisely")


def check_t60_host_report(host, gpu):
    """Real CPU vs GPU row content; skipped unless an NVIDIA GPU was observed."""
    if not gpu.get("present"):
        return record("t60-host-report", "skipped", {"host": host, "gpu": gpu},
                      "no NVIDIA GPU observed on this host; the compat row needs one")
    row_md = (
        "| 路径 | 结果 |\n|---|---|\n"
        f"| CPU ({host['platform']}, py{host['python']}) | "
        "local runtime profile, checksum/ABI matrix in-artifact |\n"
        f"| GPU ({gpu['devices'][0]['name'] if gpu['devices'] else 'observed'}) | "
        "offload evidence in-artifact |\n")
    return record("t60-host-report", "pass",
                  {"host": host, "gpu": gpu, "compat_row_md": row_md})


# ------------------------------------------------------------------ runner

async def run_async_checks(workdir, base_port, *, mode, installer=None):
    results = []
    results.append(await check_t19_backend_unsupported(workdir))
    results.append(await check_t19_oom_visible(workdir))
    if mode == "gpu" and installer is not None:
        model_id, gpu_rt, cpu_rt = "qwen3-1.7b-q8_0", "llama-cpp-vulkan-linux-x64", \
            "llama-cpp-cpu-linux-x64"
        results.append(await check_t19_gpu_offload_evidence(workdir, installer, model_id, gpu_rt))
        results.append(await check_t19_gpu_oom(
            workdir, installer, model_id, gpu_rt, free_before=_free_mib()))
        results.append(await _real_cpu_recovery(workdir, installer, model_id, cpu_rt))
    else:
        results.append(record("t19-gpu-oom", "skipped", {"mode": mode},
                              "needs a real NVIDIA GPU + Vulkan runtime"))
        results.append(record("t19-gpu-offload-evidence", "skipped", {"mode": mode},
                              "needs a real NVIDIA GPU + Vulkan runtime"))
        results.append(await check_t19_explicit_cpu_recovery(workdir))
    results.append(await check_t59_gateway_concurrency(base_port))
    results.append(await check_t59_generation_budget(base_port))
    results.append(await check_t59_queue_and_field_caps(base_port))
    results.append(await check_t59_upload_search_caps(workdir))
    results.append(await asyncio.to_thread(check_t59_cache_caps_gc))
    results.append(await asyncio.to_thread(check_t59_federated_budget))
    if mode != "gpu" or installer is None:
        results.append(record("t59-gpu-concurrency", "skipped", {"mode": mode},
                              "no owned engine on this host; the GPU run starts one"))
        results.append(record("t59-gpu-generation-budget", "skipped", {"mode": mode},
                              "no owned engine on this host; the GPU run starts one"))
        return results
    engine, start_error = await _start_owned_vulkan_engine(installer)
    if engine is None:
        results.append(record(
            "t59-gpu-concurrency", "fail", {"start_error": start_error},
            "owned Vulkan engine failed to start; refusing to skip"))
        results.append(record(
            "t59-gpu-generation-budget", "fail", {"start_error": start_error},
            "owned Vulkan engine failed to start; refusing to skip"))
        return results
    try:
        results.append(await check_t59_engine_concurrency(base_port, engine.selection))
        results.append(await check_t59_engine_generation_budget(engine.selection))
    finally:
        await engine.stop()
    return results


async def _start_owned_vulkan_engine(installer):
    """Start the real Vulkan engine through the real ``ModelProcess``.

    Returns (``_StartedEngine``, None) with the live ``ModelSelection`` and
    the process kept alive across both T59 legs, or (None, raw_code) when the
    start itself fails. A failed start is a fail, never a skip.
    """
    from ddp_local.model_runtime.process import ModelProcess
    owned = ModelProcess(installer)
    try:
        selected = await owned.start("qwen3-1.7b-q8_0",
                                     runtime_id="llama-cpp-vulkan-linux-x64",
                                     timeout=300)
    except Exception as exc:
        await owned.stop()
        return None, getattr(exc, "code", type(exc).__name__)
    return _StartedEngine(owned, selected), None


class _StartedEngine:
    """Live owned llama-server: ``ModelSelection`` + lifecycle handle."""

    def __init__(self, process, selection):
        self.process = process
        self.selection = selection

    async def stop(self):
        await self.process.stop()


async def _real_cpu_recovery(workdir, installer, model_id, cpu_rt):
    from ddp_local.model_runtime.process import ModelProcess
    owned = ModelProcess(installer)
    try:
        try:
            selected = await owned.start(model_id, runtime_id=cpu_rt, timeout=300)
        except Exception as exc:
            return record("t19-explicit-cpu-recovery", "fail",
                          {"error": getattr(exc, "code", type(exc).__name__)},
                          "explicit real-CPU start failed")
        provenance = selected.provenance or {}
        evidence = {"device": provenance.get("device"),
                    "model_id": provenance.get("model_id"),
                    "runtime_id": provenance.get("runtime_id"),
                    "real_hardware": True}
        if provenance.get("device") == "cpu":
            return record("t19-explicit-cpu-recovery", "pass", evidence)
        return record("t19-explicit-cpu-recovery", "fail", evidence,
                      "CPU selection does not report the actual engine")
    finally:
        await owned.stop()


def ensure_gpu_downloads(workspace):
    """Download the real catalog artifacts on the GPU host (network needed).

    Honors the ``*_PROXY`` environment (e.g. AutoDL's academic proxy from
    ``source /etc/network_turbo``, which covers huggingface.co and github
    releases): the installer builds its HTTP client with ``trust_env=False``,
    so pass an explicitly proxy-configured client. Retries each artifact
    (resuming the partial file) because transient cloud-link drops are
    routine on these hosts.
    """
    import asyncio as _asyncio
    import httpx as _httpx
    from ddp_local.model_runtime.install import ModelInstaller
    installer = ModelInstaller(workspace / "models")
    ids = ["qwen3-1.7b-q8_0", "llama-cpp-vulkan-linux-x64", "llama-cpp-cpu-linux-x64"]

    async def download_all():
        # AutoDL's academic proxy MITMs TLS with its own CA
        # (/usr/local/share/ca-certificates/autodl-signed.crt): the uv-built
        # venv's certifi bundle lacks it, so verify must point at the proxy
        # CA when a proxy is configured. REQUESTS_CA_BUNDLE/SSL_CERT_FILE
        # (set by /etc/network_turbo) or the well-known path are honored;
        # without a proxy the default bundle is used untouched.
        proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
        verify: object = True
        if proxy:
            from pathlib import Path as _Path
            ca_hint = (os.environ.get("REQUESTS_CA_BUNDLE")
                       or os.environ.get("SSL_CERT_FILE")
                       or "/usr/local/share/ca-certificates/autodl-signed.crt")
            if ca_hint and _Path(ca_hint).is_file():
                verify = ca_hint
        async with _httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                      timeout=60, proxy=proxy or None,
                                      verify=verify) as client:
            for identifier in ids:
                state = installer.status(identifier)
                print(f"[gpu-acceptance] {identifier}: {state['status']} "
                      f"{state.get('downloaded_bytes', 0)} bytes"
                      + (" (via proxy)" if proxy else " (direct)"), flush=True)
                if state["status"] == "installed":
                    continue
                last = None
                for attempt in range(1, 5):
                    try:
                        result = await installer.download(identifier, client=client)
                    except Exception as exc:
                        last = exc
                        print(f"[gpu-acceptance] {identifier} attempt {attempt}/4: "
                              f"{getattr(exc, 'code', type(exc).__name__)}; retrying",
                              flush=True)
                        continue
                    print(f"[gpu-acceptance] {identifier} -> {result['status']}", flush=True)
                    last = None
                    break
                if last is not None:
                    raise last
    _asyncio.run(download_all())
    return installer


def build_artifact(mode, checks, host, gpu, out_path):
    by_id = {c["id"]: c for c in checks}
    rows = {}
    for row, ids in (("T19", CHECKS_T19), ("T59", CHECKS_T59), ("T60", CHECKS_T60)):
        members = [by_id[i] for i in ids if i in by_id]
        skipped = [m["id"] for m in members if m["status"] == "skipped"]
        failed = [m["id"] for m in members if m["status"] == "fail"]
        rows[row] = {
            "pass": bool(members) and not skipped and not failed,
            "checks": [m["id"] + "=" + m["status"] for m in members],
            "skipped": skipped, "failed": failed,
        }
    artifact = {
        "schema": "ddp-gpu-acceptance/1",
        "mode": mode,
        "generated_at": utcnow(),
        "host": host, "gpu": gpu,
        "checks": checks, "rows": rows,
        "notes": [
            "skipped checks never count as pass; any skipped check makes its row NOT pass.",
            ("dry-run: GPU-required checks are skipped by design; non-GPU paths "
             "ran for real on a CPU-only machine.")
            if mode == "dry-run" else
            "gpu mode: ran on a real NVIDIA Linux host; see host/gpu facts.",
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(redact(artifact), indent=2, ensure_ascii=False) + "\n")
    return artifact


PRINT_ORDER = (CHECKS_T19, CHECKS_T59, CHECKS_T60)


def summarize(artifact_path):
    data = json.loads(Path(artifact_path).read_text())
    lines = [f"# gpu-acceptance {data['mode']} {data['generated_at']} "
             f"(rev {data['host'].get('repo_rev', '?')[:12]})", ""]
    for row in ("T19", "T59", "T60"):
        info = data["rows"][row]
        members = [c for c in data["checks"] if c["row"] == row]
        lines.append(f"## {row} {'PASS' if info['pass'] else 'NOT PASS'}")
        for member in members:
            first = (member.get("reason") or
                     json.dumps(member.get("evidence", {}), ensure_ascii=False)[:200])
            lines.append(f"- {member['id']}: {member['status']} — {first}")
        if info["pass"]:
            lines.append(f"proposed evidence: `docs/refactor/artifacts/{Path(artifact_path).name}` "
                         f"({', '.join(m['id'] for m in members)} all pass)")
            lines.append("proposed gap: —")
        else:
            missing = [m["id"] for m in members if m["status"] != "pass"]
            lines.append(f"proposed evidence: `docs/refactor/artifacts/{Path(artifact_path).name}` "
                         f"(partial: {', '.join(m['id'] + '=' + m['status'] for m in members)})")
            lines.append(f"proposed gap: {', '.join(missing)} 未覆盖（需 GPU/外部条件或仍红）")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dry-run", "gpu"], default="dry-run")
    parser.add_argument("--out", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--base-port", type=int, default=15490)
    parser.add_argument("--summarize", default="")
    args = parser.parse_args(argv)
    if args.summarize:
        print(summarize(args.summarize))
        return 0
    if not (15490 <= args.base_port <= 15491):
        print("base-port must be 15490 or 15491: the kit binds base..base+3 "
              "(15494 stays reserved for the local HTTP probe)", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="gpu-acceptance-"))
    workspace = Path(args.workspace) if args.workspace else tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    host, gpu = host_facts(), gpu_facts()
    host["revision_file"] = (ROOT / "REVISION").read_text().strip() \
        if (ROOT / "REVISION").is_file() else None
    installer = None
    try:
        if args.mode == "gpu":
            if not gpu["present"]:
                print("gpu mode needs nvidia-smi with at least one device; "
                      "aborting before any GPU check", file=sys.stderr)
                return 2
            installer = ensure_gpu_downloads(workspace)
        checks = asyncio.run(run_async_checks(
            workspace, args.base_port, mode=args.mode, installer=installer))
        checks.append(check_t60_model_checksums(
            workspace, real_installer=installer if args.mode == "gpu" else None))
        checks.append(check_t60_release_verify(workspace))
        checks.append(check_t60_host_report(host, gpu))
        order = [c for group in PRINT_ORDER for c in group]
        checks.sort(key=lambda c: order.index(c["id"]) if c["id"] in order else 99)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        if args.out:
            out_path = Path(args.out)
        elif args.mode == "gpu":
            out_path = ROOT / f"docs/refactor/artifacts/gpu-acceptance-{stamp}.json"
        else:
            out_path = Path.cwd() / f"gpu-acceptance-dry-run-{stamp}.json"
        artifact = build_artifact(args.mode, checks, host, gpu, out_path)
        for check in checks:
            print(f"[{check['status']:>7}] {check['id']} {check.get('reason', '')}")
        failed_rows = []
        for row in ("T19", "T59", "T60"):
            info = artifact["rows"][row]
            print(f"row {row}: {'PASS' if info['pass'] else 'NOT PASS'} "
                  f"({', '.join(info['checks'])})")
            if not info["pass"]:
                failed_rows.append(row)
        print(f"artifact: {out_path}")
        if failed_rows:
            print(f"NOT PASS rows: {', '.join(failed_rows)}", file=sys.stderr)
            return 1
        return 0
    finally:
        if installer is not None:
            installer.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
