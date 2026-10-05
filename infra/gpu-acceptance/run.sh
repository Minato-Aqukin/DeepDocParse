#!/usr/bin/env bash
# GPU acceptance on a fresh NVIDIA Linux host (AutoDL 4090D-class target).
# One command for the human (after exporting AUTODL_TOKEN); everything else
# is prompted, guarded, and auto-shut down.
#
#   export AUTODL_TOKEN=...; bash infra/gpu-acceptance/run.sh [--dry-run-local] [--yes]
#
# Auth: AUTODL_TOKEN is optional. When it is unset, the script probes
# `autodl balance --json`: if the CLI authenticates from its own config file,
# the paid run continues (the token value itself is never printed). Only when
# the CLI has no working auth does the script fall back to the local dry-run.
# --yes skips the interactive spend confirmation for non-interactive runs;
# without it the prompt stays the default.
#
# Cost (attested): infra/autodl/README.md 2026-08-25 run took ~115 min on 4090D;
# SINGLE-CENTER-WEB-PLAN §4F + artifacts/core-f-gpu-retest-20260925.json record
# ¥1.88/h with ~32 min costing ¥0.96. ESTIMATE for this kit: 60-90 min,
# roughly ¥1.9-2.8 at that rate. Without AUTODL_TOKEN only the local dry-run
# runs (no spending). CLI surface verified against `autodl <cmd> --help` and
# /home/minatoaqukin/Projects/AutoDL-cli/src (commands/ssh.ts push/pull/exec,
# commands/instances.ts create/list/stop/release, guard ttl; GPU names resolve
# case-insensitively, e.g. 4090D == 4090d).
set -euo pipefail
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$KIT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

GPU="${GPU:-4090D}"
# 4090D 24G is the kit's target class on purpose: the T19 OOM probe sizes ctx
# for ~30 GiB KV that must NOT fit. A 48G card could fit it and would flip a
# real verdict, so do not "upgrade" past 24G without resizing that probe.
IMAGE="${IMAGE:-base-image-l2t43iu6uk}"
# No 22.04+ public base image exists: `autodl images --base` (2026-10-06)
# lists 13 images, all Ubuntu 16.04/18.04/20.04 (newest glibc 2.31), and the
# CLI catalog (AutoDL-cli src/core/catalog.ts) bakes in the same 13. The
# create API takes only a UUID, so the kit keeps the CLI default and relies
# on the host_incompatible preflight (remote script + kit) to fail fast
# instead of reporting model_start_failed. Re-check with
# `autodl images --base --json` before changing this default.
TTL="${TTL:-90m}"
DISK="${DISK:-50}"
NAME="${NAME:-ddp-gpu-acceptance}"
ID_FILE=".dev-logs/gpu-acceptance/instance"
DRY_RUN_LOCAL=0
ASSUME_YES=0
STAGE_ONLY=""
for arg in "$@"; do case "$arg" in
  --dry-run-local) DRY_RUN_LOCAL=1 ;;
  --yes|-y) ASSUME_YES=1 ;;
  --stage-only=*) STAGE_ONLY="${arg#--stage-only=}" ;;
  *) echo "unknown flag: $arg (expected --dry-run-local, --yes or --stage-only=DIR)" >&2; exit 2 ;;
esac; done

# Build exactly the tree that is pushed to the host: the kit's subset of
# tracked HEAD plus REVISION. --stage-only=DIR runs this locally and exits
# before any cloud call, so the staging path is checked before money is spent.
stage_tree() {
  local stage="$1"
  git archive HEAD infra/gpu-acceptance scripts services/model-gateway \
    services/corpus-api services/corpus-worker python/ddp_contracts \
    python/ddp_core python/ddp_local docs/refactor \
    tests packages/contracts -o "$stage/repo.tar"
  mkdir -p "$stage/tree"
  tar -xf "$stage/repo.tar" -C "$stage/tree"
  rm -f "$stage/repo.tar"
  git rev-parse HEAD > "$stage/tree/REVISION"
  for required in REVISION infra/gpu-acceptance/gpu_acceptance_remote.py scripts/gpu_acceptance.py \
                  python/ddp_local/ddp_local/model_runtime/catalog.json \
                  packages/contracts/generated/schemas-resolved.json tests/ddp_bundle_fixture.py; do
    [[ -f "$stage/tree/$required" ]] || { echo "staged tree is missing $required" >&2; return 1; }
  done
}
if [[ -n "$STAGE_ONLY" ]]; then
  mkdir -p "$STAGE_ONLY"
  stage_tree "$STAGE_ONLY" && echo "[gpu-acceptance] staged $(find "$STAGE_ONLY/tree" -type f | wc -l) files at $STAGE_ONLY/tree"
  exit $?
fi

if ! command -v autodl >/dev/null 2>&1; then
  echo "autodl CLI not found; install it or run scripts/gpu_acceptance.py --mode dry-run locally." >&2
  exit 2
fi

CLI_AUTH_OK=0
if autodl balance --json >/dev/null 2>&1; then CLI_AUTH_OK=1; fi

if [[ "$DRY_RUN_LOCAL" == 1 ]] || { [[ -z "${AUTODL_TOKEN:-}" ]] && [[ "$CLI_AUTH_OK" == 0 ]]; }; then
  echo "[gpu-acceptance] no working CLI auth (and/or --dry-run-local): local dry-run only, no spending."
  exec .venv/bin/python scripts/gpu_acceptance.py --mode dry-run
fi

# Exclusive local lock for the whole paid run. The name guard below is
# check-then-create: two local invocations racing it both see "no instance"
# and both create (this happened with two background loops on 2026-10-05,
# billing two instances at once). flock serializes that window; the second
# invocation fails fast instead of spending. The lock releases on exit, so a
# later re-run is unaffected.
LOCK_FILE=".dev-logs/gpu-acceptance/run.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[gpu-acceptance] another run.sh holds $LOCK_FILE; refusing a concurrent paid run." >&2
  exit 2
fi

find_by_name() {
  GPU_ACCEPTANCE_NAME="$NAME" autodl list --json 2>/dev/null | python3 -c "
import json,os,sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
items = data if isinstance(data, list) else data.get('data', data.get('instances', []))
want = os.environ.get('GPU_ACCEPTANCE_NAME', '')
for it in items if isinstance(items, list) else []:
    if isinstance(it, dict) and it.get('name') == want and it.get('status') not in ('released', 'deleted'):
        print(it.get('uuid') or it.get('id') or '')
" | head -1
}

cleanup() {
  target="${INSTANCE:-}"
  if [[ -z "$target" && -f "$ID_FILE" ]]; then target="$(cat "$ID_FILE")"; fi
  if [[ -z "$target" ]]; then target="$(find_by_name || true)"; fi
  if [[ -n "$target" ]]; then
    echo "[gpu-acceptance] releasing $target ..."
    autodl stop "$target" || true
    # Verified semantics (AutoDL-cli src/commands/instances.ts): release refuses
    # a running instance unless --force (which powers off and waits for
    # shutdown first); -y skips the confirm. Both flags exist in --help.
    autodl release -y --force "$target" || true
    rm -f "$ID_FILE"
  fi
}
trap cleanup EXIT

echo "=== GPU acceptance: paid cloud GPU ahead ==="
echo "GPU=$GPU IMAGE=$IMAGE TTL=$TTL DISK=${DISK}G NAME=$NAME"
echo "ESTIMATE (not measured): 60-90 min on 4090D-class, roughly ¥1.9-2.8 at the attested ¥1.88/h."
autodl balance || true
EXISTING="$(find_by_name || true)"
if [[ -n "$EXISTING" ]]; then
  echo "An instance named $NAME already exists ($EXISTING); refusing to create a duplicate." >&2
  echo "Re-run after releasing it, or set NAME= to use a different name." >&2
  exit 2
fi
if [[ "$ASSUME_YES" == 1 ]]; then
  echo "[gpu-acceptance] --yes: proceeding without an interactive prompt."
else
  read -r -p "Create the instance and spend this money? [yes/NO] " answer
  if [[ "$answer" != "yes" ]]; then echo "aborted; nothing created."; exit 0; fi
fi

INSTANCE=""
echo "[gpu-acceptance] creating instance (gpu $GPU, image $IMAGE, ttl $TTL auto-shutdown)..."
CREATE_OUT=""
CREATE_RC=0
CREATE_OUT="$(autodl create --json --gpu "$GPU" --image "$IMAGE" --disk "$DISK" --ttl "$TTL" --wait --name "$NAME")" \
  || CREATE_RC=$?
if [[ $CREATE_RC -ne 0 ]]; then
  echo "$CREATE_OUT" >&2
  INSTANCE="$(find_by_name || true)"
  echo "create failed; the EXIT trap releases '${INSTANCE:-nothing found by name}'." >&2
  exit 1
fi
echo "$CREATE_OUT"
INSTANCE="$(printf '%s' "$CREATE_OUT" | python3 -c "
import json,sys
try:
    data = json.loads(sys.stdin.read())
except Exception:
    data = None
inner = (data or {}).get('data', data) if isinstance(data, dict) else data
if isinstance(inner, dict):
    print(inner.get('uuid') or inner.get('id') or '')
" || true)"
if [[ -z "$INSTANCE" ]]; then
  INSTANCE="$(printf '%s' "$CREATE_OUT" | grep -oE 'pro-[a-z0-9]+' | head -1)"
fi
if [[ -z "$INSTANCE" ]]; then
  INSTANCE="$(find_by_name || true)"
fi
if [[ -z "$INSTANCE" ]]; then
  echo "create reported success but no instance id found; refusing to continue." >&2
  exit 1
fi
mkdir -p "$(dirname "$ID_FILE")"
printf '%s' "$INSTANCE" > "$ID_FILE"
autodl guard ttl "$INSTANCE" "$TTL" || true

echo "[gpu-acceptance] pushing kit subset of tracked HEAD (git archive)..."
# Subset, not the whole tree: the full archive is ~36MB/1400 files and the
# first attempt died mid-push at file ~295/1245 on a dropped SSH session.
# This subset is everything the remote kit imports: the kit scripts, the
# three python packages, the three services the kit installs and runs
# (gateway + corpus-api/corpus-worker for the T59 regression subsets),
# the matrix docs dir the artifact path lives under, plus the two outside
# trees the T59 subsets read: tests/ (ddp_bundle_fixture.py + ddp_paths.py,
# resolved via each service's pyproject pythonpath) and packages/contracts
# (generated/schemas-resolved.json + openapi/federation-tasks-v1.yaml +
# schemas/ddp-discovery/v1.json, read via parents[3]-relative paths).
STAGE="$(mktemp -d)"
stage_tree "$STAGE"
# One file, not ~700: per-file SFTP sessions kept dropping ("SSH 连接已意外断开")
# mid-push. Wait until SSH answers, push a single archive with retries, verify
# its digest on the host, then unpack there. Any failure ends in the EXIT trap,
# which releases the instance.
tar -czf "$STAGE/kit.tar.gz" -C "$STAGE/tree" .
KIT_SHA="$(sha256sum "$STAGE/kit.tar.gz" | cut -d' ' -f1)"
ssh_ready=0
for _ in $(seq 1 24); do
  if autodl exec "$INSTANCE" true >/dev/null 2>&1; then ssh_ready=1; break; fi
  sleep 5
done
[[ "$ssh_ready" == 1 ]] || { echo "[gpu-acceptance] SSH never became ready" >&2; exit 1; }
pushed=0
for attempt in 1 2 3 4 5; do
  if autodl push "$INSTANCE" "$STAGE/kit.tar.gz" /root/kit.tar.gz 2>&1 | tail -2 \
     && [[ "$(autodl exec "$INSTANCE" 'sha256sum /root/kit.tar.gz' 2>/dev/null | cut -d' ' -f1)" == "$KIT_SHA" ]]; then
    pushed=1; break
  fi
  echo "[gpu-acceptance] push attempt $attempt failed; retrying in 10 s" >&2
  sleep 10
done
[[ "$pushed" == 1 ]] || { echo "[gpu-acceptance] could not push the kit archive" >&2; exit 1; }
autodl exec "$INSTANCE" "rm -rf /root/gpu-acceptance && mkdir -p /root/gpu-acceptance && tar -xzf /root/kit.tar.gz -C /root/gpu-acceptance && cat /root/gpu-acceptance/REVISION"
rm -rf "$STAGE"

echo "[gpu-acceptance] running kit on the GPU host..."
autodl exec "$INSTANCE" 'nvidia-smi -L && python3 --version'
# Remote command is one shell string; TTL is baked in by the local shell
# (single quotes would prevent expansion — the old "$0" bug).
REMOTE_RC=0
autodl exec --timeout 120m "$INSTANCE" \
  "cd /root/gpu-acceptance && python3 infra/gpu-acceptance/gpu_acceptance_remote.py --ttl $TTL" \
  || REMOTE_RC=$?
echo "[gpu-acceptance] remote kit exit: $REMOTE_RC"

echo "[gpu-acceptance] pulling artifact (even on failure)..."
ARTIFACT="$(autodl exec "$INSTANCE" 'ls -t /root/gpu-acceptance/docs/refactor/artifacts/gpu-acceptance-*.json 2>/dev/null | head -1' || true)"
if [[ -n "$ARTIFACT" ]]; then
  mkdir -p docs/refactor/artifacts
  if autodl pull "$INSTANCE" "$ARTIFACT" docs/refactor/artifacts/; then
    .venv/bin/python scripts/gpu_acceptance.py --summarize "docs/refactor/artifacts/$(basename "$ARTIFACT")" || true
  else
    echo "artifact pull failed: $ARTIFACT" >&2
    [[ "$REMOTE_RC" -eq 0 ]] && REMOTE_RC=1
  fi
else
  echo "no artifact found on host." >&2
  [[ "$REMOTE_RC" -eq 0 ]] && REMOTE_RC=1
fi
exit "$REMOTE_RC"
