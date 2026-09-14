#!/usr/bin/env bash
# Real-PostgreSQL validation for the P5 federation plane (queue/admission/
# execution/delivery) and the P6 cache it rides on.
#
# Creates a throwaway pgvector container on a distinct high port (15460-15469),
# migrates the real alembic chain into it, runs the opt-in PG suites against it
# and removes the container again — repeatable, self-contained, and it never
# touches the dev database or its ports (15432/15439/15450/15455) or volumes.
#
# Usage:
#   bash scripts/check_federation_pg.sh
#   FEDERATION_PG_PORT=15468 bash scripts/check_federation_pg.sh   # 15460-15469
#
# The opt-in suites skip loudly when the env var is absent, so the default
# corpus-api suite stays green without docker; this script is what sets it.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
ALEMBIC="$ROOT/.venv/bin/alembic"
CONTAINER="ddp-r2c-pg"
IMAGE="pgvector/pgvector:pg16"
PORT="${FEDERATION_PG_PORT:-15467}"
DSN="postgresql+asyncpg://ddp:ddp@127.0.0.1:${PORT}/deepdocparse"
FAILURES=0

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step() {
  local label="$1"
  shift
  printf '\n=== %s ===\n' "$label"
  if "$@"; then
    printf '=== %s: PASS ===\n' "$label"
  else
    local rc=$?
    printf '=== %s: FAIL (exit %s) ===\n' "$label" "$rc"
    FAILURES=$((FAILURES + 1))
  fi
}

run_alembic() {
  (cd "$ROOT/database/corpus" && env DATABASE_URL="$DSN" "$ALEMBIC" -c alembic.ini "$@")
}

run_pytest() {
  (cd "$ROOT/services/corpus-api" && env \
    FEDERATION_TEST_DATABASE_URL="$DSN" CORPUS_TEST_DATABASE_URL="$DSN" \
    COLLECTION_CATALOG_TEST_DATABASE_URL="$DSN" CENTER_CLIENT_TEST_DATABASE_URL="$DSN" \
    CACHE_TEST_DATABASE_URL="$DSN" \
    "$PY" -m pytest -v -p no:cacheprovider "$@")
}

# ---------------------------------------------------------------- preflight
case "$PORT" in
  1546[0-9]) ;;
  *) echo "FEDERATION_PG_PORT must be in 15460-15469 (got '$PORT')" >&2; exit 2 ;;
esac
command -v docker >/dev/null 2>&1 || { echo "docker is not installed" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker daemon is not reachable" >&2; exit 2; }
[ -x "$PY" ] || { echo "$PY not found (create .venv first)" >&2; exit 2; }
[ -x "$ALEMBIC" ] || { echo "$ALEMBIC not found (create .venv first)" >&2; exit 2; }
# A crashed previous run may still hold the port; remove our own container first.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
if ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .; then
  echo "port $PORT is already in use; pick another one in 15460-15469" >&2
  exit 2
fi

# ---------------------------------------------------------------- scratch PG
echo "starting $CONTAINER ($IMAGE) on 127.0.0.1:$PORT"
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD=ddp -e POSTGRES_USER=ddp -e POSTGRES_DB=deepdocparse \
  -p "127.0.0.1:${PORT}:5432" "$IMAGE" >/dev/null || { echo "docker run failed" >&2; exit 2; }
READY=0
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U ddp -d deepdocparse >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "postgres did not become ready in 60s" >&2
  docker logs "$CONTAINER" 2>&1 | tail -20
  exit 2
fi
docker exec "$CONTAINER" psql -U ddp -d deepdocparse -tAc \
  "select version()" | sed 's/^/server: /'

# ---------------------------------------------------------------- migrate + test
step "alembic upgrade head" run_alembic upgrade head
step "alembic current" run_alembic current
step "alembic heads" run_alembic heads

step "P5/P6 federation PostgreSQL suites (new)" \
  run_pytest tests/test_federation_pg.py tests/test_federation_concurrency_pg.py
step "existing opt-in PG suites (regression: collection/client/cache)" \
  run_pytest tests/test_collection_catalog_pg.py tests/test_client_projection_pg.py \
  tests/test_cache_pg.py

# ---------------------------------------------------------------- summary
printf '\n================ check_federation_pg summary ================\n'
printf 'scratch container : %s (%s) on 127.0.0.1:%s, removed on exit\n' \
  "$CONTAINER" "$IMAGE" "$PORT"
printf 'database          : deepdocparse (user ddp), DSN %s\n' "$DSN"
printf 'suite result      : '
if [ "$FAILURES" -eq 0 ]; then
  printf 'ALL STEPS PASS\n'
  exit 0
fi
printf '%s STEP(S) FAILED\n' "$FAILURES"
exit 1
