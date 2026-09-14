#!/usr/bin/env python3
"""Actual CPU model evaluation. No mock transport, downloads or remote model fallback."""

import argparse
import asyncio
import io
import hashlib
import json
import os
import platform
import socket
import subprocess
import time
import uuid
from pathlib import Path

from ddp_core.application.ports import ApplicationError
from ddp_local.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]


def chinese_pdf():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    page = canvas.Canvas(output, invariant=True)
    page.setFont("STSong-Light", 14)
    page.drawString(50, 730, "极光计划性能说明")
    page.drawString(50, 695, "极光计划每秒处理125件。该指标是持续处理吞吐量。")
    page.drawString(50, 660, "原始测试条件为本地资料输入；所有结论必须指向原文证据。")
    page.save()
    return output.getvalue()


async def evaluate(args):
    if args.output.exists():
        raise RuntimeError("evaluation output already exists; select a new report path")
    isolated = False
    network_boundary = None
    if args.offline_namespace:
        if socket.if_nameindex() != [(1, "lo")]:
            raise RuntimeError("strict offline evaluation requires a network namespace with only loopback")
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
        routes = json.loads(subprocess.check_output(["ip", "-j", "route"]))
        if routes:
            raise RuntimeError("offline network namespace unexpectedly has external routes")
        routes_v6 = json.loads(subprocess.check_output(["ip", "-j", "-6", "route"]))
        if any(route.get("dev") != "lo" for route in routes_v6):
            raise RuntimeError("offline network namespace unexpectedly has external IPv6 routes")
        network_boundary = {"interfaces": socket.if_nameindex(), "ipv4_routes": routes, "ipv6_routes": routes_v6}
        isolated = True
    runtime = LocalRuntime(args.workspace)
    run_id = uuid.uuid4().hex
    report = {"schema": "ddp-local-model-eval/1", "run_id": run_id,
              "python": platform.python_version(), "platform": platform.platform(),
              "network_namespace": os.readlink("/proc/self/ns/net"),
              "offline_namespace": isolated, "network_boundary": network_boundary, "model_id": args.model,
              "started_at": time.time(), "cases": [], "status": "running"}
    try:
        started = time.monotonic()
        await runtime.start_model(args.model)
        report["startup_seconds"] = time.monotonic() - started
        report["provider"] = runtime.provider.model.provenance
        actual_generate = runtime.provider.generate
        trace = []

        async def recorded_generate(messages, **kwargs):
            # Record the real adapter call; do not replace transport or output.
            record = {"messages": messages, "policy": kwargs}
            trace.append(record)
            output, provider = await actual_generate(messages, **kwargs)
            record.update(output=output, provider=provider)
            return output, provider

        runtime.provider.generate = recorded_generate
        cases = [
            ("english", (ROOT / "tests/fixtures/sample.pdf").read_bytes(),
             "What is the answer in the contract? State the value with an original citation.", "42"),
            ("chinese", chinese_pdf(), "极光计划每秒处理多少件？", "125"),
            ("code", (ROOT / "tests/fixtures/code-corpus.pdf").read_bytes(),
             "In the code mentioning HttpRequestParser, what is the value of TARGET_IDENTIFIER?", "HttpRequestParser"),
        ]
        for name, data, query, expected in cases:
            entry = {"name": name, "query": query, "expected_value": expected,
                     "input_sha256": hashlib.sha256(data).hexdigest(), "input_bytes": len(data)}
            try:
                task = runtime.upload_stream(io.BytesIO(data), filename=name + ".pdf", operation_key="model-eval-source-" + name)
                if runtime.store.task(task["id"])["status"] != "succeeded":
                    completed = await runtime.work_once()
                    if not completed or completed["id"] != task["id"] or completed["status"] != "succeeded":
                        raise RuntimeError("CPU parse did not complete for the selected fixture")
                version = task["version_id"]
                entry["source"] = runtime.source(runtime.store.version(version))
                for operation in ("answer", "wiki"):
                    began = time.monotonic()
                    trace.clear()
                    try:
                        result = await runtime.answer(
                            query, version_ids=[version], wiki=operation == "wiki",
                            execution_policy="local_only", allow_remote=False,
                            operation_key=f"model-eval-{run_id}-{name}-{operation}",
                        )
                        obtained = {item["id"] for item in result["evidence"]}
                        structural = bool(result["assertions"]) and all(
                            not claim["unsupported"] and claim["evidence_ids"] and
                            set(claim["evidence_ids"]).issubset(obtained)
                            for claim in result["assertions"]
                        )
                        original = bool(result["evidence"]) and all(
                            item["evidence"]["source_type"] == "source" and
                            item["evidence"]["locator"]["bbox"] and
                            item["evidence"]["source_version_id"] == version and
                            item["evidence"]["source_digest"] == entry["source"]["source_digest"]
                            for item in result["evidence"]
                        )
                        value_correct = expected in result["answer"]
                        entry[operation] = {
                            "answer": result["answer"], "assertions": result["assertions"],
                            "evidence": result["evidence"], "provider": result.get("provider"),
                            "pages": result.get("pages"), "structural_citations": structural,
                            "original_bbox": original, "expected_value_present": value_correct,
                            "semantic_review": result.get("semantic_review"),
                            "status": "passed" if structural and original and value_correct else "failed",
                        }
                    except (ApplicationError, RuntimeError) as exc:
                        entry[operation] = {"status": "failed", "error": getattr(exc, "code", type(exc).__name__),
                                            "message": str(exc)}
                    entry[operation].update(seconds=time.monotonic() - began, trace=list(trace))
                    print(json.dumps({"case": name, "operation": operation, "status": entry[operation]["status"]}), flush=True)
                entry["status"] = "passed" if all(entry[op]["status"] == "passed" for op in ("answer", "wiki")) else "failed"
            except (ApplicationError, RuntimeError) as exc:
                entry.update(status="failed", error=getattr(exc, "code", type(exc).__name__), message=str(exc))
            report["cases"].append(entry)
        report["status"] = "passed" if all(c["status"] == "passed" for c in report["cases"]) else "failed"
        report["limits"] = ["expected-value checks do not establish general semantic entailment",
                            "Wiki is a citation-bound draft; multi-page relations and revision editing remain separate acceptance"]
    except Exception as exc:
        report.update(status="failed", error=getattr(exc, "code", type(exc).__name__), message=str(exc))
    finally:
        process, directory = runtime.model_process.process, runtime.model_process.workdir
        await runtime.stop_model()
        report["shutdown"] = {"owned_process_exited": process is None or process.poll() is not None,
                              "private_runtime_directory_removed": directory is None or not directory.exists()}
        runtime.close()
        report["finished_at"] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
            out.write("\n")
    print(json.dumps({"status": report["status"], "report": str(args.output.absolute()),
                      "cases": [{"name": c["name"], "status": c["status"]} for c in report["cases"]]}))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="qwen3-1.7b-q8_0")
    parser.add_argument("--offline-namespace", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))


if __name__ == "__main__":
    main()
