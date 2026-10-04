"""Real, adjacent-trust A -> P -> R -> S federation acceptance harness."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from federation_two_node import A_KEY, A_PUBLIC_B64, NODE_A, NodeSeed, seed_sqlite
from node_credentials_fixture import (
    LocalControlSigner, StaticPeerTrust, key_for, node_id_for, public_key_b64, trust_record,
)

LABELS = {name: "ddp-recursive-http-" + name.lower() for name in ("P", "R", "S")}
NODE_P, NODE_R, NODE_S = (node_id_for(LABELS[name]) for name in ("P", "R", "S"))
KEYS = {NODE_A: A_KEY, **{node_id_for(label): key_for(label) for label in LABELS.values()}}
PUBLIC_KEYS = {NODE_A: A_PUBLIC_B64, **{
    node_id_for(label): public_key_b64(key_for(label)) for label in LABELS.values()}}
ADJACENCY = {NODE_A: (NODE_P,), NODE_P: (NODE_A, NODE_R),
             NODE_R: (NODE_P, NODE_S), NODE_S: (NODE_R,)}


def _append(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if path.exists() else []


class RecordingTransport(httpx.AsyncBaseTransport):
    """Instrument real sockets, never substitute peer responses."""

    def __init__(self, sink):
        self.sink = sink
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        record = {"method": request.method, "url": str(request.url),
                  "path": request.url.path, "target": request.headers.get("X-DDP-Target-Node")}
        if request.content:
            record["body"] = json.loads(request.content)
        if isinstance(self.sink, list):
            self.sink.append(record)
        else:
            _append(self.sink, record)
        response = await self.inner.handle_async_request(request)
        return response

    async def aclose(self):
        await self.inner.aclose()


def directory_factory(node_id, sink):
    """Same production directory seam as the two-node acceptance fixture."""
    from ddp_corpus.config import settings
    from ddp_corpus.federation_peers import PeerDirectory, parse_peers

    def factory(actor, delegation=None):
        return PeerDirectory(
            parse_peers(settings.federation_peers, allow_loopback=True), actor=actor,
            transport=RecordingTransport(sink), delegation=delegation,
            signer=LocalControlSigner(issuer_node_id=node_id, key=KEYS[node_id]))
    return factory


def build_node_app(config):
    from ddp_core.search import MemoryIndex
    from ddp_corpus import db, federation_tasks, node_identity
    from ddp_corpus.config import settings
    from ddp_corpus.errors import install_error_handlers
    from ddp_corpus.routers.federation import router
    from fastapi import FastAPI

    node = config["node_id"]
    settings.database_url = "sqlite+aiosqlite:///" + config["database"]
    settings.bundle_node_id = node
    settings.federation_admissions_enabled = True
    settings.federation_execution_inline = True
    settings.federation_allow_loopback = True
    settings.federation_peers = json.dumps(config["peers"])
    settings.chat_model = ""
    db.reset_engine()
    node_identity.reset()
    node_identity.bind_static_for_tests(node)
    federation_tasks.peer_directory = directory_factory(node, Path(config["outbound_log"]))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await db.get_engine().dispose()
        db.reset_engine()

    app = FastAPI(title="Recursive HTTP acceptance peer", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(router)
    app.state.search_index = MemoryIndex()
    app.state.http = None
    app.state.redis = None
    app.state.peer_trust = StaticPeerTrust(config["trust"])

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "node_id": node}

    @app.middleware("http")
    async def instrument(request, call_next):
        body = await request.body()
        response = await call_next(request)
        if request.url.path != "/healthz":
            record = {"method": request.method, "path": request.url.path,
                      "status": response.status_code}
            if body:
                record["body"] = json.loads(body)
            _append(Path(config["call_log"]), record)
        return response

    return app


@dataclass
class PeerProcess:
    node_id: str
    endpoint: str
    seed: NodeSeed
    database: Path
    call_log: Path
    outbound_log: Path
    log_path: Path
    process: subprocess.Popen | None = None

    async def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)

    def calls(self, suffix=None):
        return [call for call in _records(self.call_log)
                if suffix is None or call["path"].endswith(suffix)]

    def outbound(self):
        return _records(self.outbound_log)


class RecursiveFixture:
    def __init__(self):
        self.nodes: dict[str, PeerProcess] = {}
        self.outbound: list[dict] = []

    @classmethod
    async def create(cls, tmpdir, *, policies=None):
        from conftest import ACTOR, ORG
        from ddp_corpus import catalog
        from ddp_corpus.collection_models import Collection
        from ddp_corpus.deps import Actor
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        fixture = cls()
        tmpdir = Path(tmpdir)
        ports = set()
        for name, node in (("P", NODE_P), ("R", NODE_R), ("S", NODE_S)):
            database = tmpdir / f"node-{name}.sqlite3"
            seed = await seed_sqlite(database, texts=(f"leaf {name} federation keyword fact",),
                                     collection_name=f"Recursive node {name}")
            if policies is not None and node in policies:
                engine = create_async_engine("sqlite+aiosqlite:///" + str(database))
                try:
                    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                        collection = await session.get(Collection, seed.collection_id)
                        actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
                        changed = await catalog.mutate(session, actor, "replace", {
                            "expected_revision": collection.revision, "name": collection.name,
                            **collection.metadata_json, "onward_recipients": policies[node],
                            "version_ids": [seed.version_id]}, "recursive-policy", seed.collection_id)
                        published = await catalog.mutate(session, actor, "publish", {
                            "expected_revision": changed["revision"]}, "recursive-policy-publish",
                            seed.collection_id)
                        seed = replace(seed, index_revision=published["index_revision"])
                finally:
                    await engine.dispose()
            while True:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                if port not in ports:
                    ports.add(port)
                    break
            fixture.nodes[node] = PeerProcess(node, f"http://127.0.0.1:{port}", seed, database,
                tmpdir / f"{name}-calls.jsonl", tmpdir / f"{name}-outbound.jsonl",
                tmpdir / f"{name}.log")

        corpus_api = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(corpus_api), str(corpus_api / "tests"), env.get("PYTHONPATH"))))
        env["PYTHONUNBUFFERED"] = "1"
        try:
            for node, peer in fixture.nodes.items():
                config = {"node_id": node, "database": str(peer.database),
                    "call_log": str(peer.call_log), "outbound_log": str(peer.outbound_log),
                    "peers": {adjacent: {"endpoint": fixture.nodes[adjacent].endpoint}
                              for adjacent in ADJACENCY[node] if adjacent != NODE_A},
                    "trust": {adjacent: trust_record(adjacent, PUBLIC_KEYS[adjacent],
                        organization_id=ORG, authority_node_id=node)
                              for adjacent in ADJACENCY[node]}}
                config_path = tmpdir / f"{node}-config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with peer.log_path.open("wb") as log:
                    peer.process = subprocess.Popen([
                        sys.executable, str(Path(__file__).resolve()), "--config", str(config_path),
                        "--port", peer.endpoint.rsplit(":", 1)[1]], cwd=corpus_api,
                        env=env, stdout=log, stderr=subprocess.STDOUT)
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                for peer in fixture.nodes.values():
                    deadline = asyncio.get_running_loop().time() + 20
                    while asyncio.get_running_loop().time() < deadline:
                        if peer.process.poll() is not None:
                            raise RuntimeError(peer.log_path.read_text(encoding="utf-8"))
                        try:
                            response = await client.get(peer.endpoint + "/healthz")
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError("Peer startup timeout: " + peer.log_path.read_text())
        except BaseException:
            await fixture.stop()
            raise
        return fixture

    def install_root(self, monkeypatch, app):
        from conftest import ORG
        from ddp_corpus import federation_tasks
        from ddp_corpus.config import settings
        from node_credentials_fixture import install

        install(monkeypatch, app, node_id=NODE_A, organization_id=ORG,
                peers=((NODE_P, PUBLIC_KEYS[NODE_P]),))
        monkeypatch.setattr(settings, "federation_admissions_enabled", True)
        monkeypatch.setattr(settings, "federation_allow_loopback", True)
        monkeypatch.setattr(settings, "federation_peers", json.dumps({
            NODE_P: {"endpoint": self.nodes[NODE_P].endpoint}}))
        monkeypatch.setattr(settings, "chat_model", "")
        monkeypatch.setattr(federation_tasks, "peer_directory", directory_factory(NODE_A, self.outbound))

    async def stop(self):
        await asyncio.gather(*(peer.stop() for peer in self.nodes.values()))


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(build_node_app(json.loads(Path(args.config).read_text())),
                host="127.0.0.1", port=args.port, log_level="warning")
