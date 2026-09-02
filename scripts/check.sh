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

if want go; then
  if command -v go >/dev/null; then
    run "go vet"   in_dir services/control-api go vet ./...
    run "go test"  in_dir services/control-api go test ./... -count=1
    run "gofmt"    bash -c 'cd services/control-api && [ -z "$(gofmt -l .)" ] || { gofmt -l .; false; }'
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
