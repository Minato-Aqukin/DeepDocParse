# 一次性数据迁移器

旧库 → `control` / `corpus` 双 schema。**它是交付工具，不是运行路径** ——
切换完成后从运行镜像删除（§11.4）。

## 用法

```bash
# 0. 先把两套 schema 建好（顺序无关，没有跨 schema 外键）
control-migrate -database "$DSN" up
cd database/corpus && alembic upgrade head

# 1. dry-run：只做源侧预检。**必须通过才允许进入切换窗口**
python database/migrator/migrate.py --source "$OLD" --target "$NEW"

# 2. 真跑，带对象存储对账
python database/migrator/migrate.py --source "$OLD" --target "$NEW" --apply \
    --object-endpoint 127.0.0.1:19000 --object-bucket deepdocparse \
    --report out/migration-report.json
```

原地升级时 `--source` 与 `--target` 可以是**同一个连接串**：语料表本来就在
那个库里，迁移器只是把账号层搬进 `control` schema 并回填 `organization_id`。

## 它做什么

| 步骤 | 旧 → 新 |
|---|---|
| 组织 | 建（或复用）默认组织 —— 首发是单组织独占部署 |
| 用户 | `users` → `control.users` + `control.memberships`（`is_admin` → admin 角色） |
| API key | `api_keys` → `control.api_keys`（**存量 key 给全部作用域**） |
| 计量 | `usage_records` → `control.usage_ledger`（`event_id` = 旧主键，幂等） |
| 文件凭证 | `file_tokens` → `control.file_grants`（**token 原样保留**） |
| 组织回填 | 语料表的 `organization_id` 从 `''` 改写成真实组织 |

## 它**不**做什么

- **不删旧表。** 搬完并对账通过之后另起一个迁移删除 —— 在同一个工具里
  又搬又删，失败时既没法回滚也说不清搬到哪了。
- **不搬对象。** 对象键新旧一致、桶也没换，所以只**核对存在性**。
  真要换桶那是另一件事，应当由对象存储自己的复制机制做。

## 三个不能改的地方

1. **token 原样保留。** `/files/{token}` 是模型网关下载原件的稳定 URL，
   而文档身份 `doc_hash` 在没有 `doc_id` 时会回退成 `sha256(file_url)` ——
   换一个 token 等于换一个文档身份，历史解析缓存与向量索引**全部失效**
   （ADR #11/#12，这个项目踩过两次）。
2. **`event_id` 填旧主键。** 那是计量的幂等键；换成新生成的 id，重跑就会重复记账。
3. **存量 key 给全部作用域。** 旧系统没有 scope 概念，等价形态是"全部平面"；
   给空数组等于把所有存量 key 静默作废（空 = 默认拒绝）。

## 幂等

每一步都是 `INSERT ... ON CONFLICT DO NOTHING`，键取旧库主键 —— 重跑时
第二遍全部落空。**没有一处用自增或随机 id 建新行**，那是重跑产生重复的唯一来源。
第二轮演练实测：重跑写 0、跳过全部、对账仍然全 PASS。

## 演练记录

见 `docs/refactor/MIGRATION-DRILLS.md`（三轮：空库 / 生产快照 / 对抗数据集，
共抓到 3 个真缺陷）。
