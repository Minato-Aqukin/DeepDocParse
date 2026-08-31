#!/usr/bin/env bash
# 在 AutoDL（无 docker）上把 **Web 侧全栈** 用裸进程起起来。
#
#   bash deploy/autodl/serve-web.sh --install   # 首次：装 PG/MinIO/Redis/Node/venv
#   bash deploy/autodl/serve-web.sh             # 起服务（幂等，可反复跑）
#   bash deploy/autodl/serve-web.sh --stop      # 停掉本脚本起的所有进程
#
# 为什么需要它：`quickstart.sh` 硬依赖 docker（`docker info` 失败就 die），
# 而 AutoDL 实例是非特权容器，dind / rootless / podman 全堵死。
# 于是 plan.md 阶段 8 §1「照 quickstart 全新部署」在 AutoDL 上做不了，
# §2「真实用户路径」也就没有载体 —— 2026-08-29 那次上机整套是手工敲的，
# 光这部分吃掉近一半机时。这个脚本就是把那次手工过程固化下来。
#
# 它**不管模型**：OCR / 指令模型仍归 bootstrap.sh + serve-vllm.sh + serve-chat.sh。
# 起的是数据面（PG + MinIO + Redis）与产品面（gateway / arq worker / 后端 / 前端）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

pass() { printf '\033[32m  OK  \033[0m %s\n' "$*"; }
fail() { printf '\033[31m FAIL \033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
info() { printf '\033[36m[web]\033[0m %s\n' "$*"; }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }
FAILS=0

# ---------------------------------------------------------------- 路径与端口
# 两个仓库要同级放（Web 的 quickstart 也是这个假设）
SRC_ROOT="${SRC_ROOT:-$(cd "$HERE/../../.." && pwd)}"
SERVICE_DIR="${SERVICE_DIR:-$SRC_ROOT/DeepDocParse}"
WEB_DIR="${WEB_DIR:-$SRC_ROOT/DeepDocParse-Web}"
WEB_VENV="${WEB_VENV:-$DDP_ROOT/webvenv}"     # 装 gateway[corpus] + backend
GW_VENV="${GW_VENV:-$DDP_ROOT/gwvenv}"        # 只装 gateway + mcp_server
WEB_LOGS="${WEB_LOGS:-$LOG_DIR}"

PG_PORT="${PG_PORT:-5432}"
MINIO_PORT="${MINIO_PORT:-19000}"
REDIS_PORT="${REDIS_PORT:-6379}"
GATEWAY_PORT="${GATEWAY_PORT:-9000}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
# AutoDL 只有 6006/6008 两个端口有对外 URL，前端必须挂在其中之一才能用浏览器访问
FRONTEND_PORT="${FRONTEND_PORT:-6006}"

# ---------------------------------------------------------------- 停
if [ "${1:-}" = "--stop" ]; then
  for pat in "uvicorn app.main:app" "arq app.worker.tasks" "http.server $FRONTEND_PORT" "minio server"; do
    pkill -f "$pat" 2>/dev/null && info "停掉 $pat"
  done
  pg_ctlcluster 15 main stop 2>/dev/null && info "停掉 PostgreSQL"
  redis-cli -p "$REDIS_PORT" shutdown nosave 2>/dev/null && info "停掉 Redis"
  exit 0
fi

# ---------------------------------------------------------------- 装
if [ "${1:-}" = "--install" ]; then
  section "安装依赖（只需跑一次）"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq

  # PG 15 + pgvector。**焦点镜像的选择是实测出来的**：
  # apt.postgresql.org 在 AutoDL 上 404，TUNA/NJU 的 postgresql 镜像没有 apt 仓库，
  # 只有阿里云这一份能用（2026-08-29 实测）。focal 自带源只有 postgresql-12 且无 pgvector。
  apt-get install -y -qq curl ca-certificates gnupg lsb-release redis-server
  install -d /usr/share/postgresql-common/pgdg
  curl -fsS --retry 3 https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
       "https://mirrors.aliyun.com/postgresql/repos/apt/ $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq postgresql-15 postgresql-15-pgvector
  [ -d /usr/lib/postgresql/15 ] && pass "PostgreSQL 15 + pgvector" || fail "PG 装不上"

  # MinIO。**dl.min.io 与所有国内镜像、GitHub 代理 2026-08-29 实测全不可用或龟速
  # （20 分钟 6MB）。** 所以这里只做一次尝试，失败就明确告诉你手动推 ——
  # 静默重试半小时比直接说"拿不到"更浪费机时。
  if [ ! -x /usr/local/bin/minio ]; then
    curl -fsSL --retry 2 --max-time 300 -o /usr/local/bin/minio \
      https://dl.min.io/server/minio/release/linux-amd64/minio && chmod +x /usr/local/bin/minio
  fi
  if /usr/local/bin/minio --version >/dev/null 2>&1; then
    pass "MinIO $(/usr/local/bin/minio --version | head -1 | awk '{print $3}')"
  else
    fail "MinIO 拉不下来 —— 在本地下好后： autodl push <id> ./minio /usr/local/bin/minio && chmod +x"
  fi

  # Node（前端构建）。走阿里云 nodejs 镜像，官方源在这里很慢
  if ! command -v node >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o /tmp/node.tar.xz \
      https://mirrors.aliyun.com/nodejs-release/v22.14.0/node-v22.14.0-linux-x64.tar.xz
    mkdir -p /opt/node && tar -xf /tmp/node.tar.xz -C /opt/node --strip-components=1
    ln -sf /opt/node/bin/node /usr/local/bin/node && ln -sf /opt/node/bin/npm /usr/local/bin/npm
  fi
  command -v node >/dev/null && pass "Node $(node -v)" || fail "Node 装不上"

  # 两个 venv。系统 Python 是 3.8（backend 要 >=3.11），用 uv 装一个 3.12。
  # **gateway 与 web 必须是两个 venv**：gateway 的最小集里不许有 sqlalchemy
  # （plan.md §2 的 [corpus] 切分），而 backend 需要它。
  export PATH="$HOME/.local/bin:$HOME/miniconda3/bin:$PATH"
  command -v uv >/dev/null || { fail "没有 uv —— 先跑 bootstrap.sh"; exit 1; }
  uv python install 3.12 >/dev/null 2>&1
  uv venv --python 3.12 "$GW_VENV" >/dev/null 2>&1
  VIRTUAL_ENV="$GW_VENV" uv pip install -q -e "$SERVICE_DIR/gateway" -e "$SERVICE_DIR/mcp_server"
  uv venv --python 3.12 "$WEB_VENV" >/dev/null 2>&1
  VIRTUAL_ENV="$WEB_VENV" uv pip install -q -e "$SERVICE_DIR/gateway[corpus]"
  VIRTUAL_ENV="$WEB_VENV" uv pip install -q -e "$WEB_DIR/backend"
  "$WEB_VENV/bin/python" -c "import ddp_core.models, app.main" 2>/dev/null \
    && pass "两个 venv 就绪" || fail "venv 装不全"

  [ "$FAILS" -eq 0 ] && info "装完了。接着跑：bash $0" || info "有 $FAILS 项没装上，修完再跑"
  exit $(( FAILS > 0 ))
fi

# ---------------------------------------------------------------- 前置
# 缺 venv 时早点说人话。不检查的话表现是后面三条 `setsid: No such file or
# directory`，看起来像"服务崩了"，而其实只是没跑过 --install（实测踩过）。
for pair in "$GW_VENV:gateway" "$WEB_VENV:web"; do
  dir="${pair%%:*}"; name="${pair##*:}"
  [ -x "$dir/bin/python" ] || {
    fail "$name venv 不在 $dir —— 先跑 \`bash $0 --install\`；"
    fail "  venv 在别处就覆盖变量：GW_VENV=... WEB_VENV=... bash $0"
    exit 1; }
done

# ---------------------------------------------------------------- 配置
section "1. 配置（首次生成随机密钥，之后复用）"
mkdir -p "$WEB_LOGS"
WEB_ENV="$WEB_DIR/.env"
SERVICE_ENV="$SERVICE_DIR/.env"
SERVICE_MCP_ENV="$SERVICE_DIR/.env.mcp"

if [ ! -f "$WEB_ENV" ]; then
  SVC=$(openssl rand -hex 24); JWT=$(openssl rand -hex 32)
  MK=$(openssl rand -hex 8);   MS=$(openssl rand -hex 20)
  cat > "$WEB_ENV" <<EOF
DATABASE_URL=postgresql+asyncpg://ddp:ddp@127.0.0.1:$PG_PORT/deepdocparse
JWT_SECRET=$JWT
POSTGRES_USER=ddp
POSTGRES_PASSWORD=ddp
POSTGRES_DB=deepdocparse
MINIO_INTERNAL_ENDPOINT=127.0.0.1:$MINIO_PORT
MINIO_PUBLIC_ENDPOINT=127.0.0.1:$MINIO_PORT
MINIO_ACCESS_KEY=$MK
MINIO_SECRET_KEY=$MS
MINIO_SECURE=false
MINIO_BUCKET=deepdocparse
SERVICE_URL=http://127.0.0.1:$GATEWAY_PORT
MCP_URL=http://127.0.0.1:9100
SERVICE_TOKEN=$SVC
DEFAULT_PARSE_ENGINE=vlm-ocr
EMBEDDING_DIM=1024
PUBLIC_BASE_URL=http://127.0.0.1:$BACKEND_PORT
REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0
EOF
  # **gateway 读的那份 .env 里不许出现语料 MCP 的键。**
  # gateway 的 Settings 是 extra="forbid"，而 pydantic-settings 直接读 cwd 下的
  # .env 文件 —— 混进去会让它当场 extra_forbidden 拒绝启动（2026-08-29 实测撞到）。
  cat > "$SERVICE_ENV" <<EOF
SERVICE_TOKEN=$SVC
REDIS_URL=redis://127.0.0.1:$REDIS_PORT/0
MODELS_CONFIG=$SERVICE_DIR/models.autodl.yaml
EOF
  cat > "$SERVICE_MCP_ENV" <<EOF
SERVICE_TOKEN=$SVC
CORPUS_DATABASE_URL=postgresql+asyncpg://ddp:ddp@127.0.0.1:$PG_PORT/deepdocparse
MINIO_ENDPOINT=http://127.0.0.1:$MINIO_PORT
MINIO_ACCESS_KEY=$MK
MINIO_SECRET_KEY=$MS
MINIO_BUCKET=deepdocparse
MCP_PUBLIC_BASE_URL=http://127.0.0.1:$BACKEND_PORT
EOF
  chmod 600 "$WEB_ENV" "$SERVICE_ENV" "$SERVICE_MCP_ENV"
  pass "生成三份 .env（SERVICE_TOKEN 三处同值）"
else
  pass "复用已有 .env"
fi
set -a; . "$WEB_ENV"; set +a

# ---------------------------------------------------------------- 数据面
section "2. 数据面：PostgreSQL / MinIO / Redis"
pg_ctlcluster 15 main start 2>/dev/null; sleep 2
su - postgres -c "psql -tAc \"SELECT 1\"" >/dev/null 2>&1 \
  && pass "PostgreSQL 起来了" || fail "PostgreSQL 起不来"
su - postgres -c "psql -tAc \"CREATE USER ddp WITH PASSWORD 'ddp' SUPERUSER\"" >/dev/null 2>&1
su - postgres -c "psql -tAc \"CREATE DATABASE deepdocparse OWNER ddp\"" >/dev/null 2>&1
su - postgres -c "psql -d deepdocparse -tAc \"CREATE EXTENSION IF NOT EXISTS vector\"" >/dev/null 2>&1
su - postgres -c "psql -d deepdocparse -tAc \"SELECT extversion FROM pg_extension WHERE extname='vector'\"" 2>/dev/null \
  | grep -q . && pass "pgvector 已启用" || fail "pgvector 没装上"

if ! curl -fsS --max-time 5 "http://127.0.0.1:$MINIO_PORT/minio/health/live" >/dev/null 2>&1; then
  mkdir -p "$DDP_ROOT/miniodata"
  MINIO_ROOT_USER="$MINIO_ACCESS_KEY" MINIO_ROOT_PASSWORD="$MINIO_SECRET_KEY" \
    setsid -f /usr/local/bin/minio server "$DDP_ROOT/miniodata" \
    --address "127.0.0.1:$MINIO_PORT" --console-address "127.0.0.1:19001" \
    > "$WEB_LOGS/minio.log" 2>&1 < /dev/null
  sleep 5
fi
curl -fsS --max-time 5 "http://127.0.0.1:$MINIO_PORT/minio/health/live" >/dev/null 2>&1 \
  && pass "MinIO 健康" || fail "MinIO 起不来（看 $WEB_LOGS/minio.log）"

redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 || redis-server --daemonize yes --port "$REDIS_PORT"
sleep 1
redis-cli -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG \
  && pass "Redis 健康" || fail "Redis 起不来"

# ---------------------------------------------------------------- 迁移
section "3. 数据库迁移"
( cd "$WEB_DIR/backend" && "$WEB_VENV/bin/alembic" upgrade head ) > "$WEB_LOGS/alembic.log" 2>&1 \
  && pass "alembic upgrade head（$(grep -c 'Running upgrade' "$WEB_LOGS/alembic.log") 步）" \
  || fail "迁移失败（看 $WEB_LOGS/alembic.log）"

# ---------------------------------------------------------------- 产品面
section "4. 产品面：gateway / worker / 后端 / 前端"
# **一律 setsid -f**：`nohup ... &` 会随 SSH 会话被回收，而包一层 `bash -c` 在
# autodl exec 的传输里会被吃掉（表现是日志建了但一直 0 字节）。2026-08-29 实测。
start_bg() {  # start_bg <名字> <日志> <工作目录> <命令...>
  local name="$1" log="$2" cwd="$3"; shift 3
  ( cd "$cwd" && setsid -f "$@" > "$log" 2>&1 < /dev/null )
}

pgrep -f "uvicorn app.main:app --host 127.0.0.1 --port $GATEWAY_PORT" >/dev/null || {
  ( set -a; . "$SERVICE_ENV"; set +a
    start_bg gateway "$WEB_LOGS/gateway.log" "$SERVICE_DIR" \
      "$GW_VENV/bin/python" -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$GATEWAY_PORT" --app-dir gateway ); sleep 8; }
curl -fsS --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null 2>&1 \
  && pass "gateway /healthz" || fail "gateway 起不来（看 $WEB_LOGS/gateway.log）"

# worker 不可省：vlm-ocr / borndigital 都是进程内引擎，识别就发生在
# poll_and_archive -> fetch_result 里。没有它，任务永远停在 running。
pgrep -f "arq app.worker.tasks" >/dev/null || {
  ( set -a; . "$SERVICE_ENV"; set +a
    start_bg worker "$WEB_LOGS/arq.log" "$SERVICE_DIR/gateway" \
      "$GW_VENV/bin/arq" app.worker.tasks.WorkerSettings ); sleep 5; }
pgrep -f "arq app.worker.tasks" >/dev/null \
  && pass "arq worker 在跑" || fail "arq worker 起不来（看 $WEB_LOGS/arq.log）"

pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port $BACKEND_PORT" >/dev/null || {
  start_bg backend "$WEB_LOGS/backend.log" "$WEB_DIR/backend" \
    "$WEB_VENV/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT"
  sleep 10; }
curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/healthz" >/dev/null 2>&1 \
  && pass "backend /healthz" || fail "backend 起不来（看 $WEB_LOGS/backend.log）"

# 前端：构建产物用 http.server 静态托管到 6006（AutoDL 唯一对外的端口之一）。
# 前端是 hash 路由 + axios baseURL='/'，所以静态托管不需要 SPA 回退规则；
# 但 API 要同源，因此这里挂一个把 /api、/files、/mcp 转给后端的极简反代。
if [ ! -d "$WEB_DIR/frontend/dist" ]; then
  info "前端还没构建，正在 npm ci + build（首次约 2 分钟）"
  ( cd "$WEB_DIR/frontend" && npm ci --registry=https://registry.npmmirror.com \
      && npm run build-only ) > "$WEB_LOGS/frontend-build.log" 2>&1
fi
[ -f "$WEB_DIR/frontend/dist/index.html" ] \
  && pass "前端构建产物就绪" || fail "前端没构建出来（看 $WEB_LOGS/frontend-build.log）"

pgrep -f "ddp_static_proxy" >/dev/null || {
  cat > "$DDP_ROOT/ddp_static_proxy.py" <<PYEOF
"""把 dist/ 静态托管到对外端口，并把 /api /files /mcp 转给后端（同源，免跨域）。"""
import http.server, socketserver, urllib.request, urllib.error, sys
ROOT, PORT, BACKEND = sys.argv[1], int(sys.argv[2]), sys.argv[3]
PREFIXES = ("/api", "/files", "/mcp", "/healthz")

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def _proxy(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        req = urllib.request.Request(BACKEND + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"): req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=300) as up:
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"): self.send_header(k, v)
                self.end_headers(); self.wfile.write(up.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code); self.end_headers(); self.wfile.write(e.read())
        except Exception as exc:
            self.send_response(502); self.end_headers(); self.wfile.write(str(exc).encode())
    def do_GET(self):
        if self.path.startswith(PREFIXES): return self._proxy()
        return super().do_GET()
    do_POST = do_PUT = do_DELETE = do_PATCH = _proxy
    def log_message(self, *a): pass

socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as srv:
    srv.serve_forever()
PYEOF
  start_bg frontend "$WEB_LOGS/frontend.log" "$DDP_ROOT" \
    "$WEB_VENV/bin/python" -u "$DDP_ROOT/ddp_static_proxy.py" \
    "$WEB_DIR/frontend/dist" "$FRONTEND_PORT" "http://127.0.0.1:$BACKEND_PORT"
  sleep 3; }
curl -fsS --max-time 5 "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null 2>&1 \
  && pass "前端 :$FRONTEND_PORT（AutoDL 自定义服务里就是这个端口）" \
  || fail "前端起不来（看 $WEB_LOGS/frontend.log）"

# ---------------------------------------------------------------- 汇总
section "汇总"
if [ "$FAILS" -eq 0 ]; then
  printf '\033[32m全部就绪。\033[0m\n'
  echo "  浏览器：AutoDL 控制台 -> 自定义服务 -> $FRONTEND_PORT"
  echo "  模型：还需 serve-vllm.sh（识别）与 serve-chat.sh（抽取），未起时会如实降级"
  echo "  停：bash $0 --stop"
else
  printf '\033[31m%d 项未通过。\033[0m 修完再往下走。\n' "$FAILS"
fi
exit $(( FAILS > 0 ))
