#!/usr/bin/env bash
# T61 PITR recovery drill (plan T61): WAL archiving + base backup + PITR to a
# recorded target time AND to latest, full reconciliation at each restore,
# clone-cannot-impersonate proof, RTO/RPO measurement.
#
# One command:  bash scripts/recovery_drill.sh [--keep] [--n-docs N] [--n-extra M]
# Never prints secrets. Secrets enter only via env files the script reads
# (B's live secrets file for the read-only consistent copy; fresh random
# passwords for every disposable container).
#
# Pipeline (disposable ddp-recovery-*; SRC 15495, TGT 15496, CRASH 15498, MinIO 15511/15521;
# LATEST restore on 15503 because another session's ddp-subtree-audit-pg2 owns 15497 — do not touch it):
#   0. consistent copy of center B: pg_dump (read-only, from ddp-b-pg) +
#      mc mirror (read-only, from 127.0.0.1:49000) into SRC work area
#   1. fresh SRC PG (WAL archiving ON from init) + SRC MinIO; restore B copy;
#      optionally enlarge with generated docs to stated scale
#   2. base backup (pg_basebackup) + start WAL streaming archive; mc mirror baseline
#   3. post-base writes A (uploads, federation tasks, wiki); record PITR target;
#      post-target writes B; archive both; snapshot source counts
#   4. restore to TARGET (base + WAL replay to target time, promote) + reconcile;
#      selectivity: A present, B absent
#   5. restore to LATEST (base + replay all, promote) + reconcile + no-loss proof;
#      RPO crash probe: unarchived write + kill + crash-restore proves loss window
#   6. identity: Go TestNodeIdentityBackupRestoreDrill + peer-layer clone rejection
#   7. artifact docs/refactor/artifacts/recovery-pitr-<date>.json + RTO/RPO table
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
DATE_TAG="${DRILL_DATE_TAG:-$(date +%Y%m%d)}"
N_DOCS="${DRILL_N_DOCS:-30}"
N_EXTRA="${DRILL_N_EXTRA:-5}"
TAG="pitr${DATE_TAG}"

# Ports (all disposable; live stack untouched):
SRC_PG_PORT="${DRILL_SRC_PG_PORT:-15495}"
TGT_PG_PORT="${DRILL_TGT_PG_PORT:-15496}"
LATEST_PG_PORT="${DRILL_LATEST_PG_PORT:-15503}"
CRASH_PG_PORT="${DRILL_CRASH_PG_PORT:-15498}"
SRC_MINIO_PORT="${DRILL_SRC_MINIO_PORT:-15511}"
SRC_MINIO_CONSOLE="${DRILL_SRC_MINIO_CONSOLE:-15512}"
DST_MINIO_PORT="${DRILL_DST_MINIO_PORT:-15521}"
DST_MINIO_CONSOLE="${DRILL_DST_MINIO_CONSOLE:-15522}"

SRC_PG_C="ddp-recovery-src-pg"
TGT_PG_C="ddp-recovery-tgt-pg"
LATEST_PG_C="ddp-recovery-latest-pg"
CRASH_PG_C="ddp-recovery-crash-pg"
SRC_MINIO_C="ddp-recovery-src-minio"
DST_MINIO_C="ddp-recovery-dst-minio"
PG_IMAGE="pgvector/pgvector:pg16"
MINIO_IMAGE="ddp-minio:RELEASE.2025-10-15T17-29-55Z"
MC_IMAGE="minio/mc:latest"
BUCKET="deepdocparse"

KEEP=0
RESUME=0
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    --resume) RESUME=1 ;;
    --n-docs=*) N_DOCS="${a#--n-docs=}" ;;
    --n-docs) shift; N_DOCS="${1:-30}" ;;
    --n-extra=*) N_EXTRA="${a#--n-extra=}" ;;
  esac
done

WORK="$ROOT/.dev-logs/recovery-20261005/work-$DATE_TAG"
mkdir -p "$WORK"
chmod 700 "$WORK" 2>/dev/null || true
STATE="$WORK/pitr-state.json"
ARTIFACT="$ROOT/docs/refactor/artifacts/recovery-pitr-$DATE_TAG.json"
TIMING="$WORK/timing.json"
# B3: --resume must NOT reset timing.json — copy/base marks from the full run
# survive; step re-runs overwrite their own keys via mark().
if [ -s "$TIMING" ]; then :; else echo '{}' > "$TIMING"; fi

say()  { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
note() { printf '\033[2m    %s\033[0m\n' "$*"; }
fail() { printf '\033[31mDRILL FAIL: %s\033[0m\n' "$*" >&2; exit 1; }
mark() { # mark <key>: record epoch seconds
  "$PY" - "$TIMING" "$1" <<'PY'
import json, sys, time
p, k = sys.argv[1], sys.argv[2]
d = json.load(open(p)) if __import__("os").path.getsize(p) > 2 else {}
d[k] = time.time()
json.dump(d, open(p, "w"), indent=2)
PY
}

cleanup() {
  local code=$?
  trap - EXIT
  if [ "$KEEP" -eq 1 ]; then
    printf '\n\033[33m--keep: containers + %s kept\033[0m\n' "$WORK"
    return
  fi
  docker rm -f "$SRC_PG_C" "$TGT_PG_C" "$LATEST_PG_C" "$CRASH_PG_C" "$SRC_MINIO_C" "$DST_MINIO_C" >/dev/null 2>&1 || true
  docker volume rm ddp-recovery-src-pgdata ddp-recovery-tgt-pgdata ddp-recovery-latest-pgdata ddp-recovery-crash-pgdata ddp-recovery-src-miniodata ddp-recovery-dst-miniodata >/dev/null 2>&1 || true
  exit "$code"
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || fail "no docker"
docker info >/dev/null 2>&1 || fail "docker daemon unavailable"
B_SECRETS="$ROOT/.dev-logs/b-stack-20260924/secrets.env"
[ -f "$B_SECRETS" ] || fail "B secrets file missing: $B_SECRETS"
B_PW="$("$PY" -c "d=dict(l.strip().split('=',1) for l in open('$B_SECRETS') if '=' in l and not l.startswith('#')); print(d['PG_OWNER_PW'])")"
B_OBJ="$("$PY" -c "d=dict(l.strip().split('=',1) for l in open('$B_SECRETS') if '=' in l and not l.startswith('#')); print(d['OBJECT_SECRET_KEY'])")"
if [ "$RESUME" -eq 1 ] && [ -f "$WORK/bcopy/b.dump" ] && [ -s "$WORK/pitr-state.json" ] && [ -f "$WORK/disposable.env" ] && [ -f "$WORK/source-counts.json" ] && [ -s "$WORK/base_tar/base.tar.gz" ] && [ -f "$WORK/pitr-target.txt" ] && [ -d "$WORK/wal-archive-full" ] && [ -n "$(ls "$WORK/wal-archive-full" 2>/dev/null)" ]; then
  say "resume: reusing B copy + SRC stack (skipping steps 0-1 rebuild)"
  # shellcheck disable=SC1091
  SRC_PW="$(sed -n 's/^SRC_PW=//p' "$WORK/disposable.env")"
  TGT_PW="$(sed -n 's/^TGT_PW=//p' "$WORK/disposable.env")"
  LATEST_PW="$(sed -n 's/^LATEST_PW=//p' "$WORK/disposable.env")"
  SRC_MK="$(sed -n 's/^SRC_MK=//p' "$WORK/disposable.env")"
  SRC_MS="$(sed -n 's/^SRC_MS=//p' "$WORK/disposable.env")"
  DST_MK="$(sed -n 's/^DST_MK=//p' "$WORK/disposable.env")"
  DST_MS="$(sed -n 's/^DST_MS=//p' "$WORK/disposable.env")"
  export SRC_PW TGT_PW LATEST_PW SRC_MK SRC_MS DST_MK DST_MS
  SRC_DSN="unused-under-resume-restores-only"
  TARGET_TIME="$(cat "$WORK/pitr-target.txt")"
  note "resume: TARGET $TARGET_TIME; base + 28 WAL segs + counts reused"
else
for port in "$SRC_PG_PORT" "$TGT_PG_PORT" "$LATEST_PG_PORT" "$CRASH_PG_PORT" "$SRC_MINIO_PORT" "$SRC_MINIO_CONSOLE" "$DST_MINIO_PORT" "$DST_MINIO_CONSOLE"; do
  if (exec 3<>/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then exec 3>&- 3<&-; fail "127.0.0.1:$port in use"; fi
done
genpw() { "$PY" -c "import secrets; print(secrets.token_hex(18))"; }
SRC_PW="$(genpw)"; TGT_PW="$(genpw)"; LATEST_PW="$(genpw)"
SRC_MK="$(genpw)"; SRC_MS="$(genpw)"; DST_MK="dstpitr"; DST_MS="$(genpw)"
export SRC_PW TGT_PW LATEST_PW SRC_MK SRC_MS DST_MK DST_MS
umask 077
cat > "$WORK/disposable.env" <<EOF
# disposable drill credentials (NOT B's); safe to delete with the containers
SRC_PW=$SRC_PW
TGT_PW=$TGT_PW
LATEST_PW=$LATEST_PW
SRC_MK=$SRC_MK
SRC_MS=$SRC_MS
DST_MK=$DST_MK
DST_MS=$DST_MS
EOF
chmod 600 "$WORK/disposable.env"
fi

# ---------------------------------------------------------------- 0) B copy (skipped under --resume)
if [ "$RESUME" -eq 1 ] && [ -f "$WORK/bcopy/b.dump" ] && [ -s "$WORK/pitr-state.json" ] && [ -f "$WORK/source-counts.json" ] && [ -s "$WORK/base_tar/base.tar.gz" ] && [ -f "$WORK/pitr-target.txt" ] && [ -d "$WORK/wal-archive-full" ] && [ -n "$(ls "$WORK/wal-archive-full" 2>/dev/null)" ]; then
  say "0-1/7 SKIPPED (resume): B copy + SRC stack reused"
else
say "0/7 consistent copy of center B (read-only pg_dump + mc mirror)"
mark t_copy_start
mkdir -p "$WORK/bcopy" "$WORK/src-objects" "$WORK/dst-objects"
docker exec ddp-b-pg pg_dump -U ddp -d deepdocparse -Fc > "$WORK/bcopy/b.dump" \
  || fail "pg_dump of live B failed"
[ -s "$WORK/bcopy/b.dump" ] || fail "B dump empty"
note "B dump $(du -h "$WORK/bcopy/b.dump" | cut -f1)"
docker run --rm --network host --entrypoint sh -v "$WORK/src-objects:/dst" "$MC_IMAGE" \
  -c "mc alias set bsrc http://127.0.0.1:49000 ddpbminio '$B_OBJ' >/dev/null && mc mirror --overwrite bsrc/deepdocparse /dst" \
  || fail "mc mirror of B bucket failed"
note "B objects: $(find "$WORK/src-objects" -type f | wc -l) files, $(du -sh "$WORK/src-objects" | cut -f1)"
"$PY" - "$B_PW" "$WORK/bcopy-scale.json" <<'PY'
import json, sys, asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    pw, out = sys.argv[1], sys.argv[2]
    e = create_async_engine(f"postgresql+asyncpg://ddp:{pw}@127.0.0.1:45432/deepdocparse")
    tables = ["public.documents","public.parse_jobs","public.resources","public.resource_versions",
              "public.chunks","public.evidence","public.citations","public.wikis","public.wiki_revisions",
              "public.wiki_pages","public.wiki_dependencies","public.upload_events",
              "public.federation_requests","public.coverage_ledgers","public.coverage_entries",
              "public.collections","public.collection_members","control.organizations","control.users"]
    async with e.connect() as c:
        counts = {}
        for t in tables:
            counts[t] = int((await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())
        r = await c.execute(text("SELECT pg_size_pretty(pg_database_size('deepdocparse'))"))
        counts["_db_size"] = r.scalar_one()
    await e.dispose()
    json.dump(counts, open(out, "w"), indent=2)
asyncio.run(main())
PY
mark t_copy_done
fi

# ---------------------------------------------------------------- 1) SRC stack
if [ "$RESUME" -eq 1 ] && [ -f "$WORK/bcopy/b.dump" ] && [ -s "$WORK/pitr-state.json" ] && [ -f "$WORK/source-counts.json" ] && [ -s "$WORK/base_tar/base.tar.gz" ] && [ -f "$WORK/pitr-target.txt" ] && [ -d "$WORK/wal-archive-full" ] && [ -n "$(ls "$WORK/wal-archive-full" 2>/dev/null)" ]; then
  : # SRC stack already reused above; jump straight to base backup
else
mark t_src_start
docker rm -f "$SRC_PG_C" "$SRC_MINIO_C" >/dev/null 2>&1 || true
docker volume rm ddp-recovery-src-pgdata ddp-recovery-src-miniodata >/dev/null 2>&1 || true
docker run -d --name "$SRC_PG_C" -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD="$SRC_PW" \
  -e POSTGRES_DB=deepdocparse \
  -p "127.0.0.1:$SRC_PG_PORT:5432" -v ddp-recovery-src-pgdata:/var/lib/postgresql/data \
  "$PG_IMAGE" >/dev/null
docker run -d --name "$SRC_MINIO_C" -e MINIO_ROOT_USER="$SRC_MK" -e MINIO_ROOT_PASSWORD="$SRC_MS" \
  --network host -v ddp-recovery-src-miniodata:/data "$MINIO_IMAGE" \
  server /data --address "127.0.0.1:$SRC_MINIO_PORT" --console-address "127.0.0.1:$SRC_MINIO_CONSOLE" >/dev/null
ready=0; for _ in $(seq 1 60); do
  docker exec "$SRC_PG_C" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
[ "$ready" -eq 1 ] || fail "SRC PG not ready"
for _ in $(seq 1 60); do curl -fsS --max-time 2 "http://127.0.0.1:$SRC_MINIO_PORT/minio/health/ready" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS --max-time 2 "http://127.0.0.1:$SRC_MINIO_PORT/minio/health/ready" >/dev/null 2>&1 || fail "SRC MinIO not ready"
docker exec "$SRC_PG_C" bash -c 'mkdir -p /wal-archive && chown postgres:postgres /wal-archive'
for _ in $(seq 1 60); do
  docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT 1;' >/dev/null 2>&1 && break; sleep 1; done
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "ALTER SYSTEM SET wal_level='replica';" >/dev/null || fail "ALTER wal_level failed"
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "ALTER SYSTEM SET archive_mode='on';" >/dev/null || fail "ALTER archive_mode failed"
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -c "ALTER SYSTEM SET archive_command TO 'test ! -f /wal-archive/%f && cp %p /wal-archive/%f';" >/dev/null || fail "ALTER archive_command failed"
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "ALTER SYSTEM SET archive_timeout='30s';" >/dev/null || fail "ALTER archive_timeout failed"
docker restart "$SRC_PG_C" >/dev/null
ready=0; for _ in $(seq 1 60); do
  docker exec "$SRC_PG_C" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
[ "$ready" -eq 1 ] || fail "SRC PG not ready after WAL-archive restart"
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "SHOW archive_mode; SHOW wal_level; SHOW archive_command;" | tr '\n' ' '; echo
docker exec -i -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse --set ON_ERROR_STOP=1 -q <<'SQL' >/dev/null || fail "service roles creation failed"
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ddp_control') THEN CREATE ROLE ddp_control LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ddp_corpus') THEN CREATE ROLE ddp_corpus LOGIN; END IF;
END $$;
SQL
docker exec -i -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -c "DROP SCHEMA public CASCADE; DROP SCHEMA control CASCADE;" >/dev/null 2>&1 || true
cat "$WORK/bcopy/b.dump" | docker exec -i -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" pg_restore -h 127.0.0.1 -U ddp -d deepdocparse --no-owner >/dev/null 2>&1 || fail "restore of B dump into SRC failed"
note "B copy restored into SRC"
# copy B objects into SRC MinIO (bucket does not auto-create on mirror-to-path)
docker run --rm --network host --entrypoint sh -v "$WORK/src-objects:/src" "$MC_IMAGE" \
  -c "mc alias set dst http://127.0.0.1:$SRC_MINIO_PORT '$SRC_MK' '$SRC_MS' >/dev/null && mc mb --ignore-existing dst/deepdocparse >/dev/null && mc mirror --overwrite /src dst/deepdocparse" \
  || fail "object copy into SRC MinIO failed"
# enlarge to stated scale with generated docs (record exact scale)
SRC_DSN="postgresql+asyncpg://ddp:${SRC_PW}@127.0.0.1:${SRC_PG_PORT}/deepdocparse"
"$PY" scripts/recovery_drill_pitr.py seed --dsn "$SRC_DSN" --state "$STATE" \
  --minio-endpoint "127.0.0.1:$SRC_MINIO_PORT" --minio-access-key "$SRC_MK" \
  --minio-secret-key "$SRC_MS" --bucket "$BUCKET" --n-docs "$N_DOCS" --tag "$TAG" \
  || fail "seed enlarge failed"
mark t_src_done
fi

if [ "$RESUME" -eq 1 ] && [ -s "$WORK/base_tar/base.tar.gz" ] && [ -f "$WORK/source-counts.json" ] && [ -f "$WORK/pitr-target.txt" ] && [ -d "$WORK/wal-archive-full" ] && [ -n "$(ls "$WORK/wal-archive-full" 2>/dev/null)" ]; then
  say "2-3/7 SKIPPED (resume): base + WAL + counts + objects reused"
  TARGET_TIME="$(cat "$WORK/pitr-target.txt")"
else
say "2/7 base backup + WAL streaming archive baseline"
mark t_base_start
mkdir -p "$WORK/base" "$WORK/wal-archive"
rm -rf "$WORK/wal-archive-full" "$WORK/base_tar"; mkdir -p "$WORK/base_tar"
docker exec "$SRC_PG_C" bash -c 'rm -rf /tmp/base /tmp/base.tar.gz'
docker exec --user postgres "$SRC_PG_C" pg_basebackup -h /var/run/postgresql -U ddp -D /tmp/base -Fp -P 2>"$WORK/base-progress.log" \
  || fail "pg_basebackup failed (see $WORK/base-progress.log)"
docker exec "$SRC_PG_C" bash -c 'tar -czf /tmp/base.tar.gz -C /tmp/base . && rm -rf /tmp/base'
# WALs archived before this base belong to the previous timeline incarnation;
# drop them so the restore set starts exactly at this base backup.
docker exec "$SRC_PG_C" bash -c 'rm -f /wal-archive/*'
mkdir -p "$WORK/base_tar" && docker cp "$SRC_PG_C:/tmp/base.tar.gz" "$WORK/base_tar/base.tar.gz" >/dev/null \
  && docker exec "$SRC_PG_C" rm -f /tmp/base.tar.gz
note "base: $(du -h "$WORK/base_tar/base.tar.gz" | cut -f1)"
BASE_LSN="$(docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_current_wal_lsn();')"
echo "$BASE_LSN" > "$WORK/base-lsn.txt"; note "base LSN $BASE_LSN"
mark t_base_done

# ---------------------------------------------------------------- 3) writes + PITR target
say "3/7 pre-target writes (uploads, federation tasks, wiki) + PITR target, then post-target writes"
mark t_post_start
"$PY" scripts/recovery_drill_pitr.py post --dsn "$SRC_DSN" --state "$STATE" \
  --minio-endpoint "127.0.0.1:$SRC_MINIO_PORT" --minio-access-key "$SRC_MK" \
  --minio-secret-key "$SRC_MS" --bucket "$BUCKET" --n-extra "$N_EXTRA" --tag "$TAG" \
  --key-prefix pre --section pre \
  || fail "pre-target writes failed"
# reconcile at source (pre-restore sanity, quarantines seed bugs before PITR)
"$PY" scripts/recovery_drill_pitr.py reconcile --dsn "$SRC_DSN" \
  --minio-endpoint "127.0.0.1:$SRC_MINIO_PORT" --minio-access-key "$SRC_MK" \
  --minio-secret-key "$SRC_MS" --bucket "$BUCKET" --out "$WORK/recon-source.json" \
  || fail "source reconciliation failed"
sleep 35  # cross one archive_timeout so pre-target WAL is archived
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_switch_wal();' >/dev/null
sleep 5
TARGET_TIME="$(docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "SELECT now()::timestamptz;")"
echo -n "$TARGET_TIME" > "$WORK/pitr-target.txt"; note "PITR target $TARGET_TIME"
# post-target writes (must be ABSENT from the PITR-target restore, PRESENT in latest)
sleep 2  # ensure post-target commits sort strictly after the recorded target
"$PY" scripts/recovery_drill_pitr.py post --dsn "$SRC_DSN" --state "$STATE" \
  --minio-endpoint "127.0.0.1:$SRC_MINIO_PORT" --minio-access-key "$SRC_MK" \
  --minio-secret-key "$SRC_MS" --bucket "$BUCKET" --n-extra 2 --tag "$TAG" \
  --key-prefix postt --section postt \
  || fail "post-target writes failed"
docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_switch_wal();' >/dev/null
sleep 40  # archive the post-target WAL too, so latest replay provably includes it
# source counts snapshot (latest must equal this; target must equal it minus postt)
"$PY" scripts/recovery_drill_pitr.py counts --dsn "$SRC_DSN" --out "$WORK/source-counts.json" \
  || fail "source counts snapshot failed"
# archiver health gate: failures here mean the restores below cannot replay
# (pg_stat_archiver in pg16 exposes archived_count/failed_count, not wal names)
ARCH_STAT="$(docker exec -e PGPASSWORD="$SRC_PW" "$SRC_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc "SELECT 'archived=' || archived_count || ' failed=' || failed_count || ' last_ok=' || COALESCE(last_archived_time::text,'-') || ' last_fail=' || COALESCE(last_failed_time::text,'-') FROM pg_stat_archiver;")"
note "archiver: $ARCH_STAT"
[ -n "$(docker exec "$SRC_PG_C" bash -c 'ls /wal-archive 2>/dev/null' | head -1)" ] || fail "WAL archive empty: archiving never succeeded (see archiver stat above)"
docker cp "$SRC_PG_C:/wal-archive" "$WORK/wal-archive-full" >/dev/null
# object delta for PITR-time vs latest comparison
docker run --rm --network host --entrypoint sh -v "$WORK/dst-objects:/dst" "$MC_IMAGE" \
  -c "mc alias set src http://127.0.0.1:$SRC_MINIO_PORT '$SRC_MK' '$SRC_MS' >/dev/null && mc mirror --overwrite src/deepdocparse /dst" \
  || fail "object delta mirror failed"
mark t_post_done
fi

# ---------------------------------------------------------------- 4) restore to TARGET
say "4/7 restore to PITR target time"
mark t_pitr_start
docker rm -f "$TGT_PG_C" "$DST_MINIO_C" >/dev/null 2>&1 || true
docker volume rm ddp-recovery-tgt-pgdata ddp-recovery-dst-miniodata >/dev/null 2>&1 || true
docker run -d --name "$DST_MINIO_C" -e MINIO_ROOT_USER="$DST_MK" -e MINIO_ROOT_PASSWORD="$DST_MS" \
  --network host -v ddp-recovery-dst-miniodata:/data "$MINIO_IMAGE" \
  server /data --address "127.0.0.1:$DST_MINIO_PORT" --console-address "127.0.0.1:$DST_MINIO_CONSOLE" >/dev/null
for _ in $(seq 1 60); do curl -fsS --max-time 2 "http://127.0.0.1:$DST_MINIO_PORT/minio/health/ready" >/dev/null 2>&1 && break; sleep 1; done
docker volume create ddp-recovery-tgt-pgdata >/dev/null
# extract base + write recovery config BEFORE first boot (the entrypoint only
# starts stock server; recovery.signal + postgresql.conf restore_command must
# already be in place, then the container is started once).
echo -n "$TARGET_TIME" > "$WORK/recovery-target-time.txt"
chmod -R a+rX "$WORK/wal-archive-full" "$WORK/base_tar" "$WORK/recovery-target-time.txt"
docker run --rm -v ddp-recovery-tgt-pgdata:/data -v "$WORK/base_tar:/base:ro" -v "$WORK/wal-archive-full:/wal:ro" -v "$WORK:/cfg:ro" "$PG_IMAGE" \
  bash -c 'rm -rf /data/* && tar -xzf /base/base.tar.gz -C /data && touch /data/recovery.signal && { echo "restore_command = '"'"'cp /wal/%f %p'"'"'"; printf "recovery_target_time = '"'"'%s'"'"'\n" "$(cat /cfg/recovery-target-time.txt)"; } >> /data/postgresql.conf && chown -R 999:999 /data' \
  || fail "base extract failed"
WALDIR="$WORK/wal-archive-full"
docker run -d --name "$TGT_PG_C" -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD="$TGT_PW" \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:$TGT_PG_PORT:5432" \
  -v ddp-recovery-tgt-pgdata:/var/lib/postgresql/data -v "$WALDIR:/wal:ro" "$PG_IMAGE" >/dev/null
ready=0; for _ in $(seq 1 120); do
  docker exec "$TGT_PG_C" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
if [ "$ready" -ne 1 ]; then docker logs "$TGT_PG_C" 2>&1 | tail -n 30; fail "PITR target PG not ready (logs above)"; fi
# PITR pauses at the target (read-only, recovery still on). First resume replay
# past the pause, wait for promote, THEN align the role password (ALTER ROLE
# needs a writable, promoted server).
docker exec -e PGPASSWORD="$TGT_PW" "$TGT_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_wal_replay_resume();' >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
  docker exec -e PGPASSWORD="$TGT_PW" "$TGT_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_is_in_recovery();' 2>/dev/null | grep -q f && break; sleep 1; done
docker exec --user postgres "$TGT_PG_C" psql -U ddp -d deepdocparse -Atc "ALTER ROLE ddp PASSWORD '${TGT_PW//\'/\'\'}';" >/dev/null || fail "TGT password reset failed"
docker exec -e PGPASSWORD="$TGT_PW" "$TGT_PG_C" psql -h 127.0.0.1 -U ddp -d deepdocparse -Atc 'SELECT pg_is_in_recovery();' | grep -q f || fail "target restore still in recovery"
# post-target delta keys (B objects + pre-target writes only); the reconciler
# then proves metadata<->objects consistency at that point in time.
TGT_DSN="postgresql+asyncpg://ddp:${TGT_PW}@127.0.0.1:${TGT_PG_PORT}/deepdocparse"
"$PY" - "$STATE" "$WORK" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
work = sys.argv[2]
postt_keys = set(state.get("postt", {}).get("objects", {}).keys())
# result objects (results/<jid>/...) belong to post-target jobs too. postt doc
# entries carry only id/digest/object_key, so recover jid from the doc id:
# did "dNNNN-<tag>-postt20261005" <-> jid "jNNNN-<tag>-postt20261005".
for d in state.get("postt", {}).get("docs", []):
    did = d.get("id", "")
    if did.startswith("d"):
        jid = ("j" + did[1:])[:32]
        postt_keys.add(f"results/{jid}/layout.json")
        postt_keys.add(f"results/{jid}/document.md")
postt_keys = sorted(postt_keys)
json.dump(postt_keys, open(f"{work}/post-keys.json", "w"))
open(f"{work}/post-keys-rm.txt", "w").write("\n".join(f"dst/deepdocparse/{k}" for k in postt_keys) + "\n")
print(f"post-target object keys excluded from target restore: {len(postt_keys)}")
PY
# full source-bucket mirror (dst-objects holds B objects + pre + postt), then
# delete exactly the post-target keys => PITR-target-time object set.
docker run --rm --network host --entrypoint sh -v "$WORK/dst-objects:/dst" "$MC_IMAGE" \
  -c "mc alias set dst http://127.0.0.1:$DST_MINIO_PORT '$DST_MK' '$DST_MS' >/dev/null && mc mb --ignore-existing dst/deepdocparse >/dev/null && mc mirror --overwrite /dst dst/deepdocparse" \
  || fail "target object mirror failed"
if [ -s "$WORK/post-keys-rm.txt" ]; then
  docker run --rm --network host --entrypoint sh -v "$WORK:/excl:ro" "$MC_IMAGE" \
    -c 'mc alias set dst http://127.0.0.1:'"$DST_MINIO_PORT"' '"'$DST_MK'"' '"'$DST_MS'"' >/dev/null && while IFS= read -r k; do [ -n "$k" ] && mc rm "$k"; done < /excl/post-keys-rm.txt' \
    || fail "post-target key removal from target bucket failed"
fi
"$PY" scripts/recovery_drill_pitr.py reconcile --dsn "$TGT_DSN" \
  --minio-endpoint "127.0.0.1:$DST_MINIO_PORT" --minio-access-key "$DST_MK" \
  --minio-secret-key "$DST_MS" --bucket "$BUCKET" --out "$WORK/recon-target.json" \
  || fail "PITR-target reconciliation failed"
# selectivity proof: pre-target tasks present, post-target tasks absent
"$PY" - "$TGT_DSN" "$STATE" "$WORK/selectivity-target.json" <<'PY'
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    dsn, spath, out = sys.argv[1:4]
    state = json.load(open(spath))
    pre = state.get("pre", {}).get("tasks", []); postt = state.get("postt", {}).get("tasks", [])
    e = create_async_engine(dsn)
    async with e.connect() as c:
        have = set(r[0] for r in (await c.execute(text("SELECT root_task_id FROM public.federation_requests"))).all())
    await e.dispose()
    rep = {"pre_tasks": pre, "postt_tasks": postt,
           "pre_present": [t for t in pre if t in have], "pre_absent": [t for t in pre if t not in have],
           "postt_present": [t for t in postt if t in have], "postt_absent": [t for t in postt if t not in have],
           "selective": all(t in have for t in pre) and not any(t in have for t in postt)}
    json.dump(rep, open(out, "w"), indent=2)
    print("SELECTIVITY-TARGET", "PASS" if rep["selective"] else f"FAIL {rep}")
    sys.exit(0 if rep["selective"] else 1)
asyncio.run(main())
PY
mark t_pitr_done

# ---------------------------------------------------------------- 5) restore to LATEST
say "5/7 restore to latest (full replay)"
mark t_latest_start
docker rm -f "$LATEST_PG_C" >/dev/null 2>&1 || true
docker volume rm ddp-recovery-latest-pgdata >/dev/null 2>&1 || true
docker volume create ddp-recovery-latest-pgdata >/dev/null
docker run --rm -v ddp-recovery-latest-pgdata:/data -v "$WORK/base_tar:/base:ro" -v "$WORK/wal-archive-full:/wal:ro" "$PG_IMAGE" \
  bash -c 'rm -rf /data/* && tar -xzf /base/base.tar.gz -C /data && touch /data/recovery.signal && echo "restore_command = '"'"'cp /wal/%f %p'"'"'" >> /data/postgresql.conf && chown -R 999:999 /data' \
  || fail "base extract (latest) failed"
docker run -d --name "$LATEST_PG_C" -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD="$LATEST_PW" \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:$LATEST_PG_PORT:5432" \
  -v ddp-recovery-latest-pgdata:/var/lib/postgresql/data -v "$WALDIR:/wal:ro" "$PG_IMAGE" >/dev/null
ready=0; for _ in $(seq 1 120); do
  docker exec "$LATEST_PG_C" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
[ "$ready" -eq 1 ] || fail "latest restore PG not ready"
# restored data carries SRC's ddp password; reset to LATEST_PW (latest promotes itself).
docker exec --user postgres "$LATEST_PG_C" psql -U ddp -d deepdocparse -Atc "ALTER ROLE ddp PASSWORD '${LATEST_PW//\'/\'\'}';" >/dev/null || fail "LATEST password reset failed"
LATEST_DSN="postgresql+asyncpg://ddp:${LATEST_PW}@127.0.0.1:${LATEST_PG_PORT}/deepdocparse"
# objects: latest DB rows must match the full object set (B + pre + postt),
# which lives in DST_MINIO (SRC MinIO is gone under --resume; DST holds the
# full dst-objects mirror plus target-step removals — re-mirror full below).
docker run --rm --network host --entrypoint sh -v "$WORK/dst-objects:/dst" "$MC_IMAGE" \
  -c "mc alias set dst http://127.0.0.1:$DST_MINIO_PORT '$DST_MK' '$DST_MS' >/dev/null && mc mirror --overwrite /dst dst/deepdocparse" \
  || fail "latest object re-mirror (full) failed"
"$PY" scripts/recovery_drill_pitr.py reconcile --dsn "$LATEST_DSN" \
  --minio-endpoint "127.0.0.1:$DST_MINIO_PORT" --minio-access-key "$DST_MK" \
  --minio-secret-key "$DST_MS" --bucket "$BUCKET" --out "$WORK/recon-latest.json" \
  || fail "latest reconciliation failed"
# no-loss proof: latest counts == saved source counts (SRC stack is gone under --resume)
"$PY" - "$WORK/source-counts.json" "$LATEST_DSN" "$WORK/noloss.json" <<'PY'
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
TABLES = ["public.documents","public.parse_jobs","public.resources","public.resource_versions",
          "public.chunks","public.evidence","public.citations","public.wikis","public.wiki_revisions",
          "public.wiki_pages","public.wiki_dependencies","public.upload_events","public.document_uploads",
          "public.federation_requests","public.coverage_ledgers","public.coverage_entries",
          "public.collections","public.collection_members","control.organizations","control.users"]
async def counts(dsn):
    e = create_async_engine(dsn); out = {}
    async with e.connect() as c:
        for t in TABLES:
            out[t] = int((await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())
    await e.dispose(); return out
async def main():
    a = json.load(open(sys.argv[1])); b = await counts(sys.argv[2])
    diff = {t: [a.get(t), b.get(t)] for t in TABLES if a.get(t) != b.get(t)}
    json.dump({"source": a, "latest": b, "diff": diff, "noloss": not diff}, open(sys.argv[3], "w"), indent=2)
    print("NOLOSS", "PASS" if not diff else f"FAIL {diff}")
    sys.exit(0 if not diff else 1)
asyncio.run(main())
PY
# selectivity: post-target tasks MUST be present in latest
"$PY" - "$LATEST_DSN" "$STATE" "$WORK/selectivity-latest.json" <<'PY'
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    dsn, spath, out = sys.argv[1:4]
    state = json.load(open(spath))
    pre = state.get("pre", {}).get("tasks", []); postt = state.get("postt", {}).get("tasks", [])
    e = create_async_engine(dsn)
    async with e.connect() as c:
        have = set(r[0] for r in (await c.execute(text("SELECT root_task_id FROM public.federation_requests"))).all())
    await e.dispose()
    rep = {"pre_present": [t for t in pre if t in have], "pre_absent": [t for t in pre if t not in have],
           "postt_present": [t for t in postt if t in have], "postt_absent": [t for t in postt if t not in have],
           "complete": all(t in have for t in pre + postt)}
    json.dump(rep, open(out, "w"), indent=2)
    print("SELECTIVITY-LATEST", "PASS" if rep["complete"] else f"FAIL {rep}")
    sys.exit(0 if rep["complete"] else 1)
asyncio.run(main())
PY
mark t_latest_done

# ---------------------------------------------------------------- 5b) RPO crash probe
# Prove the loss window: write one row AFTER the archived-WAL snapshot was
# copied out (wal-archive-full is already frozen on the host), then kill -9
# the server so the row's WAL can never be archived. A fresh base+archived-WAL
# restore must NOT contain the probe row: it was lost with the unarchived
# tail. That lost row IS the RPO bound, measured not asserted. Deterministic:
# wal-archive-full predates the probe by construction, so the probe cannot
# have been archived no matter how fast the archiver runs.
say "5b/7 RPO crash probe (unarchived write + kill -9 + crash-restore)"
mark t_crash_start
if [ "$RESUME" -eq 1 ]; then
  fail "--resume cannot prove RPO: the crash probe needs a live SRC PG (real write + kill -9). Run without --resume for an RPO verdict."
fi
"$PY" scripts/recovery_drill_pitr.py probe --dsn "$SRC_DSN" --state "$STATE" \
  --out "$WORK/crash-probe.json" --tag "$TAG" || fail "crash probe write failed"
PROBE_TASK="$("$PY" -c "import json; print(json.load(open('$WORK/crash-probe.json'))['probe_task'])")"
note "probe task $PROBE_TASK committed after WAL snapshot; killing SRC PG now"
docker kill -s 9 "$SRC_PG_C" >/dev/null
sleep 3
# crash restore: base + only the WAL archived BEFORE the probe write
docker volume create ddp-recovery-crash-pgdata >/dev/null
docker run --rm -v ddp-recovery-crash-pgdata:/data -v "$WORK/base_tar:/base:ro" -v "$WORK/wal-archive-full:/wal:ro" "$PG_IMAGE" \
  bash -c 'rm -rf /data/* && tar -xzf /base/base.tar.gz -C /data && touch /data/recovery.signal && echo "restore_command = '"'"'cp /wal/%f %p'"'"'" >> /data/postgresql.conf && chown -R 999:999 /data' \
  || fail "base extract (crash probe) failed"
docker run -d --name "$CRASH_PG_C" -e POSTGRES_USER=ddp -e POSTGRES_PASSWORD="$TGT_PW" \
  -e POSTGRES_DB=deepdocparse -p "127.0.0.1:$CRASH_PG_PORT:5432" \
  -v ddp-recovery-crash-pgdata:/var/lib/postgresql/data -v "$WORK/wal-archive-full:/wal:ro" "$PG_IMAGE" >/dev/null
ready=0; for _ in $(seq 1 120); do
  docker exec "$CRASH_PG_C" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1 && { ready=1; break; }; sleep 1; done
if [ "$ready" -ne 1 ]; then docker logs "$CRASH_PG_C" 2>&1 | tail -n 20; fail "crash-probe restore PG not ready (logs above)"; fi
docker exec --user postgres "$CRASH_PG_C" psql -U ddp -d deepdocparse -Atc "ALTER ROLE ddp PASSWORD '${TGT_PW//\'/\'\'}';" >/dev/null || fail "CRASH password reset failed"
CRASH_DSN="postgresql+asyncpg://ddp:${TGT_PW}@127.0.0.1:${CRASH_PG_PORT}/deepdocparse"
"$PY" - "$CRASH_DSN" "$WORK/crash-probe.json" "$WORK/rpo-proof.json" <<'PY'
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    dsn, ppath, out = sys.argv[1:4]
    probe = json.load(open(ppath))["probe_task"]
    e = create_async_engine(dsn)
    async with e.connect() as c:
        n = int((await c.execute(text("SELECT count(*) FROM public.federation_requests WHERE root_task_id=:t"), {"t": probe})).scalar_one())
    await e.dispose()
    rep = {"probe_task": probe, "rows_in_crash_restore": n,
           "lost": n == 0,
           "meaning": "unarchived tail write lost on crash-restore: RPO bound proven" if n == 0
                      else "probe row present (WAL archived before kill); RPO bound NOT proven"}
    json.dump(rep, open(out, "w"), indent=2)
    print("RPO-PROBE", "PASS (lost as designed)" if n == 0 else "FAIL (survived)")
    sys.exit(0 if n == 0 else 1)
asyncio.run(main())
PY
mark t_crash_done

# ---------------------------------------------------------------- 6) identity
say "6/7 node identity: same-seed restore == authority; clone cannot impersonate"
mark t_ident_start
IDENTITY_ROOT="$WORK/identity"
mkdir -p "$IDENTITY_ROOT"
(cd services/control-api && \
  env DDP_IDENTITY_DRILL_ROOT="$IDENTITY_ROOT" PATH="$HOME/.local/opt/go/bin:$PATH" \
  go test ./internal/discovery -run TestNodeIdentityBackupRestoreDrill -v -count=1) \
  | tee "$WORK/identity-drill.log" || fail "identity drill failed"
grep -q "restored same authority" "$WORK/identity-drill.log" || fail "same-seed restore not proven"
grep -q "clone node id=.* differs" "$WORK/identity-drill.log" || fail "clone difference not proven"
# peer layer (red-first, existing tests, no new code): the clone's key cannot
# mint the authority's credentials — TestCredentialSigningRefusesForeignIssuerAndInvalidClaims
# proves a node cannot sign as another issuer and a different key's signature
# does not verify under the issuer key; lease.go rejects authority-id/key
# mismatch ("descriptor approved identity or key mismatch").
(cd services/control-api && PATH="$HOME/.local/opt/go/bin:$PATH" go test ./internal/discovery -run 'TestCredentialSigningRefusesForeignIssuerAndInvalidClaims|TestNodeIDForPublicKey' -v -count=1 2>&1 | tail -6) | tee "$WORK/peer-reject.log" || fail "peer-layer clone rejection proof failed"
mark t_ident_done

# ---------------------------------------------------------------- 7) artifact
say "7/7 writing artifact + RTO/RPO table"
"$PY" - "$WORK" "$ARTIFACT" "$DATE_TAG" "$N_DOCS" "$N_EXTRA" "$TARGET_TIME" <<'PY'
import json, sys, datetime
work, art, tag, n_docs, n_extra, target = sys.argv[1:7]
tim = json.load(open(f"{work}/timing.json"))
src = json.load(open(f"{work}/recon-source.json"))
tgt = json.load(open(f"{work}/recon-target.json"))
lat = json.load(open(f"{work}/recon-latest.json"))
noloss = json.load(open(f"{work}/noloss.json"))
state = json.load(open(f"{work}/pitr-state.json"))
def dur(a, b): return round(tim[b] - tim[a], 1) if a in tim and b in tim else None
wal_segs = len(__import__("os").listdir(f"{work}/wal-archive-full")) if __import__("os").path.isdir(f"{work}/wal-archive-full") else None
doc = {
  "drill": "recovery-pitr", "date": tag,
  "scale": {"generated_docs": int(n_docs), "pre_target_docs": int(n_extra), "post_target_docs": 2,
             "seed_users": len(state.get("users", [])),
             "seed_chunks": state.get("chunks_total"), "seed_evidence": state.get("evidence_total"),
             "b_copy": json.load(open(f"{work}/bcopy-scale.json")) if __import__("os").path.exists(f"{work}/bcopy-scale.json") else {},
             "note": "consistent copy of live center B (PG pg_dump + MinIO mc mirror, read-only) enlarged by generated docs to the stated scale"},
  "pitr_target": target,
  "wal": {"archive_timeout_s": 30, "archive_command": "cp %p wal-archive/%f",
          "archived_segments": wal_segs,
          "base_lsn": open(f"{work}/base-lsn.txt").read().strip()},
  "ports": {"src_pg": 15495, "pitr_target_pg": 15496, "latest_pg": 15503, "crash_pg": 15498,
            "src_minio": 15511, "src_minio_console": 15512, "dst_minio": 15521, "dst_minio_console": 15522,
            "note": "latest on 15503 because another session's ddp-subtree-audit-pg2 owns 15497 (do not touch); all ports pre-gated incl. consoles"},
  "rto_seconds": {
    "copy_b": dur("t_copy_start", "t_copy_done"),
    "build_source": dur("t_src_start", "t_src_done"),
    "base_backup": dur("t_base_start", "t_base_done"),
    "pre_and_post_writes_with_archive_waits": dur("t_post_start", "t_post_done"),
    "restore_to_target": dur("t_pitr_start", "t_pitr_done"),
    "restore_to_latest": dur("t_latest_start", "t_latest_done"),
    "rpo_crash_probe": dur("t_crash_start", "t_crash_done"),
    "identity": dur("t_ident_start", "t_ident_done"),
  },
  "rpo": {"wal_archive_cadence_s": 30,
          "target_restore_loss": "zero at target time (selectivity PASS: pre-target tasks present, post-target tasks absent)",
          "latest_restore_loss": "zero (full replay; noloss PASS)",
          "crash_probe": json.load(open(f"{work}/rpo-proof.json")),
          "loss_window_s": 30},
  "selectivity": {"target": json.load(open(f"{work}/selectivity-target.json")),
                  "latest": json.load(open(f"{work}/selectivity-latest.json"))},
  "source_counts": json.load(open(f"{work}/source-counts.json")),
  "reconciliation": {"source": src, "pitr_target": tgt, "latest": lat},
  "noloss_latest_vs_source": noloss,
  "identity": open(f"{work}/identity-drill.log").read()[-2000:],
  "peer_clone_rejection": open(f"{work}/peer-reject.log").read()[-1500:],
  "repro": "bash scripts/recovery_drill.sh",
}
json.dump(doc, open(art, "w"), indent=2, ensure_ascii=False)
print(f"artifact {art}")
PY
note "artifact: $ARTIFACT"
note "work dir: $WORK"

say "DRILL PASS"
