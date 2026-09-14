# P6 有界缓存与 Wiki 依赖失效自验（2026-09-13）

当前状态：**有界缓存服务、负面缓存、探测复用与 Wiki 依赖失效切片自验通过**。
执行权威是工作区 `DeepDocParse_桌面与可验证联邦路由升级计划_v3.md` §5.5 与 P6
（PR32「有界副本/证据/负面缓存、Wiki依赖失效」）。接口形状沿用
`docs/refactor/P5-INTERFACES-v3.md` 的既有对象；本文件只记录本切片新增的形状。
**这不是 commit 前的独立验收，也不代表 P6 全部出口**（集合摘要增量、排序早停、
路由质量基准仍缺）。

## 实现范围

| 文件 | 内容 |
|---|---|
| `services/corpus-api/ddp_corpus/cache.py`（新） | 缓存表 ORM、`CacheLimits`、`get/put/invalidate/purge_expired`、探测键/负面键、`find_reusable_probe`、scope 约定与失效工具 |
| `config.py` | `FEDERATION_CACHE_MAX_ENTRIES=10000` / `MAX_BYTES=64 MiB` / `TTL_SECONDS=900` + 正整数校验 |
| `catalog.py` | `cache_revision(row, index_revision)`（复用本模块 `digest`，不写第二份指纹）；`withdraw` 分支同事务清该集合缓存 |
| `wiki.py` | `invalidate_dependents(session, resource_id)`：清已发布指针 + 清 wiki scope 缓存；不复制读路径策略 |
| `resources.py` | `tombstone_resource` 在提交前调用 wiki 失效，并清 resource/version/所属 collection 的缓存 scope |
| `database/corpus/alembic/versions/0032_federation_cache.py`（新） | `federation_cache_entries` |
| `services/corpus-api/tests/test_cache.py`（新，21 条） | 上限/驱逐/失效/键/复用/目录指纹/撤回 |
| `services/corpus-api/tests/test_wiki_cache_invalidation.py`（新，3 条） | 撤回 -> Wiki 失发布 + 缓存清空、幂等、反向不误伤 |
| `services/corpus-api/tests/test_cache_pg.py`（新，2 条 opt-in） | 真 PG：迁移 head、唯一约束、JSON 往返、并发上限 |
| `services/corpus-api/CONFIG.md` | 重新生成（81 项） |

## 冻结 API（协调者按此调用）

```python
@dataclass(frozen=True)
class CacheLimits:                      # entries / bytes / ttl_seconds / per_scope_entries
    # 缺省取 FEDERATION_CACHE_*；四者必须为正（0 不是「关闭」，显式拒绝）

async def get(session, *, scope_key, cache_key, now) -> dict | None
async def put(session, *, scope_key, cache_key, kind, value, ttl_seconds,
              limits, now) -> None
async def invalidate(session, *, scope_key=None, cache_key=None, kind=None) -> int
async def purge_expired(session, *, now, limit=500) -> int
def probe_cache_key(target_key, query_digest, index_revision, policy_revision) -> str
def negative_cache_key(scope_key, node_id, node_revision) -> str
async def find_reusable_probe(session, actor, *, target_key, query_digest,
                              index_revision, policy_revision, now) -> dict | None
```

附带工具（非冻结面，但协调者应当用它们而不是自拼字符串）：

```python
def organization_scope / resource_scope / version_scope / collection_scope / wiki_scope
def payload_bytes(value) -> int
async def record_negative(session, *, scope_key, node_id, node_revision, reason, now,
                          ttl_seconds=60, limits=None) -> None
async def get_negative(session, *, scope_key, node_id, node_revision, now) -> dict | None
NEGATIVE_KIND = "negative"; NEGATIVE_TTL_SECONDS = 60
```

## 上限与驱逐语义（写入返回前必须已成立）

1. **先清过期**：`put` 先 `DELETE ... WHERE expires_at <= now`（全表，走索引）。
2. **单 scope 条目上限**：在某一个 scope 内按固定全序驱逐到 `per_scope_entries`。
3. **全局条目上限**：超出的数量一次性删除最小项。
4. **全局字节上限**：一次一行的最少使用项，直到 `sum(bytes) <= limits.bytes`。
5. **驱逐全序**：`(hits ASC, created_at ASC, id ASC)` —— 最少使用优先、先建先出、
   最后 id 兜底，完全确定性；`get` 命中 `hits += 1`，热条目活得更久。
6. **TTL**：落库 `expires_at = now + min(ttl_seconds, limits.ttl_seconds)`；`ttl_seconds <= 0`
   显式 `ValueError`。`get` 读过期的按不存在并懒删除。
7. **装不下就不写**：value 规范 JSON 字节 > `limits.bytes` 时**不截断**；同键旧行一并
   删除（旧值已被这次写入取代，继续命中它是更糟的错）。

`bytes` 的口径是 `ddp_core.application.plans.canonical_bytes`（排序键、无空白、
UTF-8），与键摘要同源。

**注意这不是严格 LRU**：冻结表形状里没有 `last_hit_at`，最近使用的信号由
`hits` 承担，`created_at` 只在同 hits 时做先建先出。缓存满时一个新写入
（hits=0）可能立刻成为被驱逐项 —— 这是刻意的「按使用付费」准入策略，
报告不得把它描述成 LRU。变更它需要先改表形状。

## 失效语义

- `invalidate` 支持 `scope_key` / `cache_key` / `kind` 的任意组合（等值过滤）；
  **不给任何过滤器显式 `ValueError`** —— 全表清空不允许靠漏参发生。
- `resources.tombstone_resource`（删除资源、删除文档的唯一落点）在同一事务内：
  - `wiki.invalidate_dependents`：把「已发布修订依赖该资源」的 Wiki 的
    `published_revision_id` 置空，并清 `wiki:<id>` scope；依赖关系读的是
    `wiki_dependencies` 同一张表，**不另写一套「谁依赖谁」**。
  - 清 `resource:<id>`、每个 `version:<vid>`、以及包含这些版本的
    `collection:<cid>` scope。
- `catalog.mutate(operation="withdraw")` 在同一事务内清 `collection:<id>` scope。
  `replace`/`publish` 不做显式失效：它们的缓存键由 `cache_revision` 绑住版本，
  旧键自然不可达；撤回不同 —— 撤回的对象**任何版本都不该再被提供**，所以必须清。
- 读路径的可读性仍由 `wiki.dependency_state` / `check_frozen_access` / 检索授权
  每次重判；失效钩子只保证「当前指针」与「缓存投影」不活过这次提交。

## 负面缓存

- 键 = `sha256(scope_key, node_id, node_revision)`，短 TTL（默认 60s）。
  新节点修订或新上传产生新键，旧否定条目**结构上不可能**挡住新资料。
- `record_negative` / `get_negative` 只读写缓存表；`find_reusable_probe` 只读
  `federation_probes`，两者命名空间不相通 —— 负面条目永远不会被当成探测回执。
- `get_negative` 还会核对 value 内的 `node_id`/`node_revision` 与键一致，
  缓存内容不可信也不放行。

## 探测复用

`find_reusable_probe` 在 `organization_id` 作用域内按
`(target_node_id, collection_id, probe_kind="evidence_retrieval")` 取最新的至多
64 行，逐行：

1. 回执形状与绑定（`probe_kind`、`target_node_id`、`retrieval.collection_ref`）核对；
2. TTL：`ttl_seconds = 行.expires_at - 回执.observed_at`（持久行的有效期是唯一口径，
   不另造一份），连同 query digest 与 `retrieval.index_revision` 一起交给
   `ddp_core.application.probe.reusable` 判定；
3. `policy_revision`：行/回执/`retrieval` 任一处有记录就必须与调用方一致；
   **没有记录就不假装它存在**（当前 P5 回执不记录，这是已知缺口，不编造）；
4. 全部通过才返回存下来的 `ProbeResult`（深拷贝），缺 `index_revision`、
   `observed_at` 解析失败、过期、摘要/修订不符一律 `None` —— 由调用方重新探测。

## 持久化（corpus 0032）

`federation_cache_entries`：id、scope_key、cache_key、kind、value_json、bytes、
hits、created_at、expires_at；唯一 `(scope_key, cache_key)`；索引
scope_key / kind / expires_at / created_at。无回填（缓存可重建，空表是正确终态）。

**迁移链**：写这段时 head 是 `0030`，因此 `0032.down_revision = "0030"`。
若并行切片随后加入 `0031`，**必须把 0032 的 down_revision 改为 0031**，
否则出现双 head。提交前用 `alembic heads`（或读 versions 目录）复核一次。

## 验证环境与结果

| 项目 | 结果与证据 |
| --- | --- |
| 全量门禁 | **29/29 PASS**（2026-09-13，改动后重跑 `./scripts/check.sh`） |
| corpus-api 默认套件 | **717 passed, 5 skipped**（含并行切片的用例；本切片新增 24 条执行 + 2 条 opt-in skip） |
| 本切片三文件 | `test_cache.py` 21、`test_wiki_cache_invalidation.py` 3、`test_cache_pg.py` 2（未设 DSN 时显式 skip） |
| 配置文档 | `scripts/gen_config_docs.py --check` 绿（81 项，含 3 个新缓存项） |
| ruff（F,B） | 全绿 |
| SQLite 并发 | 文件库 12 个并发 `put` 后条目数 ≤ 上限（写事务串行 + put 内判定） |
| 真 PG（一次性容器 `ddp-p6-cache-pg`，`pgvector/pgvector:pg16`，回环 15460） | 迁移 0001→0032 应用；0032 downgrade/upgrade 往返通过；ORM 列/索引/唯一约束与库一致；`CACHE_TEST_DATABASE_URL=…` 下 2 条 PG 用例通过（含 24 个并发 `put` 上限） |
| 失效正向 | 删除资源后：`published_revision_id` 置空、匿名读 404、wiki/resource scope 缓存均不可读；撤回集合后 collection scope 缓存不可读 |
| 失效反向 | 撤销无关资源不影响其他 Wiki 的发布指针 |
| 幂等 | `wiki.invalidate_dependents` 重复调用返回同一受影响集合且删除行数为 0；重复 `tombstone_resource` 不报错 |

## 变异确认（改掉被守的那一行，确认真的红，再还原）

| 变异 | 结果 |
| --- | --- |
| `put` 去掉 `_trim` 调用 | 红：条目/单 scope/字节三个上限用例全部失败（表超出上限） |
| `negative_cache_key` 去掉 `node_revision` 分量 | 红：修订绑定用例失败 |
| `find_reusable_probe` 用固定巨大 TTL 代替行推导 TTL | 红：过期回执被判为可复用 |
| `tombstone_resource` 去掉 `wiki.invalidate_dependents` | 红：撤回后仍有已发布指针 |
| `catalog.withdraw` 去掉缓存失效 | 红：撤回集合后仍能从缓存读到投影 |

每个变异改完先确认文件内容真的变了再跑（本仓库踩过「变异没生效、守卫被误判为假」的坑）。

## 限制与后续

- **不是严格 LRU**（见上）：需要严格 LRU 就得加 `last_hit_at` 列，那是新的表形状。
- **`get` 是写操作**（`hits += 1`）：调用方必须拥有事务并提交；只在只读连接上调用
  会静默丢掉命中计数（不影响正确性，只影响驱逐顺序）。
- **缓存住在语料库而不是 Redis**：这是刻意的 —— 失效必须与资源/集合的写在同一
  事务里提交，Redis 做不到；代价是每次 put 一次 DB 往返。要换 Redis 必须先解决
  「撤回提交成功但 Redis 失效失败」的窗口，不能直接替。
- **多进程**：PostgreSQL 上 `put` 取全局事务级 advisory lock 串行化上限判定
  （同一个锁键 `federation-cache`，跨 scope 也串行）。上限因此是精确的，代价是
  高并发写入会排队；如果以后成为瓶颈，正确做法是按 (scope, 分片) 维护计数，
  而不是把锁拿掉。
- **有界扫描**：单目标最多回看 64 行探测回执；极端情况下更旧但仍有效的回执会被
  漏掉（退化为重新探测，不是错误结果）。
- **policy_revision 目前只在回执记录时才强制**：P5 的 ProbeResult 没有该字段，
  等协议侧补上后此处自动生效，不需要改缓存。
- **未做**：Redis 缓存、跨进程指标（命中率/驱逐数）、缓存条目的审计事件。
  这些不属于 P6 本切片出口；PR32 的验收还应包含「内存磁盘上限」的运行期观测，
  本切片只证明了**表级**上限与失效正确性。

## 协调者接入说明

1. `from ddp_corpus import cache`（依赖表会随 import 注册；`main.py` 的现有
   import 链不经过 cache，接入后即注册）。
2. 复用探测：在发 Probe 之前调用
   `cache.find_reusable_probe(session, actor, target_key=…, query_digest=<同一个 canonical digest>,
   index_revision=<计划绑定的集合指纹>, policy_revision=…, now=now)`。
   命中时用返回的 `ProbeResult`；它的 `retrieval.evidence_set_ref` 指向
   `federation-probe:<probe_id>`，真实摘录仍在 `federation_probes.result_json["evidence"]`。
   `None` 只表示「不能复用」，不是失败。
3. 缓存投影：集合描述符用 `catalog.cache_revision` 绑键，探测类用
   `cache.probe_cache_key`；scope 用 `organization_scope` / `collection_scope` /
   `resource_scope` / `wiki_scope`，`kind` 自定（如 `"probe"` / `"catalog"`）。
4. 负面缓存：peer 不可达/被拒时 `record_negative(...)`；联系前 `get_negative(...)`。
   新注册或新上传必须推进 `node_revision`（键分量）。
5. 定期清理：reconcile 循环可调用 `purge_expired(session, now=now, limit=…)`；
   不调用也不会泄漏（`put`/`get` 已经在清）。
6. `catalog.cache_revision(row, index_revision)` 是本切片给协调者的集合有效性
   指纹：`sha256` + 现有 `catalog.digest([row.id, row.revision, row.publication,
   index_revision])`，`index_revision` 取自 `catalog.members_state`。
   别再写第二份指纹实现。

## 复跑

```bash
cd DeepDocParse && ./scripts/check.sh
cd services/corpus-api && ../../.venv/bin/python -m pytest -q tests/test_cache.py tests/test_wiki_cache_invalidation.py
# 真 PG（一次性容器，示例）：
docker run -d --name ddp-p6-cache-pg -e POSTGRES_PASSWORD=ddp -e POSTGRES_USER=ddp \
  -e POSTGRES_DB=deepdocparse -p 15460:5432 pgvector/pgvector:pg16
cd database/corpus && env DATABASE_URL=postgresql+asyncpg://ddp:ddp@127.0.0.1:15460/deepdocparse \
  PYTHONPATH=../../services/corpus-api ../../.venv/bin/alembic -c alembic.ini upgrade head
cd services/corpus-api && env CACHE_TEST_DATABASE_URL=postgresql+asyncpg://ddp:ddp@127.0.0.1:15460/deepdocparse \
  ../../.venv/bin/python -m pytest -q tests/test_cache_pg.py
```

没有 commit/push。本记录由本次作者自验，不替代独立 agent 的提交验收。
