"""运维面：限速、对象回收、就绪探针。

多副本正确性的关键在限速（进程内计数会让实际上限变成 N×limit）与对账选主，
这里覆盖可在进程内验证的部分；真 Redis 的 Lua 行为由 e2e 覆盖。
"""
import httpx
import pytest
import respx
from sqlalchemy import select

from ddp_corpus.config import settings
from ddp_corpus.errors import APIError
from ddp_corpus.gc import collect_deleted_objects
from ddp_corpus.models import Document, utcnow
import ddp_corpus.db as db
from tests.test_documents import PDF, _callback, _mock_service, _upload


# ---------------------------------------------------------------------------
# **限速的三条用例已迁去 services/control-api。**
#
# 合仓前这里有 memory/redis 两种限速器的用例（含"Redis 抖动时放行"那条）。
# 限速整体归了控制面：入口按 key 限，再按路由类别做领域限速（问答/图谱/抽取）。
# 语料侧一条限速都不做 —— 两处各限一次会让"到底是谁把我限了"没人答得上。
#
# 等价覆盖在 Go 侧：
#   internal/ratelimit  固定窗口计数、跨副本共享、Redis 不可用时退回内存并明说
#   internal/api        按 key 限速 + 领域限速（domainThrottle）
# ---------------------------------------------------------------------------


async def _delete_and_age(client, session, document_id: str) -> None:
    """删掉文档并把 deleted_at 拨回宽限期之前 —— GC 只回收"删了有一阵子"的文档。"""
    from datetime import timedelta

    await client.delete(f"/api/documents/{document_id}")
    row = await session.get(Document, document_id)
    await session.refresh(row)
    row.deleted_at = utcnow() - timedelta(seconds=settings.gc_grace_seconds + 60)
    # Resource/Version now own deletion; age the logical tombstones as well, so
    # this old storage test does not accidentally assert GC ignores their grace period.
    from ddp_corpus.models import Resource, ResourceVersion
    versions = list((await session.execute(select(ResourceVersion).where(
        ResourceVersion.document_id == document_id))).scalars())
    for version in versions:
        if version.deleted_at:
            version.deleted_at = row.deleted_at
        resource = await session.get(Resource, version.resource_id)
        if resource.deleted_at:
            resource.deleted_at = row.deleted_at
    await session.commit()


@respx.mock
async def test_gc_removes_objects_of_deleted_documents(actor_client, session, app_state):
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    storage = app_state.storage
    # 对象键现在由 control-api 在**签发预签名时**生成（`uploads/{org}/{日期}/{随机}`）——
    # 那时还没有 document.id，所以键不再带文档 id。GC 不受影响：它按
    # `document.object_key` 逐个删，从不按前缀猜
    assert any(k.startswith("uploads/") for k in storage.objects)
    assert any(k.startswith("results/") for k in storage.objects)

    await _delete_and_age(actor_client, session, document["id"])
    cleaned = await collect_deleted_objects(db.get_sessionmaker(), storage)
    assert cleaned == 1
    assert not [k for k in storage.objects if k.startswith("uploads/")]
    assert not [k for k in storage.objects if k.startswith("results/")]

    row = await session.get(Document, document["id"])
    await session.refresh(row)
    assert row.object_key == "", "回收完要留标记，下一轮不再重复删"

    # 再跑一次不该重复处理
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0


@respx.mock
async def test_gc_handles_migrated_job_whose_prefix_differs(actor_client, session, app_state):
    """M5 迁移过来的 job：id 是新 uuid，归档产物却还在 results/{原 task_id}/ 下。

    GC 只按 job.id 拼前缀的话会列到空集，然后把 object_key 置空标成"已回收"——
    对象永久留在存储里再也不会被重试。而 crops 又是按 job.id 写的，
    所以两个前缀都得列。
    """
    from ddp_corpus.models import ParseJob

    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)

    job = (await session.execute(select(ParseJob))).scalars().one()
    storage = app_state.storage
    # 造出迁移后的形态：result_prefix 指向另一个前缀，crops 仍按 job.id 落
    legacy_prefix = "results/legacy-task-id/"
    for key in [k for k in storage.objects if k.startswith(f"results/{job.id}/")]:
        storage.objects[key.replace(f"results/{job.id}/", legacy_prefix)] = storage.objects.pop(key)
    job.result_prefix = legacy_prefix
    await session.commit()
    await storage.put(f"results/{job.id}/crops/0_abc.png", b"crop", "image/png")

    await _delete_and_age(actor_client, session, document["id"])
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 1
    assert not [k for k in storage.objects if k.startswith(legacy_prefix)], "归档产物要清掉"
    assert not [k for k in storage.objects if k.startswith(f"results/{job.id}/")], "crops 也要清掉"
    assert not [k for k in storage.objects if k.startswith("uploads/")]


@respx.mock
async def test_gc_respects_the_grace_period(actor_client, app_state):
    """刚删掉的文档不回收 —— 删对象不可逆，而"误删后马上重传"是常见操作。"""
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    before = set(app_state.storage.objects)

    await actor_client.delete(f"/api/documents/{document['id']}")
    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert set(app_state.storage.objects) == before, "宽限期内一个对象都不许动"


@respx.mock
async def test_gc_does_not_delete_a_revived_documents_source(actor_client, session, app_state):
    """回归：删了又传回来的文档，其原件不得被 GC 删掉。

    旧实现先 SELECT 出待回收的行，再无条件删对象并清 object_key —— 期间用户
    重新上传（upload 的复活分支）就会把刚传上去的原件删掉，且 object_key 被清空。
    现在删对象前要先 claim（条件 UPDATE 带 deleted_at IS NOT NULL），
    已提交的复活会让 claim 落空。
    """
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    storage = app_state.storage

    old_row = await session.get(Document, document["id"])
    await session.refresh(old_row, ["object_key"])
    old_key = old_row.object_key
    await _delete_and_age(actor_client, session, document["id"])
    # 复活：同一份文件重新上传（新对象键；旧 key 被 park 进 manifest）
    revived = await _upload(actor_client, content=PDF)
    assert revived["id"] == document["id"], "同内容重传应复活同一行"

    row = await session.get(Document, document["id"])
    await session.refresh(row, ["deleted_at", "object_key", "gc_pending_keys"])
    assert row.deleted_at is None and row.object_key
    assert row.object_key != old_key, "复活必须换新对象键，旧 key 才有 park 的意义"
    assert old_key in row.gc_pending_keys, "被取代的旧 key 必须 park 进 gc_pending_keys"
    assert await storage.exists(old_key)

    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0, \
        "已复活的文档不得被回收"
    await session.refresh(row, ["deleted_at", "object_key", "gc_pending_keys"])
    assert row.deleted_at is None and row.object_key
    assert await storage.get(row.object_key) == PDF, "复活后的原件被 GC 删掉了"
    # live-drain 删掉旧 key、排空 manifest，而不是永远泄漏。
    assert row.gc_pending_keys == [], "live-drain 必须排空 manifest"
    assert not await storage.exists(old_key), "旧 key 必须被 live-drain 删掉"
    assert await storage.get(row.object_key) == PDF, "复活后的原件必须保留"


@respx.mock
async def test_gc_revival_racing_manifest_commit_loses_claim_without_deleting(
    actor_client, session, app_state, monkeypatch,
):
    """复活 racing manifest commit：条件 claim 落空，一个字节都不许删。"""
    import ddp_corpus.gc as gc_module

    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    storage = app_state.storage
    await _delete_and_age(actor_client, session, document["id"])
    row = await session.get(Document, document["id"])
    await session.refresh(row, ["object_key"])
    live_key = row.object_key

    real_claim = gc_module._claim_manifest

    async def claim_then_revive(claim_session, claim_document, *, deleted_at, keys):
        # 在 claim 的条件 UPDATE 生效前提交一次复活：row 已 live，claim 必须落空。
        # 复活按 ingest._revive 的语义把旧 key park 进 manifest 再换新 key。
        revived_key = live_key + ".revived"
        await storage.put(revived_key, PDF, "application/pdf")
        async with db.get_sessionmaker()() as revival:
            live = await revival.get(Document, claim_document.id)
            parked = list(live.gc_pending_keys or [])
            for key in (live.object_key, revived_key):
                if key and key not in parked:
                    parked.append(key)
            await revival.execute(
                gc_module.update(Document).where(Document.id == claim_document.id).values(
                    deleted_at=None, object_key=revived_key, gc_pending_keys=parked,
                    updated_at=gc_module.utcnow()))
            await revival.commit()
        return await real_claim(claim_session, claim_document, deleted_at=deleted_at, keys=keys)

    monkeypatch.setattr(gc_module, "_claim_manifest", claim_then_revive)
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0
    assert await storage.exists(live_key), "claim 落空后旧 key 不得被删"
    await session.refresh(row, ["deleted_at", "object_key"])
    assert row.deleted_at is None
    assert await storage.get(row.object_key) == PDF


@respx.mock
async def test_gc_drains_manifest_committed_before_revival(actor_client, session, app_state):
    """已回收行的旧 key 被复活引用时：复活 park 住它，live-drain 复核后放行。"""
    _mock_service()
    document = await _upload(actor_client)
    await _callback(actor_client)
    storage = app_state.storage
    await _delete_and_age(actor_client, session, document["id"])
    # 宽限期已过：第一轮直接收完字节并计 cleaned == 1；第二轮验证幂等。
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 1
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0
    row = await session.get(Document, document["id"])
    await session.refresh(row, ["object_key", "gc_pending_keys"])
    assert row.object_key == "" and row.gc_pending_keys == []
    old_objects = dict(storage.objects)
    revived = await _upload(actor_client, content=PDF)
    assert revived["id"] == document["id"]
    await session.refresh(row, ["deleted_at", "object_key", "gc_pending_keys"])
    assert row.deleted_at is None and row.object_key
    # 旧 key 已不在存储里：复活 park 住的是已删 key，live-drain 复核引用后放行。
    assert await collect_deleted_objects(db.get_sessionmaker(), storage) == 0
    await session.refresh(row, ["gc_pending_keys"])
    assert row.gc_pending_keys == []
    assert await storage.get(row.object_key) == PDF
    assert set(storage.objects) == set(old_objects) | {row.object_key}


@respx.mock
async def test_gc_leaves_live_documents_alone(actor_client, app_state):
    _mock_service()
    await _upload(actor_client)
    await _callback(actor_client)
    before = set(app_state.storage.objects)

    assert await collect_deleted_objects(db.get_sessionmaker(), app_state.storage) == 0
    assert set(app_state.storage.objects) == before


@pytest.mark.parametrize("field", ["service_token", "minio_secret_key", "database_url"])
def test_placeholder_secrets_refuse_to_start(monkeypatch, field):
    """占位密钥必须启动即失败。

    service_token 是 change-me = 本服务的唯一门禁形同虚设。而 actor 上下文头
    之所以可信，前提正是"只有持有它的调用方能进来"—— 门禁没了，
    任何能连到本端口的人都能自称 admin。

    minio 出厂默认口令（minioadmin）与 database_url 开发默认口令（ddp:ddp）同理：
    只查 service_token 会让人误以为"密钥都查过了"，而对象存储/数据库等于
    没有门禁。

    **jwt_secret 那条迁去了 Go**（control-api 拥有会话签发）：
    见 services/control-api/internal/config/config_test.go 的
    TestPlaceholderSecretsAreRejected。
    """
    from ddp_corpus.config import assert_secrets_configured

    placeholders = {"service_token": "change-me",
                    "minio_secret_key": "minioadmin",
                    "database_url": "postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse"}
    monkeypatch.setattr(settings, field, placeholders[field])
    # **逃生口要显式关掉，不能靠环境里恰好没开。**
    # CI 的 job 级 env 设了 `ALLOW_INSECURE_DEFAULTS: "true"`（一次性容器没有
    # .env，不开的话任何走 lifespan 的用例都会被拦），于是这条断言
    # DID NOT RAISE —— 而它守的正是"门禁形同虚设"。
    monkeypatch.setattr(settings, "allow_insecure_defaults", False)
    with pytest.raises(RuntimeError, match=field.upper()):
        assert_secrets_configured()

    # 有明确的逃生口，但必须显式打开
    monkeypatch.setattr(settings, "allow_insecure_defaults", True)
    assert_secrets_configured()


def test_default_minio_account_name_with_random_secret_passes(monkeypatch):
    """access key 是账号名不是凭据：口令随机时它叫 minioadmin 不该拦启动。"""
    from ddp_corpus.config import assert_secrets_configured

    monkeypatch.setattr(settings, "allow_insecure_defaults", False)
    monkeypatch.setattr(settings, "service_token", "another-random-value")
    monkeypatch.setattr(settings, "minio_access_key", "minioadmin")
    monkeypatch.setattr(settings, "minio_secret_key", "real-secret-key")
    monkeypatch.setattr(settings, "database_url",
                         "postgresql+asyncpg://ddp:real-password@127.0.0.1:15432/deepdocparse")
    assert_secrets_configured()


def test_real_secrets_pass_the_check(monkeypatch):
    monkeypatch.setattr(settings, "service_token", "another-random-value")
    monkeypatch.setattr(settings, "minio_access_key", "real-access-key")
    monkeypatch.setattr(settings, "minio_secret_key", "real-secret-key")
    monkeypatch.setattr(settings, "database_url",
                         "postgresql+asyncpg://ddp:real-password@127.0.0.1:15432/deepdocparse")
    from ddp_corpus.config import assert_secrets_configured

    assert_secrets_configured()


async def test_empty_service_token_never_authenticates(monkeypatch):
    """空 service_token 配置永远拒绝 —— 即使 ALLOW_INSECURE 打开。

    `compare_digest("", "")` 是 True：没有这道闸的话，
    SERVICE_TOKEN='' + `Authorization: Bearer ` 就是一把万能钥匙。
    直接调门禁依赖（/readyz 本身不挂门禁，没有可打的端点）。
    """
    from ddp_corpus import deps

    monkeypatch.setattr(settings, "service_token", "")
    monkeypatch.setattr(settings, "allow_insecure_defaults", True)
    with pytest.raises(deps.APIError) as exc:
        await deps.require_gateway_credentials(f"Bearer {settings.service_token}")
    assert exc.value.status_code == 401
    assert exc.value.code == "invalid_service_token"


async def test_empty_bearer_rejected_before_compare(monkeypatch):
    """空 bearer 头在 compare_digest 之前 401。

    配置是正常密钥，但请求头是 `Bearer `（strip 后为空）：必须 401，
    不能走到与正常密钥的比较里去。
    """
    from ddp_corpus import deps

    assert settings.service_token not in ("", "change-me")
    with pytest.raises(deps.APIError) as exc:
        await deps.require_gateway_credentials("Bearer ")
    assert exc.value.status_code == 401
    with pytest.raises(deps.APIError) as exc2:
        await deps.require_gateway_credentials("Bearer    ")
    assert exc2.value.status_code == 401


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


@respx.mock
async def test_readyz_exposes_outbox_and_queue_backlog(client, app_state, session):
    """/readyz 报 outbox 积压（含 abandoned）与队列水位。

    空库时两组都是零值且 ready；abandoned > 0（重投封顶后仍躺在表里）
    直接判 not-ready —— 那是需要人工处理的丢账风险，不是"正常"。
    """
    from tests.conftest import SERVICE

    from ddp_corpus.models import CorpusOutbox

    respx.get(f"{SERVICE}/healthz").mock(return_value=httpx.Response(200))
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["outbox"] == "ok"
    assert body["outbox"]["undelivered"] == 0
    assert body["outbox"]["oldest_seconds"] == 0.0
    assert body["outbox"]["abandoned"] == 0
    assert isinstance(body["queue"], dict) and body["queue"], "队列水位必须用 queue.backlog() 报出来"
    first = next(iter(body["queue"].values()))
    assert set(first) == {"pending", "oldest_seconds"}

    # 一条重投封顶的 parked 事件：仍算未投递 + abandoned，ready 翻成 False
    from ddp_corpus.outbox import MAX_ATTEMPTS

    session.add(CorpusOutbox(organization_id="org-1", type="UsageRecorded",
                             payload={}, attempts=MAX_ATTEMPTS))
    await session.commit()
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["outbox"]["undelivered"] == 1
    assert body["outbox"]["abandoned"] == 1
    assert body["outbox"]["oldest_seconds"] >= 0.0
    assert body["checks"]["outbox"].startswith("error")


async def test_healthz_does_not_depend_on_anything(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


def test_content_diagnostics_off_by_default(monkeypatch):
    """内容诊断默认关，留存有界，权限钩子默认拦非 admin。

    plan.md §8.6：日志默认不记完整问题/原文片段；诊断要有显式开关、
    主体权限和保留期限。三道门缺一即不放行。
    """
    from ddp_corpus.config import content_diagnostics_permitted
    from ddp_corpus.deps import Actor

    assert settings.content_diagnostics_enabled is False
    assert settings.content_diagnostics_require_admin is True
    assert 0 < settings.content_diagnostics_retention_seconds <= 30 * 24 * 3600
    # 开关没开：谁来都不放行
    assert content_diagnostics_permitted(
        Actor(id="a", kind="user", organization_id="o", role="admin")) is False

    # 开关开了：admin 放行，非 admin 照样拦
    monkeypatch.setattr(settings, "content_diagnostics_enabled", True)
    assert content_diagnostics_permitted(
        Actor(id="a", kind="user", organization_id="o", role="admin")) is True
    assert content_diagnostics_permitted(
        Actor(id="u", kind="user", organization_id="o", role="contributor")) is False
    # actor 没传（None）= 无权，绝不因"没传 actor"而放行
    assert content_diagnostics_permitted(None) is False

    # 显式放开 admin 门（require_admin=false）后普通身份也可诊断 —— 但开关仍是总闸
    monkeypatch.setattr(settings, "content_diagnostics_require_admin", False)
    assert content_diagnostics_permitted(
        Actor(id="u", kind="user", organization_id="o", role="contributor")) is True
    monkeypatch.setattr(settings, "content_diagnostics_enabled", False)
    assert content_diagnostics_permitted(
        Actor(id="a", kind="user", organization_id="o", role="admin")) is False


def test_content_diagnostics_retention_bounded():
    """留存上限 30 天：配超了启动即失败，而不是悄悄变成第二份原文库。"""
    from ddp_corpus.config import Settings

    with pytest.raises(Exception, match="CONTENT_DIAGNOSTICS_RETENTION_SECONDS"):
        Settings(content_diagnostics_retention_seconds=31 * 24 * 3600)


@respx.mock
async def test_metrics_exposed(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200 and "http_request" in resp.text


def test_rerank_config_falls_back_to_service_token(monkeypatch):
    """`rerank_token` 留空时必须退回 `service_token`。

    这条兜底是**部署约定**：rerank 与 embed 是同一类 TEI 容器，在同一张内网里，
    绝大多数部署不会为它单独配一个令牌。`docs/CONFIG.md` 与 `config.py:149`
    的注释都写着"留空用 service_token"，但之前没有任何用例覆盖这条兜底 ——
    去掉 `or settings.service_token` 后既有验收仍全绿。

    砍掉的后果是**静默的**：token 变空串 -> TEI 返回 401 ->
    `rerank_hits` 打 `rerank_unavailable` 照常返回原名次。
    看起来只是"没部署 rerank"，实际是配置装配错了（降级必须可见，
    但这里降级的原因会指向错误的方向）。
    """
    from ddp_corpus.config import rerank_config

    monkeypatch.setattr(settings, "service_token", "svc-token")

    monkeypatch.setattr(settings, "rerank_token", "")
    assert rerank_config().token == "svc-token", "留空没有退回 service_token"

    # 配了就用配的那个 —— 不许反过来被 service_token 盖掉
    monkeypatch.setattr(settings, "rerank_token", "its-own-token")
    assert rerank_config().token == "its-own-token"


def test_rerank_config_carries_the_rest_of_the_settings(monkeypatch):
    """其余四个字段是直传，接错一个就是"配了但没生效"（而且照样是 404 + 可见降级，
    看起来跟"没部署 rerank"一模一样）。

    `endpoint` 不是直传而是走 `rerank_endpoint` 那个计算属性：
    留空回落到 `{service_url}/v1/rerank`，配了就用配的。这条一并钉住。
    """
    from ddp_corpus.config import rerank_config

    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_model", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setattr(settings, "rerank_timeout", 12.5)

    # 留空 -> 回落到 service_url
    monkeypatch.setattr(settings, "rerank_url", "")
    monkeypatch.setattr(settings, "service_url", "http://svc:9000")
    cfg = rerank_config()
    assert cfg.endpoint == "http://svc:9000/v1/rerank", "留空没有回落到 service_url"
    assert (cfg.enabled, cfg.model, cfg.timeout) == (
        True, "BAAI/bge-reranker-v2-m3", 12.5)

    # 配了就用配的 —— 独立部署一个 TEI rerank 容器时走这条
    monkeypatch.setattr(settings, "rerank_url", "http://tei-rerank:80/rerank")
    assert rerank_config().endpoint == "http://tei-rerank:80/rerank"
