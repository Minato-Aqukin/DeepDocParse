"""测试装配：不跑 lifespan，手工注入 app.state（fakeredis + respx mock 上游）。

这样单测不需要真 Redis / 真 mineru / GPU，纯进程内跑完（respx 拦截 httpx 出网）。
真环境 e2e 见 README「验证」一节（dev compose）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))

import fakeredis.aioredis
import httpx
import pytest

from app.config import load_registry, settings
from app.main import app
from app.services.mineru_client import MineruClient
from app.services.task_store import TaskStore

MINERU = "http://mineru:8000"  # 与 models.yaml parse_engines.mineru.endpoint 一致


class ArqStub:
    """记录 enqueue_job 调用；测试里手动驱动 worker 函数。"""

    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name: str, *args):
        self.jobs.append((name, *args))


@pytest.fixture
async def app_state():
    """把 lifespan 该做的注入手工做一遍（fakeredis 版），测试结束清理。"""
    import asyncio

    app.state.registry = load_registry(Path(__file__).resolve().parent.parent / "models.yaml")
    # trust_env=False：测试不读本机代理环境变量（respx 拦截出网，无需真实网络）
    app.state.http = httpx.AsyncClient(timeout=5.0, trust_env=False)
    app.state.vqa_semaphore = asyncio.Semaphore(settings.vqa_max_concurrency)
    app.state.redis = fakeredis.aioredis.FakeRedis()
    app.state.arq = ArqStub()
    app.state.task_store = TaskStore(app.state.redis, settings.result_ttl)
    app.state.mineru_client = MineruClient(app.state.http)
    yield app.state
    await app.state.http.aclose()
    await app.state.redis.aclose()


@pytest.fixture
async def client(app_state):
    """ASGI 进程内客户端，带合法 service token。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gateway",
        headers={"Authorization": f"Bearer {settings.service_token}"},
        trust_env=False,
    ) as c:
        yield c


@pytest.fixture
def worker_ctx(app_state):
    """poll_and_archive 的 ctx —— 与 worker startup 注入的结构一致。
    'redis' 是 arq 自带的任务池（worker 里用它 enqueue 后续链），测试用 ArqStub。"""
    return {
        "http": app_state.http,
        "task_store": app_state.task_store,
        "mineru_client": app_state.mineru_client,
        "registry": app_state.registry,
        "redis": app_state.arq,
    }
