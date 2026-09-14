"""Two-node federation acceptance harness for P5.

What is real, and what is not
=============================

**Node B runs in a real `uvicorn` subprocess on `127.0.0.1` with its own SQLite
file database.** The coordinator (node A) reaches it through the production
`PeerClient` / `PeerDirectory` code path over **real loopback TCP**, with the
real `Authorization` service bearer, `X-DDP-Peer-Token`, actor headers and
`X-DDP-Target-Node` header. Nothing about the peer protocol is stubbed: the
only test scaffolding on B is a request-log middleware and an opt-in fault
injection middleware, both defined in this file on top of the unmodified
`ddp_corpus.routers.federation` router.

The in-process part is node A: the tests drive A's entry endpoints through
`httpx.ASGITransport` against the repo's standard pytest app (SQLite
in-memory, `MemoryIndex`, no MinIO/PG). That is the normal test harness, not a
substitute for the protocol under test. The original in-process design (swap
`db._engine` globals while forwarding to a second ASGI app) was deliberately
*not* used: a real subprocess removes the serialization/limitation caveats
entirely for every scenario.

Server-side instrumentation (test-only, lives in this file's `build_node_app`):

- `--call-log`: one JSON line per inbound request (`method`, `path`, `status`)
  so a test can assert exactly how many probe/admission calls the peer saw.
- `--fault-file`: while the file exists, `POST /api/v1/federation/admissions`
  answers `503 fault_injected`. Used to fail a peer *after* planning succeeded
  (probe receipt already stored), then clear the fault and resume.

The caller is responsible for starting/stopping the fixture and for pointing
node A's `settings.federation_peers` at `peers_json()`.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

#: Node identities. `NODE_PATTERN` (ddp_core) accepts all three.
NODE_A = "node-a"
NODE_B = "node-b"
NODE_C = "node-c"

#: Default keyword-findable fact seeded into node B. Distinct from A's split text.
NODE_B_TEXTS = ("beta federation keyword fact",)

SERVICE_TOKEN_A = "test-service-token"
SERVICE_TOKEN_B = "two-node-service-token-b"
PEER_TOKEN_A = "two-node-peer-token-a"
PEER_TOKEN_B = "two-node-peer-token-b"
C_SERVICE_TOKEN = "two-node-service-token-c"
C_PEER_TOKEN = "two-node-peer-token-c"


@dataclass(frozen=True)
class NodeSeed:
    """The real identities a published collection+document carries on a node."""

    collection_id: str
    index_revision: str
    resource_id: str
    version_id: str
    evidence_id: str
    text: str


class _CountingTransport(httpx.AsyncBaseTransport):
    """Real sockets underneath, plus an exact count of A-side outbound attempts."""

    def __init__(self, sink: list[dict]):
        self._inner = httpx.AsyncHTTPTransport()
        self._sink = sink

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._sink.append({"method": request.method, "path": request.url.path})
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def seed_sqlite(path: Path, *, texts, collection_name: str) -> NodeSeed:
    """Create tables + a published collection with real Document/Chunk/Evidence rows.

    Reuses the exact insertion helpers the P4/P5 tests use (`indexed_source`),
    then publishes through the real `catalog.mutate` implementation (the same
    call the collections HTTP route makes) so `index_revision` is the actual
    members_state digest the executor will later report.
    """
    # Import for metadata registration (mirrors what importing main.py does).
    import ddp_corpus.collection_models  # noqa: F401
    import ddp_corpus.federation_models  # noqa: F401
    from conftest import ACTOR, ORG
    from ddp_corpus import catalog
    from ddp_corpus.deps import Actor
    from ddp_corpus.models import Base
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from test_federation_probes import indexed_source

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            resource, version, _job, _document, evidence_rows = await indexed_source(
                session, owner=ACTOR, texts=texts)
            actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
            created = await catalog.mutate(
                session, actor, "create",
                {"name": collection_name, "licence": "CC0", "languages": ["en"],
                 "topics": ["federation"], "version_ids": [version.id]},
                "two-node-seed-create")
            published = await catalog.mutate(
                session, actor, "publish", {"expected_revision": created["revision"]},
                "two-node-seed-publish", created["collection_id"])
    finally:
        await engine.dispose()
    return NodeSeed(
        collection_id=published["collection_id"],
        index_revision=published["index_revision"],
        resource_id=resource.id, version_id=version.id,
        evidence_id=evidence_rows[0].id, text=texts[0])


class ModelStub:
    """A tiny OpenAI-compatible model endpoint for node B's generation plane.

    Real HTTP over loopback (threaded `http.server`): `GET /v1/capabilities`
    reports an observed instruct chat channel, `POST /v1/chat/completions`
    returns the canned cited answer. This is the only non-federation piece of
    the delegation acceptance test; the A -> B path stays real HTTP.
    """

    def __init__(self, answer: str):
        self.answer = answer
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):     # keep the test log quiet
                pass

            def _json(self, status: int, body: dict) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):                  # noqa: N802 —— http.server 接口
                if self.path == "/v1/capabilities":
                    now = datetime.now(timezone.utc)
                    return self._json(200, {
                        "capability_status": "observed", "profiles": [],
                        "model_channels": [{
                            "channel": "chat", "model": "stub-instruct",
                            "profile": "stub-instruct", "default": True,
                            "readiness": "ready",
                            "supports": {"instruct": True, "vision": False},
                            "observed_at": now.isoformat(),
                            "valid_until": (now + timedelta(seconds=300)).isoformat()}]})
                return self._json(404, {"error": "not found"})

            def do_POST(self):                 # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                outer.requests.append({"path": self.path, "payload": payload})
                if self.path == "/v1/chat/completions":
                    return self._json(200, {
                        "choices": [{"message": {"role": "assistant",
                                                 "content": outer.answer}}],
                        "model": "stub-instruct"})
                return self._json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.endpoint

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def build_node_app(*, call_log: str = "", fault_file: str = "", generate: bool = False):
    """A real FastAPI app mounting the unmodified federation executor router.

    No lifespan: this process intentionally has no MinIO/PG dependency. Search
    runs on `MemoryIndex`. `generate=False` (default) leaves `app.state.http`
    None so executors take the keyword path and every generation operation
    reports unknown — the honest not-ready shape. `generate=True` gives the
    node a real HTTP client so its capability producer and grounded answer can
    reach the test's `ModelStub` via `settings.service_url`.
    """
    from ddp_core.search import MemoryIndex
    from ddp_corpus.errors import install_error_handlers
    from ddp_corpus.routers.federation import router as federation_router
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="P5 two-node acceptance peer")
    install_error_handlers(app)
    app.include_router(federation_router)
    app.state.search_index = MemoryIndex()
    app.state.http = (httpx.AsyncClient(trust_env=False, follow_redirects=False)
                      if generate else None)
    app.state.redis = None

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.middleware("http")
    async def instrument(request, call_next):
        path = request.url.path
        if fault_file and path == "/api/v1/federation/admissions" \
                and Path(fault_file).exists():
            response = JSONResponse(status_code=503, content={"error": {
                "message": "fault injected by the two-node acceptance harness",
                "type": "server_error", "code": "fault_injected"}})
        else:
            response = await call_next(request)
        if call_log:
            # Idempotency-Key 只用于测试区分"证据探测"（plan:）与"生成能力
            # 探测"（answer-probe:）；它是业务键，不是凭据。
            with open(call_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"method": request.method, "path": path,
                     "status": response.status_code,
                     "idempotency_key": request.headers.get("Idempotency-Key")}) + "\n")
        return response

    return app


def _serve(argv=None) -> None:
    import argparse

    import uvicorn
    from ddp_corpus import db
    from ddp_corpus.config import settings

    parser = argparse.ArgumentParser(description="two-node acceptance peer")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--node-id", default=NODE_B)
    parser.add_argument("--service-token", required=True)
    parser.add_argument("--peer-token", required=True)
    parser.add_argument("--call-log", default="")
    parser.add_argument("--fault-file", default="")
    parser.add_argument("--service-url", default="",
                        help="model gateway origin; empty = no generation plane")
    args = parser.parse_args(argv)

    settings.database_url = "sqlite+aiosqlite:///" + str(Path(args.database).resolve())
    settings.bundle_node_id = args.node_id
    settings.service_token = args.service_token
    settings.federation_peer_token = args.peer_token
    settings.federation_admissions_enabled = True
    # 验收夹具的 B 节点没有 worker 进程：执行留在请求内完成，否则受理后的
    # `federation_execute` 任务永远没人领取，A 会看到 queued/超时。
    # 生产默认是队列模式（FEDERATION_EXECUTION_INLINE=false）。
    settings.federation_execution_inline = True
    if args.service_url:
        settings.service_url = args.service_url
    db.reset_engine()
    uvicorn.run(build_node_app(call_log=args.call_log, fault_file=args.fault_file,
                               generate=bool(args.service_url)),
                host="127.0.0.1", port=args.port, log_level="warning")


class TwoNodeFixture:
    """Node B as a real subprocess; node A stays on the repo's pytest app."""

    def __init__(self, *, tmpdir: Path, b_seed: NodeSeed, b_port: int, c_port: int,
                 call_log: Path, fault_file: Path, log_path: Path,
                 process: subprocess.Popen, model_stub: ModelStub | None = None):
        self.tmpdir = tmpdir
        self.b_seed = b_seed
        self.b_port = b_port
        self.c_port = c_port
        self.call_log = call_log
        self.fault_file = fault_file
        self.log_path = log_path
        self._process = process
        self.model_stub = model_stub
        self.b_endpoint = f"http://127.0.0.1:{b_port}"
        self.c_endpoint = f"http://127.0.0.1:{c_port}"
        self.service_token_b = SERVICE_TOKEN_B
        self.peer_token_b = PEER_TOKEN_B
        self.outbound: list[dict] = []

    # ------------------------------------------------------------- lifecycle

    @classmethod
    async def create(cls, tmpdir, *, b_texts=NODE_B_TEXTS,
                     b_name: str = "Node B federation collection",
                     b_generate_answer: str | None = None) -> TwoNodeFixture:
        tmpdir = Path(tmpdir)
        database = tmpdir / "node-b.sqlite3"
        b_seed = await seed_sqlite(database, texts=tuple(b_texts), collection_name=b_name)
        b_port = _free_port()
        c_port = _free_port()
        while c_port == b_port:
            c_port = _free_port()
        call_log = tmpdir / "node-b-calls.jsonl"
        fault_file = tmpdir / "node-b-fault.flag"
        call_log.write_text("", encoding="utf-8")
        log_path = tmpdir / "node-b.log"

        # A real (threaded) model endpoint on loopback: B's generation plane
        # must be exercised over HTTP too, not by monkeypatching B's process.
        model_stub = None
        service_url = ""
        if b_generate_answer is not None:
            model_stub = ModelStub(b_generate_answer)
            service_url = model_stub.start()

        corpus_api = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        pythonpath = [str(corpus_api)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        env["PYTHONUNBUFFERED"] = "1"

        args = [sys.executable, str(Path(__file__).resolve()), "--serve",
                "--port", str(b_port), "--database", str(database),
                "--node-id", NODE_B,
                "--service-token", SERVICE_TOKEN_B,
                "--peer-token", PEER_TOKEN_B,
                "--call-log", str(call_log), "--fault-file", str(fault_file)]
        if service_url:
            args += ["--service-url", service_url]
        log = open(log_path, "wb")
        process = subprocess.Popen(args, cwd=str(corpus_api), env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        fixture = cls(tmpdir=tmpdir, b_seed=b_seed, b_port=b_port, c_port=c_port,
                      call_log=call_log, fault_file=fault_file, log_path=log_path,
                      process=process, model_stub=model_stub)
        try:
            await fixture._wait_healthy()
        except Exception:
            await fixture.stop()
            raise
        # Startup health probes are not protocol traffic.
        call_log.write_text("", encoding="utf-8")
        return fixture

    async def _wait_healthy(self, timeout: float = 20.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient(trust_env=False, timeout=1.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError(
                        f"node B exited early ({self._process.returncode}):\n"
                        f"{self.log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}")
                try:
                    response = await client.get(self.b_endpoint + "/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise RuntimeError(
            f"node B did not become healthy:\n"
            f"{self.log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}")

    async def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._process.wait, 10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                await asyncio.get_event_loop().run_in_executor(
                    None, self._process.wait, 5)
        if self.model_stub is not None:
            self.model_stub.stop()
            self.model_stub = None

    # ----------------------------------------------------------- A's config

    def peers_json(self, *, peer_token_b: str | None = None,
                   service_token_b: str | None = None,
                   endpoint_b: str | None = None, include_c: bool = True) -> str:
        """Exactly the JSON an administrator would register for node A."""
        peers = {NODE_B: {
            "endpoint": endpoint_b or self.b_endpoint,
            "service_token": service_token_b or self.service_token_b,
            "peer_token": peer_token_b or self.peer_token_b,
        }}
        if include_c:
            peers[NODE_C] = {"endpoint": self.c_endpoint,
                             "service_token": C_SERVICE_TOKEN,
                             "peer_token": C_PEER_TOKEN}
        return json.dumps(peers)

    def install_counting_transport(self, monkeypatch) -> None:
        """Keep the production PeerClient/real sockets; count A-side attempts.

        `peer_directory` is the same seam the existing P5 tests use to inject a
        transport; here the injected transport wraps `httpx.AsyncHTTPTransport`,
        so requests still go out on real loopback sockets.
        """
        from ddp_corpus import federation_tasks
        from ddp_corpus.config import settings
        from ddp_corpus.federation_peers import PeerDirectory, parse_peers

        def factory(actor):
            peers = parse_peers(settings.federation_peers,
                                allow_loopback=settings.federation_allow_loopback)
            return PeerDirectory(peers, actor=actor,
                                 transport=_CountingTransport(self.outbound))

        monkeypatch.setattr(federation_tasks, "peer_directory", factory)

    def outbound_to(self, suffix: str) -> list[dict]:
        return [call for call in self.outbound if call["path"].endswith(suffix)]

    # ------------------------------------------------------- B-side evidence

    def set_fault(self, enabled: bool) -> None:
        if enabled:
            self.fault_file.write_text("1", encoding="utf-8")
        else:
            self.fault_file.unlink(missing_ok=True)

    def calls(self) -> list[dict]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in
                self.call_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def calls_to(self, suffix: str, *, status: int | None = None) -> list[dict]:
        return [call for call in self.calls()
                if call["path"].endswith(suffix)
                and (status is None or call["status"] == status)]

    def calls_containing(self, fragment: str, *, status: int | None = None) -> list[dict]:
        return [call for call in self.calls()
                if fragment in call["path"]
                and (status is None or call["status"] == status)]


if __name__ == "__main__":
    _serve()
