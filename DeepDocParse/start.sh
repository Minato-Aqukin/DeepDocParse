#!/usr/bin/env bash
# DeepDocParse 工作区唯一运行入口。
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SELF")"
WEB="$ROOT/DeepDocParse-Web"
AUTODL="$ROOT/DeepDocParse/deploy/autodl"
LOCAL="$ROOT/DeepDocParse/deploy/local.bash"

usage() {
  cat <<'EOF'
用法：./start.sh [local|docker|autodl] [start|stop|restart|status|logs|doctor] [参数]

  local    本机混合开发环境（默认，borndigital/CPU）
  docker   Docker 完整部署
  autodl   AutoDL 裸进程部署

示例：
  ./start.sh
  ./start.sh local logs worker
  ./start.sh docker doctor
  ./start.sh autodl start
  ./start.sh autodl stop
EOF
}

profile="${1:-local}"
case "$profile" in local|docker|autodl) [[ $# -gt 0 ]] && shift ;; *) profile=local ;; esac
action="${1:-start}"
[[ $# -gt 0 ]] && shift

case "$profile:$action" in
  local:start|local:stop|local:status) exec "$LOCAL" "$action" "$@" ;;
  local:logs) exec "$LOCAL" logs "${1:?请指定日志名}" ;;
  local:restart) "$LOCAL" stop; exec "$LOCAL" start ;;
  docker:start|docker:stop|docker:restart|docker:status|docker:doctor)
    exec "$WEB/deploy/docker.bash" "$action" "$@" ;;
  docker:logs) exec "$WEB/deploy/docker.bash" logs "${1:?请指定服务名}" "${@:2}" ;;
  autodl:start)
    source "$AUTODL/env.bash"
    "$AUTODL/ocr.bash" --daemon
    [[ "$ENABLE_CHAT" == 1 ]] && "$AUTODL/chat.bash" --daemon
    [[ "${DDP_WITH_EMBED:-0}" == 1 ]] && "$AUTODL/embed.bash" --daemon
    exec "$AUTODL/web.bash"
    ;;
  autodl:stop)
    # 与真实进程命令匹配；静态前端由 ddp_static_proxy.py 提供，不是 http.server。
    "$AUTODL/web.bash" --stop
    pkill -f 'vllm.entrypoints.openai.api_server' 2>/dev/null || true
    pkill -f 'uvicorn embed_shim:app' 2>/dev/null || true
    ;;
  autodl:status|autodl:doctor) exec "$AUTODL/verify.bash" ;;
  autodl:e2e) exec "$AUTODL/e2e-llm.bash" ;;
  autodl:logs)
    # AutoDL 的路径只由 env.bash 定义，不能假定日志就在源码目录。
    source "$AUTODL/env.bash"
    exec tail -f "$LOG_DIR/${1:?请指定日志名}.log"
    ;;
  *:help|*:-h|*:--help) usage ;;
  *) echo "不支持：$profile $action" >&2; usage >&2; exit 2 ;;
esac
