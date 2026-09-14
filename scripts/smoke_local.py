#!/usr/bin/env python3
"""Real CPU/SQLite/CLI/loopback HTTP smoke. No control SQL, Redis or model downloads."""

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def compare_server(archive):
    """Opt-in adapter parity; needs server packages, never connects to a server DB."""
    import httpx

    from ddp_core.bundle import read_bundle
    from ddp_corpus.compilation import compile_document
    from ddp_corpus.config import settings
    from ddp_corpus.models import Document, ParseJob
    from ddp_corpus.storage import MemoryStorage
    from ddp_gateway.services import borndigital, layout

    bundle = read_bundle(io.BytesIO(archive.read_bytes()))
    pdf = bundle.files["source.bin"]
    frozen_layout = json.loads(bundle.files["layout.json"])["layout"]
    gateway_layout = layout.build(
        borndigital.extract_pages(pdf), engine="borndigital", code_detection="heuristic"
    )
    assert gateway_layout == frozen_layout, "gateway and local parser layouts diverged"
    provider = json.loads(bundle.files["provenance.json"])[0]["provider"]
    storage = MemoryStorage()
    await storage.put("source.pdf", pdf, "application/pdf")
    original_settings = {
        name: getattr(settings, name)
        for name in ("chunk_max_chars", "embedding_model", "chat_model", "compile_vision_enabled")
    }

    def deny_network(request):
        raise AssertionError("CPU compile parity must not dispatch a model request")

    try:
        settings.chunk_max_chars = 800
        settings.embedding_model = provider["embedding_model"]
        settings.chat_model = provider["vision_model"]
        settings.compile_vision_enabled = False
        async with httpx.AsyncClient(transport=httpx.MockTransport(deny_network)) as http:
            compiled = await compile_document(
                storage=storage, http=http,
                document=Document(id="parity-document", mime="application/pdf",
                                  object_key="source.pdf"),
                job=ParseJob(id="parity-job", options_hash=provider["parse_options_hash"]),
                layout=gateway_layout,
            )
    finally:
        for name, value in original_settings.items():
            setattr(settings, name, value)
    assert compiled.provider == provider
    assert len(compiled.chunks) == len(bundle.evidence) > 0
    for chunk, record in zip(compiled.chunks, bundle.evidence):
        evidence = record["evidence"]
        locator = evidence["locator"]
        assert (chunk["seq"], chunk["text"], chunk["page_idx"], chunk["bbox"],
                chunk["block_type"]) == (
            locator["seq"], record["excerpt"], locator["physical_page_index"],
            locator["bbox"], evidence["block_type"],
        )
        assert chunk["seq"] in compiled.crop_keys, "server must render each source anchor"
    return {"status": "passed", "source_atoms": len(compiled.chunks),
            "vision_requests": compiled.vision_requests,
            "degraded": compiled.degraded}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=ROOT / "tests/fixtures/sample.pdf")
    parser.add_argument("--query", default="contract")
    parser.add_argument(
        "--model-endpoint", help="explicit local loopback OpenAI-compatible /v1 endpoint"
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--compare-server", action="store_true",
        help="also compare the real gateway parser and corpus compilation adapter (server packages required)",
    )
    args = parser.parse_args()
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(ROOT / "python/ddp_local"),
                str(ROOT / "python/ddp_core"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    }
    with tempfile.TemporaryDirectory(prefix="ddp-local-smoke-") as directory:
        workspace, replica = Path(directory) / "one", Path(directory) / "two"
        base = [sys.executable, "-m", "ddp_local", "--workspace", str(workspace)]

        def cli(arguments, *, target=None, extra=None, success=True):
            command = (
                base
                if target is None
                else [sys.executable, "-m", "ddp_local", "--workspace", str(target)]
            )
            process = subprocess.run(
                [*command, *(extra or []), *arguments],
                env=environment,
                capture_output=True,
                text=True,
                timeout=140,
            )
            result = json.loads(process.stdout)
            assert (process.returncode == 0) == success, result
            return result

        identity = cli(["init"])
        task = cli(["upload", str(args.pdf), "--key", "smoke"])
        assert cli(["work", "--once"])["status"] == "succeeded"
        hit = cli(["search", args.query])["hits"][0]
        evidence = cli(["evidence", hit["evidence_id"]])
        assert evidence["evidence"]["locator"]["bbox"]
        archive = Path(directory) / "result.ddp.zip"
        cli(["export", task["version_id"], "--output", str(archive)])
        parity = asyncio.run(compare_server(archive)) if args.compare_server else "not_requested"
        imported = cli(["import", str(archive), "--key", "replica"], target=replica)
        replica_hit = cli(["search", args.query], target=replica)["hits"][0]
        copied = cli(["evidence", replica_hit["evidence_id"]], target=replica)
        assert copied["evidence"] == evidence["evidence"]
        missing = cli(["answer", args.query], success=False)
        assert missing["error"]["code"] == "model_unavailable"
        generation = "not_run_model_unavailable"
        if args.model_endpoint or args.model:
            assert args.model_endpoint and args.model
            selected = ["--model-endpoint", args.model_endpoint, "--model", args.model]
            answer = cli(["answer", args.query], extra=selected)
            wiki = cli(["wiki", args.query], extra=selected)
            assert answer["assertions"] and wiki["pages"]
            assert answer["provider"]["location"] == "local"
            generation = "actual_local_model_passed"
        token_file = Path(directory) / "private-session.json"
        server = subprocess.Popen(
            [*base, "serve", "--token-file", str(token_file)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            import httpx

            deadline = time.monotonic() + 15
            while not token_file.exists():
                assert server.poll() is None and time.monotonic() < deadline, (
                    "local HTTP did not start"
                )
                time.sleep(0.05)
            # File publication may precede uvicorn serving by a short interval.
            while True:
                try:
                    session = json.loads(token_file.read_text())
                    with httpx.Client(
                        base_url=session["url"], trust_env=False, timeout=1
                    ) as client:
                        unauth = client.get("/api/v1/capabilities")
                    break
                except (httpx.HTTPError, json.JSONDecodeError):
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            with httpx.Client(
                base_url=session["url"],
                trust_env=False,
                headers={"Authorization": "Bearer " + session["token"]},
                timeout=5,
            ) as client:
                assert unauth.status_code == 401
                assert (
                    client.get(
                        "/api/v1/capabilities", headers={"Origin": "https://foreign.example"}
                    ).status_code
                    == 403
                )
                assert (
                    client.get(
                        "/api/v1/capabilities", headers={"Host": "foreign.example"}
                    ).status_code
                    == 403
                )
                result = client.post("/api/v1/search", json={"query": args.query})
                assert result.status_code == 200 and result.json()["hits"]
                assert client.get("/api/v1/evidence/" + hit["evidence_id"]).status_code == 200
                assert (
                    client.get("/api/v1/versions/" + task["version_id"] + "/bundle").status_code
                    == 200
                )
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        print(
            json.dumps(
                {
                    "status": "passed",
                    "parse": "actual_pdfium_cpu_subprocess",
                    "retrieval": "sqlite_fts5_shared_tokenizer_and_gates",
                    "evidence": "stable_bbox_identity",
                    "bundle": "separate_workspace_identity_preserved",
                    "http": "actual_loopback_auth_host_origin",
                    "generation": generation,
                    "server_adapter_parity": parity,
                    "fixture": str(args.pdf),
                    "source_version": task["version_id"],
                    "replica_version": imported["version_id"],
                    "environment_id": identity["environment_id"],
                }
            )
        )


if __name__ == "__main__":
    main()
