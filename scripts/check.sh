#!/usr/bin/env bash
# 本地全量门禁 —— 与 CI 同一套判据，跑一条命令看全。
#
#   scripts/check.sh            # 全跑
#   scripts/check.sh guards     # 只跑守卫
#   scripts/check.sh python go web
#
# **不 set -e**：一处红就停会让人只看到第一个问题，然后修一个跑一遍。
# 这里全部跑完再汇总 —— 一次看到全部问题比早停有用得多。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
export PATH="$HOME/.local/opt/go/bin:$PATH"

FAILED=()
PASSED=()

run() {
  local name="$1"; shift
  printf '\n\033[1m>>> %s\033[0m\n' "$name"
  if "$@"; then
    PASSED+=("$name")
  else
    FAILED+=("$name")
  fi
}

in_dir() {
  local dir="$1"; shift
  ( cd "$dir" && "$@" )
}

want() {
  [ $# -eq 0 ] && return 0
  local target="$1"; shift
  for arg in "${WANTED[@]}"; do
    [ "$arg" = "$target" ] && return 0
  done
  return 1
}

WANTED=("$@")
[ ${#WANTED[@]} -eq 0 ] && WANTED=(guards python go web)

if want guards; then
  run "契约生成物"        "$PY" packages/contracts/scripts/generate.py --check
  run "契约守卫"          "$PY" scripts/check_contract.py
  run "数据所有权"        "$PY" scripts/check_data_ownership.py
  run "块类型判据"        "$PY" scripts/check_blocktype_parity.py
  run "分块回归"          "$PY" scripts/check_chunk_regression.py
  run "枚举用法"          "$PY" scripts/check_enum_usage.py
  run "control 迁移同步"  "$PY" scripts/check_control_migrations.py
  run "配置参考文档"      "$PY" scripts/gen_config_docs.py --check
  run "架构守卫"          "$PY" -m pytest -q
fi

if want python; then
  run "ddp_core"       in_dir python/ddp_core        "$PY" -m pytest -q
  run "model-gateway"  in_dir services/model-gateway "$PY" -m pytest -q
  run "corpus-api"     in_dir services/corpus-api    "$PY" -m pytest -q
  run "corpus-worker"  in_dir services/corpus-worker "$PY" -m pytest -q
  run "mcp"            in_dir services/mcp           "$PY" -m pytest -q
  run "eval"           in_dir eval                   "$PY" -m pytest -q
fi

# 数据所有权的**物理**验证要有真库，本机 dev 库起着就顺手跑一遍。
# 没起就说出来 —— 静默跳过与真的绿长得一模一样
if want guards; then
  if docker exec "${DDP_PG_CONTAINER:-ddp-postgres-1}" true 2>/dev/null; then
    run "数据所有权（真库）" ./scripts/check_db_boundary.sh
  else
    printf '\033[33m    注意：dev 的 postgres 没起，check_db_boundary.sh 跳过\033[0m\n'
    printf '\033[2m    它验的是"越界 SQL 会不会被数据库拒绝"，与静态守卫互补；CI 里是必跑的\033[0m\n'
  fi
fi

if want go; then
  if command -v go >/dev/null; then
    run "go vet"   in_dir services/control-api go vet ./...
    run "go test"  in_dir services/control-api go test ./... -count=1
    run "gofmt"    bash -c 'cd services/control-api && [ -z "$(gofmt -l .)" ] || { gofmt -l .; false; }'
    # go.mod/go.sum 少一条 **本机不一定红** —— 本机的 module cache 里已经有那个模块了。
    # 只有干净环境（容器构建、CI）才会报 "missing go.sum entry"，而那时已经在部署路上了。
    # 2026-09-02 就是这么发现少了 puddle/v2 的：本机 go build 绿，镜像构建第一步就炸
    run "go mod tidy" in_dir services/control-api go mod tidy -diff
    # 计量聚合只能对着真 PostgreSQL 验（SQL 里的 date_trunc / make_interval
    # 没有可替代的假实现）。dev 库起着就连上去跑，没起就**说出来**——
    # 那几条会 t.Skip，而 skip 与 pass 在 `go test` 的总结里长得一模一样
    if [ -n "${CONTROL_TEST_DATABASE_URL:-}" ]; then
      printf '\033[2m    （计量用例连着 %s）\033[0m\n' "${CONTROL_TEST_DATABASE_URL%%\?*}"
    else
      printf '\033[33m    注意：没有 CONTROL_TEST_DATABASE_URL，internal/store 的 4 条计量用例被跳过\033[0m\n'
      printf '\033[2m    起 dev 库后：scripts/dev.sh up postgres 并导出该变量；CI 里是必跑的\033[0m\n'
    fi
  else
    # **显式报缺，不静默跳过**：静默跳过的绿与真的绿长得一模一样
    printf '\033[33m>>> 跳过 Go：PATH 上没有 go\033[0m\n'
    FAILED+=("go（工具链缺失）")
  fi
fi

if want web; then
  if [ -d apps/web/node_modules ]; then
    run "前端类型检查"  in_dir apps/web npm run --silent type-check
    run "前端单测"      in_dir apps/web npx vitest run
  else
    printf '\033[33m>>> 跳过前端：apps/web/node_modules 不存在（npm ci）\033[0m\n'
    FAILED+=("前端（依赖未安装）")
  fi
fi

printf '\n\033[1m===== 汇总 =====\033[0m\n'
for name in "${PASSED[@]}"; do printf '  \033[32mPASS\033[0m %s\n' "$name"; done
for name in "${FAILED[@]}"; do printf '  \033[31mFAIL\033[0m %s\n' "$name"; done
printf '通过 %d / 失败 %d\n' "${#PASSED[@]}" "${#FAILED[@]}"
[ ${#FAILED[@]} -eq 0 ]
