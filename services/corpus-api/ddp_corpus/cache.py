"""P6 有界缓存：条目/字节/TTL 三重上限、确定性驱逐、探测复用与失效钩子。

缓存是**可重建的投影**，不是事实来源。写不进去、被驱逐、过期都只是退化成
一次重算，绝不允许它变成"比源数据更权威"的第二份真相。三条边界：

1. **上限由写入端确定性执行**（不是靠后台任务事后清理）：`put` 先删过期，
   再按 `hits`（使用次数）→ `created_at` → `id` 的固定次序驱逐，返回时表的
   条目数/字节数一定不超限。`get` 顺手删掉读到的过期行。
2. **不重新实现联邦策略**：探测复用一律走 `ddp_core.application.probe.reusable`
   （TTL / query digest / `retrieval.index_revision`）；集合缓存有效性走
   `catalog.cache_revision`；Wiki 失效走 `wiki_dependencies` 同一张依赖表。
3. **负面缓存绑定修订**：键里带 `(scope, node, node_revision)`，新的节点修订或
   新上传产生新键 —— 旧的黑名单条目不能把新资料永远挡在门外。负面条目也
   **永远不会**被 `find_reusable_probe` 当成探测回执：后者只读
   `federation_probes`，两个命名空间互不相通。

并发说明：PostgreSQL 上 `put` 取一个事务级 advisory lock 来串行化上限判定
（多进程部署下两个写入者不会各自按"我看到的不超限"提交出超限结果）。
SQLite 不需要 —— 写事务本身就是串行的。
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import (
    DateTime, Integer, JSON, String, UniqueConstraint,
    delete, func, select, text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from ddp_core.application.plans import canonical_bytes, instant
from ddp_core.application.probe import reusable
from ddp_core.application.ports import ApplicationError
from ddp_core.models import Base, new_id

from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.federation_models import FederationProbe
from ddp_corpus.models import as_aware


class FederationCacheEntry(Base):
    """一行有界缓存条目。`value_json` 只存有界投影（摘要/描述符/小结果）。

    `(scope_key, cache_key)` 唯一：scope 是**授权/归属边界**（org / resource /
    collection / wiki），cache_key 是投影身份。失效按三者任意子集删除。
    """

    __tablename__ = "federation_cache_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    cache_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("scope_key", "cache_key", name="uq_federation_cache_scope_key"),
    )


@dataclass(frozen=True)
class CacheLimits:
    """一次缓存操作的三个上限 + 单 scope 条目上限。

    默认值取自部署配置（`FEDERATION_CACHE_*`）。调用方可以给更紧的值；给更松
    的值没有意义 —— 全局上限仍由同一组字段决定，不存在"绕过"的入口。
    `per_scope_entries` 防止一个 scope 把整张表占满，把其他组织饿死。
    """

    entries: int = field(default_factory=lambda: settings.federation_cache_max_entries)
    bytes: int = field(default_factory=lambda: settings.federation_cache_max_bytes)
    ttl_seconds: int = field(default_factory=lambda: settings.federation_cache_ttl_seconds)
    per_scope_entries: int = field(default_factory=lambda: settings.federation_cache_max_entries)

    def __post_init__(self):
        if min(self.entries, self.bytes, self.ttl_seconds, self.per_scope_entries) < 1:
            raise ValueError("cache limits must be positive; 0 is not「off」")


#: 负面缓存的 kind 与默认 TTL。短是刻意的：不可达/被拒是**当时的观测**，
#: 不是长期事实，重试成本远低于错误地长期绕开一个已经恢复的节点。
NEGATIVE_KIND = "negative"
NEGATIVE_TTL_SECONDS = 60
#: `find_reusable_probe` 只复用证据探测回执；能力/定位探测没有证据可复用。
PROBE_KIND = "evidence_retrieval"
#: 单目标最多回看多少行探测回执。有界扫描；命中足够新的行由 TTL 决定。
PROBE_SCAN_LIMIT = 64
#: key 列宽（唯一约束/索引都用它）。超长显式拒绝，不靠数据库截断。
MAX_KEY_CHARS = 160


def _require_text(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > MAX_KEY_CHARS:
        raise ValueError(f"{name} exceeds {MAX_KEY_CHARS} characters")
    return value


def _stamp(now: datetime) -> datetime:
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    return as_aware(now)


def _target_parts(target_key: dict) -> tuple[str, str, str]:
    if not isinstance(target_key, dict):
        raise ValueError("target_key must be an object")
    parts = tuple(_require_text(name, target_key.get(name)) for name in
                  ("origin_node_id", "collection_id", "operation"))
    return parts[0], parts[1], parts[2]


# ---------------------------------------------------------------------------
# scope / key conventions
# ---------------------------------------------------------------------------
# scope_key 是授权边界而不是普通标签：撤销/删除必须能按它精确失效。
# 这些构造函数是唯一约定，协调者与失效钩子都从这里取，避免各写一套字符串。

def organization_scope(organization_id: str) -> str:
    return "organization:" + _require_text("organization_id", organization_id)


def resource_scope(resource_id: str) -> str:
    return "resource:" + _require_text("resource_id", resource_id)


def version_scope(version_id: str) -> str:
    return "version:" + _require_text("version_id", version_id)


def collection_scope(collection_id: str) -> str:
    return "collection:" + _require_text("collection_id", collection_id)


def wiki_scope(wiki_id: str) -> str:
    return "wiki:" + _require_text("wiki_id", wiki_id)


def probe_cache_key(target_key: dict, query_digest: str, index_revision: str,
                    policy_revision: str) -> str:
    """探测结果（证据/摘要投影）的缓存键：目标 + 查询 + 索引修订 + 策略修订。

    四个分量缺一不可：少了目标会把别的集合的结果端过来；少了索引修订会在
    重建后命中旧块；少了策略修订会把撤销授权前的结果继续当成可用。
    """
    origin, collection, operation = _target_parts(target_key)
    if not isinstance(policy_revision, str):
        raise ValueError("policy_revision must be a string")
    payload = [origin, collection, operation,
               _require_text("query_digest", query_digest),
               _require_text("index_revision", index_revision),
               policy_revision]
    return "probe:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def negative_cache_key(scope_key: str, node_id: str, node_revision: str) -> str:
    """不可达/被拒观测的键：**必须**绑定节点修订。

    少了 `node_revision`，一次网络抖动就会变成"这个节点永远不可达"；
    新上传（产生新修订）也会被旧的否定条目挡住 —— 正是负面缓存最危险的用法。
    """
    return "negative:" + hashlib.sha256(canonical_bytes([
        _require_text("scope_key", scope_key),
        _require_text("node_id", node_id),
        _require_text("node_revision", node_revision)])).hexdigest()


def payload_bytes(value: dict) -> int:
    """value 的规范 JSON 字节数 —— 与落库时记录的 `bytes` 同一口径。"""
    if not isinstance(value, dict):
        raise ValueError("cache value must be a JSON object")
    return len(canonical_bytes(value))


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

async def _lock(session: AsyncSession, key: str) -> None:
    """PostgreSQL 事务级 advisory lock；SQLite 写事务本身串行，不需要。"""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


async def get(session: AsyncSession, *, scope_key: str, cache_key: str,
              now: datetime) -> dict | None:
    """读一条缓存；过期按不存在处理并懒删除。命中 +1（驱逐的排序依据）。"""
    _require_text("scope_key", scope_key)
    _require_text("cache_key", cache_key)
    stamp = _stamp(now)
    row = await session.scalar(select(FederationCacheEntry).where(
        FederationCacheEntry.scope_key == scope_key,
        FederationCacheEntry.cache_key == cache_key))
    if row is None:
        return None
    if as_aware(row.expires_at) <= stamp:
        await session.delete(row)
        await session.flush()
        return None
    row.hits += 1
    await session.flush()
    return copy.deepcopy(row.value_json)


def _eviction_order():
    """最少使用优先；并列时先建先出；再并列按 id —— 全序，结果可复现。"""
    return (FederationCacheEntry.hits, FederationCacheEntry.created_at,
            FederationCacheEntry.id)


async def _count(session: AsyncSession, *, scope_key: str | None) -> int:
    statement = select(func.count(FederationCacheEntry.id))
    if scope_key is not None:
        statement = statement.where(FederationCacheEntry.scope_key == scope_key)
    return int(await session.scalar(statement) or 0)


async def _evict(session: AsyncSession, *, scope_key: str | None, count: int) -> int:
    if count <= 0:
        return 0
    statement = (select(FederationCacheEntry.id).order_by(*_eviction_order()).limit(count))
    if scope_key is not None:
        statement = statement.where(FederationCacheEntry.scope_key == scope_key)
    victims = list((await session.scalars(statement)).all())
    if not victims:
        return 0
    await session.execute(delete(FederationCacheEntry).where(
        FederationCacheEntry.id.in_(victims)).execution_options(synchronize_session=False))
    return len(victims)


async def _trim(session: AsyncSession, *, limits: CacheLimits, scope_key: str) -> None:
    """把表压回所有上限之内：先按 scope，再按全局条目，最后按全局字节。"""
    scoped = await _count(session, scope_key=scope_key)
    if scoped > limits.per_scope_entries:
        await _evict(session, scope_key=scope_key, count=scoped - limits.per_scope_entries)
    while True:
        total, size = (await session.execute(select(
            func.count(FederationCacheEntry.id),
            func.coalesce(func.sum(FederationCacheEntry.bytes), 0)))).one()
        if total <= limits.entries and size <= limits.bytes:
            return
        if total > limits.entries:
            await _evict(session, scope_key=None, count=total - limits.entries)
            continue
        # 字节超限：一次驱逐一行的最少使用项，直到总量回落到上限内。
        await _evict(session, scope_key=None, count=1)


async def put(session: AsyncSession, *, scope_key: str, cache_key: str, kind: str,
              value: dict, ttl_seconds: int, limits: CacheLimits,
              now: datetime) -> None:
    """写入一条缓存并在**返回前**执行全部上限。

    - TTL 取 `min(ttl_seconds, limits.ttl_seconds)`；非正 TTL 显式拒绝。
    - value 规范字节超过 `limits.bytes` 时**不截断、不写入**；同键旧值一并删除
      （旧值已被这次写入取代，继续命中它是更糟的错）。
    - 驱逐顺序：过期行 → 单 scope 超限 → 全局条目超限 → 全局字节超限。
    """
    _require_text("scope_key", scope_key)
    _require_text("cache_key", cache_key)
    _require_text("kind", kind)
    if not isinstance(value, dict):
        raise ValueError("cache value must be a JSON object")
    if type(ttl_seconds) is not int or ttl_seconds < 1:
        raise ValueError("ttl_seconds must be a positive integer")
    stamp = _stamp(now)
    size = payload_bytes(value)
    expires = stamp + timedelta(seconds=min(ttl_seconds, limits.ttl_seconds))
    await _lock(session, "federation-cache")
    await session.execute(delete(FederationCacheEntry).where(
        FederationCacheEntry.expires_at <= stamp).execution_options(synchronize_session=False))
    row = await session.scalar(select(FederationCacheEntry).where(
        FederationCacheEntry.scope_key == scope_key,
        FederationCacheEntry.cache_key == cache_key))
    if size > limits.bytes:
        if row is not None:
            await session.delete(row)
            await session.flush()
        return
    if row is None:
        session.add(FederationCacheEntry(
            scope_key=scope_key, cache_key=cache_key, kind=kind, value_json=value,
            bytes=size, hits=0, created_at=stamp, expires_at=expires))
    else:
        row.kind = kind
        row.value_json = value
        row.bytes = size
        row.expires_at = expires
    await session.flush()
    await _trim(session, limits=limits, scope_key=scope_key)


async def invalidate(session: AsyncSession, *, scope_key: str | None = None,
                     cache_key: str | None = None, kind: str | None = None) -> int:
    """按任意过滤器组合删除缓存行，返回删除数。

    **至少要给一个过滤器**：全表删除必须是显式动作，不能让一次调用参数漏传
    把整张缓存清空（那会表现为"缓存效率一直上不去"，而不报任何错）。
    """
    if scope_key is None and cache_key is None and kind is None:
        raise ValueError("invalidate requires at least one filter")
    conditions = []
    if scope_key is not None:
        conditions.append(FederationCacheEntry.scope_key == scope_key)
    if cache_key is not None:
        conditions.append(FederationCacheEntry.cache_key == cache_key)
    if kind is not None:
        conditions.append(FederationCacheEntry.kind == kind)
    result = await session.execute(delete(FederationCacheEntry).where(*conditions)
                                   .execution_options(synchronize_session=False))
    return int(result.rowcount or 0)


async def purge_expired(session: AsyncSession, *, now: datetime, limit: int = 500) -> int:
    """删掉至多 `limit` 条已过期行（最旧过期优先），返回删除数。"""
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    stamp = _stamp(now)
    victims = list((await session.scalars(
        select(FederationCacheEntry.id)
        .where(FederationCacheEntry.expires_at <= stamp)
        .order_by(FederationCacheEntry.expires_at, FederationCacheEntry.id)
        .limit(limit))).all())
    if not victims:
        return 0
    result = await session.execute(delete(FederationCacheEntry).where(
        FederationCacheEntry.id.in_(victims)).execution_options(synchronize_session=False))
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# 负面缓存
# ---------------------------------------------------------------------------

async def record_negative(session: AsyncSession, *, scope_key: str, node_id: str,
                          node_revision: str, reason: str, now: datetime,
                          ttl_seconds: int = NEGATIVE_TTL_SECONDS,
                          limits: CacheLimits | None = None) -> None:
    """记录一次不可达/被拒观测。键绑定节点修订，默认 TTL 很短。"""
    _require_text("reason", reason)
    await put(
        session, scope_key=scope_key,
        cache_key=negative_cache_key(scope_key, node_id, node_revision),
        kind=NEGATIVE_KIND,
        value={"negative": True, "node_id": node_id, "node_revision": node_revision,
               "reason": reason, "recorded_at": _stamp(now).isoformat()},
        ttl_seconds=ttl_seconds, limits=limits or CacheLimits(), now=now)


async def get_negative(session: AsyncSession, *, scope_key: str, node_id: str,
                       node_revision: str, now: datetime) -> dict | None:
    """读负面条目；键/内容任一对不上都按没有处理（缓存内容不可信）。"""
    value = await get(session, scope_key=scope_key,
                      cache_key=negative_cache_key(scope_key, node_id, node_revision), now=now)
    if value is None or value.get("negative") is not True:
        return None
    if value.get("node_id") != node_id or value.get("node_revision") != node_revision:
        return None
    return value


# ---------------------------------------------------------------------------
# 探测复用
# ---------------------------------------------------------------------------

def _observed_epoch(probe: dict) -> float | None:
    """解析 `observed_at`；解析不了按不可复用处理（不抛给调用方）。"""
    raw = probe.get("observed_at")
    if not isinstance(raw, str):
        return None
    try:
        return instant(raw)
    except ApplicationError:
        return None


def _recorded_policy_revision(stored: dict, probe: dict, retrieval: dict) -> str | None:
    """探测行可能在哪里记录策略修订；None = 没有记录（不据此拒绝复用）。

    当前 ProbeResult 契约里没有 policy_revision，所以行/信封级键都允许。
    **不编造**：任何位置有记录就必须与调用方一致；没有记录就不假装它存在。
    """
    for source in (stored, probe, retrieval):
        value = source.get("policy_revision")
        if isinstance(value, str) and value:
            return value
    return None


async def find_reusable_probe(session: AsyncSession, actor: Actor, *,
                              target_key: dict, query_digest: str, index_revision: str,
                              policy_revision: str, now: datetime) -> dict | None:
    """找出仍可复用的证据探测回执；没有就返回 None（由调用方重新探测）。

    只读 `federation_probes`（organization 作用域），不读缓存表 —— 所以
    负面缓存条目**结构上不可能**被当成探测回执。TTL/摘要/索引修订一律由
    `ddp_core.application.probe.reusable` 判定：TTL 取持久行的 `expires_at`
    与回执 `observed_at` 之差（不另造一份有效期口径）。
    """
    origin, collection, _operation = _target_parts(target_key)
    if not isinstance(policy_revision, str):
        raise ValueError("policy_revision must be a string")
    _require_text("index_revision", index_revision)
    stamp = _stamp(now)
    rows = list((await session.scalars(
        select(FederationProbe).where(
            FederationProbe.organization_id == actor.organization_id,
            FederationProbe.target_node_id == origin,
            FederationProbe.collection_id == collection,
            FederationProbe.probe_kind == PROBE_KIND,
        ).order_by(FederationProbe.created_at.desc(), FederationProbe.probe_id.desc())
        .limit(PROBE_SCAN_LIMIT))).all())
    for row in rows:
        stored = row.result_json or {}
        probe = stored.get("result")
        if not isinstance(probe, dict) or probe.get("probe_kind") != PROBE_KIND:
            continue
        if probe.get("target_node_id") != origin:
            continue
        retrieval = probe.get("retrieval")
        if not isinstance(retrieval, dict) or retrieval.get("collection_ref") != collection:
            continue
        observed = _observed_epoch(probe)
        if observed is None:
            continue
        # 行上写了 expires_at = observed_at + PROBE_TTL；把它折算成 reusable 的
        # ttl 参数，过期的行就在这里被拒绝，而不是靠 SQL 预过滤（单一权威判定）。
        ttl_seconds = int(as_aware(row.expires_at).timestamp() - observed)
        if ttl_seconds < 0:
            continue
        candidate = dict(probe)
        # query_digest 是**行上的列**（契约 ProbeResult 没有这个字段），
        # reusable 需要它参与比对，所以显式注入而不是编造一个值。
        candidate["query_digest"] = row.query_digest
        if not reusable(candidate, now=stamp.timestamp(), query_digest=query_digest,
                        index_revision=index_revision, ttl_seconds=ttl_seconds):
            continue
        recorded = _recorded_policy_revision(stored, probe, retrieval)
        if recorded is not None and recorded != policy_revision:
            continue
        return copy.deepcopy(probe)
    return None
