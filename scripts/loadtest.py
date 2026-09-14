#!/usr/bin/env python3
"""Local capacity harness for a running dev stack.

Drives realistic user flows through the unified entry (default 127.0.0.1:8080):
register/login, presigned upload + server digest, parse, corpus search, and a
federation task submit/poll attempt.  Concurrency levels 1/4/16 are run in
sequence and each step's latency percentiles and error rate land in a JSON
report plus a human summary.

No new dependencies: argparse + httpx (the stack's own e2e scripts use it).

    scripts/dev.sh up                    # once
    .venv/bin/python scripts/loadtest.py --levels 1,4,16 --iterations 8

Accounts are throwaway with a unique run suffix, so no user data is touched and
re-runs never collide.  `--self-test` exercises the aggregation code without a
network and prints `{"self_test": "passed"}`.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "sample.pdf"
DEFAULT_REPORT = ROOT / "dist" / "loadtest"


def percentile(values, quantile):
    """Nearest-rank percentile; no interpolation, so numbers are reproducible."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 3)


def summarize(values):
    return {
        "count": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 3) if values else None,
    }


def error_code(response):
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return error.get("code") or error.get("type")
    return None


class Recorder:
    def __init__(self):
        self.latencies: dict[str, list[float]] = {}
        self.errors: dict[str, list[dict]] = {}
        self.outcomes: dict[str, int] = {}

    def note(self, key):
        self.outcomes[key] = self.outcomes.get(key, 0) + 1

    def record(self, step, elapsed_s, *, status=None, code=None, detail=None):
        self.latencies.setdefault(step, []).append(round(elapsed_s * 1000, 3))
        if status is not None and not (200 <= status < 300):
            self.errors.setdefault(step, []).append({
                "status": status, "code": code, "detail": (detail or "")[:200]})

    def failure(self, step, message):
        self.errors.setdefault(step, []).append({"status": None, "code": "transport_error",
                                                 "detail": message[:200]})

    def steps(self):
        result = {}
        for step in sorted(set(self.latencies) | set(self.errors)):
            values = self.latencies.get(step, [])
            failures = self.errors.get(step, [])
            attempts = len(values) + sum(1 for f in failures if f["status"] is None)
            result[step] = {**summarize(values), "attempts": attempts,
                            "failures": len(failures),
                            "error_rate": round(len(failures) / attempts, 4) if attempts else None,
                            "first_error": failures[0] if failures else None}
        return result


def host_facts():
    cpu = platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    memory = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                memory = int(line.split()[1]) * 1024
                break
    except OSError:
        pass
    return {"cpu": cpu, "cores": os.cpu_count(), "memory_bytes": memory,
            "kernel": platform.release(), "machine": platform.machine(),
            "python": platform.python_version()}


def federation_spec(flow_id):
    return {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "rag.answer.cited", "workspace_ref": "workspace-loadtest",
        "query": "contract",
        "resource_scope": {"kind": "local_only"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "local_only"},
        "consent_refs": {"exploration": f"explore-{flow_id}", "execution": None},
        "budget_ref": f"budget-{flow_id}",
    }


def exploration_consent(flow_id):
    return {
        "schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": f"explore-{flow_id}",
        "granted_by": "loadtest", "granted_at": "2026-01-01T00:00:00Z",
        "valid_until": "2030-01-01T00:00:00Z", "egress_mode": "local_only",
        "allowed_payload": [], "allowed_recipients": [],
        "budget": {"max_probe_requests": 0, "max_egress_bytes": 0},
    }


async def poll(http, path, done, *, timeout, interval=0.3, pick=None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            response = await http.get(path)
        except httpx.HTTPError:
            await asyncio.sleep(interval)
            continue
        if response.status_code == 200:
            payload = response.json()
            last = pick(payload) if pick else payload
            if last is not None and done(last):
                return last
        await asyncio.sleep(interval)
    return last


async def run_flow(client, recorder, *, fixture, run_id, flow_id, level, timeout):
    """One realistic user flow; every step is timed and recorded separately.

    The worker's client is already authenticated: a real client logs in once and
    reuses the session, and re-logging in per flow would only measure the
    control plane's login throttle (LOGIN_RATE_LIMIT_PER_MIN).
    """
    # 1. presigned upload + finalize + server digest
    # Level is part of the nonce: a same-digest upload would be served from the
    # idempotent document cache and would not exercise parse at all.
    content = fixture.read_bytes() + f"\n% loadtest {run_id} c{level} f{flow_id}\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    started = time.monotonic()
    created = await client.post("/api/uploads", json={
        "filename": f"loadtest-{flow_id}.pdf", "size": len(content),
        "mime": "application/pdf", "sha256": digest})
    if created.status_code != 201:
        recorder.record("upload", time.monotonic() - started, status=created.status_code,
                        code=error_code(created), detail=created.text)
        return
    session = created.json()
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as raw:
            for part in session["parts"]:
                start = (part["part_number"] - 1) * session["part_size"]
                put = await raw.put(part["url"],
                                    content=content[start:start + session["part_size"]])
                if put.status_code not in (200, 204):
                    recorder.record("upload", time.monotonic() - started,
                                    status=put.status_code, code="part_put_failed")
                    return
        finalized = await client.post(f"/api/uploads/{session['id']}/finalize",
                                      json={"parts": None},
                                      headers={"Idempotency-Key": session["id"]})
        if finalized.status_code != 202:
            recorder.record("upload", time.monotonic() - started,
                            status=finalized.status_code, code=error_code(finalized),
                            detail=finalized.text)
            return
        settled = await poll(client, f"/api/uploads/{session['id']}",
                             lambda d: d["status"] not in ("created", "uploading", "verifying"),
                             timeout=min(timeout, 120))
        if not settled or settled["status"] != "ready":
            recorder.record("upload", time.monotonic() - started, status=409,
                            code="digest_not_ready",
                            detail=json.dumps(settled)[:200] if settled else "timeout")
            return
    except (KeyError, httpx.HTTPError) as exc:
        recorder.failure("upload", f"{type(exc).__name__}: {exc}")
        return
    recorder.record("upload", time.monotonic() - started, status=200)

    # 3. outbox -> document row
    started = time.monotonic()
    document = await poll(client, "/api/documents",
                          lambda d: bool(d), timeout=min(timeout, 60),
                          pick=lambda docs: next(
                              (d for d in docs if d.get("doc_id") == digest), None))
    if not document:
        recorder.record("doc_ingest", time.monotonic() - started, status=504,
                        code="document_not_ingested")
        return
    recorder.record("doc_ingest", time.monotonic() - started, status=200)

    # 4. parse to terminal state
    started = time.monotonic()
    detail = await poll(client, f"/api/documents/{document['id']}",
                        lambda d: d["status"] in ("succeeded", "failed"), timeout=timeout)
    if not detail:
        recorder.record("parse", time.monotonic() - started, status=504,
                        code="parse_timeout")
    else:
        recorder.record("parse", time.monotonic() - started, status=200
                        if detail["status"] == "succeeded" else 500,
                        code=None if detail["status"] == "succeeded"
                        else (detail.get("error") or "parse_failed"))
        recorder.note(f"parse_status={detail['status']}")
        recorder.note(f"index_status={detail.get('index_status')}")

    # 5. corpus search (degraded without embeddings, but still a real query)
    started = time.monotonic()
    response = await client.get("/api/search", params={"q": "contract"})
    recorder.record("search", time.monotonic() - started, status=response.status_code,
                    code=error_code(response), detail=response.text)
    if response.status_code == 200:
        recorder.note(f"search_degraded={response.json().get('degraded')}")

    # 6. federation task submit/poll (coordinator endpoints through the entry)
    started = time.monotonic()
    intent = await client.post("/api/v1/task-intents",
                               json={"task_spec": federation_spec(flow_id),
                                     "exploration_consent": exploration_consent(flow_id)},
                               headers={"Idempotency-Key": f"loadtest-intent-{run_id}-{flow_id}"})
    recorder.record("federation_intent", time.monotonic() - started,
                    status=intent.status_code, code=error_code(intent), detail=intent.text)
    recorder.note(f"federation_intent={intent.status_code}:{error_code(intent) or 'ok'}")
    if intent.status_code != 201:
        return
    root_id = intent.json()["root_task_id"]
    started = time.monotonic()
    plan = await client.post("/api/v1/task-plans", json={"root_task_id": root_id})
    recorder.record("federation_plan", time.monotonic() - started,
                    status=plan.status_code, code=error_code(plan), detail=plan.text)
    if plan.status_code != 200:
        return
    plan_body = plan.json()
    recipients = sorted({step["executor_node_id"] for step in plan_body.get("steps", [])})
    edges = [edge["edge_id"] for edge in plan_body.get("data_edges", [])]
    consent = {
        "schema": "ddp-plan-admission/1#ExecutionConsent",
        "consent_id": f"execute-{run_id}-{flow_id}", "plan_digest": plan_body["plan_digest"],
        "granted_by": "loadtest", "granted_at": "2026-01-01T00:00:00Z",
        "valid_until": "2030-01-01T00:00:00Z", "allowed_recipients": recipients,
        "allowed_edges": edges, "output_locations": ["local:workspace-loadtest"],
        "retention": "temporary",
    }
    started = time.monotonic()
    approved = await client.post(f"/api/v1/task-plans/{root_id}/approve",
                                 json={"plan_digest": plan_body["plan_digest"],
                                       "execution_consent": consent})
    recorder.record("federation_approve", time.monotonic() - started,
                    status=approved.status_code, code=error_code(approved),
                    detail=approved.text)
    if approved.status_code != 200:
        return
    started = time.monotonic()
    submitted = await client.post("/api/v1/tasks",
                                  json={"root_task_id": root_id,
                                        "plan_digest": plan_body["plan_digest"]},
                                  headers={"Idempotency-Key":
                                           f"loadtest-exec-{run_id}-{flow_id}"})
    recorder.record("federation_submit", time.monotonic() - started,
                    status=submitted.status_code, code=error_code(submitted),
                    detail=submitted.text)
    if submitted.status_code != 200:
        return
    started = time.monotonic()
    status = await poll(client, f"/api/v1/tasks/{root_id}",
                        lambda d: d.get("status") in ("succeeded", "failed", "cancelled"),
                        timeout=timeout)
    recorder.record("federation_poll", time.monotonic() - started,
                    status=200 if status else 504,
                    code=None if status else "task_poll_timeout",
                    detail=json.dumps(status)[:150] if status else "timeout")


def read_cpu_jiffies():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = [int(value) for value in fields[:8]]
    idle = values[3] + values[4]
    return sum(values), idle


async def sample_host_cpu(samples, interval=1.0):
    """Host CPU busy percent, sampled so the report can explain the wall clock."""
    total, idle = read_cpu_jiffies()
    while True:
        await asyncio.sleep(interval)
        now_total, now_idle = read_cpu_jiffies()
        if now_total > total:
            samples.append(round(100 * (1 - (now_idle - idle) / (now_total - total)), 1))
        total, idle = now_total, now_idle


async def run_level(args, *, fixture, run_id, password, username, level, iterations):
    recorder = Recorder()
    queue = list(range(iterations))
    lock = asyncio.Lock()

    async def login():
        async with httpx.AsyncClient(base_url=args.base, timeout=60.0,
                                     trust_env=False) as probe:
            started = time.monotonic()
            response = await probe.post("/api/auth/login",
                                        json={"username": username, "password": password})
            recorder.record("auth_login", time.monotonic() - started,
                            status=response.status_code, code=error_code(response),
                            detail=response.text)
            return response.json().get("access_token") if response.status_code == 200 else None

    async def worker(worker_id, token):
        client = httpx.AsyncClient(base_url=args.base, timeout=60.0, trust_env=False)
        if token:
            client.headers["Authorization"] = f"Bearer {token}"
        try:
            while True:
                async with lock:
                    if not queue or not token:
                        return
                    flow_id = queue.pop(0)
                await run_flow(client, recorder, fixture=fixture,
                               run_id=run_id, flow_id=flow_id, level=level,
                               timeout=args.timeout)
        finally:
            await client.aclose()

    cpu_samples = []
    cpu_task = asyncio.create_task(sample_host_cpu(cpu_samples))
    started = time.monotonic()
    token = await login()
    load_before = os.getloadavg()
    await asyncio.gather(*(worker(index, token) for index in range(level)))
    wall = round(time.monotonic() - started, 3)
    load_after = os.getloadavg()
    cpu_task.cancel()
    try:
        await cpu_task
    except asyncio.CancelledError:
        pass
    return {"concurrency": level, "iterations": iterations,
            "wall_seconds": wall, "host_cpu_busy_percent": {
                "mean": round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None,
                "max": max(cpu_samples) if cpu_samples else None,
                "samples": len(cpu_samples)},
            "loadavg_before": [round(value, 2) for value in load_before],
            "loadavg_after": [round(value, 2) for value in load_after],
            "outcomes": recorder.outcomes,
            "steps": recorder.steps()}


def register_account(args, run_id, password):
    client = httpx.Client(base_url=args.base, timeout=60.0, trust_env=False)
    username = f"loadtest-{run_id}"
    started = time.monotonic()
    response = client.post("/api/auth/register", json={"username": username,
                                                       "password": password})
    # Never persist the response body here: a successful registration returns a
    # session token, and this JSON report is meant to be shareable.
    body = response.json() if response.status_code < 300 else {}
    return {"username": username, "status": response.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "user_id": body.get("user", {}).get("id"),
            "error_code": None if response.status_code < 300 else error_code(response)}


def self_test():
    assert percentile([], 0.5) is None
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4
    assert percentile([5], 0.99) == 5
    assert summarize([10, 20])["p50_ms"] == 10
    recorder = Recorder()
    recorder.record("s", 0.010, status=200)
    recorder.record("s", 0.020, status=500, code="boom")
    recorder.failure("s", "connection reset")
    steps = recorder.steps()["s"]
    assert steps["count"] == 2 and steps["failures"] == 2
    assert steps["error_rate"] == 0.6667
    print(json.dumps({"self_test": "passed"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="统一入口")
    parser.add_argument("--levels", default="1,4,16", help="逗号分隔的并发档位")
    parser.add_argument("--iterations", default="8",
                        help="每档的总流程数：一个数用于所有档位，或与 --levels 对齐的列表")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--report", type=Path, default=None,
                        help="JSON 报告路径（缺省 dist/loadtest/loadtest-<时间>.json）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单步轮询上限（秒）")
    parser.add_argument("--password", default="loadtest-correct-horse-battery")
    parser.add_argument("--self-test", action="store_true",
                        help="只验证聚合/错误统计代码，不连网络")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.fixture.is_file():
        print(f"::error::fixture not found: {args.fixture}", file=sys.stderr)
        return 2
    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    if not levels or any(level < 1 for level in levels):
        print("::error::--levels must be positive integers", file=sys.stderr)
        return 2
    iterations = [int(item) for item in args.iterations.split(",") if item.strip()]
    if len(iterations) == 1:
        iterations = iterations * len(levels)
    if len(iterations) != len(levels) or any(item < 1 for item in iterations):
        print("::error::--iterations must be one value or match --levels", file=sys.stderr)
        return 2
    settings = dict(zip(levels, iterations))

    run_id = secrets.token_hex(4)
    report = {
        "tool": "loadtest.py", "format": 1, "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": args.base, "host": host_facts(),
        "fixture": {"path": str(args.fixture), "size": args.fixture.stat().st_size,
                    "sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest()},
        "levels": [], "registration": None,
        "caveats": ["local CPU, no GPU; not a production capacity claim",
                    "search hits the embedding path and degrades with "
                    "embedding_unavailable on this deployment"],
    }
    print(f"loadtest {run_id}: levels={levels} iterations={iterations} base={args.base}")
    registration = register_account(args, run_id, args.password)
    report["registration"] = registration
    if registration["status"] != 201:
        print(f"::error::registration failed: {registration['body']}", file=sys.stderr)
        return 1
    for level in levels:
        summary = asyncio.run(run_level(args, fixture=args.fixture, run_id=run_id,
                                        password=args.password,
                                        username=registration["username"], level=level,
                                        iterations=settings[level]))
        report["levels"].append(summary)
        print(f"\nconcurrency={level} wall={summary['wall_seconds']}s")
        for step, values in summary["steps"].items():
            print(f"  {step:24s} n={values['count']:3d} err={values['failures']:3d} "
                  f"p50={values['p50_ms']}ms p95={values['p95_ms']}ms "
                  f"p99={values['p99_ms']}ms max={values['max_ms']}ms")
    report_path = args.report or (DEFAULT_REPORT / f"loadtest-{run_id}.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
