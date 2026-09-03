#!/usr/bin/env bash
# 在**跑不了 docker 的机器**（AutoDL 一类）上把 monorepo 全栈用裸进程起起来。
#
#   bash infra/autodl/stack.bash install    # 装系统依赖 + venv（幂等，可重跑）
#   bash infra/autodl/stack.bash migrate    # 两套迁移 + 授权
#   bash infra/autodl/stack.bash start      # 起全部进程（幂等）
#   bash infra/autodl/stack.bash tunnel     # 接 Cloudflare Tunnel（令牌见函数说明）
#   bash infra/autodl/stack.bash doctor     # 体检，别跳过
#   bash infra/autodl/stack.bash stop|status|logs <名字>
#
# ## 为什么需要它
#
# `scripts/dev.sh` 是 docker compose 的入口，而 AutoDL 实例本身就是**非特权
# 容器**（无 CAP_SYS_ADMIN、`unshare --user` 返回 EPERM），dind / rootless /
# podman 全堵死 —— 见 infra/autodl/README.md 里那张实测表。
#
# **架构一个字都没改。** 换的只是进程的承载方式与地址：容器服务名换成回环
# 地址，`depends_on: service_healthy` 换成显式等健康。谁写哪张表、谁能连哪个
# 库、注册表驱动这些边界全部照旧 —— 尤其是两个受限角色：长跑进程一律用
# ddp_control / ddp_corpus 连库，超级用户只在迁移那一步出现。
#
# 它取代的是合仓前的 `web.bash`（那份按两个仓库并列的旧布局写，已失效）。
# 模型线（OCR / 指令模型）仍归 bootstrap.bash + ocr.bash + chat.bash。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$HERE/../.." && pwd)}"          # monorepo 根

# ---------------------------------------------------------------- 配置面
# 装到哪。AutoDL 系统盘小，有数据盘就用数据盘（建实例时 --disk 扩容）
DDP_ROOT="${DDP_ROOT:-$( [ -d /root/autodl-tmp ] && echo /root/autodl-tmp || echo /root )/ddp}"
VENV="${VENV:-$DDP_ROOT/appvenv}"                 # **与模型线的 venv 分开**：
                                                  # 那边装 torch/vLLM，两者互不相干
LOG_DIR="${LOG_DIR:-$DDP_ROOT/logs}"
RUN_DIR="${RUN_DIR:-$DDP_ROOT/run}"               # 进程的中立 CWD，见下方说明
DIST="${DIST:-$SRC/apps/web/dist}"                # 前端构建产物（本地构建后推上来）
# nginx 的 worker 不是 root，而产物大概率躺在 /root 下（700，进不去）——
# 表现是首页 403 而 /api 一切正常。所以静态那一份复制到公共位置去服务。
DIST_SERVE="${DIST_SERVE:-/var/www/ddp}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"              # control-api / control-migrate / minio

PY_VERSION="${PY_VERSION:-3.12}"                  # 各包 requires-python >= 3.11
PG_VERSION="${PG_VERSION:-16}"

PG_PORT="${PG_PORT:-5432}"
MINIO_PORT="${MINIO_PORT:-19000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-19001}"
REDIS_PORT="${REDIS_PORT:-6379}"
CONTROL_PORT="${CONTROL_PORT:-8080}"
CORPUS_PORT="${CORPUS_PORT:-8081}"
GATEWAY_PORT="${GATEWAY_PORT:-9000}"
MCP_PORT="${MCP_PORT:-9100}"
EDGE_PORT="${EDGE_PORT:-8000}"                    # nginx：唯一对外的那个端口

# **公网身份。** 预签名 URL 的签名覆盖 host，稳定文件 URL 也要拼它 ——
# 填错的表现是浏览器 403 SignatureDoesNotMatch 或跨域被拦，而服务端全绿。
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1:$EDGE_PORT}"
PUBLIC_SCHEME="${PUBLIC_SCHEME:-http}"            # 隧道 / 反代终结 TLS 时填 https

# **缺省是 models.local.yaml 而不是 models.cpu.yaml。** 后者注册了
# `http://embed:8080`（compose 的内部 DNS），这台机器上解析不了 ——
# 而网关的 /readyz 是 all(up)：注册了却没起的东西会让探针恒 503，
# 副本永远不接流量。注册表里写了什么就得真的起什么。
# 起了模型线之后换成 models.autodl.yaml（OCR-2 + 指令模型 + 可选 bge-m3）。
MODELS_CONFIG="${MODELS_CONFIG:-$SRC/infra/registry/models.local.yaml}"
DEFAULT_PARSE_ENGINE="${DEFAULT_PARSE_ENGINE:-borndigital}"
REGISTRATION_MODE="${REGISTRATION_MODE:-open}"
# **问答用哪个 chat 模型。留空是有风险的。**
# 留空时 corpus-api 不带 model 字段，网关就取 vqa_models 段的 default ——
# 而它**不按能力词筛**（抽值那条路筛 no_instruct，问答这条没有）。
# 注册表里 default 是 OCR 专用模型时（models.autodl.yaml 就是），
# 问答会被一个"只会抄字、不听指令"的模型接走，答出来的东西看着像答案。
# 所以起了指令模型的部署要在这里点名它。
CHAT_MODEL="${CHAT_MODEL:-}"
OBJECT_BUCKET="${OBJECT_BUCKET:-deepdocparse}"

CONDA_BIN="${CONDA_BIN:-/root/miniconda3/bin/conda}"   # 镜像自带，channel 指向 TUNA
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple}"
PIP_FALLBACK_INDEX="${PIP_FALLBACK_INDEX:-https://mirrors.ustc.edu.cn/pypi/simple}"

STACK_ENV="$DDP_ROOT/stack.env"                   # 生成一次就复用（含全部密钥）

# ---------------------------------------------------------------- 小工具
FAILS=0
pass()    { printf '\033[32m  OK  \033[0m %s\n' "$*"; }
fail()    { printf '\033[31m FAIL \033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn()    { printf '\033[33m WARN \033[0m %s\n' "$*"; }
info()    { printf '\033[36m[stack]\033[0m %s\n' "$*"; }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# **判进程存活别用 `pgrep -f`** —— 命令行里就含被查的字符串，它会匹配到自己
# 那条命令，于是永远回答"在跑"。方括号那一招同理防自匹配。
# **管道里一律不用 `grep -q`。** 它一命中就退出，上游收到 SIGPIPE 退 141，
# 而 `set -o pipefail` 把整条管道判成失败 —— 于是"匹配上了"反而返回非零。
# 在 ps 那种几百行的输出上这是必然发生的（只有目标恰好排在最后几行才侥幸躲过，
# 而 start 刚跑完时这几个进程 PID 最大、正好排在末尾 —— 所以首跑看不出来）。
# 后果三条：status 把在跑的全报成"没了"；start 失去幂等，对不占端口的 worker
# 每跑一次多起一份；重起时 `>` 会把**还在跑**的那个服务的日志截断。
# 不带 -q 的 grep 会把输入读完，不提前关管道。
qgrep() { grep "$@" >/dev/null; }

alive() {
  # 首字符换成字符组，`grep` 自己那条命令行就匹配不上自己了
  local pat="$1"
  case "$pat" in "["*) ;; *) pat="[${pat%"${pat#?}"}]${pat#?}" ;; esac
  ps -eo pid,cmd --no-headers | grep -v ' grep ' | qgrep -- "$pat"
}

# **一律 `setsid -f 二进制`**：`nohup ... &` 会随 SSH 会话被回收；
# 而包一层 `bash -c '...'` 在 autodl exec 的传输里会被吃掉，
# 表现是日志文件建了但一直 0 字节（2026-08-29 实测）。
start_bg() {  # start_bg <日志名> <工作目录> <命令...>
  local name="$1" cwd="$2"; shift 2
  ( cd "$cwd" && setsid -f "$@" > "$LOG_DIR/$name.log" 2>&1 < /dev/null )
}

wait_http() {  # wait_http <url> <秒数> —— 起进程与"真的能服务"之间差着几秒到几分钟
  local url="$1" limit="${2:-60}" i=0
  while [ "$i" -lt "$limit" ]; do
    curl -fsS --max-time 3 --noproxy '*' "$url" >/dev/null 2>&1 && return 0
    i=$((i + 1)); sleep 1
  done
  return 1
}

need_root() { [ "$(id -u)" = 0 ] || { echo "要 root（AutoDL 上本来就是）" >&2; exit 1; }; }

# ================================================================ install
do_install() {
  need_root
  mkdir -p "$DDP_ROOT" "$LOG_DIR" "$RUN_DIR"

  section "1. 系统依赖"
  local codename; codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  info "发行版 $codename"

  # PostgreSQL 16 + pgvector：发行版自带的版本太老且没有 pgvector。
  # **apt.postgresql.org 2026-08-29 实测 404**，走阿里云那份镜像。
  if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
    curl -fsSL --noproxy '*' https://mirrors.aliyun.com/postgresql/repos/apt/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg 2>/dev/null
    echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://mirrors.aliyun.com/postgresql/repos/apt/ ${codename}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list
  fi
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
      "postgresql-$PG_VERSION" "postgresql-$PG_VERSION-pgvector" "postgresql-client-$PG_VERSION" \
      nginx redis-server curl ca-certificates gnupg >/dev/null \
    && pass "apt：postgresql-$PG_VERSION + pgvector + nginx + redis" \
    || fail "apt 装依赖失败"

  # RediSearch：网关那份**块级向量索引**要它。没有的话不是坏了 ——
  # task_store.py 有一条 scan 兜底路径，产品主链路的向量检索本来也走
  # PostgreSQL + pgvector。但它是可见降级，doctor 会如实报出来。
  if [ "${REDIS_STACK:-1}" = 1 ] && ! command -v redis-stack-server >/dev/null 2>&1; then
    if curl -fsSL --max-time 20 --noproxy '*' https://packages.redis.io/gpg \
         | gpg --dearmor -o /usr/share/keyrings/redis.gpg 2>/dev/null; then
      echo "deb [signed-by=/usr/share/keyrings/redis.gpg] https://packages.redis.io/deb ${codename} main" \
        > /etc/apt/sources.list.d/redis.list
      apt-get update -qq && apt-get install -y -qq redis-stack-server >/dev/null 2>&1 \
        && pass "redis-stack-server（带 RediSearch）" \
        || warn "redis-stack 装不上，退到发行版 redis —— 网关侧向量索引会走 scan 兜底"
    else
      warn "packages.redis.io 拉不到，退到发行版 redis —— 网关侧向量索引会走 scan 兜底"
    fi
  fi

  section "2. Python $PY_VERSION 与四个服务包"
  # 系统 Python 是 3.8，而各包 requires-python >= 3.11 —— 要另起一份。
  #
  # **优先用镜像自带的 conda**：`uv python install` 是去 GitHub 取
  # python-build-standalone 的，而 GitHub 在这类机器上实测不可用
  # （20 秒 0 字节，见 infra/autodl/README.md 里那张下载源表），
  # 而 conda 的 channel 指向 TUNA，是这里唯一稳的路。uv 留作兜底。
  if [ ! -x "$VENV/bin/python" ] && [ -x "$CONDA_BIN" ]; then
    info "conda 建 Python $PY_VERSION 环境（$VENV）"
    "$CONDA_BIN" create -y -q -p "$VENV" "python=$PY_VERSION" >"$LOG_DIR/conda.log" 2>&1 \
      || warn "conda 建环境失败（看 $LOG_DIR/conda.log），退到 uv"
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || python3 -m pip install -q -i "$PIP_INDEX" uv
    uv venv --python "$PY_VERSION" "$VENV"
  fi
  [ -x "$VENV/bin/python" ] || { fail "建不出 Python $PY_VERSION 环境"; return 1; }
  pass "Python $("$VENV/bin/python" -V 2>&1)"

  # **装包用 uv 而不是 pip。** 不是偏好问题：pip 的解析器在这六个本地包
  # 加起来的依赖树上会长时间回溯 —— 实测 11 分钟只落地 11 个包，而且 `-q`
  # 之下看不出它在干什么，很容易被当成"网络慢"。uv 解析同一棵树是秒级的。
  # 这里的 uv 只用来**装**，不用它造 Python：`uv python install` 是去 GitHub
  # 取 python-build-standalone，而这类机器上 GitHub 不通（见 README 的源表）。
  "$VENV/bin/python" -m uv --version >/dev/null 2>&1 \
    || "$VENV/bin/python" -m pip install -q --index-url "$PIP_INDEX" uv \
    || warn "uv 装不上，退回 pip（会很慢）"

  # **换源重试而不是原地重试**：镜像给坏包时（THESE PACKAGES DO NOT MATCH
  # THE HASHES）坏的是那份缓存，同一个源重试多少次还是坏的。
  pipi() {
    local installer=(-m uv pip install)
    "$VENV/bin/python" -m uv --version >/dev/null 2>&1 || installer=(-m pip install -q)
    "$VENV/bin/python" "${installer[@]}" --index-url "$PIP_INDEX" "$@" && return 0
    info "换源重试：$PIP_FALLBACK_INDEX"
    "$VENV/bin/python" "${installer[@]}" --index-url "$PIP_FALLBACK_INDEX" "$@"
  }
  # 一次装齐：四个服务在同一台机器上，共用一个 venv 没有边界问题
  # （无状态网关不装 ORM 那条铁律由**镜像**与 CI 的最小装 job 钉着，
  #  裸进程部署共用 venv 不改变谁 import 什么）。
  pipi "$SRC/python/ddp_contracts" "$SRC/python/ddp_core[db,cjk]" \
       "$SRC/services/model-gateway" "$SRC/services/corpus-api" \
       "$SRC/services/corpus-worker" "$SRC/services/mcp" \
       alembic \
    && pass "venv 就绪：$VENV" || { fail "装 Python 包失败"; return 1; }

  section "3. 二进制"
  for b in control-api control-migrate minio; do
    [ -x "$BIN_DIR/$b" ] && pass "$b 在位" || fail "$BIN_DIR/$b 缺失（本地交叉编译后 autodl push 上来）"
  done
  [ -f "$DIST/index.html" ] && pass "前端产物在位" || fail "$DIST/index.html 缺失（本地 npm run build-only 后推上来）"

  section "4. PostgreSQL 集群"
  pg_ctlcluster "$PG_VERSION" main start 2>/dev/null
  wait_pg 30 && pass "PostgreSQL 起来了" || { fail "PostgreSQL 起不来"; return 1; }
  gen_env
  set -a; . "$STACK_ENV"; set +a
  su postgres -c "psql -p $PG_PORT -tAc \"SELECT 1 FROM pg_roles WHERE rolname='ddp'\"" 2>/dev/null | qgrep 1 \
    || su postgres -c "psql -p $PG_PORT -c \"CREATE ROLE ddp LOGIN SUPERUSER PASSWORD '$POSTGRES_PASSWORD'\"" >/dev/null
  su postgres -c "psql -p $PG_PORT -tAc \"SELECT 1 FROM pg_database WHERE datname='deepdocparse'\"" 2>/dev/null | qgrep 1 \
    || su postgres -c "psql -p $PG_PORT -c \"CREATE DATABASE deepdocparse OWNER ddp\"" >/dev/null
  su postgres -c "psql -p $PG_PORT -d deepdocparse -c 'CREATE EXTENSION IF NOT EXISTS vector'" >/dev/null 2>&1
  su postgres -c "psql -p $PG_PORT -d deepdocparse -tAc \"SELECT extversion FROM pg_extension WHERE extname='vector'\"" 2>/dev/null \
    | qgrep . && pass "pgvector 已启用" || fail "pgvector 没装上"

  printf '\n%s\n' "install 完成，$FAILS 项失败。下一步：stack.bash migrate"
  return $FAILS
}

wait_pg() {
  local limit="${1:-30}" i=0
  while [ "$i" -lt "$limit" ]; do
    su postgres -c "psql -p $PG_PORT -tAc 'SELECT 1'" >/dev/null 2>&1 && return 0
    i=$((i + 1)); sleep 1
  done
  return 1
}

# ---------------------------------------------------------------- 环境
# **三个必填密钥没有默认值是有意的**：占位值跑起来鉴权形同虚设，而运行时
# 不会有任何报错 —— 这个项目在 JWT_SECRET 与 SERVICE_TOKEN 上各踩过一次。
gen_env() {
  if [ -f "$STACK_ENV" ]; then pass "复用已有 $STACK_ENV"; return 0; fi
  mkdir -p "$(dirname "$STACK_ENV")"
  gen() { "$VENV/bin/python" -c "import secrets;print(secrets.token_urlsafe(32))"; }
  # **先收紧 umask 再写。** 先 `cat >` 后 `chmod 600` 中间有一个窗口，
  # 那一瞬间文件已经是 644 且已经含着全部密钥
  local old_umask; old_umask="$(umask)"
  umask 077
  cat > "$STACK_ENV" <<EOF
# 由 infra/autodl/stack.bash 生成。**不进 git**，权限 600。
JWT_SECRET=$(gen)
SERVICE_TOKEN=$(gen)
OBJECT_SECRET_KEY=$(gen)
OBJECT_ACCESS_KEY=ddpadmin
POSTGRES_PASSWORD=$(gen)
CONTROL_DB_PASSWORD=$(gen)
CORPUS_DB_PASSWORD=$(gen)
EOF
  umask "$old_umask"
  chmod 600 "$STACK_ENV"
  pass "生成 $STACK_ENV（600）"
}

load_env() {
  [ -f "$STACK_ENV" ] || { echo "$STACK_ENV 不存在，先跑 install" >&2; exit 1; }
  set -a; . "$STACK_ENV"; set +a
  export PATH="$HOME/.local/bin:$PATH"

  PUBLIC_BASE="$PUBLIC_SCHEME://$PUBLIC_HOST"
  # 公网走 https、内网走回环明文 —— 两侧 scheme 不同，所以是两个开关。
  # 只有一个的话：关着则给浏览器的预签名 URL 是 http，https 页面里按混合内容
  # 被拦（服务端零报错）；开着则内网 client 去 https 连回环，启动自检就断。
  OBJ_PUBLIC_SECURE=false
  # 末句写成 `[ ... ] && VAR=true` 的话，http 部署下整个函数返回 1 ——
  # 现在没人检查，但哪天有人写 `load_env || die` 就成了"只有 https 才起得来"
  if [ "$PUBLIC_SCHEME" = https ]; then OBJ_PUBLIC_SECURE=true; fi
}

# ================================================================ migrate
do_migrate() {
  load_env
  section "数据库迁移（两套各管各的 schema）"
  # **顺序不是随意的**：control 侧的 0002_roles.sql 建 ddp_control / ddp_corpus
  # 两个角色，而 corpus 侧的 grants.sql 要对它们授权 —— 角色不存在时那句
  # GRANT 直接失败。compose 里两个一次性容器都 depends_on postgres 而互不依赖，
  # 靠的是"总有一次会跑过"；裸进程只跑一遍，得把顺序钉死。
  CONTROL_DATABASE_URL="postgres://ddp:$POSTGRES_PASSWORD@127.0.0.1:$PG_PORT/deepdocparse" \
  CONTROL_DB_PASSWORD="$CONTROL_DB_PASSWORD" CORPUS_DB_PASSWORD="$CORPUS_DB_PASSWORD" \
    "$BIN_DIR/control-migrate" -database "postgres://ddp:$POSTGRES_PASSWORD@127.0.0.1:$PG_PORT/deepdocparse" up \
    > "$LOG_DIR/control-migrate.log" 2>&1 \
    && pass "control 迁移 + 角色口令" || fail "control 迁移失败（看 $LOG_DIR/control-migrate.log）"

  # 迁移用**属主身份**跑：建表要 DDL 权限，而长跑的服务恰恰不该有。
  # ALLOW_INSECURE_DEFAULTS：一次性动作，不需要真凭据（与 compose 同款逃生口）
  ( cd "$SRC/database/corpus" \
    && env DATABASE_URL="postgresql+asyncpg://ddp:$POSTGRES_PASSWORD@127.0.0.1:$PG_PORT/deepdocparse" \
           ALLOW_INSECURE_DEFAULTS=true PATH="$VENV/bin:$PATH" \
       sh ./migrate.sh ) > "$LOG_DIR/corpus-migrate.log" 2>&1 \
    && pass "corpus 迁移 + 授权（$(grep -c 'Running upgrade' "$LOG_DIR/corpus-migrate.log") 步）" \
    || fail "corpus 迁移失败（看 $LOG_DIR/corpus-migrate.log）"
  return $FAILS
}

# ================================================================ start
do_start() {
  load_env
  mkdir -p "$LOG_DIR" "$RUN_DIR" "$DDP_ROOT/miniodata"

  section "1. 数据面"
  pg_ctlcluster "$PG_VERSION" main start 2>/dev/null
  wait_pg 30 && pass "PostgreSQL" || fail "PostgreSQL 起不来"

  if ! curl -fsS --max-time 3 --noproxy '*' "http://127.0.0.1:$MINIO_PORT/minio/health/live" >/dev/null 2>&1; then
    MINIO_ROOT_USER="$OBJECT_ACCESS_KEY" MINIO_ROOT_PASSWORD="$OBJECT_SECRET_KEY" \
      start_bg minio "$RUN_DIR" "$BIN_DIR/minio" server "$DDP_ROOT/miniodata" \
        --address "127.0.0.1:$MINIO_PORT" --console-address "127.0.0.1:$MINIO_CONSOLE_PORT"
  fi
  wait_http "http://127.0.0.1:$MINIO_PORT/minio/health/live" 40 \
    && pass "MinIO" || fail "MinIO 起不来（$LOG_DIR/minio.log）"

  if ! redis-cli -p "$REDIS_PORT" ping 2>/dev/null | qgrep PONG; then
    if command -v redis-stack-server >/dev/null 2>&1; then
      start_bg redis "$RUN_DIR" redis-stack-server --port "$REDIS_PORT" --daemonize no
    else
      redis-server --daemonize yes --port "$REDIS_PORT" --dir "$DDP_ROOT"
    fi
    sleep 2
  fi
  redis-cli -p "$REDIS_PORT" ping 2>/dev/null | qgrep PONG && pass "Redis" || fail "Redis 起不来"

  section "2. 应用面"
  # **进程的 CWD 一律是中立目录 $RUN_DIR。** pydantic-settings 会读 CWD 下的
  # `.env`，而网关的 Settings 是 extra="forbid" —— 在仓库里起就可能被一份
  # 不属于它的 .env 直接顶成 extra_forbidden 拒绝启动（2026-08-29 实测）。
  #
  # **长跑进程一律用受限角色连库**，超级用户只在迁移那一步出现：
  # 这是"一个数据对象只能有一个写入所有者"在数据库层面的落点，
  # 用 ddp 连的话这一整层保护等于没有。
  local corpus_db="postgresql+asyncpg://ddp_corpus:$CORPUS_DB_PASSWORD@127.0.0.1:$PG_PORT/deepdocparse"

  # ---- model-gateway ----
  if ! alive "ddp_gateway.main:app"; then
    ( export SERVICE_TOKEN="$SERVICE_TOKEN" \
             REDIS_URL="redis://127.0.0.1:$REDIS_PORT/2" \
             MODELS_CONFIG="$MODELS_CONFIG"
      start_bg model-gateway "$RUN_DIR" "$VENV/bin/uvicorn" ddp_gateway.main:app \
        --host 127.0.0.1 --port "$GATEWAY_PORT" )
  fi
  wait_http "http://127.0.0.1:$GATEWAY_PORT/healthz" 40 \
    && pass "model-gateway /healthz" || fail "model-gateway 起不来（$LOG_DIR/model-gateway.log）"

  # ---- model-gateway 的 arq worker ----
  # **不可省**：网关只受理与转发，真正去调引擎、轮询、归档的是它。
  # 没有它的表现极难归因 —— 请求 200、状态查得到、error 是 null，
  # 只是那个 running 永远不变，看起来像"模型很慢"（合仓时漏掉过，F-21）。
  if ! alive "[a]rq ddp_gateway.worker"; then
    ( export SERVICE_TOKEN="$SERVICE_TOKEN" \
             REDIS_URL="redis://127.0.0.1:$REDIS_PORT/2" \
             MODELS_CONFIG="$MODELS_CONFIG"
      start_bg gateway-worker "$RUN_DIR" "$VENV/bin/arq" ddp_gateway.worker.tasks.WorkerSettings )
    sleep 3
  fi
  alive "[a]rq ddp_gateway.worker" && pass "gateway arq worker" \
    || fail "gateway worker 起不来（$LOG_DIR/gateway-worker.log）"

  # ---- corpus-api / corpus-worker ----
  corpus_env() {
    export DATABASE_URL="$corpus_db" \
           SERVICE_TOKEN="$SERVICE_TOKEN" \
           SERVICE_URL="http://127.0.0.1:$GATEWAY_PORT" \
           CONTROL_URL="http://127.0.0.1:$CONTROL_PORT" \
           PUBLIC_BASE_URL="http://127.0.0.1:$CORPUS_PORT" \
           MINIO_INTERNAL_ENDPOINT="127.0.0.1:$MINIO_PORT" \
           MINIO_PUBLIC_ENDPOINT="$PUBLIC_HOST" \
           MINIO_SECURE=false \
           MINIO_PUBLIC_SECURE="$OBJ_PUBLIC_SECURE" \
           MINIO_ACCESS_KEY="$OBJECT_ACCESS_KEY" \
           MINIO_SECRET_KEY="$OBJECT_SECRET_KEY" \
           MINIO_BUCKET="$OBJECT_BUCKET" \
           REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0" \
           DEFAULT_PARSE_ENGINE="$DEFAULT_PARSE_ENGINE" \
           CHAT_MODEL="$CHAT_MODEL"
  }
  if ! alive "ddp_corpus.main:app"; then
    ( corpus_env
      start_bg corpus-api "$RUN_DIR" "$VENV/bin/uvicorn" ddp_corpus.main:app \
        --host 127.0.0.1 --port "$CORPUS_PORT" )
  fi
  wait_http "http://127.0.0.1:$CORPUS_PORT/healthz" 60 \
    && pass "corpus-api /healthz" || fail "corpus-api 起不来（$LOG_DIR/corpus-api.log）"

  if ! alive "[d]dp-corpus-worker"; then
    ( corpus_env; start_bg corpus-worker "$RUN_DIR" "$VENV/bin/ddp-corpus-worker" ); sleep 3
  fi
  alive "[d]dp-corpus-worker" && pass "corpus-worker" \
    || fail "corpus-worker 起不来（$LOG_DIR/corpus-worker.log）"

  # ---- MCP ----
  if ! alive "ddp_mcp.server"; then
    ( export SERVICE_TOKEN="$SERVICE_TOKEN" \
             GATEWAY_URL="http://127.0.0.1:$GATEWAY_PORT" \
             CORPUS_DATABASE_URL="$corpus_db" \
             MINIO_ENDPOINT="127.0.0.1:$MINIO_PORT" \
             MINIO_ACCESS_KEY="$OBJECT_ACCESS_KEY" \
             MINIO_SECRET_KEY="$OBJECT_SECRET_KEY" \
             MINIO_BUCKET="$OBJECT_BUCKET" \
             REDIS_URL="redis://127.0.0.1:$REDIS_PORT/2" \
             MCP_HOST=127.0.0.1 MCP_PORT="$MCP_PORT"
      start_bg mcp "$RUN_DIR" "$VENV/bin/python" -m ddp_mcp.server ); sleep 4
  fi
  alive "ddp_mcp.server" && pass "mcp" || fail "mcp 起不来（$LOG_DIR/mcp.log）"

  # ---- control-api（唯一直面公网的进程）----
  if ! alive "[c]ontrol-api"; then
    ( export CONTROL_ADDR="127.0.0.1:$CONTROL_PORT" \
             CONTROL_DATABASE_URL="postgres://ddp_control:$CONTROL_DB_PASSWORD@127.0.0.1:$PG_PORT/deepdocparse" \
             CONTROL_AUTO_MIGRATE=false \
             JWT_SECRET="$JWT_SECRET" SERVICE_TOKEN="$SERVICE_TOKEN" \
             CORPUS_URL="http://127.0.0.1:$CORPUS_PORT" \
             GATEWAY_URL="http://127.0.0.1:$GATEWAY_PORT" \
             MCP_URL="http://127.0.0.1:$MCP_PORT" \
             OBJECT_ENDPOINT="127.0.0.1:$MINIO_PORT" \
             OBJECT_PUBLIC_ENDPOINT="$PUBLIC_HOST" \
             OBJECT_SECURE=false OBJECT_PUBLIC_SECURE="$OBJ_PUBLIC_SECURE" \
             OBJECT_ACCESS_KEY="$OBJECT_ACCESS_KEY" OBJECT_SECRET_KEY="$OBJECT_SECRET_KEY" \
             OBJECT_BUCKET="$OBJECT_BUCKET" \
             REDIS_URL="redis://127.0.0.1:$REDIS_PORT/1" \
             PUBLIC_BASE_URL="$PUBLIC_BASE" \
             INTERNAL_BASE_URL="http://127.0.0.1:$CONTROL_PORT" \
             CORS_ORIGINS="$PUBLIC_BASE" \
             REGISTRATION_MODE="$REGISTRATION_MODE"
      start_bg control-api "$RUN_DIR" "$BIN_DIR/control-api" )
  fi
  wait_http "http://127.0.0.1:$CONTROL_PORT/healthz" 60 \
    && pass "control-api /healthz" || fail "control-api 起不来（$LOG_DIR/control-api.log）"

  section "3. 边缘：nginx"
  mkdir -p "$DIST_SERVE"
  cp -r "$DIST/." "$DIST_SERVE/" 2>/dev/null && chmod -R a+rX "$DIST_SERVE" \
    && pass "前端产物就位 $DIST_SERVE" || fail "前端产物复制失败（$DIST 有吗？）"
  write_nginx_conf
  if ! alive "[n]ginx: master.*$RUN_DIR/nginx.conf"; then
    nginx -c "$RUN_DIR/nginx.conf" 2>>"$LOG_DIR/nginx.log"
    sleep 1
  else
    nginx -c "$RUN_DIR/nginx.conf" -s reload 2>>"$LOG_DIR/nginx.log"
  fi
  wait_http "http://127.0.0.1:$EDGE_PORT/healthz" 20 \
    && pass "nginx -> control-api /healthz" || fail "nginx 起不来（$LOG_DIR/nginx.log）"

  printf '\n入口  %s   （前端 / /api / /v1 / /mcp / /files）\n' "$PUBLIC_BASE"
  printf '本机  http://127.0.0.1:%s\n' "$EDGE_PORT"
  return $FAILS
}

write_nginx_conf() {
  # **下面这个 heredoc 没有加引号**（因为要展开 $EDGE_PORT 这些变量），
  # 于是 `反引号` 会被当成命令替换 —— 连注释里的也会执行。
  # 2026-09-03 真机上撞到过：注释里写了一个 X-Forwarded-For 的例子，
  # 起服务时刷出三行 "command not found"。nginx 变量一律写 \$name，
  # 举例子用「」不要用反引号。
  # 只有这一层对外。corpus-api / model-gateway / mcp 三个都绑在回环上
  # （mcp 靠 MCP_HOST，它在容器编排里才需要 0.0.0.0）—— 它们不做用户鉴权，
  # 只信任入口下发的 actor 上下文头，暴露出去等于任何人都能自称 admin。
  cat > "$RUN_DIR/nginx.conf" <<EOF
worker_processes auto;
error_log $LOG_DIR/nginx-error.log warn;
pid $RUN_DIR/nginx.pid;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log $LOG_DIR/nginx-access.log;
  sendfile on;
  server_tokens off;

  # 直传的分片是 16MB 一片，别在这里缓冲落盘（不变式 6：大文件不进应用进程内存，
  # 也不由应用长期中转 —— 反代同理，流式转发）
  client_max_body_size 0;
  proxy_request_buffering off;
  proxy_http_version 1.1;
  # SSE（问答流式）要逐块出，开缓冲会攒到结束才吐
  proxy_buffering off;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;

  # **X-Forwarded-For 必须由入口"重写"，不能追加。** control-api 的 clientIP()
  # 取最左一跳，而 Cloudflare 边缘也是把真实 IP **追加**在客户端自带的 XFF
  # 之后 —— 用 \$proxy_add_x_forwarded_for 的话，请求方自带一个
  # 「X-Forwarded-For: 1.2.3.4」就完全掌握了限速的键，登录与注册的暴力破解
  # 防护等于不存在（而 REGISTRATION_MODE 缺省是 open，站点在公网上）。
  # CF-Connecting-IP 由 Cloudflare 边缘写入并覆盖客户端伪造的同名头；
  # 不经隧道的部署里它不存在，退回 \$remote_addr。
  map \$http_cf_connecting_ip \$ddp_client_ip {
    ""      \$remote_addr;
    default \$http_cf_connecting_ip;
  }

  # **Host 必须原样透传，而且要用 \$http_host 不是 \$host。** 对象存储的预签名
  # 走 SigV4，签名覆盖 host —— 而 \$host 会把端口去掉：PUBLIC_HOST 带端口时
  # （脚本缺省的 127.0.0.1:8000 就是）签的是「127.0.0.1:8000」、MinIO 收到的是
  # 「127.0.0.1」，403 SignatureDoesNotMatch，而 doctor 恰好接受 403，体检照样绿。
  proxy_set_header Host \$http_host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$ddp_client_ip;
  proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;

  server {
    listen $EDGE_PORT;
    server_name _;
    root $DIST_SERVE;
    index index.html;

    location /api/     { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location /v1/      { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location /mcp      { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location /files/   { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location = /healthz { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location = /readyz  { proxy_pass http://127.0.0.1:$CONTROL_PORT; }
    location = /metrics { proxy_pass http://127.0.0.1:$CONTROL_PORT; }

    # 对象存储：**预签名 URL 的路径是 /{桶}/{键}**，签名把它一并覆盖了，
    # 所以这里既不能改路径也不能改 Host，原样转给 MinIO。
    # 浏览器直传与原件直读走这一条 —— 不经过任何应用进程。
    location /$OBJECT_BUCKET/ { proxy_pass http://127.0.0.1:$MINIO_PORT; }

    # 前端是 hash 路由，静态回退给 index.html 就够
    location / { try_files \$uri \$uri/ /index.html; }
  }
}
EOF
}

# ================================================================ stop
do_stop() {
  load_env
  nginx -c "$RUN_DIR/nginx.conf" -s quit 2>/dev/null
  # **别用 `pkill -f`**：它会连发出命令的那个 shell 一起杀掉。
  # 按 pid 精确杀，模式用方括号防自匹配。
  for pat in "[c]ontrol-api" "[d]dp-corpus-worker" "ddp_corpus.main:app" \
             "ddp_gateway.main:app" "[a]rq ddp_gateway.worker" "ddp_mcp.server" \
             "[m]inio server" "[c]loudflared tunnel"; do
    pids="$(ps -eo pid,cmd --no-headers | grep -v ' grep ' | grep -- "$pat" | awk '{print $1}')"
    [ -n "$pids" ] && kill $pids 2>/dev/null && info "停 $pat（$pids）"
  done
  redis-cli -p "$REDIS_PORT" shutdown nosave 2>/dev/null
  pg_ctlcluster "$PG_VERSION" main stop 2>/dev/null
  pass "已停"
}

# ================================================================ status
do_status() {
  load_env
  printf '%-20s %s\n' "进程" "状态"
  for kv in "control-api:[c]ontrol-api" "corpus-api:ddp_corpus.main:app" \
            "corpus-worker:[d]dp-corpus-worker" "model-gateway:ddp_gateway.main:app" \
            "gateway-worker:[a]rq ddp_gateway.worker" "mcp:ddp_mcp.server" \
            "minio:[m]inio server" "nginx:[n]ginx: master"; do
    name="${kv%%:*}"; pat="${kv#*:}"
    alive "$pat" && printf '%-20s \033[32m在跑\033[0m\n' "$name" \
                 || printf '%-20s \033[31m没了\033[0m\n' "$name"
  done
  redis-cli -p "$REDIS_PORT" ping 2>/dev/null | qgrep PONG \
    && printf '%-20s \033[32m在跑\033[0m\n' redis || printf '%-20s \033[31m没了\033[0m\n' redis
  su postgres -c "psql -p $PG_PORT -tAc 'SELECT 1'" >/dev/null 2>&1 \
    && printf '%-20s \033[32m在跑\033[0m\n' postgres || printf '%-20s \033[31m没了\033[0m\n' postgres
}

# ================================================================ doctor
# 专抓**不会报错的失效**：探针绿而链路断的那几处。
do_doctor() {
  load_env
  section "1. 探针"
  curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$CONTROL_PORT/healthz" >/dev/null \
    && pass "control-api /healthz" || fail "control-api /healthz"
  # **/readyz 是 all(up)**：注册表里注册了什么就得真的起什么，
  # 注册了没起的话探针恒 503，副本永远不接流量
  # **判据是 `"ready":true`，不是"body 里有没有 ok 这个词"。**
  # 不就绪时的 body 长这样：
  #   {"ready":false,"checks":{"postgres":"ok","objectstore":"error: ...","outbox":"ok"}}
  # 只要有一项是 ok，找词那种写法就命中了 —— 一个把 503 报成 PASS 的体检项，
  # 正是这个 doctor 声称自己专抓的那一类失效。
  local ready; ready="$(curl -s --max-time 10 --noproxy '*' "http://127.0.0.1:$CONTROL_PORT/readyz")"
  echo "$ready" | qgrep '"ready":true' && pass "control-api /readyz: $ready" \
    || fail "control-api /readyz: $ready"
  curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$GATEWAY_PORT/readyz" >/dev/null \
    && pass "model-gateway /readyz" || warn "model-gateway /readyz 不绿（注册表里注册了没起的服务？）"

  # **两个 worker 死了是零报错失效**：任务永远停在 running、error 是 null，
  # 看起来像"模型很慢"。start 查了，doctor 也必须查 —— 挂掉往往发生在起来之后。
  for kv in "gateway arq worker:[a]rq ddp_gateway.worker" "corpus-worker:[d]dp-corpus-worker"; do
    alive "${kv#*:}" && pass "${kv%%:*} 活着" \
      || fail "${kv%%:*} 死了 —— 任务会一直停在 running 而 error 是 null"
  done

  section "2. 数据库边界（企业边界 5）"
  # Go 用的角色对语料表**一个字都不该写得了**。这一层只有在数据库里做到才算数 ——
  # 静态守卫只是自觉。2026-09-02 真起全栈时发现那段 SQL 从未生效过。
  local out
  out="$(PGPASSWORD="$CONTROL_DB_PASSWORD" psql -h 127.0.0.1 -p "$PG_PORT" -U ddp_control \
        -d deepdocparse -tAc 'INSERT INTO documents (id) VALUES (gen_random_uuid())' 2>&1)"
  echo "$out" | qgrep -i 'permission denied\|denied for' \
    && pass "ddp_control 写不了语料表" \
    || fail "ddp_control 居然能写语料表（或表不存在）：$out"
  out="$(PGPASSWORD="$CORPUS_DB_PASSWORD" psql -h 127.0.0.1 -p "$PG_PORT" -U ddp_corpus \
        -d deepdocparse -tAc 'SELECT count(*) FROM documents' 2>&1)"
  echo "$out" | qgrep -E '^[0-9]+$' && pass "ddp_corpus 读得了语料表（$out 行）" \
    || fail "ddp_corpus 读不了自己的表：$out"

  section "3. 对象存储"
  curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$MINIO_PORT/minio/health/live" >/dev/null \
    && pass "MinIO 活着" || fail "MinIO 挂了"
  # 预签名那条路必须经边缘可达 —— 直传与原件直读全靠它
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --noproxy '*' \
          "http://127.0.0.1:$EDGE_PORT/$OBJECT_BUCKET/probe-does-not-exist")"
  [ "$code" = 403 ] || [ "$code" = 404 ] \
    && pass "边缘 -> MinIO 通（$code，S3 风格拒绝而不是 nginx 404）" \
    || fail "边缘到 MinIO 的那条 location 不对：HTTP $code"

  section "4. RediSearch"
  if redis-cli -p "$REDIS_PORT" module list 2>/dev/null | qgrep -i search; then
    pass "RediSearch 在（网关侧向量索引可用）"
  else
    warn "没有 RediSearch —— 网关侧向量检索走 scan 兜底（可见降级，不是坏了）"
  fi

  section "5. 前端"
  curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$EDGE_PORT/" | qgrep -i '<div id="app"' \
    && pass "前端 index.html 出得来" || fail "前端出不来"

  printf '\n%s\n' "doctor 完成，$FAILS 项失败。"
  return $FAILS
}

# ================================================================ tunnel
# Cloudflare Tunnel：把边缘端口接到公网域名。**只有出站连接** ——
# 不需要公网 IP，也不用开任何入站端口，这正是它适合 AutoDL 这类
# 只映射了两个端口的机器的原因。
#
# 令牌放 $DDP_ROOT/cloudflared.token（600），**不进 git**，
# 并且用环境变量交给 cloudflared —— 写在命令行上会出现在 `ps` 里，
# 而那个令牌等于这条隧道的完全控制权。
#
# **ingress（域名 -> 哪个本地端口）配在 Cloudflare 那一侧**，cloudflared
# 启动时拉下来。所以本地改 EDGE_PORT 是没用的：对不上的表现是公网 502
# 而本机一切正常。下面那段解析就是专门盯这个的。
do_tunnel() {
  load_env
  local token_file="$DDP_ROOT/cloudflared.token"
  [ -f "$token_file" ] || { fail "缺 $token_file（600，内容是 tunnel token）"; return 1; }
  mkdir -p "$LOG_DIR" "$RUN_DIR"

  if ! alive "cloudflared tunnel"; then
    ( export TUNNEL_TOKEN="$(cat "$token_file")"
      start_bg cloudflared "$RUN_DIR" cloudflared tunnel --no-autoupdate \
        --metrics 127.0.0.1:20241 run )
    sleep 12
  fi
  alive "cloudflared tunnel" && pass "cloudflared 在跑" || { fail "cloudflared 起不来（$LOG_DIR/cloudflared.log）"; return 1; }
  qgrep "Registered tunnel connection" "$LOG_DIR/cloudflared.log" \
    && pass "隧道已注册（$(grep -c 'Registered tunnel connection' "$LOG_DIR/cloudflared.log") 条连接）" \
    || fail "隧道没连上（$LOG_DIR/cloudflared.log）"

  # **对不上就直说。** Cloudflare 那侧指向哪个本地端口，是这条链路上
  # 最容易错又最不像错的一处：公网 502、本机全绿、日志里什么都没有。
  local cfg; cfg="$(grep -o 'Updated to new configuration config=.*' "$LOG_DIR/cloudflared.log" | tail -1)"
  if [ -n "$cfg" ]; then
    info "Cloudflare 下发的 ingress：$cfg"
    echo "$cfg" | qgrep "localhost:$EDGE_PORT\|127.0.0.1:$EDGE_PORT" \
      && pass "ingress 指向本地 $EDGE_PORT，与 EDGE_PORT 一致" \
      || fail "ingress 指的不是 $EDGE_PORT —— 公网会 502 而本机全绿。改 EDGE_PORT 或去控制台改 ingress"
  else
    warn "日志里还没看到下发的配置，稍后再 doctor 一次"
  fi
  return $FAILS
}

case "${1:-start}" in
  install) do_install ;;
  tunnel)  do_tunnel ;;
  migrate) do_migrate ;;
  start)   do_start ;;
  stop)    do_stop ;;
  status)  do_status ;;
  doctor)  do_doctor ;;
  logs)    tail -n "${3:-200}" -f "$LOG_DIR/${2:?用法：stack.bash logs <名字>}.log" ;;
  *) echo "用法：stack.bash {install|migrate|start|tunnel|stop|status|doctor|logs <名字>}" >&2; exit 1 ;;
esac
