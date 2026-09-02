"""测试装配：SQLite in-memory + 内存对象存储 + 内存检索 + respx mock 上游。

不跑 lifespan（那会连真 MinIO/PG），手工注入 app.state —— 与 model-gateway 的
conftest 同套路。SQLite 用 StaticPool：`:memory:` 每条连接都是独立库，
不共享连接就看不到建好的表。

向量检索在 SQLite 上做不了（pgvector 是 PG 扩展），所以注入 MemoryIndex：
真实 pgvector 路径由 `scripts/e2e_web.py` 覆盖。这样单测仍然不需要任何外部依赖。

## 合仓后最大的变化：本服务不再做鉴权

没有注册、没有登录、没有 API key。调用方带的是 control-api 下发的
**actor 上下文头** + 服务凭据（见 `ddp_corpus/deps.py`）。所以测试客户端
`actor_client` 直接把那组头填好 —— 这正是生产里入口做的事。

**不要在测试里绕过服务凭据。** 那道门是 actor 上下文可信的前提；
绕过它的测试等于在验证一个生产里不存在的形态。
"""
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import ddp_corpus.db as db
from ddp_corpus.config import settings

# 编译指纹测试必须模拟可追溯部署；另有专门用例把模型置空，验证
# provider_unresolved，不能让整套测试无意中都跑在不可比较的默认模型下。
settings.embedding_model = "test-embedding"
settings.chat_model = "test-vision"
# 既有问答用例只测试检索/回答/核对；是否检索判定由专门用例显式开启，
# 避免每条旧用例都被迫 mock 第二种 JSON chat 响应。
settings.qa_decision_enabled = False
# 服务凭据：测试进程里给一个确定值，客户端按它带 Authorization。
# **不设 ALLOW_INSECURE_DEFAULTS** —— 那会跳过启动检查，
# 而我们恰恰要让测试跑在"检查是打开的"那个形态上
settings.service_token = "test-service-token"

from ddp_corpus.main import app  # noqa: E402
from ddp_corpus.models import Base  # noqa: E402
from ddp_core.search import MemoryIndex  # noqa: E402
from ddp_corpus import directory  # noqa: E402
from ddp_corpus.service_client import ServiceClient  # noqa: E402
from ddp_corpus.storage import MemoryStorage  # noqa: E402

SERVICE = "http://127.0.0.1:9000"       # 与 settings.service_url 默认值一致
CONTROL = "http://127.0.0.1:8080"       # 与 settings.control_url 默认值一致
EMBEDDINGS = f"{SERVICE}/v1/embeddings"
CHAT = f"{SERVICE}/v1/chat/completions"

#: 测试里用的默认组织与调用者。生产里这组值由 control-api 填。
ORG = "org-test"
ACTOR = "actor-alice"


def actor_headers(actor_id: str = ACTOR, *, role: str = "contributor",
                  kind: str = "user", organization_id: str = ORG,
                  api_key_id: str | None = None) -> dict[str, str]:
    """一次调用的完整身份头。

    默认角色是 contributor（能上传、能问答、不能删别人的文档）——
    **不要默认给 admin**：那会让"权限不足"这类用例在默认装配下永远绿。
    """
    headers = {
        "Authorization": f"Bearer {settings.service_token}",
        "X-DDP-Organization": organization_id,
        "X-DDP-Actor": actor_id,
        "X-DDP-Actor-Kind": kind,
        "X-DDP-Role": role,
        "X-Request-Id": "test-request",
    }
    if api_key_id:
        headers["X-DDP-Api-Key"] = api_key_id
    return headers


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})

    # SQLite 默认**不强制外键**，而生产是 PG。不开这个 pragma，
    # "删父行时子行还挂着"这类 bug 单测全都放行，只有真机 e2e 才炸（已经被咬过一次）
    @event.listens_for(eng.sync_engine, "connect")
    def _enforce_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 直接替换 db 模块的缓存，让 get_session 依赖走测试库
    db._engine, db._sessionmaker = eng, async_sessionmaker(eng, expire_on_commit=False)
    yield eng
    await eng.dispose()
    db.reset_engine()


@pytest.fixture
async def app_state(engine):
    app.state.http = httpx.AsyncClient(timeout=5.0, trust_env=False)
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MemoryStorage()
    app.state.search_index = MemoryIndex()
    app.state.redis = None                          # 单实例模式（多副本路径由 e2e 覆盖）
    # 显示名缓存跨用例泄漏会让"改了名字没生效"这类断言飘
    directory.reset_cache()
    yield app.state
    await app.state.http.aclose()


@pytest.fixture
async def client(app_state):
    """裸客户端 —— **不带任何身份头**。

    用它来验"没有身份会被拒"。要干活请用 `actor_client`。
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://corpus",
                                 trust_env=False) as c:
        yield c


@pytest.fixture
async def actor_client(client):
    """带完整 actor 上下文的客户端（生产里入口填的那组头）。"""
    client.headers.update(actor_headers())
    return client


@pytest.fixture
async def admin_client(client):
    """管理员视角。删别人的文档、复核队列这类用例用它。"""
    client.headers.update(actor_headers("actor-admin", role="admin"))
    return client


@pytest.fixture
async def service_client_headers():
    """服务身份（`/internal/*` 用）。"""
    return actor_headers("control-api", role="admin", kind="service")


@pytest.fixture
async def session(engine):
    async with db.get_sessionmaker()() as s:
        yield s


# ---------------------------------------------------------------- 上传替身

async def submit_document(client: httpx.AsyncClient, storage, content: bytes,
                          *, filename: str = "sample.pdf",
                          mime: str = "application/pdf",
                          actor_id: str = ACTOR, organization_id: str = ORG,
                          engine: str = "", options: dict | None = None,
                          event_id: str | None = None) -> httpx.Response:
    """走**新的上传链路**：对象先在存储里，再投一个 DocumentSubmitted 事件。

    合仓前这里是 `POST /api/documents` 的 multipart —— 那条路已经删了，
    因为它把整份文件读进一个 `bytes`（违反不变式 6）。现在字节流由浏览器
    直传对象存储，本服务只收元数据。

    `doc_id` 用内容 sha256，与 control-api 流式校验算出来的一致。
    `engine` 留空表示"按部署默认"—— 与真实事件一致（用户没选引擎时
    control-api 也不填），这样 `DEFAULT_PARSE_ENGINE` 的行为才被真的测到。
    """
    import hashlib
    import uuid

    digest = hashlib.sha256(content).hexdigest()
    object_key = f"uploads/{organization_id}/{uuid.uuid4().hex}.pdf"
    await storage.put(object_key, content, mime)
    return await client.post(
        "/internal/events",
        json={
            "event_id": event_id or uuid.uuid4().hex,
            "type": "DocumentSubmitted",
            "organization_id": organization_id,
            "payload": {
                "actor_id": actor_id, "object_key": object_key, "filename": filename,
                "mime": mime, "size": len(content), "sha256": digest,
                "engine": engine, "options": options or {},
            },
        },
        headers=actor_headers("control-api", role="admin", kind="service",
                              organization_id=organization_id),
    )


# ------------------------------------------------------------ 用量断言替身

async def usage_events(session, kind: str | None = None) -> list[dict]:
    """从 outbox 里取用量事件。

    合仓前用量是 `UsageRecord` 表的行，直接 select 就能断言。现在计量的
    **真相在 control schema**（Go 扣配额、出账单），语料侧只把用量作为
    事件发出去 —— 两边都写同一张表就是两个写入所有者（企业边界 5）。

    所以断言口径变成"outbox 里有没有这条事件"。**语义没变**：
    "这次操作有没有被计量"仍然是可断言的，而且更严格 —— 事件必须与业务
    写入在同一个事务里，漏掉就是漏掉，不会因为"记账那句在别处 commit 过了"而蒙混过关。
    """
    from sqlalchemy import select

    from ddp_corpus.models import CorpusOutbox

    rows = (await session.execute(
        select(CorpusOutbox).where(CorpusOutbox.type == "UsageRecorded")
        .order_by(CorpusOutbox.created_at)
    )).scalars().all()
    out = [dict(r.payload, _event_id=r.id) for r in rows]
    if kind is not None:
        out = [e for e in out if e.get("kind") == kind]
    return out


def as_actor(actor_id: str, *, role: str = "contributor") -> dict[str, str]:
    """"换一个人"的头。

    合仓前这里是 `register(client, username="bob")` 再拿它的 JWT —— 本服务
    已经不认识用户了，"另一个人"就是另一组 actor 头。这让这类用例更贴近
    生产：入口验完身份之后，语料侧看到的**只有**这几个字符串。
    """
    return actor_headers(actor_id, role=role)
