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
  minio/minio:RELEASE.2025-04-22T22-12-26Z server /data

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

## 4. 复跑

```bash
cd /home/minatoaqukin/Projects/CSIE/DeepDocParse
bash scripts/backup_restore_drill.sh            # 默认清理
bash scripts/backup_restore_drill.sh --keep     # 保留容器/卷/工作目录
# 端口冲突时：DRILL_PG_PORT=15456 DRILL_MINIO_PORT=19056 ...
```

## 5. 明确不覆盖（不要读成"备份恢复已经验完"）

- **生产卷与生产量级**：这里每个种子表 1 行、备份 220K。索引重建时间、
  HNSW 向量索引的恢复性能、大表 `pg_restore` 时长都没量。
- **PITR / WAL 归档 / 时间点恢复**：只有逻辑备份（`pg_dump -Fc`），没有
  物理备份、增量备份或 WAL 恢复演练。
- **跨区域 / 异地备份**：没有对象存储跨区复制，也没有跨机恢复。
- **对象存储的数据量**：MinIO 里只有 2 个种子对象；没有真实文档字节的
  备份/恢复对账，也没有桶级版本控制/生命周期策略。
- **secret 轮换**：恢复后的部署要不要换 SERVICE_TOKEN/JWT_SECRET/MinIO
  凭据没有演练（恢复出的是旧凭据的快照）。
- **RTO/RPO**：没有计时目标，§18 第 7 条仍是用户待定项。
- **迁移的 downgrade**：本次只跑 `upgrade head`；双向可跑由
  `.github/workflows/python.yml` 的 `upgrade → downgrade base → upgrade` 覆盖。
