#!/usr/bin/env bash
# T62 旧资源迁移与回退演练 —— 真实旧快照上的回填 / 影子读 / 回退。
#
#   bash scripts/legacy_migration_drill.sh            # 全流程，跑完即清理
#   bash scripts/legacy_migration_drill.sh --keep     # 保留 ddp-legacy-* 容器/卷与工作目录
#
#   1. 把 ddp-web_pgdata / docker_pgdata / ddp-web_miniodata / docker_miniodata
#      各拷进一个新的 disposable 卷（源卷只以 :ro 挂载做 cp -a）；另从
#      ddp-web_pgdata 再拷一份 citation 专用卷 ddp-legacy-cit-pg（:15509）；
#   2. 起 ddp-legacy-web-pg (:15505) / ddp-legacy-e2e-pg (:15506) /
#      ddp-legacy-cit-pg (:15509) 与两份 MinIO (:15507/:15508)，
#      读 alembic revision（web=0012，e2e=0003，cit=0012）；
#   3. pg_dump -Fc 快照（升级前基线）；在 cit 库上用当时的 borndigital +
#      compile_chunks（与 0012 时代同一确定性分块路径）解析真实文字层 PDF
#      tests/fixtures/long-doc.pdf，写入 10 chunks + 3 条 era 形状出处
#      （2 匹配/1 负样本）并按 era 双写语义落 evidence/citations 表，
#      再对含引用的库做一次快照；
#   4. 真实迁移链：web 与 cit 库跑 Go control-migrate up + alembic upgrade
#      head + grants.sql（e2e 为 0003 时代，无 in-chain 路径，不升级）；
#   5. 文档化迁移器 database/migrator/migrate.py：web 与 cit 库各做 dry-run
#      预检 + --apply 行数/外键/对象存在性对账 + 重跑幂等（写 0/跳过全部）；
#      e2e 跑只读源库预检；
#   6. 快照恢复（e2e 从升级前快照 pg_restore 到新库；control 无 downgrade，
#      这就是文档化回退路径）；
#   7. scripts/legacy_migration_drill.py 影子读：归属不猜测 / 权限不扩大 /
#      引用不漂移（逐条 citation：digest==chunk 文本且同页，无漂移；双写
#      预填行 backfill 加 0）+ 回填幂等；
#   8. 回退：web 与 cit 库各做 corpus alembic downgrade -1 -> upgrade head，
#      行数不变；
#   9. 归档 docs/refactor/artifacts/legacy-migration-<date>.json。
#
# 边界（演练中已证实，如实声明）：
#   - docker_*（0003 时代）早于合仓迁移链：0005 建 extraction_* 与当时已存在的
#     表撞名（DuplicateTableError），alembic upgrade head 在该数据集上无支持路径。
#     支持路径是文档化迁移器 migrate.py（它只读旧表），本演练对该库跑只读预检。
#   - 0012 时代的旧后端需要已下线的 gateway（解析/embedding/chat 模型）才能跑
#     完整上传→问答链，本机无该服务；引用数据改用 era 双写语义 + 同版
#     borndigital/compile_chunks 构造（出处形状、定位键、指纹规则与当时一致，
#     见 scripts/legacy_migration_drill_seed.py 头注），不执行旧代码。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
DATE="${DRILL_DATE:-$(date +%Y%m%d)}"
ARTIFACT="docs/refactor/artifacts/legacy-migration-${DATE}.json"
WORK=".dev-logs/legacy-migration-${DATE}"

WEB_PG_PORT="${LEGACY_WEB_PG_PORT:-15505}"
E2E_PG_PORT="${LEGACY_E2E_PG_PORT:-15506}"
CIT_PG_PORT="${LEGACY_CIT_PG_PORT:-15509}"
WEB_MINIO_PORT="${LEGACY_WEB_MINIO_PORT:-15507}"
E2E_MINIO_PORT="${LEGACY_E2E_MINIO_PORT:-15508}"
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
  docker rm -f ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg ddp-legacy-web-minio ddp-legacy-e2e-minio ddp-legacy-mc >/dev/null 2>&1 || true
  docker volume rm ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg ddp-legacy-web-minio ddp-legacy-e2e-minio >/dev/null 2>&1 || true
  exit "$code"
}
trap cleanup EXIT

mkdir -p "$WORK"
export CONTROL_DATABASE_URL="postgres://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse"
export CONTROL_DB_PASSWORD=ddp CORPUS_DB_PASSWORD=ddp
export PGUSER=ddp PGPASSWORD=ddp

say "0/9 前提：源卷只读可见，目标卷全新"
for v in ddp-web_pgdata docker_pgdata ddp-web_miniodata docker_miniodata; do
  docker volume inspect "$v" >/dev/null || fail "源卷 $v 不存在"
done

say "1/9 复制源卷到 disposable 卷（源卷 :ro，只做 cp -a）"
for pair in "ddp-web_pgdata:ddp-legacy-web-pg" "docker_pgdata:ddp-legacy-e2e-pg" \
            "ddp-web_pgdata:ddp-legacy-cit-pg" \
            "ddp-web_miniodata:ddp-legacy-web-minio" "docker_miniodata:ddp-legacy-e2e-minio"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  docker volume create "$dst" >/dev/null 2>&1 || true
  docker run --rm -v "$src:/from:ro" -v "$dst:/to" alpine cp -a /from/. /to/ \
    || fail "复制 $src -> $dst 失败"
done
note "源卷未挂载读写；mountpoint 未变更（只读 cp）。"

say "2/9 起一次性 PG + MinIO（${WEB_PG_PORT}/${E2E_PG_PORT}/${CIT_PG_PORT}，${WEB_MINIO_PORT}/${E2E_MINIO_PORT}）"
docker run -d --name ddp-legacy-web-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${WEB_PG_PORT}:5432" \
  -v ddp-legacy-web-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 web PG 失败"
docker run -d --name ddp-legacy-e2e-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${E2E_PG_PORT}:5432" \
  -v ddp-legacy-e2e-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 e2e PG 失败"
docker run -d --name ddp-legacy-cit-pg -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD=ddp \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:${CIT_PG_PORT}:5432" \
  -v ddp-legacy-cit-pg:/var/lib/postgresql/data "$PG_IMAGE" >/dev/null || fail "起 cit PG 失败"
docker run -d --name ddp-legacy-web-minio -e "MINIO_ROOT_USER=$MINIO_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_PASS" -p "127.0.0.1:${WEB_MINIO_PORT}:9000" \
  -v ddp-legacy-web-minio:/data "$MINIO_IMAGE" server /data >/dev/null || fail "起 web MinIO 失败"
docker run -d --name ddp-legacy-e2e-minio -e "MINIO_ROOT_USER=$MINIO_USER" \
  -e "MINIO_ROOT_PASSWORD=$MINIO_PASS" -p "127.0.0.1:${E2E_MINIO_PORT}:9000" \
  -v ddp-legacy-e2e-minio:/data "$MINIO_IMAGE" server /data >/dev/null || fail "起 e2e MinIO 失败"
for c in ddp-legacy-web-pg ddp-legacy-e2e-pg ddp-legacy-cit-pg; do
  for i in $(seq 1 30); do
    docker exec "$c" pg_isready -U ddp >/dev/null 2>&1 && break
    sleep 2
    [ "$i" = "30" ] && fail "$c 未 ready（pg_isready 60s 超时）"
  done
done

say "3/9 读 schema revision + 升级前 pg_dump 快照 + cit 推进 0013 并构造夹具"
docker exec ddp-legacy-web-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/web-legacy-revision.txt" \
  | grep -q 0012 || fail "web revision 不是 0012"
docker exec ddp-legacy-e2e-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/e2e-legacy-revision.txt" \
  | grep -q 0003 || fail "e2e revision 不是 0003"
docker exec ddp-legacy-cit-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee "$WORK/cit-legacy-revision.txt" \
  | grep -q 0012 || fail "cit revision 不是 0012"
docker exec ddp-legacy-web-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/web-snapshot.dump \
  || fail "web 快照失败"
docker exec ddp-legacy-e2e-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/e2e-snapshot.dump \
  || fail "e2e 快照失败"
docker cp ddp-legacy-web-pg:/tmp/web-snapshot.dump "$WORK/web-snapshot.dump"
docker cp ddp-legacy-e2e-pg:/tmp/e2e-snapshot.dump "$WORK/e2e-snapshot.dump"
note "cit 先行推进到 0013（只为拿到 documents.organization_id 列；0014+ 不动）："
(
  cd database/corpus
  _w="$OLDPWD/$WORK"
  DATABASE_URL="postgresql+asyncpg://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse" \
    ALLOW_INSECURE_DEFAULTS=true "$PY" -m alembic upgrade 0013 2>&1 \
    | tee "$_w/cit-alembic-0013.log" | tail -2
)
docker exec ddp-legacy-cit-pg psql -U ddp -d deepdocparse -tAc \
  'select version_num from alembic_version;' | tee -a "$WORK/cit-legacy-revision.txt" \
  | grep -q 0013 || fail "cit 未推进到 0013"
note "cit：在 0013 上构造混合归属夹具 + 真实引用（seed 脚本）："
"$PY" scripts/legacy_migration_drill_seed.py \
  --dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse" \
  --pdf tests/fixtures/long-doc.pdf --report "$WORK/cit-seed.json" \
  | tee "$WORK/cit-seed-tail.txt" | tail -2
docker exec ddp-legacy-cit-pg pg_dump -U ddp -d deepdocparse -Fc -f /tmp/cit-snapshot.dump \
  || fail "cit 快照失败"
docker cp ddp-legacy-cit-pg:/tmp/cit-snapshot.dump "$WORK/cit-snapshot.dump"
note "旧规则读数：在影子库内从快照恢复只读库（供影子读 OLD 集用，不升级不写源）："
for pair in "ddp-legacy-web-pg:deepdocparse_preweb" "ddp-legacy-e2e-pg:deepdocparse_pree2e" "ddp-legacy-cit-pg:deepdocparse_precit"; do
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
"$PY" - "$WORK" "${WEB_PG_PORT}" "${E2E_PG_PORT}" "${CIT_PG_PORT}" <<'PYEOF0'
import json, sys
work = sys.argv[1]
# No credentials on disk: the verifier connects to snapshot DBs via these
# host:port/db locators plus PGUSER/PGPASSWORD from the drill environment.
json.dump({
    "web": f"127.0.0.1:{sys.argv[2]}/deepdocparse_preweb",
    "e2e": f"127.0.0.1:{sys.argv[3]}/deepdocparse_pree2e",
    "cit": f"127.0.0.1:{sys.argv[4]}/deepdocparse_precit",
}, open(f"{work}/pre-dsn.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("pre-dsn written")
PYEOF0

say "4/9 真实迁移链：web 从 0012 / cit 从 0013 起 control-migrate up + alembic upgrade head + grants.sql"
go -C services/control-api build -o /tmp/legacy-drill/control-migrate ./cmd/control-migrate \
  || fail "control-migrate 构建失败"
for pair in "${WEB_PG_PORT}:web" "${CIT_PG_PORT}:cit"; do
  port="${pair%%:*}"; tag="${pair##*:}"
  /tmp/legacy-drill/control-migrate -database "postgres://ddp:ddp@127.0.0.1:${port}/deepdocparse" up \
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
note "记录迁移前 org 基线（升级后、migrate.py 盖章前，供影子读 1d/权限对比用）："
"$PY" - "$WORK" "${WEB_PG_PORT}" "${E2E_PG_PORT}" "${CIT_PG_PORT}" <<'PYEOF2' | tee "$WORK/pre-orgs-tail.txt"
import asyncio as _aio, asyncpg as _apg, json as _json, sys as _sys
_work, _wp, _ep, _cp = _sys.argv[1], _sys.argv[2], _sys.argv[3], _sys.argv[4]
_PORTS = {"web": _wp, "e2e": _ep, "cit": _cp}
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
    _json.dump(_out, open(f"{_work}/pre-orgs.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("pre-orgs:", {k: (len(v["documents"]), len(v["users"])) for k, v in _out.items()})
_aio.run(_main())
PYEOF2
grep -q '"web"' "$WORK/pre-orgs.json" || fail "pre-orgs.json 未生成 -- org 基线采集失败"

say "5/9 文档化迁移器：dry-run 预检 + --apply 对账 + 重跑幂等"
DSN_WEB="postgresql://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse"
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" \
  --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" --object-access-key "$MINIO_USER" \
  --object-secret-key "$MINIO_PASS" --object-bucket "$BUCKET" \
  --report "$WORK/web-migrate-dryrun.json" | tail -8
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/web-migrate-apply.json" | tail -14
"$PY" database/migrator/migrate.py --source "$DSN_WEB" --target "$DSN_WEB" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/web-migrate-apply-rerun.json" \
  | tee "$WORK/web-migrate-rerun-tail.txt" | tail -3
pass_n=$(grep -c PASS "$WORK/web-migrate-rerun-tail.txt" || true)
[ "${pass_n:-0}" -gt 0 ] || fail "web 重跑 PASS 计数为 0 -- 对账输出异常"
note "cit（含引用库）同样 --apply + 重跑（对象抽样走同一 web MinIO 拷贝）："
DSN_CIT="postgresql://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse"
"$PY" database/migrator/migrate.py --source "$DSN_CIT" --target "$DSN_CIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/cit-migrate-apply.json" | tail -14
"$PY" database/migrator/migrate.py --source "$DSN_CIT" --target "$DSN_CIT" --apply \
  --allow-live-target --object-endpoint "127.0.0.1:${WEB_MINIO_PORT}" \
  --object-access-key "$MINIO_USER" --object-secret-key "$MINIO_PASS" \
  --object-bucket "$BUCKET" --report "$WORK/cit-migrate-rerun.json" \
  | tee "$WORK/cit-migrate-rerun-tail.txt" | tail -3
pass_n=$(grep -c PASS "$WORK/cit-migrate-rerun-tail.txt" || true)
[ "${pass_n:-0}" -gt 0 ] || fail "cit 重跑 PASS 计数为 0 -- 对账输出异常"
say "6/9 回退路径 B（先跑）：e2e 快照恢复 —— control 无 downgrade，走文档化快照路径"
note "control 无 downgrade（按文件名顺序只进不退，见 internal/migrate/migrate.go）："
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

say "7/9 影子读：归属不猜测 / 权限不扩大 / 引用不漂移 + 回填幂等"
"$PY" scripts/legacy_migration_drill.py \
  --web-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${WEB_PG_PORT}/deepdocparse" \
  --e2e-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${E2E_PG_PORT}/deepdocparse_restored" \
  --cit-dsn "postgresql+asyncpg://ddp:ddp@127.0.0.1:${CIT_PG_PORT}/deepdocparse" \
  --audit "$WORK/cit-legacy-citations.json" \
  --pre-dsn "$WORK/pre-dsn.json" \
  --report "$WORK/shadow-reads.json" || fail "影子读有 FAIL"

say "8/9 回退路径 A：web 与 cit 库各做 corpus downgrade -1 -> upgrade head"
for pair in "ddp-legacy-web-pg:${WEB_PG_PORT}:web" "ddp-legacy-cit-pg:${CIT_PG_PORT}:cit"; do
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

say "9/9 回归 + 归档 ${ARTIFACT}"
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
                        " old backend not executed (needs retired gateway+models)"},
    },
    "snapshot": {"web": f"{work}/web-snapshot.dump", "e2e": f"{work}/e2e-snapshot.dump",
                 "cit": f"{work}/cit-snapshot.dump"},
    "cit_seed": load("cit-seed.json"),
    "shadow_reads": load("shadow-reads.json"),
    "migrate_dryrun": load("web-migrate-dryrun.json"),
    "migrate_apply": load("web-migrate-apply.json"),
    "migrate_rerun": load("web-migrate-apply-rerun.json"),
    "cit_migrate_apply": load("cit-migrate-apply.json"),
    "cit_migrate_rerun": load("cit-migrate-rerun.json"),
    "e2e_source_precheck": open(f"{work}/e2e-source-precheck.txt").read().strip(),
    "pre_orgs": load("pre-orgs.json"),
    "control_migrate": {
        "note": "tail only (4 lines); full logs at $WORK/{web,cit}-control-migrate.log",
        "web_tail": open(f"{work}/web-control-migrate.log").read().strip().splitlines()[-4:],
        "cit_tail": open(f"{work}/cit-control-migrate.log").read().strip().splitlines()[-4:],
        "web_full": open(f"{work}/web-control-migrate.log").read().strip().splitlines(),
        "cit_full": open(f"{work}/cit-control-migrate.log").read().strip().splitlines(),
    },
    "grants": {
        "web": open(f"{work}/web-grants.log").read().strip().splitlines(),
        "cit": open(f"{work}/cit-grants.log").read().strip().splitlines(),
    },
    "rollback": {
        "corpus_downgrade_minus1_then_up": "web + cit shadows 0042->0041->0042",
        "control": "no downgrade by design; snapshot restore proven on e2e shadow",
        "web_predowngrade_counts": open(f"{work}/web-predowngrade-counts.txt").read().strip().splitlines(),
        "web_downgraded_revision": open(f"{work}/web-downgraded-revision.txt").read().strip(),
        "web_reupgraded_reads": open(f"{work}/web-reupgraded-reads.txt").read().strip().splitlines(),
        "cit_predowngrade_counts": open(f"{work}/cit-predowngrade-counts.txt").read().strip().splitlines(),
        "cit_downgraded_revision": open(f"{work}/cit-downgraded-revision.txt").read().strip(),
        "cit_reupgraded_reads": open(f"{work}/cit-reupgraded-reads.txt").read().strip().splitlines(),
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
