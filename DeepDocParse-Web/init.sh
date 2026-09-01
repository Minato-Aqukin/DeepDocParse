#!/usr/bin/env bash
# DeepDocParse 工作区唯一初始化入口。
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SELF")"
SERVICE="$ROOT/DeepDocParse"
WEB="$ROOT/DeepDocParse-Web"
AUTODL="$ROOT/DeepDocParse/deploy/autodl"

usage() {
  cat <<'EOF'
用法：./init.sh [local|docker|autodl] [选项]

  local    安装两个 Python 环境和前端依赖（默认）
  docker   完整的 Docker 部署初始化；其余选项原样传给配置器
  autodl   AutoDL 裸进程环境、模型及 Web 数据面初始化
EOF
}

profile="${1:-local}"
if [[ $# -gt 0 ]]; then shift; fi

case "$profile" in
  local)
    [[ -d "$SERVICE" && -d "$WEB" ]] || { echo "两个仓库必须位于同一目录" >&2; exit 1; }
    command -v npm >/dev/null || { echo "缺少 Node/npm" >&2; exit 1; }
    python_bin="${PYTHON:-$(command -v python3.12 || command -v python3.11 || command -v python3 || command -v python)}"
    pip_index="${PIP_INDEX_URL:-https://pypi.org/simple}"
    [[ -n "$python_bin" ]] || { echo "缺少 Python >= 3.11" >&2; exit 1; }
    "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
      echo "需要 Python >= 3.11" >&2; exit 1; }
    [[ -d "$ROOT/DeepDocParse/.venv" ]] || "$python_bin" -m venv "$ROOT/DeepDocParse/.venv"
    "$ROOT/DeepDocParse/.venv/bin/python" -m pip install --index-url "$pip_index" -e "$ROOT/DeepDocParse/gateway[dev]" -e "$ROOT/DeepDocParse/mcp_server"
    [[ -d "$WEB/.venv" ]] || "$python_bin" -m venv "$WEB/.venv"
    "$WEB/.venv/bin/python" -m pip install --index-url "$pip_index" -e "$ROOT/DeepDocParse/gateway[corpus]" -e "$WEB/backend[dev]"
    (cd "$WEB/frontend" && npm install --registry=https://registry.npmmirror.com)
    ;;
  docker)
    case "${1:-}" in
      deps|fetch|configure|tune|models|build|systemd|help)
        stage="$1"; shift; exec "$WEB/deploy/docker.bash" "$stage" "$@" ;;
    esac
    "$WEB/deploy/docker.bash" deps "$@"
    "$WEB/deploy/docker.bash" fetch "$@"
    "$WEB/deploy/docker.bash" configure "$@"
    "$WEB/deploy/docker.bash" models "$@"
    exec "$WEB/deploy/docker.bash" build "$@"
    ;;
  autodl)
    [[ -d "$SERVICE" && -d "$WEB" ]] || { echo "两个仓库必须位于同一目录" >&2; exit 1; }
    "$AUTODL/bootstrap.bash" "$@"
    exec "$AUTODL/web.bash" --install
    ;;
  -h|--help|help) usage ;;
  *) echo "未知档位：$profile" >&2; usage >&2; exit 2 ;;
esac
