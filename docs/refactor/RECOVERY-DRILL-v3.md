# 备份 / 恢复 / 节点身份演练（v3）

> 2026-09-13，本机真跑。入口：`scripts/backup_restore_drill.sh`（数据库/对象
> 存储部分调 `scripts/backup_restore_drill.py`）。**这是一次本机 scratch 演练**，
> 不是生产快照演练 —— §5 写清了它没有覆盖什么。

## 1. 结论

- 两套真实迁移链在空库上从零跑通：control `0001..0010` + corpus alembic
  `0001..0030` + `grants.sql`。
- 最小数据集（org/user + document/parse_job/resource/version +
  federation_request/coverage_ledger/entry + 两个对象）备份、恢复到新库后：
  **10 张表行数逐一相等，5 条跨表不变量零违反，版本摘要一致，对象存储
  两两摘要一致且无未对账对象**。
- Ed25519 节点身份：同一 32 字节 seed 恢复出同一 `node-` id / fingerprint；
  新克隆是另一个 authority，拿不出原 authority 的签名；缺 seed 且
  `allowCreate=false` 时拒绝"悄悄变成新节点"。
- 默认清理：容器、卷、工作目录全部删掉；`--keep` 保留现场。

## 2. 怎么跑的（实际命令）

```bash
cd /home/minatoaqukin/Projects/CSIE/DeepDocParse
timeout 900 bash scripts/backup_restore_drill.sh 2>&1 | tee /tmp/opencode/drill-run.log
# exit=0，末尾 DRILL PASS
```

脚本内部逐条执行的命令（端口特意避开 dev 的 15432 / 并行演练占用的 15450）：

```bash
# 1) scratch 容器（不挂任何 dev 卷）
docker run -d --name ddp-recovery-drill-pg \
  -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=drill-password -e POSTGRES_DB=deepdocparse \
  -p 127.0.0.1:15455:5432 -v ddp-recovery-drill-pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16
docker run -d --name ddp-recovery-drill-minio \
  -e MINIO_ROOT_USER=drill -e MINIO_ROOT_PASSWORD=drill-secret \
  -p 127.0.0.1:19055:9000 -v ddp-recovery-drill-miniodata:/data \
  quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z server /data

# 2) 两套真实迁移链
cd services/control-api && go build -o /tmp/.../control-migrate ./cmd/control-migrate
env CONTROL_DATABASE_URL=postgres://ddp:drill-password@127.0.0.1:15455/deepdocparse \
    CONTROL_DB_PASSWORD=drill-password CORPUS_DB_PASSWORD=drill-password \
    /tmp/.../control-migrate up
cd database/corpus
env DATABASE_URL=postgresql+asyncpg://ddp:drill-password@127.0.0.1:15455/deepdocparse \
    ALLOW_INSECURE_DEFAULTS=true ../../.venv/bin/python -m alembic upgrade head
docker exec -i ddp-recovery-drill-pg psql -U ddp -d deepdocparse --set ON_ERROR_STOP=1 \
  -f - < database/corpus/grants.sql

# 3) 种数据 + 写对象
.venv/bin/python scripts/backup_restore_drill.py seed \
  --dsn postgresql+asyncpg://ddp:drill-password@127.0.0.1:15455/deepdocparse \
  --state /tmp/.../drill-state.json --minio-endpoint 127.0.0.1:19055 \
  --minio-access-key drill --minio-secret-key drill-secret --bucket deepdocparse

# 4) 备份 → 新库 → 恢复
docker exec ddp-recovery-drill-pg pg_dump -U ddp -d deepdocparse -Fc > /tmp/.../source.dump
docker exec ddp-recovery-drill-pg createdb -U ddp deepdocparse_restored
docker exec -i ddp-recovery-drill-pg pg_restore -U ddp -d deepdocparse_restored --no-owner \
  < /tmp/.../source.dump

# 5) 对账
.venv/bin/python scripts/backup_restore_drill.py verify \
  --source-dsn postgresql+asyncpg://ddp:drill-password@127.0.0.1:15455/deepdocparse \
  --restored-dsn postgresql+asyncpg://ddp:drill-password@127.0.0.1:15455/deepdocparse_restored \
  --state /tmp/.../drill-state.json --minio-endpoint 127.0.0.1:19055 ...

# 6) 节点身份（Go 测试，DDP_IDENTITY_DRILL_ROOT 控制是否启用）
cd services/control-api
env DDP_IDENTITY_DRILL_ROOT=/tmp/.../identity go test ./internal/discovery \
  -run TestNodeIdentityBackupRestoreDrill -v -count=1
```

## 3. 实际观察

### 迁移

```
已应用 0001_control_schema ... 已应用 0010_scope_remote_expansion
INFO 已设置角色口令 role=ddp_control / role=ddp_corpus
Running upgrade 0001 -> ... -> 0029 -> 0030, Federation delivery bytes
control 迁移 + alembic head + grants 全部完成
```

（控制面迁移数在本轮并行工作里涨到 0010，corpus alembic head 到 0030 ——
演练就是按当下工作树跑的，没有固定在旧 head。）

### 种子与备份

```
seed OK: 组织 org-drill / 用户 user-drill / 资源 rrrr... / 任务 task-drill-1111...
（对象 uploads/org-drill/dddd....pdf 与 bundles/vvvv.../manifest.json）
备份 220K 已恢复到 deepdocparse_restored
```

### 对账（全部 OK，原文摘录）

```
  OK  control.organizations            source=   1 restored=   1
  OK  control.users                    source=   1 restored=   1
  OK  control.memberships              source=   1 restored=   1
  OK  public.documents                 source=   1 restored=   1
  OK  public.parse_jobs                source=   1 restored=   1
  OK  public.resources                 source=   1 restored=   1
  OK  public.resource_versions         source=   1 restored=   1
  OK  public.federation_requests       source=   1 restored=   1
  OK  public.coverage_ledgers          source=   1 restored=   1
  OK  public.coverage_entries          source=   1 restored=   1
  OK  不变量：federation_requests 的组织都有 control.organizations 行（违反行数 0）
  OK  不变量：coverage_entries 都有 coverage_ledgers（违反行数 0）
  OK  不变量：resource_versions 的资源都在（违反行数 0）
  OK  不变量：resource_versions 的文档都在（违反行数 0）
  OK  不变量：parse_jobs 的文档都在（违反行数 0）
  OK  固定版本摘要保持：sha256:6bce09e13d33c376ade4d4b66712de90ac87d3015db403e9ec39620e8f76bb2d
  OK  对象对账：uploads/org-drill/dddd....pdf
  OK  对象对账：bundles/vvvv.../manifest.json
  OK  桶内没有未对账对象（共 2 个）
recovery drill 对账 PASS：行数、不变量与对象存储三方一致
```

### 节点身份

```
authority node id=node-48fcc8010370ca0df5fc94b0bd1b98a0be88ef6994fadb4e
        fingerprint=sha256:48fcc8010370ca0df5fc94b0bd1b98a0be88ef6994fadb4eae821470c2c89b4e
restored same authority: node id=node-48fcc801... fingerprint=同上
clone node id=node-63c54d55eb8ec7ccd7193463b96e4bfe307b083fa0648081 differs;
missing seed refused
PASS
```

### 演练自己抓到的两件事

1. **第一次跑种数据就红了**：`coverage_entries` 的外键指向
   `coverage_ledgers`，而种子脚本没有在两组之间显式 `flush`，SQLAlchemy 的
   UoW 先插了子行（`ForeignKeyViolationError`）。**这正是"演练不能只跑
   迁移"的理由** —— 迁移链全绿，业务数据一行都写不进去。已在
   `scripts/backup_restore_drill.py` 里分组 flush 并注明原因。
2. **docker 不可用分支实测过**：`env DOCKER_HOST=unix:///nonexistent bash
   scripts/backup_restore_drill.sh` → `DRILL FAIL: docker 守护进程不可用 ——
   演练无法进行`，退出码 1。不是静默跳过。
## 6. PITR 演练（2026-10-05，T61 真跑）

> 入口：`bash scripts/recovery_drill.sh`（`--resume` 从已冻结的 base+WAL 快照重跑恢复段）。
> 产物：`docs/refactor/artifacts/recovery-pitr-20261005.json`。
> 这是 §5 缺口的第一次真跑：WAL 归档 + base 备份 + 双恢复（PITR-target + latest）+
> 元数据/证据/对象/身份四方对账 + 克隆冒用拒绝 + RTO/RPO 实测。

### 怎么跑的

```bash
cd /home/minatoaqukin/Projects/CSIE/DeepDocParse
bash scripts/recovery_drill.sh --n-docs=30 --n-extra=5   # 一键；末尾 DRILL PASS
# 中断后：bash scripts/recovery_drill.sh --resume --n-docs=30 --n-extra=5
```

一次性 disposable 栈（live A/B/C2/D 与 t28-app 不动；B 只读）：
SRC PG `127.0.0.1:15495` + SRC MinIO `:15511`（WAL 归档开），
TARGET PG `:15496`，LATEST PG `:15503`
（`:15497` 被另一 session 的 `ddp-subtree-audit-pg2` 占用，勿动），
CRASH PG `:15498`，DST MinIO `:15521`。
`archive_timeout=30s`，base 179M tar，归档 WAL 3 段（base 之前的段已清），
base LSN `0/1A000060`，PITR target `2026-10-05 06:27:34.271207+00`。

### 规模（live B 一致性拷贝 + 生成放大）

- B 本体：PG 415MB（documents 31 / parse_jobs 58 / chunks 28438 / evidence 37817 /
  citations 132 / wiki_deps 434），MinIO 10576 对象 / 2.1G，
  `pg_dump` 148M（只读，未重启 live 服务）。
- 放大：seed 30 篇 + pre-target 5 篇 + post-target 2 篇 → source 共
  documents 68 / parse_jobs 95 / chunks 28528 / evidence 37907 /
  federation_requests 9；对象 10687 个。

### 对账（每次恢复后全量，26 项；身份为联立检查 + 单元测试双轨）

- source（pre 之后）：26/26 PASS（含新增的 2 项身份绑定）。
- PITR-target：26/26 PASS + SELECTIVITY-TARGET PASS
 （5 个 pre-target task 在，2 个 post-target task 不在；6 个 post-target
  对象键已从 target 桶剔除：2 个 upload + 4 个 results）。
- latest：26/26 PASS + NOLOSS PASS（20 张关键表行数与 source 快照逐一相等；
  `control.memberships` 不在 NOLOSS 表里——它是 control 面成员关系表，
  恢复前后不被 drill 写操作触及，行数以 source-counts 备查，不进相等门）
  + SELECTIVITY-LATEST PASS（pre + postt 共 7 个 task 全在）。
- 对账项：14 外键不变量 + version↔document 摘要绑定 +
  evidence.content_digest==digest(content)（anchor.py 严格路径，**全表分页扫描，
  无 LIMIT 抽样**）+ live version→object + wiki/collection 绑定 + uploader +
  **身份绑定 2 项**（`wiki_dependencies` 中 drill authority `node-pitr` 的
  origin/authority 双向一致；clone 改写 authority id 即红）+
  对象存在性/摘要 + result 前缀/删除文档规则 + 桶外物。
- 计数口径：`doc_count`（如 54/56）是**有 object_key 的 live 文档**，
  小于 `public.documents` 总数 68——差值是 B 自带的已删除/无对象键行，
  不是丢失。
- 对象 PITR 是**排除法重建**（全量 mirror 后删 6 个 post-target 键），
  **仅因本工作负载 append-only（postt 只新增键、不覆盖）才成立**；
  漏删→extras 门红，多删→missing 门红，对账不包庇。
- `tmp-remote-compute/` 允许列外：live B 自带的行（`remote_computes` 产物），
  非 drill 写入，reconcile 按 allowlist 放行并计数。

### 克隆不能冒用权威节点（单元级 + 联立绑定）

- 联立：reconcile 新增 2 项身份绑定（上节）——恢复出的 DB 行里 drill authority
  `node-pitr` 双向一致，clone 改写即红。这是恢复流水线内的门，不是旁路测试。
- 单元级（credential/lease 逻辑，无 live peer 握手集成）：
  `TestNodeIdentityBackupRestoreDrill` PASS：同 seed 恢复出同一 authority，
  clone 是另一个 id，缺 seed 且 `allowCreate=false` 时拒绝。
- `TestCredentialSigningRefusesForeignIssuerAndInvalidClaims` +
  `TestNodeIDForPublicKeyValidatesCanonicalEncoding` PASS：
  clone 的 key 签不出 authority 的 credential，
  lease.go authority-id/key 不一致拒绝。

### RTO / RPO（实测，disposable 硬件；全部出自 timing.json marks）

> 本轮为一次完整 `bash scripts/recovery_drill.sh` 真跑（不再 `--resume` 拼凑），
> 下表每个数字都是 `timing.json` 起止 mark 的差值，artifact `rto_seconds` 原样引用。

| 步骤 | 实测（timing.json，20261005 单轮真跑） |
|---|---|
| B 一致性拷贝（148M dump + 10576 对象/2.1G） | 17.4 s `copy_b` |
| 建源（B 恢复 + 对象拷贝 + 30 篇 seed） | 30.9 s `build_source` |
| base 备份（179M tar） | 281.5 s `base_backup` |
| pre/post 写 + 两次归档等待 | 89.7 s |
| 恢复到 target（含 promote + 对账） | 17.5 s |
| 恢复到 latest（含对账 + noloss） | 7.7 s |
| RPO crash probe（真写 + kill -9 + 冻结 WAL 恢复） | 8.5 s |
| 身份演练 | 0.6 s |

- RPO：WAL 节拍 30 s 即丢失窗口上界 30 s；crash probe 实证（真写一行、
  kill -9、只用 kill 前已归档 WAL 恢复——该行不在恢复里，
  `rpo-proof.json`: `lost=true`）。`--resume` 不做 RPO  verdict（SRC 已死，无真杀可做）。
- target/latest 恢复本身零丢失（selectivity + noloss 双门）。
- 提案（判据外，待 human 定）：RTO ≤ 30 min / RPO ≤ 30 s，
  见 `.dev-logs/human-steps/RTO-RPO.md`。

### 演练抓到的真 bug（脚本侧，已修）

1. `archive_command` 里 `&&` 被多转义成 `\&\&` → archiver 持续 exit 2，
   28 段 WAL 一段没归档；改用 `ALTER SYSTEM SET ... TO '... && ...'` 原文。
2. WAL 开关 `ALTER SYSTEM` 后没 restart → `archive_mode=off` 跑完全程；
   补 restart + ready 门。
3. PITR 恢复 `printf` 构造 `postgresql.conf` 把 `%f %p` 当格式符吃掉 →
   改走文件挂载 + `printf %s`。
4. 恢复出的库仍是 SRC 的 `ddp` 口令 → promote 后 `ALTER ROLE ddp PASSWORD`
   （单引号转义），latest/crash 同理。
5. target 对象集漏了 post-target 的 `results/<jid>/` 4 个键 →
   从 doc id 反推 jid 一并剔除。
6. `--resume` 重跑把 timing.json 清零 → 保留已测 marks。

## 4. 复跑

```bash
cd /home/minatoaqukin/Projects/CSIE/DeepDocParse
bash scripts/backup_restore_drill.sh            # 默认清理
bash scripts/backup_restore_drill.sh --keep     # 保留容器/卷/工作目录
# 端口冲突时：DRILL_PG_PORT=15456 DRILL_MINIO_PORT=19056 ...
```

## 5. 明确不覆盖（§1–§4 旧演练的边界；§6 已部分取代，见标注）

- ~~**生产卷与生产量级**：这里每个种子表 1 行、备份 220K~~ → **§6 已取代**：
  live B 一致性拷贝（PG 415MB / MinIO 2.1G / 10576 对象）+ 生成放大到
  documents 68 / evidence 37907 / 对象 10687；但仍非生产硬件，
  HNSW 向量索引恢复性能仍没量（保留）。
- ~~**PITR / WAL 归档 / 时间点恢复**：只有逻辑备份~~ → **§6 已取代**：
  WAL 归档（`archive_timeout=30s`，3 段，failed=0）+ base 179M +
  PITR-target 与 latest 双恢复 + selectivity/noloss 双门。
  未验：增量备份、WAL 流复制（streaming replication）。
- **跨区域 / 异地备份**：仍然没验（保留）——没有对象存储跨区复制，也没有跨机恢复。
- ~~**对象存储的数据量**：MinIO 里只有 2 个种子对象~~ → **§6 已取代**：
  10687 对象逐一存在性 + 摘要对账 + 桶外物门。
  未验：桶级版本控制/生命周期策略（保留）。
- **secret 轮换**：仍然没验（保留）——恢复出的是旧凭据的快照。
- ~~**RTO/RPO**：没有计时目标~~ → **§6 已实测，目标待定**：
  恢复段 target 17.5 s / latest 7.7 s，RPO 窗 30 s（crash probe 实证），
  提案 RTO ≤ 30 min / RPO ≤ 30 s 待 human 一字回复
  （`.dev-logs/human-steps/RTO-RPO.md`；判据外）。
- **迁移的 downgrade**：仍然只跑 `upgrade head`（保留）；双向可跑由
  `.github/workflows/python.yml` 的 `upgrade → downgrade base → upgrade` 覆盖。
