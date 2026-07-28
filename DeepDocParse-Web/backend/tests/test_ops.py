"""运维面：限速、对象回收、就绪探针。

多副本正确性的关键在限速（进程内计数会让实际上限变成 N×limit）与对账选主，
这里覆盖可在进程内验证的部分；真 Redis 的 Lua 行为由 e2e 覆盖。
"""
import pytest
import respx
from sqlalchemy import select

from app.errors import APIError
from app.gc import collect_deleted_objects
from app.metering import MemoryRateLimiter, RedisRateLimiter
from app.models import Document
import app.db as db
from tests.test_documents import _callback, _mock_service, _upload


async def test_memory_limiter_blocks_after_limit():
    limiter = MemoryRateLimiter()
    await limiter.check("k", 2)
    await limiter.check("k", 2)
    with pytest.raises(APIError) as exc:
        await limiter.check("k", 2)
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) >= 1
    await limiter.check("other-key", 2)     # 不同 key 互不影响


class _Script:
    """替身：模拟 Redis 端 Lua 脚本的返回。"""

    def __init__(self, allowed: int, retry_after: str = "3.0", boom: bool = False):
        self.allowed, self.retry_after, self.boom = allowed, retry_after, boom

    async def __call__(self, keys, args):
        if self.boom:
            raise ConnectionError("redis down")
        return [self.allowed, self.retry_after]


class _Redis:
    def __init__(self, script):
        self._script = script

    def register_script(self, _src):
        return self._script


async def test_redis_limiter_denies_and_reports_retry_after():
    limiter = RedisRateLimiter(_Redis(_Script(allowed=0, retry_after="2.4")))
    with pytest.raises(APIError) as exc:
        await limiter.check("k", 60)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "3"


async def test_redis_limiter_fails_open():
    """Redis 抖动不能把正常请求挡在门外 —— 限速是保护，不是鉴权。"""
    limiter = RedisRateLimiter(_Redis(_Script(allowed=0, boom=True)))
    await limiter.check("k", 60)


@respx.mock
async def test_gc_removes_objects_of_deleted_documents(auth_client, session, app_state):
    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    storage = app_state.storage
    assert any(k.startswith("sources/") for k in storage.objects)
    assert any(k.startswith("results/") for k in storage.objects)

    await auth_client.delete(f"/api/documents/{document['id']}")
    cleaned = await collect_deleted_objects(db.get_sessionmaker(), storage)
    assert cleaned == 1
    assert not [k for k in storage.objects if k.startswith("sources/")]
    assert not [k for k in storage.objects if k.startswith("results/")]

    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.object_key == "", "回收完要留标记，下一轮不再重复删"

    # 再跑一次不该重复处理
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0


@respx.mock
async def test_gc_handles_migrated_job_whose_prefix_differs(auth_client, session, app_state):
    """M5 迁移过来的 job：id 是新 uuid，归档产物却还在 results/{原 task_id}/ 下。

    GC 只按 job.id 拼前缀的话会列到空集，然后把 object_key 置空标成"已回收"——
    对象永久留在存储里再也不会被重试。而 crops 又是按 job.id 写的，
    所以两个前缀都得列。
    """
    from app.models import ParseJob

    _mock_service()
    document = await _upload(auth_client)
    await _callback(auth_client)

    job = (await session.execute(select(ParseJob))).scalars().one()
    storage = app_state.storage
    # 造出迁移后的形态：result_prefix 指向另一个前缀，crops 仍按 job.id 落
    legacy_prefix = "results/legacy-task-id/"
    for key in [k for k in storage.objects if k.startswith(f"results/{job.id}/")]:
        storage.objects[key.replace(f"results/{job.id}/", legacy_prefix)] = storage.objects.pop(key)
    job.result_prefix = legacy_prefix
    await session.commit()
    await storage.put(f"results/{job.id}/crops/0_abc.png", b"crop", "image/png")

    await auth_client.delete(f"/api/documents/{document['id']}")
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 1
    assert not [k for k in storage.objects if k.startswith(legacy_prefix)], "归档产物要清掉"
    assert not [k for k in storage.objects if k.startswith(f"results/{job.id}/")], "crops 也要清掉"
    assert not [k for k in storage.objects if k.startswith("sources/")]


@respx.mock
async def test_gc_leaves_live_documents_alone(auth_client, app_state):
    _mock_service()
    await _upload(auth_client)
    await _callback(auth_client)
    before = set(app_state.storage.objects)

    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert set(app_state.storage.objects) == before


@respx.mock
async def test_readyz_reports_each_dependency(client, app_state):
    """依赖不通要返回 503，且说清是哪个 —— 排障时这条信息最值钱。"""
    from tests.conftest import SERVICE

    respx.get(f"{SERVICE}/healthz").mock(side_effect=ConnectionError("service down"))
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["service"].startswith("error")


async def test_healthz_does_not_depend_on_anything(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


@respx.mock
async def test_metrics_exposed(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200 and "http_request" in resp.text
