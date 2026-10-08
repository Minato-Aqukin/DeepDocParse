#!/usr/bin/env bash
# 本地默认门禁；浏览器、真实服务与发行检查的边界见 docs/DEVELOPMENT.md。
#
#   scripts/check.sh            # 默认四组
#   scripts/check.sh guards     # 只跑守卫
#   scripts/check.sh python go web
#   scripts/check.sh web-e2e    # 可选：需要 Playwright Chromium
#
# **不 set -e**：一处红就停会让人只看到第一个问题，然后修一个跑一遍。
# 这里全部跑完再汇总 —— 一次看到全部问题比早停有用得多。
set -uo pipefail

CALLER_DIR="$PWD"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
用法：scripts/check.sh [guards|python|go|web|web-e2e ...] [--allow-without-db]
      scripts/check.sh --help

默认：guards python go web（依次运行，失败后继续汇总）。
  guards   契约、配置、架构等守卫；dev PostgreSQL 可用时追加数据库边界检查
  python   Ruff 与各 Python 包的单测
  go       vet、单测、gofmt、go mod tidy；真库用例需要 CONTROL_TEST_DATABASE_URL
  web      类型检查与生产构建、组件单测、共享连接层与桌面主机测试
  web-e2e  前端浏览器测试（含构建；需提前安装 Playwright Chromium）

  --allow-without-db  允许无真库时记为跳过而不失败（本地默认行为；CI 下不传则跳过视为失败）
无真库（dev postgres 未起 / 未设 CONTROL_TEST_DATABASE_URL）时相关检查记为跳过；
本地默认退出 0，CI 下跳过视为失败，除非传入 --allow-without-db。
PY 可指定解释器命令名或路径；相对路径基于调用目录。未设置时优先 .venv/bin/python。
完整验证范围与依赖见 docs/DEVELOPMENT.md。
USAGE
}

ALLOW_WITHOUT_DB=0
WANTED=()
for _arg in "$@"; do
  case "$_arg" in
    --allow-without-db) ALLOW_WITHOUT_DB=1 ;;
    *) WANTED+=("$_arg") ;;
  esac
done
unset _arg

if [ ${#WANTED[@]} -eq 1 ] && [ "${WANTED[0]}" = --help ]; then
  usage
  exit 0
fi
for arg in "${WANTED[@]}"; do
  case "$arg" in
    guards|python|go|web|web-e2e) ;;
    *) printf '未知检查目标：%s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done
[ ${#WANTED[@]} -eq 0 ] && WANTED=(guards python go web)

# 固定绝对路径后再切换包目录。显式 PY 错误必须报错，不能换成另一个解释器。
resolve_python() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  if [[ "$candidate" != */* ]]; then
    candidate="$(command -v -- "$candidate")" || return 1
  fi
  [[ "$candidate" = /* ]] || candidate="$CALLER_DIR/$candidate"
  [ -f "$candidate" ] && [ -x "$candidate" ] || return 1
  printf '%s/%s\n' "$(cd "$(dirname "$candidate")" && pwd)" "$(basename "$candidate")"
}

if [ "${PY+x}" = x ]; then
  PY="$(resolve_python "$PY")" || {
    printf 'PY 必须是可执行的 Python 命令名或文件路径。\n' >&2
    exit 2
  }
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(resolve_python python3)" || {
    printf '找不到 Python：请创建 .venv 或设置 PY。\n' >&2
    exit 2
  }
fi

cd "$ROOT" || exit 2
export PATH="$HOME/.local/opt/go/bin:$PATH"

FAILED=()
PASSED=()

SKIPPED=()

# 静默跳过与真的绿长得一模一样：记入 SKIPPED 桶，汇总里可见；CI 下默认视为失败。
skip() {
  local name="$1"
  printf '\033[33m    跳过：%s\033[0m\n' "$name"
  SKIPPED+=("$name")
}

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
  local target="$1"
  for arg in "${WANTED[@]}"; do
    [ "$arg" = "$target" ] && return 0
  done
  return 1
}

if want guards; then
  run "契约生成物"        "$PY" packages/contracts/scripts/generate.py --check
  run "契约守卫"          "$PY" scripts/check_contract.py
  run "数据所有权"        "$PY" scripts/check_data_ownership.py
  run "块类型判据"        "$PY" scripts/check_blocktype_parity.py
  run "分块回归"          "$PY" scripts/check_chunk_regression.py
  run "枚举用法"          "$PY" scripts/check_enum_usage.py
  run "日志脱敏"          "$PY" scripts/check_log_redaction.py --with-self-test
  run "联邦契约"          "$PY" scripts/check_federation_contracts.py
  run "联邦任务路由"      "$PY" scripts/check_federation_routes.py
  run "内容契约路由"      "$PY" scripts/check_content_contract.py
  run "验收台账"          "$PY" scripts/check_acceptance_matrix.py
  run "control 迁移同步"  "$PY" scripts/check_control_migrations.py
  run "动作引用 pin"      "$PY" scripts/check_action_pins.py
  run "配置参考文档"      "$PY" scripts/gen_config_docs.py --check
  run "架构守卫"          "$PY" -m pytest -q
fi

if want python; then
  # ruff 只跑会抓 bug 的两族（理由写在 pyproject 的 [tool.ruff.lint] 上面）。
  # 逐条 ignore 都有理由，不许无脑加：
  #   F401 未使用导入   —— 大量是 __init__ 的再导出与 TYPE_CHECKING
  #   B008 Depends()    —— FastAPI 的写法就是默认参数里调用函数
  #   B905 zip strict   —— 3.10 才有，且这些 zip 长度本来就相等
  #   B904 raise from   —— 值得收，但要逐个看清因果链，另起一次
  #   （B023 闭包捕获**不在这里 ignore** —— 它按文件豁免在 pyproject 的
  #    per-file-ignores 里，只放过已逐个核过的 chunking.py；
  #    别处再出现延迟执行的闭包仍然会红）
  #   B007 未用循环变量 —— 解包时占位，改名成 _ 是纯噪音
  run "ruff（F,B）"   "$PY" -m ruff check . --select F,B \
      --ignore F401,B008,B905,B904,B007
  run "ddp_core"       in_dir python/ddp_core        "$PY" -m pytest -q
  run "ddp_local"      in_dir python/ddp_local       "$PY" -m pytest -q
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
    skip "数据所有权（真库）：dev postgres 未起，已跳过"
    printf '\033[2m    它验的是"越界 SQL 会不会被数据库拒绝"，与静态守卫互补；CI 下必跑（跳过视为失败），如需放行请传 --allow-without-db\033[0m\n'
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
      printf '\033[2m    已配置 CONTROL_TEST_DATABASE_URL，计量用例使用 PostgreSQL 测试连接。\033[0m\n'
    else
      skip "计量用例（需 CONTROL_TEST_DATABASE_URL）：4 条，已跳过"
      printf '\033[2m    没有 CONTROL_TEST_DATABASE_URL 时 internal/store 的 4 条计量用例只能跳过；请准备独立 PostgreSQL 测试库并设置该变量，见 docs/DEVELOPMENT.md；CI 下跳过视为失败，如需放行请传 --allow-without-db\033[0m\n'
    fi
  else
    # **显式报缺，不静默跳过**：静默跳过的绿与真的绿长得一模一样
    printf '\033[33m>>> 跳过 Go：PATH 上没有 go\033[0m\n'
    FAILED+=("go（工具链缺失）")
  fi
fi

if want web; then
  if [ -d apps/web/node_modules ]; then
    run "前端类型检查与生产构建" in_dir apps/web npm run --silent build
    run "前端单测"      in_dir apps/web npx vitest run
    run "连接层类型检查" in_dir apps/web npx tsc -p ../../packages/client-runtime/tsconfig.json
    run "连接层持久化与协议" node --test packages/client-runtime/test/*.test.mjs
    # 与 windows-latest 工作流的「桌面主机测试」同一套用例（WSL 垫片在
    # Linux 上真跑；真 tarball 不存在时用例自己 skip 并打印理由）。
    run "桌面主机测试" node --test apps/desktop/test/*.test.mjs
  else
    printf '\033[33m>>> 跳过前端：apps/web/node_modules 不存在（npm ci）\033[0m\n'
    FAILED+=("前端（依赖未安装）")
  fi
fi

if want web-e2e; then
  if [ -d apps/web/node_modules ]; then
    run "前端浏览器测试" in_dir apps/web npm run --silent test:e2e
  else
    printf '\033[33m>>> 跳过浏览器测试：apps/web/node_modules 不存在（npm ci）\033[0m\n'
    FAILED+=("浏览器测试（依赖未安装）")
  fi
fi

# CI 下跳过视为失败：无真库的跳过在 CI 里必须红，除非显式传入 --allow-without-db。
if [ ${#SKIPPED[@]} -gt 0 ] && [ -n "${CI:-}" ] && [ "$ALLOW_WITHOUT_DB" -eq 0 ]; then
  FAILED+=("${SKIPPED[@]}")
  SKIPPED=()
fi

printf '\n\033[1m===== 汇总 =====\033[0m\n'
for name in "${PASSED[@]}"; do printf '  \033[32mPASS\033[0m %s\n' "$name"; done
for name in "${SKIPPED[@]}"; do printf '  \033[33mSKIP\033[0m %s\n' "$name"; done
for name in "${FAILED[@]}"; do printf '  \033[31mFAIL\033[0m %s\n' "$name"; done
printf '通过 %d / 跳过 %d / 失败 %d\n' "${#PASSED[@]}" "${#SKIPPED[@]}" "${#FAILED[@]}"
[ ${#FAILED[@]} -eq 0 ]
