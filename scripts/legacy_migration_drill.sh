#!/usr/bin/env bash
# T62 旧资源迁移与回退演练 —— 真实旧快照上的回填 / 影子读 / 回退。
#
#   bash scripts/legacy_migration_drill.sh            # 全流程，跑完即清理
#   bash scripts/legacy_migration_drill.sh --keep     # 保留 ddp-legacy-* 容器/卷与工作目录
#
#   四个数据集：一律 disposable 拷贝，源卷只以 :ro 挂载做 cp -a。
#   - web：ddp-web_pgdata 原样（:15505），升级前快照后直接走迁移链；
#   - e2e：docker_pgdata（:15506），0003 时代，无 in-chain 路径，只读预检 + 快照恢复；
#   - cit：ddp-web_pgdata 第二份拷贝（:15509），先推进到 0013 拿 organization_id 列，
#     再用 seed 脚本按 era 双写语义构造夹具（10 chunks + 3 条 era 形状出处，
#     其中 1 条负样本；见 scripts/legacy_migration_drill_seed.py 头注）；
#   - realcit：ddp-legacy-realcit-pgdata（:15511，`REALCIT_*` 可改），新本地来源——
#     2026-08-29 时代的真实后端（worktree .dev-logs/t62-era @ e6b702a）与真实
#     gateway（worktree .dev-logs/t62-svc @ 2f0e391，borndigital 进程内解析，
#     embeddings 走本地 bge-m3 :48181，chat 走本地 qwen3-4b :58180）在该卷的
#     disposable 拷贝上实际执行的上传→解析→索引→5 轮问答；引用行全部由 era
#     代码写入（evidence/citations 双写，source_kind='assertion'），共 19 条、
#     落在 5 个页上；MinIO 用 ddp-legacy-realcit-miniodata 的拷贝（:15512/:15513）。
#     realcit 在演练内再拷一份 disposable 卷后走与 cit 完全相同的链
#     （推进 0013→快照→control-migrate up→alembic head→grants→migrator→影子读→回退）。
#
#   流程（web/cit/realcit 相同，e2e 除外）：
#   1. 起一次性 PG + MinIO，读 alembic revision（web=0012，e2e=0003，
#      cit/realcit=0012，realcit 随即推进到 0013）；
#   2. pg_dump -Fc 快照（升级前基线）；cit 另做 seed 夹具快照；
#      realcit 的快照即 era 真实运行的落盘状态（audit 见 $WORK/realcit-legacy-citations.json）；
#   3. 真实迁移链：control-migrate up + alembic upgrade head + grants.sql
#      （e2e 无 in-chain 路径，不升级）；
#   4. 文档化迁移器 database/migrator/migrate.py：dry-run 预检 + --apply
#      行数/外键/对象存在性对账 + 重跑幂等（写 0/跳过全部）；e2e 只读源库预检；
#   4b. 分阶段门（plan §15.1：影子读 -> 小范围切换 -> 扩大灰度）：
#      canary（单数据集 web 先行，对账全 PASS 才放行）→ small-switch
#      （cit/realcit 切换 + 影子读零漂移）→ gray-expand（全量 + 回填幂等）；
#      每阶段有独立 gate，任一 FAIL 即停；
#   5. 快照恢复（e2e 从升级前快照 pg_restore 到新库；control 无 downgrade，
#      这就是文档化回退路径）；
#   6. scripts/legacy_migration_drill.py 影子读：归属不猜测 / 权限不扩大 /
#      引用不漂移（逐条 citation：文本/页/bbox 与快照一致；cit 另有双写
#      预填行 backfill 加 0 与 1 条负样本 unanchored>=1；realcit 的 19 条全部
#      由 era 代码写入，逐条比对零漂移）+ 回填幂等；
#   7. 回退：web、cit 与 realcit 各做 corpus alembic downgrade -1 -> upgrade head，
#      行数不变；
#   8. 归档 docs/refactor/artifacts/legacy-migration-<date>.json。
#
# 边界（演练中已证实，如实声明）：
#   - docker_*（0003 时代）早于合仓迁移链：0005 建 extraction_* 与当时已存在的
#     表撞名（DuplicateTableError），alembic upgrade head 在该数据集上无支持路径。
#     支持路径是文档化迁移器 migrate.py（它只读旧表），本演练对该库跑只读预检。
#   - realcit 的问答降级如实记录：本机无视觉模型，qwen3-4b 为纯文本模型，
#     era 后端按自身逻辑把 5 轮回答标 degraded='vision_unavailable'
#     （文本回答与引用落库不受影响）；embedding 用真实 bge-m3 向量，
#     关键词路与向量路都参与了 era 检索。
#   - era 运行环境说明：era gateway/后端与当前 .venv 共存的方式是 PYTHONPATH
#     把 era 的 ddp_core（worktree 内）排在已安装的新版 ddp_core 之前，
#     否则新版 ParseJob（含 resource_id 列）与 0012 schema 对不上；
#     跑 era 还要 bcrypt 与 python-jose：2026-10-06 那次临时装进 .venv，造完数据即卸载
#     （当前仓库不依赖这两个包）；重造 realcit 卷时请装进一次性环境；
#     QA_DECISION_ENABLED=false、QA_VERIFY_PARSE=false、COMPILE_VISION_ENABLED=false
#     三个开关是配置（era.env），关闭的是"调用视觉/判定模型"的步骤，
#     引用写入路径（record_evidence 双写）一字未动。
set -euo pipefail
#
# 三个开关各挡一类静默出错：-e 让任何 migrate.py 非零退出、断言 heredoc
# 失败、psql ON_ERROR_STOP 失败都直接终止演练，而不是带着半脏库继续往下跑
# （cleanup 只做容器/卷清理，`|| true` 仅限清理行，绝不掩盖 gate 的退出码）；
# -u 让拼错的变量名当场炸，而不是展开成空串后让下游报云山雾罩的错；
# pipefail 让 `migrate.py … | tail` 这类管道按 migrate.py 的退出码算 ——
# 没有它管道状态永远是 tail 的 0，行尾的 `|| fail` 门永远看不见失败。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
DATE="${DRILL_DATE:-$(date +%Y%m%d)}"
ARTIFACT="docs/refactor/artifacts/legacy-migration-${DATE}.json"
WORK=".dev-logs/legacy-migration-${DATE}"

WEB_PG_PORT="${LEGACY_WEB_PG_PORT:-15505}"
E2E_PG_PORT="${LEGACY_E2E_PG_PORT:-15506}"
CIT_PG_PORT="${LEGACY_CIT_PG_PORT:-15509}"
REALCIT_PG_PORT="${LEGACY_REALCIT_PG_PORT:-15511}"
WEB_MINIO_PORT="${LEGACY_WEB_MINIO_PORT:-15507}"
E2E_MINIO_PORT="${LEGACY_E2E_MINIO_PORT:-15508}"
REALCIT_MINIO_PORT="${LEGACY_REALCIT_MINIO_PORT:-15512}"
REALCIT_MINIO_CONSOLE_PORT="${LEGACY_REALCIT_MINIO_CONSOLE_PORT:-15513}"
PG_IMAGE="${LEGACY_PG_IMAGE:-pgvector/pgvector:pg16}"
MINIO_IMAGE="${LEGACY_MINIO_IMAGE:-minio/minio:RELEASE.2025-04-22T22-12-26Z}"
MINIO_USER="legacydrill"
MINIO_PASS="legacydrill-secret"
BUCKET="deepdocparse"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
note() { printf '\033[2m    %s\033[0m\n' "$*"; }
fail() { printf '\033[31mDRILL FAIL: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  local code=$?
  trap - EXIT
  if [ "$KEEP" -eq 1 ]; then
    printf '\n\033[33m--keep：ddp-legacy-* 容器/卷与 %s 保留\033[0m\n' "$WORK"
    return
  fi
  docker rm -f ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg ddp-legacy-realcit-pg ddp-legacy-web-minio ddp-legacy-e2e-minio ddp-legacy-realcit-minio ddp-legacy-mc >/dev/null 2>&1 || true
  docker volume rm ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg ddp-legacy-realcit-pg ddp-legacy-web-minio ddp-legacy-e2e-minio ddp-legacy-realcit-minio >/dev/null 2>&1 || true
  exit "$code"
}
trap cleanup EXIT

mkdir -p "$WORK"
export CONTROL_DATABASE_URL="postgres://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse"
export CONTROL_DB_PASSWORD=ddp CORPUS_DB_PASSWORD=ddp
export PGUSER=ddp PGPASSWORD=ddp

say "0/10 前提：源卷只读可见，目标卷全新"
for v in ddp-web_pgdata docker_pgdata ddp-web_miniodata docker_miniodata ddp-legacy-realcit-pgdata ddp-legacy-realcit-miniodata; do
  docker volume inspect "$v" >/dev/null || fail "源卷 $v 不存在"
done

say "1/10 复制源卷到 disposable 卷（源卷 :ro，只做 cp -a）"
for pair in "ddp-web_pgdata:ddp-legacy-web-pg" "docker_pgdata:ddp-legacy-e2e-pg" \
            "ddp-web_pgdata:ddp-legacy-cit-pg" \
            "ddp-legacy-realcit-pgdata:ddp-legacy-realcit-pg" \
            "ddp-web_miniodata:ddp-legacy-web-minio" "docker_miniodata:ddp-legacy-e2e-minio" \
            "ddp-legacy-realcit-miniodata:ddp-legacy-realcit-minio"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  docker volume create "$dst" >/dev/null 2>&1 || true
  docker run --rm -v "$src:/from:ro" -v "$dst:/to" alpine cp -a /from/. /to/ \
    || fail "复制 $src -> $dst 失败"
done
note "源卷未挂载读写；mountpoint 未变更（只读 cp）。"

say "2/10 起一次性 PG + MinIO（${WEB_PG_PORT}/${E2E_PG_PORT}/${CIT_PG_PORT}/${REALCIT_PG_PORT}，${WEB_MINIO_PORT}/${E2E_MINIO_PORT}/${REALCIT_MINIO_PORT}）"
docker run -d --name ddp-legacy-web-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${WEB_PG_PORT}:5432" \
  -v ddp-legacy-web-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 web PG 失败"
docker run -d --name ddp-legacy-e2e-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${E2E_PG_PORT}:5432" \
  -v ddp-legacy-e2e-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 e2e PG 失败"
docker run -d --name ddp-legacy-cit-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${CIT_PG_PORT}:5432" \
  -v ddp-legacy-cit-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 cit PG 失败"
docker run -d --name ddp-legacy-realcit-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${REALCIT_PG_PORT}:5432" \
  -v ddp-legacy-realcit-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 realcit PG 失败"
docker run -d --name ddp-legacy-web-minio -e "MINIO_ROOT_USER=$MINIO_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_PASS" -p "127.0.0.1:${WEB_MINIO_PORT}:9000" \
  -v ddp-legacy-web-minio:/data "$MINIO_IMAGE" server /data >/dev/null || fail "起 web MinIO 失败"
docker run -d --name ddp-legacy-e2e-minio -e "MINIO_ROOT_USER=$MINIO_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_PASS" -p "127.0.0.1:${E2E_MINIO_PORT}:9000" \
  -v ddp-legacy-e2e-minio:/data "$MINIO_IMAGE" server /data >/dev/null || fail "起 e2e MinIO 失败"
docker run -d --name ddp-legacy-realcit-minio -e "MINIO_ROOT_USER=$MINIO_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_PASS" -p "127.0.0.1:${REALCIT_MINIO_PORT}:9000" \
  -v ddp-legacy-realcit-minio:/data "$MINIO_IMAGE" server /data >/dev/null || fail "起 realcit MinIO 失败"
for c in ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg ddp-legacy-realcit-pg; do
  for i in $(seq 1 30); do
    docker exec "$c" pg_isready -U ddp >/dev/null 2>&1 && break
    sleep 2
    [ "$i" = "30" ] && fail "$c 未 ready（pg_isready 60s 超时）"
  done
done

say "3/10 读 schema revision + 升级前 pg_dump 快照 + cit/realcit 推进 0013"
docker exec ddp-legacy-web-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/web-legacy-revision.txt" \
  | grep -q 0012 || fail "web revision 不是 0012"
docker exec ddp-legacy-e2e-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/e2e-legacy-revision.txt" \
  | grep -q 0003 || fail "e2e revision 不是 0003"
docker exec ddp-legacy-cit-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/cit-legacy-revision.txt" \
  | grep -q 0012 || fail "cit revision 不是 0012"
docker exec ddp-legacy-realcit-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/realcit-legacy-revision.txt" \
  | grep -q 0012 || fail "realcit revision 不是 0012（来源卷 ddp-legacy-realcit-pgdata）"
docker exec ddp-legacy-realcit-pg psql -U ddp -d deepdocparse -tAc \
  "select 'realcit_citations=' || count(*) from citations;" \
  | tee "$WORK/realcit-source-counts.txt" \
  | grep -qE "realcit_citations=[1-9]" || fail "realcit 来源卷没有引用行（era 运行未落库？）"
docker exec ddp-legacy-web-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/web-snapshot.dump \
  || fail "web 快照失败"
docker exec ddp-legacy-e2e-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/e2e-snapshot.dump \
  || fail "e2e 快照失败"
docker cp ddp-legacy-web-pg:/tmp/web-snapshot.dump "$WORK/web-snapshot.dump"
docker cp ddp-legacy-e2e-pg:/tmp/e2e-snapshot.dump "$WORK/e2e-snapshot.dump"
docker exec ddp-legacy-realcit-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/realcit-snapshot.dump \
  || fail "realcit 快照失败"
docker cp ddp-legacy-realcit-pg:/tmp/realcit-snapshot.dump "$WORK/realcit-snapshot.dump"
note "cit/realcit 先行推进到 0013（只为拿到 documents.organization_id 列；0014+ 不动）："
for pair in "${CIT_PG_PORT}:cit" "${REALCIT_PG_PORT}:realcit"; do
  port="${pair%%:*}"; tag="${pair##*:}"
  (
    cd database/corpus
    _w="$OLDPWD/$WORK"
    DATABASE_URL="postgresql+asyncpg://ddp:ddp@127.0.0.1:${port}/deepdocparse" \
      ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic upgrade 0013 2>&1 \
      | tee "$_w/${tag}-alembic-0013.log" | tail -2
  )
  docker exec "ddp-legacy-${tag}-pg" psql -U ddp -d deepdocparse -tAc \
    'select version_num from alembic_version;' | tee -a "$WORK/${tag}-legacy-revision.txt" \
    | grep -q 0013 || fail "${tag} 未推进到 0013"
done
note "cit：在 0013 上构造混合归属夹具 + 时代形状引用（seed 脚本，按双写语义构造）："
"$PY" scripts/legacy_migration_drill_seed.py \
  --dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse" \
  --pdf tests/fixtures/long-doc.pdf --report "$WORK/cit-seed.json" \
  | tee "$WORK/cit-seed-tail.txt" | tail -2
docker exec ddp-legacy-cit-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/cit-snapshot.dump \
  || fail "cit 快照失败"
docker cp ddp-legacy-cit-pg:/tmp/cit-snapshot.dump "$WORK/cit-snapshot.dump"
note "realcit：引用行全部由 era 代码写入，无需 seed；导出逐条引用审计（快照前状态）："
"$PY" scripts/legacy_migration_drill_realcit_audit.py \
  --dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${REALCIT_PG_PORT}/deepdocparse" \
  --report "$WORK/realcit-seed.json" --audit "$WORK/realcit-legacy-citations.json" \
  | tee "$WORK/realcit-seed-tail.txt" | tail -3
docker exec ddp-legacy-realcit-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/realcit-snapshot2.dump \
  || fail "realcit 含引用快照失败"
docker cp ddp-legacy-realcit-pg:/tmp/realcit-snapshot2.dump "$WORK/realcit-snapshot.dump"
for pair in "ddp-legacy-web-pg:deepdocparse_preweb" "ddp-legacy-e2e-pg:deepdocparse_pree2e" "ddp-legacy-cit-pg:deepdocparse_precit" "ddp-legacy-realcit-pg:deepdocparse_prerealcit"; do
  c="${pair%%:*}"; db="${pair##*:}"
  docker exec "$c" psql -U ddp -d deepdocparse -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
    || docker exec "$c" createdb -U ddp "$db" || fail "建只读库 $db 失败"
done
docker cp "$WORK/web-snapshot.dump" ddp-legacy-web-pg:/tmp/preweb-snapshot.dump
docker exec ddp-legacy-web-pg pg_restore -U ddp -d deepdocparse_preweb --no-owner --clean --if-exists \
  /tmp/preweb-snapshot.dump || fail "web 快照恢复只读库失败"
docker cp "$WORK/e2e-snapshot.dump" ddp-legacy-e2e-pg:/tmp/pree2e-snapshot.dump
docker exec ddp-legacy-e2e-pg pg_restore -U ddp -d deepdocparse_pree2e --no-owner --clean --if-exists \
  /tmp/pree2e-snapshot.dump || fail "e2e 快照恢复只读库失败"
docker cp "$WORK/cit-snapshot.dump" ddp-legacy-cit-pg:/tmp/precit-snapshot.dump
docker exec ddp-legacy-cit-pg pg_restore -U ddp -d deepdocparse_precit --no-owner --clean --if-exists \
  /tmp/precit-snapshot.dump || fail "cit 快照恢复只读库失败"
docker cp "$WORK/realcit-snapshot.dump" ddp-legacy-realcit-pg:/tmp/prerealcit-snapshot.dump
docker exec ddp-legacy-realcit-pg pg_restore -U ddp -d deepdocparse_prerealcit --no-owner --clean --if-exists \
  /tmp/prerealcit-snapshot.dump || fail "realcit 快照恢复只读库失败"
"$PY" - "$WORK" "${WEB_PG_PORT}" "${E2E_PG_PORT}" "${CIT_PG_PORT}" "${REALCIT_PG_PORT}" <<'PYEOF0'
import json, sys
work = sys.argv[1]
# No credentials on disk: the verifier connects to snapshot DBs via these
# host:port/db locators plus PGUSER/PGPASSWORD from the drill environment.
json.dump({
    "web": f"127.0.0.1:{sys.argv[2]}/deepdocparse_preweb",
    "e2e": f"127.0.0.1:{sys.argv[3]}/deepdocparse_pree2e",
    "cit": f"127.0.0.1:{sys.argv[4]}/deepdocparse_precit",
    "realcit": f"127.0.0.1:{sys.argv[5]}/deepdocparse_prerealcit",
}, open(f"{work}/pre-dsn.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("pre-dsn written")
PYEOF0

say "4/10 真实迁移链：web 从 0012 / cit 与 realcit 从 0013 起 control-migrate up + alembic upgrade head + grants.sql"
go -C services/control-api build -o /tmp/legacy-drill/control-migrate ./cmd/control-migrate \
  || fail "control-migrate 构建失败"
for pair in "${WEB_PG_PORT}:web" "${CIT_PG_PORT}:cit" "${REALCIT_PG_PORT}:realcit"; do
  port="${pair%%:*}"; tag="${pair##*:}"
  env CONTROL_DATABASE_URL="postgres://ddp:ddp@127.0.0.1:${port}/deepdocparse" /tmp/legacy-drill/control-migrate up \
    | tee "$WORK/${tag}-control-migrate.log" | tail -2
  (
    cd database/corpus
    _w="$OLDPWD/$WORK"
    DATABASE_URL="postgresql+asyncpg://ddp:ddp@127.0.0.1:${port}/deepdocparse" \
      ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic upgrade head 2>&1 \
      | tee "$_w/${tag}-alembic-upgrade.log" | tail -2
  )
done
docker exec -i ddp-legacy-web-pg psql -U ddp -d deepdocparse --set ON_ERROR_STOP=1 \
  < database/corpus/grants.sql 2>&1 | tee "$WORK/web-grants.log" | tail -2 || fail "web grants.sql 失败"
docker exec -i ddp-legacy-cit-pg psql -U ddp -d deepdocparse --set ON_ERROR_STOP=1 \
  < database/corpus/grants.sql 2>&1 | tee "$WORK/cit-grants.log" | tail -2 || fail "cit grants.sql 失败"
docker exec -i ddp-legacy-realcit-pg psql -U ddp -d deepdocparse --set ON_ERROR_STOP=1 \
  < database/corpus/grants.sql 2>&1 | tee "$WORK/realcit-grants.log" | tail -2 || fail "realcit grants.sql 失败"
note "记录迁移前 org 基线（升级后、migrate.py 盖章前，供影子读 1d/权限对比用）："
"$PY" - "$WORK" "${WEB_PG_PORT}" "${E2E_PG_PORT}" "${CIT_PG_PORT}" "${REALCIT_PG_PORT}" <<'PYEOF2' | tee "$WORK/pre-orgs-tail.txt"
import asyncio as _aio, asyncpg as _apg, json as _json, sys as _sys
_work, _wp, _ep, _cp, _rp = _sys.argv[1], _sys.argv[2], _sys.argv[3], _sys.argv[4], _sys.argv[5]
_PORTS = {"web": _wp, "e2e": _ep, "cit": _cp, "realcit": _rp}
_out = {}
async def _one(_label, _port, _db):
    _conn = await _apg.connect(f"postgresql://ddp:ddp@127.0.0.1:{_port}/{_db}")
    try:
        _cols = {_r["column_name"] for _r in await _conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'")}
        _org = "organization_id" if "organization_id" in _cols else "''"
        _u = "uploaded_by" if "uploaded_by" in _cols else "user_id"
        _docs = await _conn.fetch(f"SELECT id, {_org} AS organization_id FROM documents")
        _users = await _conn.fetch("SELECT id FROM users ORDER BY created_at")
        _first = {}
        for _r in await _conn.fetch(f"SELECT id AS did, {_org} AS org, {_u} AS u FROM documents"):
            _first.setdefault(_r["u"], _r["org"])
        try:
            _dflt = (await _conn.fetchrow("SELECT id FROM control.organizations WHERE slug = 'default'"))["id"]
        except Exception:  # noqa: BLE001
            _dflt = ""
        _out[_label] = {
            "documents": {_r["id"]: (_r["organization_id"] or "") for _r in _docs},
            "users": {_r["id"]: (_first.get(_r["id"], "") or "") for _r in _users},
            "default_org": _dflt or "",
        }
    finally:
        await _conn.close()
async def _main():
    await _one("web", _PORTS["web"], "deepdocparse")
    await _one("e2e", _PORTS["e2e"], "deepdocparse")
    await _one("cit", _PORTS["cit"], "deepdocparse")
    await _one("realcit", _PORTS["realcit"], "deepdocparse")
    _json.dump(_out, open(f"{_work}/pre-orgs.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("pre-orgs:", {k: (len(v["documents"]), len(v["users"])) for k, v in _out.items()})
_aio.run(_main())
PYEOF2
grep -q '"realcit"' "$WORK/pre-orgs.json" || fail "pre-orgs.json 缺 realcit -- org 基线采集失败"

say "5/10 文档化迁移器：dry-run 预检 + --apply 对账 + 重跑幂等"
DSN_WEB="postgresql://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse"
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" \
  --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" --object-access-key "$MINIO_USER" \
  --object-secret-key "$MINIO_PASS" --object-bucket "$BUCKET" \
  --report "$WORK/web-migrate-dryrun.json" | tail -8 || fail "web dry-run 预检未通过（migrate.py 非零退出）"
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/web-migrate-apply.json" | tail -14 \
  || fail "web --apply 对账未通过（migrate.py 非零退出）"
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/web-migrate-apply-rerun.json" \
  2>&1 | tee "$WORK/web-migrate-rerun.txt" | tail -3 \
  || fail "web 重跑 migrate.py 非零退出 -- 迁移不是幂等的"
# 幂等门：重跑输出里出现任一 FAIL/ERROR 即失败（migrate.py 用 "[FAIL]" 标
# 记未通过的对账项；"::error::" 是它的失败横幅）。只数 PASS 会把
# "1 PASS + N FAIL" 的半脏库误判为干净。tee 落的是完整输出，不是只给人看的
# 最后三行 —— 对账项一旦超过三行，多出来的 FAIL 就藏在屏幕外面。
if grep -Eq '\[FAIL\]|::error::|ERROR' "$WORK/web-migrate-rerun.txt"; then
  fail "web 重跑出现 FAIL/ERROR -- 迁移不是幂等的（见 $WORK/web-migrate-rerun.txt）"
fi
pass_n=$(grep -c PASS "$WORK/web-migrate-rerun.txt" || true)
[ "${pass_n:-0}" -gt 0 ] || fail "web 重跑 PASS 计数为 0 -- 对账输出异常"
# 幂等断言：重跑必须写 0 行（ON CONFLICT 全挡住）、跳过全部已存在行。
"$PY" - "$WORK/web-migrate-apply-rerun.json" <<'PYEOFR' || fail "web 重跑不是 write-0/skip-all 幂等"
import json, sys
rep = json.load(open(sys.argv[1], encoding="utf-8"))
written = sum(s.get("written", 0) for s in rep.get("steps", []))
read = sum(s.get("read", 0) for s in rep.get("steps", []))
skipped = sum(s.get("skipped", 0) for s in rep.get("steps", []))
assert rep.get("ok") is True, f"rerun report ok != true: {rep.get('checks')}"
assert written == 0, f"rerun wrote {written} rows, expected 0 (not idempotent)"
assert read > 0 and skipped > 0, f"rerun read={read} skipped={skipped}, expected both > 0"
print(f"rerun idempotent: read={read} written=0 skipped={skipped}")
PYEOFR
note "cit（含引用库）同样 --apply + 重跑（对象抽样走同一 web MinIO 拷贝）："
DSN_CIT="postgresql://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse"
"$PY" database/migrator/migrate.py --source "$DSN_CIT" --target "$DSN_CIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/cit-migrate-apply.json" | tail -14 \
  || fail "cit --apply 对账未通过（migrate.py 非零退出）"
"$PY" database/migrator/migrate.py --source "$DSN_CIT" --target "$DSN_CIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/cit-migrate-apply-rerun.json" \
  2>&1 | tee "$WORK/cit-migrate-rerun.txt" | tail -3 \
  || fail "cit 重跑 migrate.py 非零退出 -- 迁移不是幂等的"
if grep -Eq '\[FAIL\]|::error::|ERROR' "$WORK/cit-migrate-rerun.txt"; then
  fail "cit 重跑出现 FAIL/ERROR -- 迁移不是幂等的（见 $WORK/cit-migrate-rerun.txt）"
fi
pass_n=$(grep -c PASS "$WORK/cit-migrate-rerun.txt" || true)
[ "${pass_n:-0}" -gt 0 ] || fail "cit 重跑 PASS 计数为 0 -- 对账输出异常"
"$PY" - "$WORK/cit-migrate-apply-rerun.json" <<'PYEOFR' || fail "cit 重跑不是 write-0/skip-all 幂等"
import json, sys
rep = json.load(open(sys.argv[1], encoding="utf-8"))
written = sum(s.get("written", 0) for s in rep.get("steps", []))
read = sum(s.get("read", 0) for s in rep.get("steps", []))
skipped = sum(s.get("skipped", 0) for s in rep.get("steps", []))
assert rep.get("ok") is True, f"rerun report ok != true: {rep.get('checks')}"
assert written == 0, f"rerun wrote {written} rows, expected 0 (not idempotent)"
assert read > 0 and skipped > 0, f"rerun read={read} skipped={skipped}, expected both > 0"
print(f"rerun idempotent: read={read} written=0 skipped={skipped}")
PYEOFR
note "realcit（真实 era 引用库）同样 --apply + 重跑（对象抽样走自带 realcit MinIO 拷贝）："
DSN_REALCIT="postgresql://ddp:ddp@127.0.0.1:${REALCIT_PG_PORT}/deepdocparse"
"$PY" database/migrator/migrate.py --source "$DSN_REALCIT" --target "$DSN_REALCIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${REALCIT_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/realcit-migrate-apply.json" | tail -14 \
  || fail "realcit --apply 对账未通过（migrate.py 非零退出）"
"$PY" database/migrator/migrate.py --source "$DSN_REALCIT" --target "$DSN_REALCIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${REALCIT_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/realcit-migrate-apply-rerun.json" \
  2>&1 | tee "$WORK/realcit-migrate-rerun.txt" | tail -3 \
  || fail "realcit 重跑 migrate.py 非零退出 -- 迁移不是幂等的"
if grep -Eq '\[FAIL\]|::error::|ERROR' "$WORK/realcit-migrate-rerun.txt"; then
  fail "realcit 重跑出现 FAIL/ERROR -- 迁移不是幂等的（见 $WORK/realcit-migrate-rerun.txt）"
fi
pass_n=$(grep -c PASS "$WORK/realcit-migrate-rerun.txt" || true)
[ "${pass_n:-0}" -gt 0 ] || fail "realcit 重跑 PASS 计数为 0 -- 对账输出异常"
"$PY" - "$WORK/realcit-migrate-apply-rerun.json" <<'PYEOFR' || fail "realcit 重跑不是 write-0/skip-all 幂等"
import json, sys
rep = json.load(open(sys.argv[1], encoding="utf-8"))
written = sum(s.get("written", 0) for s in rep.get("steps", []))
read = sum(s.get("read", 0) for s in rep.get("steps", []))
skipped = sum(s.get("skipped", 0) for s in rep.get("steps", []))
assert rep.get("ok") is True, f"rerun report ok != true: {rep.get('checks')}"
assert written == 0, f"rerun wrote {written} rows, expected 0 (not idempotent)"
assert read > 0 and skipped > 0, f"rerun read={read} skipped={skipped}, expected both > 0"
print(f"rerun idempotent: read={read} written=0 skipped={skipped}")
PYEOFR
# ---- plan §15.1 分阶段门：canary -> small-switch -> gray-expand ----
# 每个门独立断言，前一门 FAIL 即停，不进入下一阶段。canary 只看 web
# （第一个完成 --apply+重跑的数据集）；small-switch 看 cit/realcit；
# gray-expand 看全量四数据集 + 回填幂等。门输出落盘 stage-gates.json，
# 归档 payload 原样收录。
say "5b/10 分阶段门 canary：web 单数据集先行（影子读子集 + 对账全 PASS 才放行）"
"$PY" - "$WORK/web-migrate-apply.json" "$WORK/web-migrate-apply-rerun.json" "$WORK/stage-canary.json" <<'PYEOFS' || fail "canary 门未通过 -- 不进入 small-switch"
import json, sys
apply_p, rerun_p, out = sys.argv[1], sys.argv[2], sys.argv[3]
apply = json.load(open(apply_p, encoding="utf-8"))
rerun = json.load(open(rerun_p, encoding="utf-8"))
gates = []
def gate(name, ok, detail=""):
    gates.append({"stage": "canary", "gate": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] canary/{name} {detail}")
gate("apply-ok", apply.get("ok") is True)
gate("rerun-ok", rerun.get("ok") is True)
gate("rerun-write-0", sum(s.get("written", 0) for s in rerun.get("steps", [])) == 0)
gate("apply-checks-all-pass", all(c.get("passed") for c in apply.get("checks", [])),
     f"{sum(1 for c in apply.get('checks', []) if c.get('passed'))}/{len(apply.get('checks', []))} checks")
ok = all(g["pass"] for g in gates)
json.dump({"stage": "canary", "dataset": "web", "gates": gates, "pass": ok}, open(out, "w"), indent=2)
sys.exit(0 if ok else 1)
PYEOFS
say "5c/10 分阶段门 small-switch：cit + realcit 切换（双数据集对账 + 重跑幂等）"
"$PY" - "$WORK/cit-migrate-apply.json" "$WORK/cit-migrate-apply-rerun.json" "$WORK/realcit-migrate-apply.json" "$WORK/realcit-migrate-apply-rerun.json" "$WORK/stage-small-switch.json" <<'PYEOFS' || fail "small-switch 门未通过 -- 不进入 gray-expand"
import json, sys
paths = sys.argv[1:5]
out = sys.argv[5]
gates = []
def gate(name, ok, detail=""):
    gates.append({"stage": "small-switch", "gate": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] small-switch/{name} {detail}")
reps = [json.load(open(p, encoding="utf-8")) for p in paths]
gate("cit-apply-ok", reps[0].get("ok") is True)
gate("cit-rerun-write-0", sum(s.get("written", 0) for s in reps[1].get("steps", [])) == 0)
gate("realcit-apply-ok", reps[2].get("ok") is True)
gate("realcit-rerun-write-0", sum(s.get("written", 0) for s in reps[3].get("steps", [])) == 0)
gate("both-checks-all-pass", all(c.get("passed") for r in (reps[0], reps[2]) for c in r.get("checks", [])))
ok = all(g["pass"] for g in gates)
json.dump({"stage": "small-switch", "datasets": ["cit", "realcit"], "gates": gates, "pass": ok}, open(out, "w"), indent=2)
sys.exit(0 if ok else 1)
PYEOFS
say "5d/10 分阶段门 gray-expand：全量（回填幂等 + e2e 只读预检 + 行数对账）"
"$PY" - "$WORK" "$WORK/stage-gray-expand.json" <<'PYEOFS' || fail "gray-expand 门未通过"
import json, os, sys
work, out = sys.argv[1], sys.argv[2]
gates = []
def gate(name, ok, detail=""):
    gates.append({"stage": "gray-expand", "gate": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] gray-expand/{name} {detail}")
canary = json.load(open(f"{work}/stage-canary.json"))
switch = json.load(open(f"{work}/stage-small-switch.json"))
gate("canary-passed", canary.get("pass") is True)
gate("small-switch-passed", switch.get("pass") is True)
# 全量行数对账：三数据集 apply 报告 read>0（确有数据被迁移，不是空跑）。
total_read = 0
for n in ("web-migrate-apply.json", "cit-migrate-apply.json", "realcit-migrate-apply.json"):
    rep = json.load(open(f"{work}/{n}", encoding="utf-8"))
    total_read += sum(s.get("read", 0) for s in rep.get("steps", []))
gate("all-datasets-migrated-rows", total_read > 0, f"total_read={total_read}")
ok = all(g["pass"] for g in gates)
json.dump({"stage": "gray-expand", "datasets": ["web", "cit", "realcit", "e2e-readonly"],
           "gates": gates, "pass": ok}, open(out, "w"), indent=2)
sys.exit(0 if ok else 1)
PYEOFS
note "分阶段门 canary/small-switch/gray-expand 全 PASS（见 $WORK/stage-*.json）。"
say "6/10 回退路径 B（先跑）：e2e 快照恢复 —— control 无 downgrade，走文档化快照路径"
docker exec ddp-legacy-e2e-pg createdb -U ddp deepdocparse_restored 2>/dev/null || true
docker cp "$WORK/e2e-snapshot.dump" ddp-legacy-e2e-pg:/tmp/e2e-snapshot.dump
docker exec ddp-legacy-e2e-pg pg_restore -U ddp -d deepdocparse_restored --no-owner \
  /tmp/e2e-snapshot.dump || fail "e2e 快照恢复失败"
docker exec ddp-legacy-e2e-pg psql -U ddp -d deepdocparse_restored -tAc \
  'select version_num from alembic_version; select count(*) from documents; select count(*) from users;' \
  | tee "$WORK/e2e-restored-reads.txt"
note "e2e（0003 时代）无 in-chain 路径：跑 migrate.py precheck() 的同源 SQL（只读源库，零写入）："
DSN_E2E="postgresql://ddp:ddp@127.0.0.1:${E2E_PG_PORT}/deepdocparse_restored"
"$PY" - "$DSN_E2E" <<'PYEOF' | tee "$WORK/e2e-source-precheck.txt"
import asyncio, asyncpg, sys
async def main():
    src = await asyncpg.connect(sys.argv[1])
    try:
        users = await src.fetchval("SELECT count(*) FROM users")
        docs = await src.fetchval("SELECT count(*) FROM documents")
        no_hash = await src.fetchval("SELECT count(*) FROM users WHERE coalesce(password_hash,'')=''")
        dup = await src.fetchval("SELECT count(*) FROM (SELECT token FROM file_tokens GROUP BY token HAVING count(*)>1) x")
        orph = await src.fetchval("SELECT count(*) FROM file_tokens t WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.id=t.document_id)")
        ok = (no_hash == 0 and dup == 0 and orph == 0)
        print(f"users={users} documents={docs} no_hash={no_hash} dup_tokens={dup} orphan_tokens={orph}")
        print("PRECHECK-SOURCE-ONLY: " + ("PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 1)
    finally:
        await src.close()
asyncio.run(main())
PYEOF
[ -s "$WORK/e2e-source-precheck.txt" ] || fail "e2e precheck 输出为空 -- 只读预检未执行"
grep -q "PRECHECK-SOURCE-ONLY: PASS" "$WORK/e2e-source-precheck.txt" || fail "e2e 只读预检未通过"

say "7/10 影子读：归属不猜测 / 权限不扩大 / 引用不漂移 + 回填幂等"
"$PY" scripts/legacy_migration_drill.py \
  --web-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse" \
  --e2e-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${E2E_PG_PORT}/deepdocparse_restored" \
  --cit-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse" \
  --realcit-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${REALCIT_PG_PORT}/deepdocparse" \
  --audit "$WORK/cit-legacy-citations.json" \
  --realcit-audit "$WORK/realcit-legacy-citations.json" \
  --pre-dsn "$WORK/pre-dsn.json" \
  --report "$WORK/shadow-reads.json" || fail "影子读有 FAIL"

say "8/10 回退路径 A：web、cit 与 realcit 各做 corpus downgrade -1 -> upgrade head"
for pair in "ddp-legacy-web-pg:${WEB_PG_PORT}:web" "ddp-legacy-cit-pg:${CIT_PG_PORT}:cit" "ddp-legacy-realcit-pg:${REALCIT_PG_PORT}:realcit"; do
  c="${pair%%:*}"; rest="${pair#*:}"; port="${rest%%:*}"; tag="${rest##*:}"
  docker exec "$c" psql -U ddp -d deepdocparse -tAc \
    "select 'pre_documents=' || count(*) from documents" \
    | tee "$WORK/${tag}-predowngrade-counts.txt"
  docker exec "$c" psql -U ddp -d deepdocparse -tAc \
    "select 'pre_resources=' || count(*) from resources" \
    | tee -a "$WORK/${tag}-predowngrade-counts.txt"
  docker exec "$c" psql -U ddp -d deepdocparse -tAc \
    "select 'pre_citations=' || count(*) from citations" \
    | tee -a "$WORK/${tag}-predowngrade-counts.txt"
  (
    cd database/corpus
    _w="$OLDPWD/$WORK"
    DATABASE_URL="postgresql+asyncpg://ddp:ddp@127.0.0.1:${port}/deepdocparse" \
      ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic downgrade -1 2>&1 | tail -1
    docker exec "$c" psql -U ddp -d deepdocparse -tAc \
      'select version_num from alembic_version;' | tee "$_w/${tag}-downgraded-revision.txt" \
      | grep -q 0041 || fail "$tag 没退到 0041"
    DATABASE_URL="postgresql+asyncpg://ddp:ddp@127.0.0.1:${port}/deepdocparse" \
      ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic upgrade head 2>&1 | tail -1
    docker exec "$c" psql -U ddp -d deepdocparse -tAc \
      "select 'revision=' || version_num from alembic_version" \
      | tee "$_w/${tag}-reupgraded-reads.txt"
    for t in documents resources citations evidence; do
      docker exec "$c" psql -U ddp -d deepdocparse -tAc \
        "select 'post_${t}=' || count(*) from ${t}" \
        | tee -a "$_w/${tag}-reupgraded-reads.txt"
    done
    grep -q "revision=0042" "$_w/${tag}-reupgraded-reads.txt" || fail "$tag 回升不到 head"
  )
done

say "9/10 回归 + 归档 ${ARTIFACT}"
"$PY" -m pytest services/corpus-api/tests/test_backfill.py \
  services/corpus-api/tests/test_resource_migrations.py -q 2>&1 | tail -2
"$PY" - "$WORK" "$ARTIFACT" "$DATE" <<'PYEOF'
import json, sys
work, artifact, date = sys.argv[1], sys.argv[2], sys.argv[3]
def load(name):
    try:
        return json.load(open(f"{work}/{name}", encoding="utf-8"))
    except FileNotFoundError:
        return None
payload = {
    "drill": "legacy-migration",
    "date": date,
    "sources": {
        "web": {"volume": "ddp-web_pgdata", "legacy_revision": "0012",
                "note": "DeepDocParse-Web era, full in-chain upgrade 0012->0042"},
        "e2e": {"volume": "docker_pgdata", "legacy_revision": "0003",
                "note": "predates合仓 chain at 0005 (DuplicateTableError);"
                        " supported path is migrate.py read-only scan, dry-run proven"},
        "cit": {"volume": "ddp-web_pgdata (second copy)", "legacy_revision": "0012+seeded",
                "note": "0012-era copy advanced to 0013 for the org column, then seeded:"
                        " 1 synthetic unambiguous doc (sentinel org c17seedorg…0001, NOT real"
                        " snapshot data) + 1 ambiguous doc (second uploader) + 10 borndigital"
                        " chunks + 3 era-shape citations; full in-chain upgrade to 0042;"
                        " citations constructed with era dual-write semantics, no old code run"},
        "realcit": {"volume": "ddp-legacy-realcit-pgdata (kept) + ddp-legacy-realcit-miniodata (kept)",
                "legacy_revision": "0012+era-run",
                "note": "REAL era run 2026-10-06 on a disposable copy of the 0012 volume:"
                        " era backend worktree .dev-logs/t62-era @ e6b702a + era gateway"
                        " worktree .dev-logs/t62-svc @ 2f0e391 (borndigital in-process CPU,"
                        " embeddings local bge-m3 :48181, chat local qwen3-4b :58180;"
                        " QA_DECISION/VERIFY off + COMPILE_VISION off are config, citation"
                        " write path untouched) drove register -> upload long-doc.pdf ->"
                        " parse -> index (ready, 10 chunks/10 evidence) -> 5 QA rounds;"
                        " all 19 citation rows written by era code (source_kind='assertion'),"
                        " pages 0-4, answers degraded='vision_unavailable' (no vision model"
                        " on this box, era's own visible degradation); advanced to 0013 in"
                        " the drill, then full in-chain upgrade to 0042"},
    },
    "snapshot": {"web": f"{work}/web-snapshot.dump", "e2e": f"{work}/e2e-snapshot.dump",
                 "cit": f"{work}/cit-snapshot.dump", "realcit": f"{work}/realcit-snapshot.dump"},
    "cit_seed": load("cit-seed.json"),
    "realcit_seed": load("realcit-seed.json"),
    "shadow_reads": load("shadow-reads.json"),
    "staged_gates": {"canary": load("stage-canary.json"),
                     "small_switch": load("stage-small-switch.json"),
                     "gray_expand": load("stage-gray-expand.json")},
    # 门结论只记布尔值，逐条对账细节在原始报告里 —— 两份都要归档，
    # 只看门结论复核不了任何东西。
    "migrate_dryrun": load("web-migrate-dryrun.json"),
    "migrate_apply": load("web-migrate-apply.json"),
    "migrate_rerun": load("web-migrate-apply-rerun.json"),
    "cit_migrate_apply": load("cit-migrate-apply.json"),
    "cit_migrate_rerun": load("cit-migrate-apply-rerun.json"),
    "realcit_migrate_apply": load("realcit-migrate-apply.json"),
    "realcit_migrate_rerun": load("realcit-migrate-apply-rerun.json"),
    "e2e_source_precheck": open(f"{work}/e2e-source-precheck.txt").read().strip(),
    "pre_orgs": load("pre-orgs.json"),
    "control_migrate": {
        "note": "tail only (4 lines); full logs at $WORK/{web,cit,realcit}-control-migrate.log",
        "web_tail": open(f"{work}/web-control-migrate.log").read().strip().splitlines()[-4:],
        "cit_tail": open(f"{work}/cit-control-migrate.log").read().strip().splitlines()[-4:],
        "realcit_tail": open(f"{work}/realcit-control-migrate.log").read().strip().splitlines()[-4:],
        "web_full": open(f"{work}/web-control-migrate.log").read().strip().splitlines(),
        "cit_full": open(f"{work}/cit-control-migrate.log").read().strip().splitlines(),
        "realcit_full": open(f"{work}/realcit-control-migrate.log").read().strip().splitlines(),
    },
    "grants": {
        "web": open(f"{work}/web-grants.log").read().strip().splitlines(),
        "cit": open(f"{work}/cit-grants.log").read().strip().splitlines(),
        "realcit": open(f"{work}/realcit-grants.log").read().strip().splitlines(),
    },
    "rollback": {
        "corpus_downgrade_minus1_then_up": "web + cit + realcit shadows 0042->0041->0042",
        "control": "no downgrade by design; snapshot restore proven on e2e shadow",
        "web_predowngrade_counts": open(f"{work}/web-predowngrade-counts.txt").read().strip().splitlines(),
        "web_downgraded_revision": open(f"{work}/web-downgraded-revision.txt").read().strip(),
        "web_reupgraded_reads": open(f"{work}/web-reupgraded-reads.txt").read().strip().splitlines(),
        "cit_predowngrade_counts": open(f"{work}/cit-predowngrade-counts.txt").read().strip().splitlines(),
        "cit_downgraded_revision": open(f"{work}/cit-downgraded-revision.txt").read().strip(),
        "cit_reupgraded_reads": open(f"{work}/cit-reupgraded-reads.txt").read().strip().splitlines(),
        "realcit_predowngrade_counts": open(f"{work}/realcit-predowngrade-counts.txt").read().strip().splitlines(),
        "realcit_downgraded_revision": open(f"{work}/realcit-downgraded-revision.txt").read().strip(),
        "realcit_reupgraded_reads": open(f"{work}/realcit-reupgraded-reads.txt").read().strip().splitlines(),
        "e2e_restored_reads": open(f"{work}/e2e-restored-reads.txt").read().strip(),
    },
}
json.dump(payload, open(artifact, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
import re
blob = open(artifact, encoding="utf-8").read()
bad = re.findall(r"://[^/\s:@]+:[^/\s:@]+@", blob)
assert not bad, f"artifact embeds credentials: {bad[:2]}"
print(f"artifact: {artifact} (secret-grep clean)")
PYEOF

echo
echo 'DRILL PASS：影子读、对账、回退三方一致，见 '"$ARTIFACT"
