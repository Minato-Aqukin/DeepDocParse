"""Real corpus ASGI server for the opt-in Go↔Python client protocol integration test."""
import asyncio
import os
from pathlib import Path
import socket
import sys
import httpx
import uvicorn

from ddp_corpus.config import settings
settings.database_url = os.environ["CENTER_CLIENT_TEST_DATABASE_URL"]
settings.service_token = "internal-test-service"
settings.service_url = "http://127.0.0.1:9"
from ddp_corpus.main import app
from ddp_core.search import MemoryIndex
from ddp_corpus.storage import MemoryStorage
from ddp_corpus.service_client import ServiceClient

async def main():
    app.state.http = httpx.AsyncClient(timeout=1, trust_env=False)
    app.state.search_index = MemoryIndex()
    app.state.storage = MemoryStorage()
    app.state.service_client = ServiceClient(app.state.http)
    app.state.redis = None
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    config = uvicorn.Config(app, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    Path(sys.argv[1]).write_text(f"http://127.0.0.1:{sock.getsockname()[1]}")
    await task
    await app.state.http.aclose()

asyncio.run(main())
