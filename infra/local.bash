#!/usr/bin/env bash
# 本机 dev 环境起停 —— 宿主机混合模式：有状态组件用容器，四个服务进程用 venv 直接跑。
#
# 用法：./start.sh local start | stop | status | logs <name>
#
# 本机没有 N 卡，所以这套只跑得起「无 GPU 能跑的那一半」：
#   能跑：解析（borndigital 引擎）、归档、出处三件套、语料 MCP（检索会降级到关键词）
#   跑不了：向量索引 / 问答（要 TEI + bge-m3 权重）、视觉验证（要 VQA 运行时）
#   —— 这两条不是坏了，是既有降级路径：Web 的 index_status 会显式标 failed，
#      问答会打 degraded=embedding_unavailable / vision_unavailable。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SVC="$ROOT/DeepDocParse"
WEB="$ROOT/DeepDocParse-Web"
LOGS="$ROOT/.dev-logs"
# service 侧 Redis 必须是 redis-stack（向量索引要 RediSearch 的 FT.*）；
# Web 侧那个是普通 redis，只做限速令牌桶与对账选主，两者不能混用同一个库
SVC_REDIS="redis://127.0.0.1:6379/0"

mkdir -p "$LOGS"

port_pid() { ss -ltnp 2>/dev/null | grep ":$1 " | grep -oP 'pid=\K[0-9]+' | head -1; }

# 每个服务记 pid 到文件，停的时候先收子进程再收自己。
# 不用 setsid + 进程组：setsid 在需要时会 fork，$! 记下的是转瞬即逝的中间进程，
# pid 文件当场就失效（实测 gateway/worker 因此没被 kill_group 收掉，靠端口兜底才停下）。
# 直接 exec 的话 $! 就是那个进程本身，可靠；子进程另外处理。
start_bg() {   # start_bg <名字> <工作目录> <命令...>
  local name="$1" dir="$2"; shift 2
  ( cd "$dir" && exec "$@" >"$LOGS/$name.log" 2>&1 ) &
  echo $! > "$LOGS/$name.pid"
  echo "  $name -> $LOGS/$name.log"
}

# 先杀子进程再杀本体：npm run dev 会 fork 出 vite，只杀父进程会留下孤儿
# （实测过：5173 已释放但 vite 还在跑，下次 start 时它还占着 node_modules）
kill_group() {   # kill_group <名字>
  local name="$1" pidfile="$LOGS/$1.pid" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
    echo "  停 $name (pid $pid)"
  fi
  rm -f "$pidfile"
}

alive() {   # alive <名字>
  local pidfile="$LOGS/$1.pid"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

case "${1:-status}" in
start)
  echo "[1/4] 容器（项目名已在 compose 文件里钉死，两个仓库不会互相顶掉）"
  ( cd "$WEB/docker" && docker compose -f compose.web.yml --env-file ../.env up -d ) || exit 1
  ( cd "$SVC/docker" && docker compose -f compose.cpu.yml --env-file ../.env up -d redis ) || exit 1

  echo "[2/4] 等 Postgres 就绪 + 迁移"
  for _ in $(seq 30); do
    docker exec ddp-web-postgres-1 pg_isready -U ddp >/dev/null 2>&1 && break; sleep 1
  done
  ( cd "$WEB/backend" && ../.venv/bin/alembic upgrade head ) || exit 1

  echo "[3/4] service 层（gateway 9000 / arq worker / mcp 9100 / fixtures 18081）"
  export SERVICE_TOKEN="$(grep -E '^SERVICE_TOKEN=' "$SVC/.env" | cut -d= -f2-)"
  export REDIS_URL="$SVC_REDIS"
  export MODELS_CONFIG="$SVC/models.local.yaml"
  start_bg gateway  "$SVC/gateway"    ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9000
  start_bg worker   "$SVC/gateway"    ../.venv/bin/arq app.worker.tasks.WorkerSettings
  ( set -a; . "$WEB/.env"; set +a
    start_bg mcp "$SVC/mcp_server" env \
      GATEWAY_URL=http://127.0.0.1:9000 \
      CORPUS_DATABASE_URL="$DATABASE_URL" \
      MINIO_ENDPOINT="http://$MINIO_INTERNAL_ENDPOINT" \
      MINIO_ACCESS_KEY="$MINIO_ACCESS_KEY" \
      MINIO_SECRET_KEY="$MINIO_SECRET_KEY" \
      MINIO_BUCKET="$MINIO_BUCKET" \
      ../.venv/bin/python server.py )
  start_bg fixtures "$SVC"            .venv/bin/python -m http.server 18081 --bind 127.0.0.1 --directory tests/fixtures

  echo "[4/4] 产品层（backend 8080 / frontend 5173）"
  ( set -a; . "$WEB/.env"; set +a
    start_bg backend "$WEB/backend" ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 )
  # 直接跑 vite 而不是 npm run dev：少一层父子关系，pid 文件记的就是真身
  start_bg frontend "$WEB/frontend" ./node_modules/.bin/vite

  sleep 6; exec "$0" status
  ;;

stop)
  for name in gateway worker mcp fixtures backend frontend; do kill_group "$name"; done
  sleep 1   # kill 是异步的，不等一下端口兜底会把"正在退出"误报成"没收住"
  # 兜底：pid 文件丢了（比如上一轮是手工起的）就按端口再收一遍
  for p in 9000 9100 8080 5173 18081; do
    pid="$(port_pid "$p")"; [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "  停 :$p (pid $pid，pid 文件没收住)"
  done
  ( cd "$WEB/docker" && docker compose -f compose.web.yml --env-file ../.env stop )
  ( cd "$SVC/docker" && docker compose -f compose.cpu.yml --env-file ../.env stop redis )
  ;;

status)
  echo "容器："; docker ps --format '  {{.Names}}  {{.Status}}  {{.Ports}}'
  echo "进程："
  alive worker && echo "  arq worker  运行中" || echo "  arq worker  未运行"
  probe() { printf '  %-22s %s\n' "$1" "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "$2")"; }
  probe "gateway  /readyz"   http://localhost:9000/readyz
  probe "backend  /healthz"  http://localhost:8080/healthz
  probe "mcp      /mcp"      http://localhost:9100/mcp     # 406 = 活着（要 MCP 协议头）
  probe "frontend /"         http://localhost:5173/
  probe "fixtures /"         http://localhost:18081/
  echo
  echo "  gateway /readyz: $(curl -s --noproxy '*' --max-time 3 http://localhost:9000/readyz)"
  ;;

logs) tail -f "$LOGS/${2:?用法: ./start.sh local logs <gateway|worker|mcp|backend|frontend|fixtures>}.log" ;;
*) echo "用法: $0 start | stop | status | logs <name>"; exit 2 ;;
esac
