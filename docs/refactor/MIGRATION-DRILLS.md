# 迁移演练记录

> 依据：`MERGE-REFACTOR-PROPOSAL.md` §11.4 ——「至少做三次全量演练：
> 空库、生产快照、对抗数据集」。
> 工具：`database/migrator/migrate.py`（一次性交付工具，切换完成后从运行镜像删除）。

## 演练环境

| 项 | 值 |
|---|---|
| 日期 | 2026-09-02 |
| PostgreSQL | pgvector/pgvector:pg16（本机容器，端口 15432） |
| 源数据 | 本机 dev 库 `deepdocparse`，alembic head `0012`，8 用户 / 9 key / 40 计量 / 8 文件凭证 / 6 文档 |
| 对象存储 | 第四轮已接入（9 个真实对象，抽样 PASS，变异确认过）|

三轮演练都是：`pg_dump | psql` 复制一份 → 跑两套迁移 → 跑迁移器 → 看对账。

---

## 第一轮：空库

```bash
createdb ddp_drill_empty && psql -c 'CREATE EXTENSION vector'
control-migrate -database ... up          # 2 个迁移
alembic upgrade head                      # 13 个迁移
python database/migrator/migrate.py --source ... --target ... --apply
```

**结果**：12 项对账全 PASS。

**抓到 2 个真缺陷**：

1. `0001_control_schema.sql` 里又建了一次 `control.schema_migrations`，
   而迁移器的 bootstrap 已经建过 —— 全新库上 `control-migrate up` 直接
   `relation "schema_migrations" already exists`。**这是"从零部署"路径独有的
   缺陷**：任何已有库都不会撞到它。已把建表从 0001 里删掉（账本归 bootstrap）。
2. 「组织有管理员」这条对账在空库上必然红 —— 全新部署本来就没有用户，
   第一个注册的人才成为 admin。**必然红的检查会训练人忽略红色**，
   所以加了空库例外并写清理由。

---

## 第二轮：生产快照

用本机 dev 库的完整拷贝（它是真实使用过的库：有解析过的文档、真实的出处、
跨 M5–M9 各阶段留下的数据）。

**结果**：12 项对账全 PASS。

```
  organizations            读 0    写 1
  users                    读 8    写 8     其中 1 人是管理员
  api_keys                 读 9    写 9
  usage_ledger             读 40   写 40
  file_grants              读 8    写 8
  organization_id 回填      读 6    写 6
```

**抓到 1 个真缺陷**：`0013` 的 revision id 写成了 `"0013_persistent_tasks"`，
而既有迁移用的是纯数字（`"0012"`）—— alembic 直接 `KeyError: '0012_knowledge_layer'`。
混用两种命名的表现是**迁移根本跑不起来**，而本机单测走 `create_all` 不碰 alembic，
所以只有真跑迁移才会发现。

### 幂等性（§11.4 的硬要求）

同一条命令再跑一遍：

```
  users        读 8   写 0   跳过 8
  api_keys     读 9   写 0   跳过 9
  usage_ledger 读 40  写 0   跳过 40
  file_grants  读 8   写 0   跳过 8
```

**写 0、跳过全部、对账仍然全 PASS。** 没有重复计量、重复文档、重复出处。

---

## 第三轮：对抗数据集

在生产快照的副本上注入坏数据，验迁移器**拒绝**而不是静默搬坏。

| 注入 | 结果 |
|---|---|
| `password_hash = ''` 的用户 | ✅ 预检 FAIL，退出码 1 —— 搬过去会违反新库的 `users_has_credential` CHECK |
| 指向不存在文档的 file_token | ⛔ **注入不进去**：旧库有 FK 挡着 |
| 悬空 citation（evidence 不存在） | ⛔ **注入不进去**：旧库有 FK 挡着 |

后两条注入失败**不是演练失败**，是一个有价值的发现：**旧库的外键已经排除了
这两类损坏**，所以迁移器里对应的两条预检是防御性的（只在拿到损坏的 dump 时才会触发）。
留着它们的理由是：`pg_dump --disable-triggers` 恢复出来的库、或者手工改过的库，
确实可能有这类孤儿。

退出码验过：坏数据 `rc=1`、干净数据 `rc=0`。

---

## dry-run 与 apply 的分工

第一版的 dry-run 会跑完整对账 —— 于是**必然报 4 条行数 FAIL**（一行都没写，
当然对不上）。已改成：

- **dry-run**：只做**源侧预检**（数据本身有没有问题）。它必须能通过，
  否则不该进入切换窗口。
- **`--apply`**：跑完整的 12 项对账。

理由与上面空库那条一样：必然失败的检查会训练人忽略红色，而这套流程
最不能出的就是这件事。

---

## 还欠什么（切换窗口前必须补齐）

1. ~~**对象存储对账没跑过。**~~ **已跑通**（2026-09-02，第四轮）：
   真实用户路径在 MinIO 里留下 9 个对象之后，带 `--object-endpoint`
   跑 dry-run，`对象存在性抽样（9 个）` PASS。
   **并做了变异确认**：把 `--object-bucket` 改成一个不存在的桶，
   当场红并列出缺失的对象键 —— 也就是说"桶名配错"这一类它真的拦得住。

   顺带改了一处**位置**：对象抽样原本只在 `--apply` 里跑，
   而它是只读的。放在窗口里意味着"你会在维护窗口开着的时候
   才发现对象根本不在那个桶里"。现在它属于预检，dry-run 就跑。
   判据：**只读的检查都该在进窗口之前跑完**，窗口里只留非写不可的。
2. **content digest 与 bbox 抽样**（§11.4 要求的对账项）目前只有对象存在性。
   digest 需要读对象内容，属于上一条的延伸；bbox 抽样需要把
   `citations.bbox` 与当前 `chunks` 对一遍。
3. **真实生产快照。** 本机 dev 库只有 6 份文档、40 条计量流水 ——
   量级与真实部署差得远。行数对账在小样本上通过，不代表在几万行上
   不会撞到超时或内存问题。
4. **旧账号表的删除。** 迁移器**不删**旧的 `users` / `api_keys` /
   `usage_records` / `file_tokens` —— 在同一个工具里又搬又删，
   失败时既没法回滚也说不清搬到哪了。

   删除工具已经写好：`database/migrator/drop_legacy_account_tables.py`。
   **它不是 alembic 迁移**（原方案说的"另起一个 alembic 迁移"是错的）——
   进了迁移链，任何一次 `alembic upgrade head` 都会执行它，
   包括在对账通过之前、包括在一台刚从生产快照恢复出来的库上。
   而这是本仓库唯一一处不可逆丢数据的地方。

   它默认**只检查不动手**，逐张比对旧表与新表的行数，任何一条不满足就拒绝，
   **且不提供 `--force`**。已做变异确认：把旧 `users` 灌到 20 行
   （新表 15 行），`--apply` 也拒绝，四张表一张不少。
   三条性质（不在迁移链里 / DROP 挡在 `--apply` 后 / 没有 `--force`）
   有静态守卫钉着，也做过变异确认。

   **仍然欠的是真正执行它** —— 那要等生产迁移对账通过之后。
