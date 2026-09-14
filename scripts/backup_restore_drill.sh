#!/usr/bin/env bash
# 备份 / 恢复 / 节点身份演练 —— 在本机起**一次性**容器，不碰任何 dev 卷。
#
#   scripts/backup_restore_drill.sh            # 跑完即清理
#   scripts/backup_restore_drill.sh --keep     # 保留容器与工作目录，便于现场排查
#
# 它做什么：
#   1. 起 scratch Postgres（127.0.0.1:15455）与 scratch MinIO（127.0.0.1:19055）
#      —— 端口避开 dev 的 15432/19000，也避开并行 P6 演练用的 15450；
#   2. 跑**两套真实迁移链**：Go `control-migrate up` + alembic upgrade head
#      （外加 grants.sql，只跑 alembic 会得到"迁移成功但服务没权限"）；
#   3. 灌最小数据集（org/user + document/parse_job/resource/version +
#      federation_request/coverage_ledger/entry + 两个对象）；
#   4. `pg_dump` 备份 → 新建恢复库 → `pg_restore` → 行数/不变量/对象存储对账；
#   5. Ed25519 节点身份演练：备份 seed 文件恢复出同一个 authority，
#      新克隆拿不出原 authority 的签名（`identity_drill_test.go`）。
#
# **不覆盖**（写进 docs/refactor/RECOVERY-DRILL-v3.md）：生产卷、PITR、
# 跨区域、真对象存储的数据量。这里量的是"这套备份/恢复流程能不能真的跑通"。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
PG_PORT="${DRILL_PG_PORT:-15455}"
MINIO_PORT="${DRILL_MINIO_PORT:-19055}"
PG_CONTAINER="${DRILL_PG_CONTAINER:-ddp-recovery-drill-pg}"
MINIO_CONTAINER="${DRILL_MINIO_CONTAINER:-ddp-recovery-drill-minio}"
PG_VOLUME="${DRILL_PG_VOLUME:-ddp-recovery-drill-pgdata}"
MINIO_VOLUME="${DRILL_MINIO_VOLUME:-ddp-recovery-drill-miniodata}"
PG_IMAGE="${DRILL_PG_IMAGE:-pgvector/pgvector:pg16}"
MINIO_IMAGE="${DRILL_MINIO_IMAGE:-quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e}"
PG_PASSWORD="drill-password"
BUCKET="deepdocparse"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

WORK="$(mktemp -d /tmp/ddp-recovery-drill.XXXXXX)"
STATE="$WORK/drill-state.json"
DUMP="$WORK/source.dump"

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
note() { printf '\033[2m    %s\033[0m\n' "$*"; }
fail() { printf '\033[31mDRILL FAIL: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  local code=$?
  trap - EXIT
  if [ "$KEEP" -eq 1 ]; then
    printf '\n\033[33m--keep：容器 %s / %s 与工作目录 %s 保留\033[0m\n' \
      "$PG_CONTAINER" "$MINIO_CONTAINER" "$WORK"
    return
  fi
  docker rm -f "$PG_CONTAINER" "$MINIO_CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$PG_VOLUME" "$MINIO_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || fail "这台机器没有 docker —— 演练无法进行"
docker info >/dev/null 2>&1 || fail "docker 守护进程不可用 —— 演练无法进行"

# 端口占用早失败：起不来时的报错会指向 compose 而不是本脚本。
for port in "$PG_PORT" "$MINIO_PORT"; do
  if (exec 3<>/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
    exec 3>&- 3<&-
    fail "127.0.0.1:$port 已被占用 —— 换 DRILL_PG_PORT / DRILL_MINIO_PORT"
  fi
done

# ------------------------------------------------------------------ 容器
say "1/6 起 scratch Postgres（:$PG_PORT）与 scratch MinIO（:$MINIO_PORT）"
docker rm -f "$PG_CONTAINER" "$MINIO_CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$PG_VOLUME" "$MINIO_VOLUME" >/dev/null 2>&1 || true
docker run -d --name "$PG_CONTAINER" \
  -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=deepdocparse \
  -p "127.0.0.1:$PG_PORT:5432" -v "$PG_VOLUME:/var/lib/postgresql/data" "$PG_IMAGE" >/dev/null
docker run -d --name "$MINIO_CONTAINER" \
  -e MINIO_ROOT_USER=drill -e MINIO_ROOT_PASSWORD=drill-secret \
  -p "127.0.0.1:$MINIO_PORT:9000" -v "$MINIO_VOLUME:/data" \
  "$MINIO_IMAGE" server /data >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$PG_CONTAINER" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || fail "scratch Postgres 60 秒内没有 ready"
for _ in $(seq 1 60); do
  if docker exec "$MINIO_CONTAINER" mc ready local >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$MINIO_CONTAINER" mc ready local >/dev/null 2>&1 \
  || fail "scratch MinIO 60 秒内没有 ready"
note "PG_DSN=postgres://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse"
note "MINIO_ENDPOINT=127.0.0.1:$MINIO_PORT"

# ------------------------------------------------------------------ 迁移
say "2/6 跑两套真实迁移链"
CONTROL_BIN="$WORK/control-migrate"
( cd services/control-api && \
  PATH="$HOME/.local/opt/go/bin:$PATH" go build -o "$CONTROL_BIN" ./cmd/control-migrate ) \
  || fail "control-migrate 构建失败"
env CONTROL_DATABASE_URL="postgres://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse" \
    CONTROL_DB_PASSWORD="$PG_PASSWORD" CORPUS_DB_PASSWORD="$PG_PASSWORD" \
    "$CONTROL_BIN" up || fail "control 迁移链失败"
( cd database/corpus && \
  env DATABASE_URL="postgresql+asyncpg://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse" \
      ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic upgrade head ) \
  || fail "corpus 迁移链失败"
docker exec -i "$PG_CONTAINER" psql -U ddp -d deepdocparse --set ON_ERROR_STOP=1 \
  -f - < database/corpus/grants.sql >/dev/null || fail "corpus grants.sql 失败"
note "control 迁移 + alembic head + grants 全部完成"

# ------------------------------------------------------------------ 种数据
say "3/6 灌最小数据集（并写对象）"
"$PY" scripts/backup_restore_drill.py seed \
  --dsn "postgresql+asyncpg://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse" \
  --state "$STATE" \
  --minio-endpoint "127.0.0.1:$MINIO_PORT" --minio-access-key drill \
  --minio-secret-key drill-secret --bucket "$BUCKET" || fail "种子数据写入失败"

# ------------------------------------------------------------------ 备份/恢复
say "4/6 pg_dump → 新库 → pg_restore"
docker exec "$PG_CONTAINER" pg_dump -U ddp -d deepdocparse -Fc > "$DUMP" \
  || fail "pg_dump 失败"
[ -s "$DUMP" ] || fail "pg_dump 输出为空"
docker exec "$PG_CONTAINER" createdb -U ddp deepdocparse_restored \
  || fail "创建恢复库失败"
docker exec -i "$PG_CONTAINER" pg_restore -U ddp -d deepdocparse_restored --no-owner \
  < "$DUMP" || fail "pg_restore 失败"
note "备份 $(du -h "$DUMP" | cut -f1) 已恢复到 deepdocparse_restored"

# ------------------------------------------------------------------ 对账
say "5/6 行数 / 不变量 / 对象存储对账"
"$PY" scripts/backup_restore_drill.py verify \
  --source-dsn "postgresql+asyncpg://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse" \
  --restored-dsn "postgresql+asyncpg://ddp:${PG_PASSWORD}@127.0.0.1:$PG_PORT/deepdocparse_restored" \
  --state "$STATE" \
  --minio-endpoint "127.0.0.1:$MINIO_PORT" --minio-access-key drill \
  --minio-secret-key drill-secret --bucket "$BUCKET" || fail "恢复对账失败"

# ------------------------------------------------------------------ 节点身份
say "6/6 Ed25519 节点身份：备份、恢复、克隆不能冒充"
IDENTITY_ROOT="$WORK/identity"
mkdir -p "$IDENTITY_ROOT"
( cd services/control-api && \
  env DDP_IDENTITY_DRILL_ROOT="$IDENTITY_ROOT" \
      PATH="$HOME/.local/opt/go/bin:$PATH" \
      go test ./internal/discovery -run TestNodeIdentityBackupRestoreDrill -v -count=1 ) \
  | tee "$WORK/identity-drill.log" || fail "节点身份演练失败"
grep -q "restored same authority" "$WORK/identity-drill.log" \
  || fail "身份演练没有验证「同一 seed 恢复同一身份」"
grep -q "clone node id=.* differs" "$WORK/identity-drill.log" \
  || fail "身份演练没有证明克隆身份不同"
note "节点身份演练日志：$WORK/identity-drill.log"

say "DRILL PASS"
note "恢复库、对象与身份都通过；--keep 可保留现场（默认已清理：$( [ "$KEEP" -eq 0 ] && echo 是 || echo 否 )）"
exit 0
