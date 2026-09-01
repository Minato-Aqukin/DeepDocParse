#!/usr/bin/env bash
# DeepDocParse-Web 一键部署 —— 在一台干净的 Linux 服务器上从零把 Web 服务跑起来。
#
#   ssh you@server
#   git clone https://github.com/Minato-Aqukin/DeepDocParse-Web.git
#   cd DeepDocParse-Web && ./init.sh
#
# 它做四件事（也就是四个可以单独跑的子命令）：
#   configure  环境配置：拉 service 仓库、生成两份 .env（随机密钥）、写前端与 nginx 配置
#   tune       优化配置：按 CPU/内存/GPU 定并发、批量、上传上限，并生成注册表与 compose 覆盖层
#   models     模型权重：下 bge-m3（必要时转 safetensors），可选 reranker
#   start      服务启动：容器 + 数据库迁移 + backend + nginx 边缘层
#
# 设计取舍写在这里，排查时先读：
#
# 1. **对外只开一个端口**。nginx 以 network_mode: host 跑在 $PUBLIC_PORT（默认 80），
#    静态托管前端构建产物并把 /api /files /v1 /mcp /internal 反代到 backend；
#    backend 只监听 127.0.0.1:8080，不直接对公网暴露。前端 axios 的 baseURL 是 '/'，
#    路由是 hash 模式（createWebHashHistory）—— 所以静态托管不需要任何 SPA 回退规则。
# 2. **PUBLIC_BASE_URL 必须是容器能回访到的地址**。service 的 gateway 跑在容器里，
#    解析回调与稳定文件 URL 都用这个值拼，写 127.0.0.1 的话容器打到的是它自己。
#    所以缺省取本机主网卡 IP，doctor 子命令会从容器里真的 curl 一次来验证。
# 3. **引擎名三处必须一致**：Web 的 DEFAULT_PARSE_ENGINE、前端的 VITE_DEFAULT_ENGINE、
#    service 注册表里的引擎名。对不上时上传第一步就是 404 unknown_engine
#    （2026-08-19 真踩过），doctor 会显式核对这三处。
# 4. **注册表由本脚本生成**（.quickstart/models.registry.yaml），不改仓库里的
#    models.cpu.yaml。理由是 gateway 的 /readyz 是 all(up)：注册了却没起对应容器
#    会让探针恒 503。谁被注册完全取决于这次部署真的起了什么。
# 5. **backend 多进程必须配 Redis**。限速计数与对账选主都要跨进程共享，
#    否则每个 worker 各限各的（等于限速×副本数）、对账重复跑。
#    本脚本据此联动：workers>1 时自动启用 Web 侧 redis 并写 REDIS_URL。
#
# 用法：./init.sh docker [子命令] [选项]，./init.sh docker help 看全部选项。
set -euo pipefail

# ---------------------------------------------------------------- 路径与常量

WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="$(dirname "$WEB_DIR")"          # 两个仓库与 models/ 的共同父目录
SERVICE_DIR="$WORKSPACE/DeepDocParse"      # compose.cpu.yml 里写死 ../../models，故必须同级
MODELS_DIR="$WORKSPACE/models"
STATE_DIR="$WEB_DIR/.quickstart"
LOG_DIR="$STATE_DIR/logs"
RUN_DIR="$STATE_DIR/run"
STATE_FILE="$STATE_DIR/state.env"          # 记住上次选的参数，重跑子命令不用再传一遍

WEB_ENV="$WEB_DIR/.env"
SERVICE_ENV="$SERVICE_DIR/.env"
SERVICE_MCP_ENV="$SERVICE_DIR/.env.mcp"   # 语料 MCP 专用；gateway 的 Settings 是 extra=forbid，混进 .env 会让它拒绝启动
FRONT_ENV="$WEB_DIR/frontend/.env.local"
REGISTRY_FILE="$STATE_DIR/models.registry.yaml"
SERVICE_OVERRIDE="$STATE_DIR/compose.service-override.yml"
NGINX_CONF="$STATE_DIR/nginx.conf"
SYSTEMD_UNIT_FILE="$STATE_DIR/ddp-web.service"
SYSTEMD_UNIT="ddp-web"

WEB_COMPOSE="$WEB_DIR/docker/compose.web.yml"
EDGE_COMPOSE="$WEB_DIR/deploy/compose.edge.yml"
SERVICE_COMPOSE=""                          # configure 时按 profile 定

SERVICE_REPO_DEFAULT="https://github.com/Minato-Aqukin/DeepDocParse.git"
BACKEND_PORT=8080                           # 只监听 127.0.0.1，对外由 nginx 转

# ---------------------------------------------------------------- 可调选项（命令行/state 覆盖）

PROFILE="auto"                # auto | cpu | gpu
PUBLIC_HOST=""                # 对外主机名/IP，默认取主网卡地址
PUBLIC_PORT=80
PUBLIC_BASE_URL=""            # 留空则由 host+port 拼；域名 + NAT 场景可显式指定
SERVICE_REPO="$SERVICE_REPO_DEFAULT"
SERVICE_BRANCH=""
CHAT_URL=""                   # OpenAI 兼容 base（如 http://127.0.0.1:11434/v1）
CHAT_MODEL=""
CHAT_TOKEN=""
SKIP_MODELS=0                 # 不下权重：检索退回关键词路，问答不可用
WITH_RERANK=0
WITH_MINERU=0                 # 起 mineru（要 GPU + 长时间构建镜像）
WEIGHTS_SOURCE="auto"         # auto | modelscope | hf
NPM_REGISTRY="https://registry.npmmirror.com"   # lockfile 的 resolved 全指向它，见 CLAUDE.md 陷阱 10
PIP_INDEX=""                  # 留空用 pip 默认；国内建议 https://mirrors.aliyun.com/pypi/simple
ASSUME_YES=0
NO_DEPS=0                     # 跳过安装系统依赖（只检查）

# ---------------------------------------------------------------- 输出

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[36m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD$C_BLUE" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
fail() { printf '    %s✗%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()  { fail "$*"; exit 1; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# 会毁东西的确认：非交互时一律当"否"。confirm 那种"没人应答就当同意"的语义
# 用在这里等于静默批准接管别人的容器和数据卷
confirm_risky() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 1
  local reply
  printf '    %s [y/N] ' "$1"
  read -r reply || return 1
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

confirm() {   # confirm <提示>；--yes 或非交互终端一律当"是"
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 0
  local reply
  printf '    %s [Y/n] ' "$1"
  read -r reply || return 0
  case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# ---------------------------------------------------------------- .env 读写

# 只改脚本管的那些键，其余行（含注释与用户手改的值）原样保留。
set_env() {   # set_env <文件> <键> <值>
  local f="$1" k="$2" v="$3"
  touch "$f"
  if grep -qE "^${k}=" "$f"; then
    awk -v k="$k" -v v="$v" '
      $0 ~ "^" k "=" && !done { print k "=" v; done = 1; next }
      { print }
    ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    # 追加前先保证文件以换行结尾，否则会和最后一行粘在一起
    [ -s "$f" ] && [ "$(tail -c 1 "$f")" != "" ] && printf '\n' >> "$f"
    printf '%s=%s\n' "$k" "$v" >> "$f"
  fi
}

# 取第一条匹配。**不能写成 sed ... | head -1**：head 提前退出会让 sed 吃到 SIGPIPE，
# 而本脚本开了 pipefail —— 管道状态变成 141，赋值语句随即被 set -e 判成失败。
# 这一类"读一行就走"的管道在下面还有几处，都按同样的理由改成单进程写法。
get_env() {   # get_env <文件> <键>
  [ -f "$1" ] || return 0
  awk -v k="$2" 'index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' "$1"
}

# 占位值集合与两个仓库的 config.py 保持一致 —— 它们会拒绝带占位密钥启动
is_placeholder() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    ""|change-me|change-me-please|changeme|secret) return 0 ;;
    *) return 1 ;;
  esac
}

gen_secret() {   # 32 字节十六进制
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

gen_alnum() {   # gen_alnum <长度>：只用字母数字，避免进 DATABASE_URL 还要转义
  local n="${1:-24}"
  if command -v openssl >/dev/null 2>&1; then
    # 先取够量的 base64 再筛：输入有界，管道两端都能正常收尾（见 get_env 的注释）
    openssl rand -base64 "$(( n * 3 ))" | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-"$n"
  else
    # **必须循环补齐。** 62/256 的命中率下，一次取 n*4 字节筛出来的期望长度
    # 只有 ~0.97n，且方差不小 —— 直接 cut 会时不时产出一个比要求短的密钥，
    # 而且完全静默。密钥短了不会有任何报错，只是强度悄悄降了一截。
    local out=""
    while [ "${#out}" -lt "$n" ]; do
      out="$out$(LC_ALL=C dd if=/dev/urandom bs=1 count="$(( n * 4 ))" 2>/dev/null \
        | tr -dc 'A-Za-z0-9')"
    done
    printf '%s' "$out" | cut -c1-"$n"
  fi
}

save_state() {
  mkdir -p "$STATE_DIR"
  {
    echo "# 由 init.sh 生成，记住上次的部署参数；改这里不如重跑 configure"
    for k in PROFILE PUBLIC_HOST PUBLIC_PORT PUBLIC_BASE_URL SERVICE_REPO SERVICE_BRANCH \
             CHAT_URL CHAT_MODEL CHAT_TOKEN SKIP_MODELS WITH_RERANK WITH_MINERU \
             WEIGHTS_SOURCE NPM_REGISTRY PIP_INDEX SERVICE_COMPOSE; do
      printf '%s=%q\n' "$k" "${!k}"
    done
  } > "$STATE_FILE"
  # **与两份 .env 同等对待**：这里面存着 CHAT_TOKEN（上游 API key）。
  # 缺省 0644 等于把它摊给这台机器上的每个用户看
  chmod 600 "$STATE_FILE"
}

# 只吃一次：main 在解析命令行之前调用它，之后子命令里再调都是空转 ——
# 否则子命令里的 load_state 会把这一次命令行传的参数又盖回上次的值
STATE_LOADED=0
load_state() {
  [ "$STATE_LOADED" = 1 ] && return 0
  STATE_LOADED=1
  [ -f "$STATE_FILE" ] && . "$STATE_FILE" || true
}

# ---------------------------------------------------------------- 硬件探测与调参（优化配置）

CPU_CORES=1; MEM_MB=1024; DISK_FREE_GB=0; GPU_COUNT=0; GPU_NAME=""; TIER="small"
BACKEND_WORKERS=1; EMBEDDING_BATCH=8; TEI_MAX_BATCH_TOKENS=2048; TEI_CLIENT_BATCH=16
MAX_UPLOAD_MB=50; EXTRACT_CONCURRENCY=2; EXTRACT_DOC_CONCURRENCY=1; QA_RATE=20
PARSE_QUEUE_MAX=50; VQA_MAX_CONCURRENCY=2; MINERU_DEVICE="cpu"; TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3"
NEED_REDIS=0

detect_hardware() {
  CPU_CORES="$(nproc 2>/dev/null || echo 1)"
  MEM_MB="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 1024)"
  DISK_FREE_GB="$(df -P -BG "$WORKSPACE" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}')"
  DISK_FREE_GB="${DISK_FREE_GB:-0}"
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    # grep -c 没匹配时输出 0 但返回 1；这里用 || true 而不是 || echo 0，
    # 否则 pipefail 下会拼出 "0\n0" 这种东西，后面 [ -gt ] 直接报 integer expression expected
    GPU_COUNT="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)"
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | awk 'NR==1' || true)"
  fi
  case "$GPU_COUNT" in ''|*[!0-9]*) GPU_COUNT=0 ;; esac
}

# 三档全靠内存分：这套东西里几乎每个上限都是内存约束，不是算力约束。
#   - 上传体要整个进内存（算 sha256 当 doc_id 再 put 进 MinIO），所以 MAX_UPLOAD 跟内存走
#   - TEI 按 --max-batch-tokens 预热，bge-m3 支持 8192 上下文，不限住会直接 OOM（实测 exit 137）
#   - backend 每个 worker 常驻 ~250MB，PG + MinIO + gateway + TEI 另算
tune_for_hardware() {
  if   [ "$MEM_MB" -lt 6000 ];  then TIER="small"
  elif [ "$MEM_MB" -lt 14000 ]; then TIER="medium"
  else                               TIER="large"
  fi

  case "$TIER" in
    small)
      BACKEND_WORKERS=1; EMBEDDING_BATCH=8;  TEI_MAX_BATCH_TOKENS=2048; TEI_CLIENT_BATCH=16
      MAX_UPLOAD_MB=50;  EXTRACT_CONCURRENCY=2; EXTRACT_DOC_CONCURRENCY=1
      PARSE_QUEUE_MAX=50;  VQA_MAX_CONCURRENCY=2; QA_RATE=10 ;;
    medium)
      BACKEND_WORKERS=2; EMBEDDING_BATCH=16; TEI_MAX_BATCH_TOKENS=4096; TEI_CLIENT_BATCH=32
      MAX_UPLOAD_MB=100; EXTRACT_CONCURRENCY=4; EXTRACT_DOC_CONCURRENCY=2
      PARSE_QUEUE_MAX=100; VQA_MAX_CONCURRENCY=4; QA_RATE=20 ;;
    large)
      BACKEND_WORKERS=4; EMBEDDING_BATCH=24; TEI_MAX_BATCH_TOKENS=8192; TEI_CLIENT_BATCH=32
      MAX_UPLOAD_MB=200; EXTRACT_CONCURRENCY=6; EXTRACT_DOC_CONCURRENCY=3
      PARSE_QUEUE_MAX=200; VQA_MAX_CONCURRENCY=8; QA_RATE=30 ;;
  esac

  # worker 数还要受核数限制：uvicorn 是多进程模型，进程数超过核数只会互相抢
  local by_core=$(( CPU_CORES / 2 )); [ "$by_core" -lt 1 ] && by_core=1
  [ "$BACKEND_WORKERS" -gt "$by_core" ] && BACKEND_WORKERS="$by_core"

  # EMBEDDING_BATCH_SIZE 必须**严格小于** TEI 的 --max-client-batch-size，
  # 否则长文档整批被拒 413（service 侧 worker 已经踩过）
  [ "$EMBEDDING_BATCH" -ge "$TEI_CLIENT_BATCH" ] && EMBEDDING_BATCH=$(( TEI_CLIENT_BATCH - 1 ))

  # 多进程 = 必须有跨进程的 Redis：限速令牌桶与对账选主都在那儿。
  # 不联动的话限速变成"每 worker 各限各的"，对账则每个 worker 都跑一遍
  [ "$BACKEND_WORKERS" -gt 1 ] && NEED_REDIS=1

  # profile
  if [ "$PROFILE" = "auto" ]; then
    if [ "$GPU_COUNT" -gt 0 ]; then PROFILE="gpu"; else PROFILE="cpu"; fi
  fi
  if [ "$PROFILE" = "gpu" ]; then
    [ "$GPU_COUNT" -gt 0 ] || warn "指定了 --profile gpu 但没探到 NVIDIA 卡，容器起不来时先查 nvidia-container-toolkit"
    TEI_IMAGE="ghcr.io/huggingface/text-embeddings-inference:1.9.3"
  fi

  # 解析引擎：显式要了 mineru 就用它（这是用户的选择，不替他改），否则一律 borndigital
  #（进程内、不下模型、不要显卡，只覆盖有文字层的 PDF）
  if [ "$WITH_MINERU" = 1 ]; then
    DEFAULT_ENGINE="mineru"
    MINERU_DEVICE="cuda"
    if [ "$PROFILE" != "gpu" ]; then
      MINERU_DEVICE="cpu"
      warn "--with-mineru 但没有 GPU：mineru 会退 CPU backend，比 GPU 慢一到两个数量级"
    fi
  else
    DEFAULT_ENGINE="borndigital"
  fi
  SERVICE_COMPOSE="$SERVICE_DIR/docker/compose.cpu.yml"   # 覆盖层负责把 GPU 差异补上
}

print_plan() {
  info "CPU $CPU_CORES 核 · 内存 ${MEM_MB}MB · 可用磁盘 ${DISK_FREE_GB}GB · GPU ${GPU_COUNT}${GPU_NAME:+（$GPU_NAME）}"
  info "档位 $TIER · profile $PROFILE · 解析引擎 $DEFAULT_ENGINE"
  info "backend workers $BACKEND_WORKERS$([ "$NEED_REDIS" = 1 ] && echo "（已联动启用 Redis）")"
  info "上传上限 ${MAX_UPLOAD_MB}MB · embedding 批 $EMBEDDING_BATCH · TEI 批 token $TEI_MAX_BATCH_TOKENS"
  info "抽取并发 字段 $EXTRACT_CONCURRENCY × 文档 $EXTRACT_DOC_CONCURRENCY"
}

# 主网卡地址：容器要靠它回访 backend，写 127.0.0.1 的话容器打到的是它自己
detect_host_ip() {
  local ip=""
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)"
  [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "${ip:-127.0.0.1}"
}

# ---------------------------------------------------------------- deps：系统依赖

PKG=""
detect_pkg_manager() {
  for m in apt-get dnf yum pacman zypper; do
    command -v "$m" >/dev/null 2>&1 && { PKG="$m"; return 0; }
  done
  PKG=""
}

SUDO=""
need_sudo() {
  if [ "$(id -u)" = 0 ]; then SUDO=""; return 0; fi
  command -v sudo >/dev/null 2>&1 || return 1
  SUDO="sudo"
}

pkg_install() {   # pkg_install <包名...>
  need_sudo || { warn "没有 root 也没有 sudo，请手动安装：$*"; return 1; }
  case "$PKG" in
    apt-get) $SUDO apt-get update -qq && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    dnf)     $SUDO dnf install -y "$@" ;;
    yum)     $SUDO yum install -y "$@" ;;
    pacman)  $SUDO pacman -S --needed --noconfirm "$@" ;;
    zypper)  $SUDO zypper install -y "$@" ;;
    *)       warn "未知包管理器，请手动安装：$*"; return 1 ;;
  esac
}

# 找一个 >=3.11 的 python（backend 的 requires-python）。系统 python 可能是 3.14，
# 那个版本能跑 backend，但装不上 torch —— 权重转换那步会另外找解释器
PYTHON=""
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "$c")"; return 0
    fi
  done
  return 1
}

install_docker() {
  command -v docker >/dev/null 2>&1 && return 0
  info "安装 docker ..."
  case "$PKG" in
    pacman) pkg_install docker docker-compose docker-buildx || return 1 ;;
    *)
      # 官方便捷脚本覆盖 Debian/Ubuntu/RHEL 系，比各发行版仓库里的版本新
      if need_sudo && command -v curl >/dev/null 2>&1; then
        curl -fsSL https://get.docker.com -o "$STATE_DIR/get-docker.sh" && $SUDO sh "$STATE_DIR/get-docker.sh"
      else
        pkg_install docker.io docker-compose-plugin || pkg_install docker || return 1
      fi ;;
  esac
  need_sudo && { $SUDO systemctl enable --now docker || true; $SUDO usermod -aG docker "$(id -un)" || true; }
  warn "已把当前用户加入 docker 组 —— 需要重新登录（或 newgrp docker）才生效"
}

install_node() {
  if command -v node >/dev/null 2>&1; then
    local major; major="$(node -v | sed 's/^v\([0-9]*\).*/\1/')"
    [ "${major:-0}" -ge 22 ] && return 0
    warn "node $(node -v) 低于前端要求的 22，尝试装新版"
  fi
  info "安装 Node.js 22 ..."
  case "$PKG" in
    pacman) pkg_install nodejs npm || return 1 ;;
    apt-get)
      need_sudo || return 1
      curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash - && pkg_install nodejs ;;
    dnf|yum)
      need_sudo || return 1
      curl -fsSL https://rpm.nodesource.com/setup_22.x | $SUDO -E bash - && pkg_install nodejs ;;
    *) pkg_install nodejs npm || return 1 ;;
  esac
}

cmd_deps() {
  step "检查运行依赖"
  detect_pkg_manager
  mkdir -p "$STATE_DIR"

  local missing=0
  for c in git curl tar; do
    command -v "$c" >/dev/null 2>&1 && ok "$c" || { fail "$c 缺失"; missing=1; }
  done
  if [ "$missing" = 1 ] && [ "$NO_DEPS" != 1 ]; then
    pkg_install git curl tar || true
  fi

  if command -v docker >/dev/null 2>&1; then ok "docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,)"
  elif [ "$NO_DEPS" = 1 ]; then die "docker 缺失（--no-deps 模式不自动安装）"
  else install_docker || die "docker 安装失败，请按发行版文档手动装好再重跑"
  fi

  docker compose version >/dev/null 2>&1 || die "docker compose v2 插件不可用（docker compose version 失败）"
  ok "docker compose $(docker compose version --short 2>/dev/null)"

  docker info >/dev/null 2>&1 || die "连不上 docker daemon。要么服务没起（systemctl start docker），要么当前用户不在 docker 组（加组后需重新登录）"

  if find_python; then ok "python $("$PYTHON" -V 2>&1 | awk '{print $2}') -> $PYTHON"
  elif [ "$NO_DEPS" = 1 ]; then die "找不到 >=3.11 的 python"
  else
    pkg_install python3 python3-venv python3-pip 2>/dev/null || pkg_install python python-pip || true
    find_python || die "找不到 >=3.11 的 python，请手动安装（backend 的 requires-python 是 >=3.11）"
    ok "python $("$PYTHON" -V 2>&1 | awk '{print $2}')"
  fi
  "$PYTHON" -c 'import venv' 2>/dev/null || { [ "$NO_DEPS" = 1 ] || pkg_install python3-venv || true; }

  if command -v node >/dev/null 2>&1 && [ "$(node -v | sed 's/^v\([0-9]*\).*/\1/')" -ge 22 ] 2>/dev/null; then
    ok "node $(node -v)"
  elif [ "$NO_DEPS" = 1 ]; then die "node >=22 缺失"
  else install_node || die "Node.js 安装失败，请手动装 >=22 再重跑"
  fi
  command -v npm >/dev/null 2>&1 || die "npm 缺失"
}

# ---------------------------------------------------------------- fetch：拉 service 仓库

cmd_fetch() {
  step "拉取 service 仓库（DeepDocParse）"
  # Web 只依赖 service 的 openapi.yaml 契约，但解析平面本身跑在 service 里 ——
  # 没有它 Web 能起来，上传后第一步就断。两个仓库必须同级：
  # compose.cpu.yml 里挂权重的路径写死了 ../../models
  if [ -d "$SERVICE_DIR/.git" ]; then
    info "已存在：$SERVICE_DIR"
    if confirm "拉取最新代码（git pull）？"; then
      git -C "$SERVICE_DIR" pull --ff-only || warn "git pull 失败（本地有改动？），继续用当前代码"
    fi
  else
    info "clone $SERVICE_REPO -> $SERVICE_DIR"
    local args=(--depth 1)
    [ -n "$SERVICE_BRANCH" ] && args+=(--branch "$SERVICE_BRANCH")
    git clone "${args[@]}" "$SERVICE_REPO" "$SERVICE_DIR" || die "clone 失败。私有仓库请先配好凭据：
        gh auth login   或   git clone git@github.com:Minato-Aqukin/DeepDocParse.git '$SERVICE_DIR'
        也可以用 --service-repo <url> 指定自己的镜像地址"
  fi
  [ -f "$SERVICE_DIR/docker/compose.cpu.yml" ] || die "$SERVICE_DIR 不像 DeepDocParse 仓库（缺 docker/compose.cpu.yml）"
  ok "service 仓库就绪：$(git -C "$SERVICE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
}

# ---------------------------------------------------------------- configure：环境配置

DEFAULT_ENGINE="borndigital"
CHAT_BASE=""            # 去掉 /v1 的根，进 service 注册表
CHAT_COMPLETIONS=""     # 完整的 /v1/chat/completions，进 Web 的 CHAT_URL
CHAT_NEEDS_HOSTGW=0     # chat 端点在宿主机上 -> 容器要 host.docker.internal

# 宿主机地址在容器里不可达：127.0.0.1 指的是容器自己。统一改写成 host.docker.internal，
# 并在 compose 覆盖层里给相关服务加 extra_hosts: host-gateway（Linux 上必须显式加）。
#
# 这个函数**必须保持纯函数**：它的调用点是 $( ) 命令替换，子 shell 里的赋值传不回来。
# "要不要加 extra_hosts" 由 parse_chat_url 在主 shell 里判定（踩过一次：覆盖层少了
# extra_hosts，gateway 容器解析不了 host.docker.internal，抽取平面整体 error）。
to_container_url() {   # to_container_url <url>
  case "$1" in
    *://127.0.0.1*|*://localhost*|*://0.0.0.0*)
      printf '%s' "$1" | sed -E 's#://(127\.0\.0\.1|localhost|0\.0\.0\.0)#://host.docker.internal#' ;;
    *) printf '%s' "$1" ;;
  esac
}

parse_chat_url() {
  [ -n "$CHAT_URL" ] || return 0
  local u="${CHAT_URL%/}"
  case "$u" in
    */chat/completions) CHAT_COMPLETIONS="$u"; CHAT_BASE="${u%/v1/chat/completions}" ;;
    */v1)               CHAT_COMPLETIONS="$u/chat/completions"; CHAT_BASE="${u%/v1}" ;;
    *)                  CHAT_COMPLETIONS="$u/v1/chat/completions"; CHAT_BASE="$u" ;;
  esac
  case "$CHAT_BASE" in
    *://127.0.0.1*|*://localhost*|*://0.0.0.0*) CHAT_NEEDS_HOSTGW=1 ;;
  esac
}

write_web_env() {
  # 已有 .env 时只覆盖脚本管的键，密钥若已是真值就保留 —— 重跑 configure 不该
  # 把用户的会话（JWT_SECRET 变了 = 全员掉线）和 service 的握手令牌换掉
  local jwt svc pgpass mk ms
  jwt="$(get_env "$WEB_ENV" JWT_SECRET)";        is_placeholder "$jwt"    && jwt="$(gen_secret)"
  svc="$(get_env "$WEB_ENV" SERVICE_TOKEN)";     is_placeholder "$svc"    && svc="$(gen_secret)"
  pgpass="$(get_env "$WEB_ENV" POSTGRES_PASSWORD)"
  case "$pgpass" in ""|ddp) pgpass="$(gen_alnum 24)" ;; esac
  mk="$(get_env "$WEB_ENV" MINIO_ACCESS_KEY)"
  case "$mk" in ""|minioadmin) mk="ddp$(gen_alnum 13)" ;; esac
  ms="$(get_env "$WEB_ENV" MINIO_SECRET_KEY)"
  case "$ms" in ""|minioadmin) ms="$(gen_alnum 32)" ;; esac

  [ -f "$WEB_ENV" ] || cp "$WEB_DIR/.env.example" "$WEB_ENV"

  set_env "$WEB_ENV" JWT_SECRET "$jwt"
  set_env "$WEB_ENV" SERVICE_TOKEN "$svc"
  set_env "$WEB_ENV" POSTGRES_USER ddp
  set_env "$WEB_ENV" POSTGRES_PASSWORD "$pgpass"
  set_env "$WEB_ENV" POSTGRES_DB deepdocparse
  set_env "$WEB_ENV" DATABASE_URL "postgresql+asyncpg://ddp:$pgpass@127.0.0.1:15432/deepdocparse"
  set_env "$WEB_ENV" MINIO_ACCESS_KEY "$mk"
  set_env "$WEB_ENV" MINIO_SECRET_KEY "$ms"
  set_env "$WEB_ENV" MINIO_INTERNAL_ENDPOINT "127.0.0.1:19000"
  set_env "$WEB_ENV" MINIO_PUBLIC_ENDPOINT "127.0.0.1:19000"
  set_env "$WEB_ENV" MINIO_SECURE false
  set_env "$WEB_ENV" MINIO_BUCKET deepdocparse

  set_env "$WEB_ENV" SERVICE_URL "http://127.0.0.1:9000"
  set_env "$WEB_ENV" MCP_URL "http://127.0.0.1:9100"
  # 引擎名三处一致的第一处（另两处：前端 .env.local、service 注册表）
  set_env "$WEB_ENV" DEFAULT_PARSE_ENGINE "$DEFAULT_ENGINE"
  set_env "$WEB_ENV" PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
  set_env "$WEB_ENV" CORS_ORIGINS "$PUBLIC_BASE_URL,http://localhost:5173,http://127.0.0.1:5173"

  if [ -n "$CHAT_COMPLETIONS" ]; then
    set_env "$WEB_ENV" CHAT_URL "$CHAT_COMPLETIONS"
    [ -n "$CHAT_MODEL" ] && set_env "$WEB_ENV" CHAT_MODEL "$CHAT_MODEL"
    [ -n "$CHAT_TOKEN" ] && set_env "$WEB_ENV" CHAT_TOKEN "$CHAT_TOKEN"
  fi

  # 调参结果落到配置（优化配置）
  set_env "$WEB_ENV" MAX_UPLOAD_BYTES "$(( MAX_UPLOAD_MB * 1024 * 1024 ))"
  set_env "$WEB_ENV" EMBEDDING_BATCH_SIZE "$EMBEDDING_BATCH"
  set_env "$WEB_ENV" EXTRACT_CONCURRENCY "$EXTRACT_CONCURRENCY"
  set_env "$WEB_ENV" EXTRACT_DOC_CONCURRENCY "$EXTRACT_DOC_CONCURRENCY"
  set_env "$WEB_ENV" QA_RATE_PER_MIN "$QA_RATE"
  if [ "$NEED_REDIS" = 1 ]; then
    set_env "$WEB_ENV" REDIS_URL "redis://127.0.0.1:16379/0"
  else
    set_env "$WEB_ENV" REDIS_URL ""
  fi
  if [ "$WITH_RERANK" = 1 ]; then
    set_env "$WEB_ENV" RERANK_ENABLED true
    # RERANK_CANDIDATES 必须显著大于 QA_TOP_K，否则精排无米下锅（config 里有校验）
    set_env "$WEB_ENV" RERANK_CANDIDATES 24
  else
    set_env "$WEB_ENV" RERANK_ENABLED false
  fi

  chmod 600 "$WEB_ENV"
  ok "$WEB_ENV"
}

write_service_env() {
  local svc pguser pgpass pgdb mk ms
  svc="$(get_env "$WEB_ENV" SERVICE_TOKEN)"
  pguser="$(get_env "$WEB_ENV" POSTGRES_USER)"
  pgpass="$(get_env "$WEB_ENV" POSTGRES_PASSWORD)"
  pgdb="$(get_env "$WEB_ENV" POSTGRES_DB)"
  mk="$(get_env "$WEB_ENV" MINIO_ACCESS_KEY)"
  ms="$(get_env "$WEB_ENV" MINIO_SECRET_KEY)"
  [ -f "$SERVICE_ENV" ] || cp "$SERVICE_DIR/.env.example" "$SERVICE_ENV" 2>/dev/null || touch "$SERVICE_ENV"
  # 两边必须是同一个令牌：Web -> service 的所有调用与 service -> Web 的回调都靠它
  set_env "$SERVICE_ENV" SERVICE_TOKEN "$svc"
  set_env "$SERVICE_ENV" REDIS_URL "redis://redis:6379/0"
  set_env "$SERVICE_ENV" MODELS_CONFIG "/app/models.registry.yaml"
  set_env "$SERVICE_ENV" PARSE_QUEUE_MAX "$PARSE_QUEUE_MAX"
  set_env "$SERVICE_ENV" QUEUE_INFLIGHT_TTL 2400
  set_env "$SERVICE_ENV" VQA_MAX_CONCURRENCY "$VQA_MAX_CONCURRENCY"
  set_env "$SERVICE_ENV" RESULT_TTL 86400
  chmod 600 "$SERVICE_ENV"

  # 阶段 7 的五个语料 MCP 工具直读 Web 数据面。quickstart 会随机生成 PG/MinIO
  # 密钥，所以绝不能依赖 service compose 里的开发占位默认值。
  #
  # **单独一份文件，不写进 $SERVICE_ENV。** gateway 的 Settings 是
  # `extra="forbid"`，而 pydantic-settings 会直接读 cwd 下的 `.env` 文件 ——
  # 这几个键混进去，裸进程起 gateway 会当场 `extra_forbidden` 拒绝启动
  # （2026-08-29 在 AutoDL 上实测撞到）。容器部署本来就只吃 compose 里
  # 显式列出的 environment，不受影响。
  set_env "$SERVICE_MCP_ENV" CORPUS_DATABASE_URL \
    "postgresql+asyncpg://$pguser:$pgpass@host.docker.internal:15432/$pgdb"
  set_env "$SERVICE_MCP_ENV" MINIO_ENDPOINT "http://host.docker.internal:19000"
  set_env "$SERVICE_MCP_ENV" MINIO_ACCESS_KEY "$mk"
  set_env "$SERVICE_MCP_ENV" MINIO_SECRET_KEY "$ms"
  set_env "$SERVICE_MCP_ENV" MINIO_BUCKET "$(get_env "$WEB_ENV" MINIO_BUCKET)"
  set_env "$SERVICE_MCP_ENV" MCP_PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
  chmod 600 "$SERVICE_MCP_ENV"
  ok "$SERVICE_ENV（SERVICE_TOKEN 与 Web 侧同值）"
  ok "$SERVICE_MCP_ENV（语料 MCP 的 PG/MinIO 凭据；单独一份，gateway 不读）"
}

write_frontend_env() {
  # 引擎名三处一致的第二处。注意浏览器里 localStorage 的 ddp.pref.engine 会盖过它，
  # 换过引擎的浏览器要去设置页重选一次
  set_env "$FRONT_ENV" VITE_DEFAULT_ENGINE "$DEFAULT_ENGINE"
  ok "$FRONT_ENV"
}

# 注册表由本脚本生成而不是用仓库里的 models.cpu.yaml：gateway 的 /readyz 是 all(up)，
# 注册了却没起对应容器就会让探针恒 503。这里注册的每一项都对应一个真的会起的容器。
write_registry() {
  local chat_ep=""
  [ -n "$CHAT_BASE" ] && chat_ep="$(to_container_url "$CHAT_BASE")"

  {
    echo "# 由 init.sh 生成 —— 不要手改，重跑 ./init.sh docker configure 会覆盖。"
    echo "# 只登记这次部署真的起了的容器：gateway 的 /readyz 是 all(up)。"
    echo
    echo "parse_engines:"
    echo "  borndigital:"
    echo "    endpoint: \"inproc://borndigital\"    # 进程内引擎，没有远端地址"
    echo "    runtime: borndigital"
    echo "    capabilities: [parse]"
    [ "$DEFAULT_ENGINE" = "borndigital" ] && echo "    default: true"
    if [ "$WITH_MINERU" = 1 ]; then
      echo "  mineru:"
      echo "    endpoint: \"http://mineru:8000\""
      echo "    runtime: mineru-api"
      echo "    capabilities: [parse]"
      [ "$DEFAULT_ENGINE" = "mineru" ] && echo "    default: true"
      echo "    options:"
      echo "      backend: pipeline"
    fi
    if [ "$SKIP_MODELS" != 1 ]; then
      echo
      echo "embedding_models:"
      echo "  bge-m3:"
      echo "    endpoint: \"http://embed:8080\""
      echo "    default: true"
      echo "    runtime: tei"
      echo "    capabilities: [dense]"
    fi
    if [ "$WITH_RERANK" = 1 ]; then
      echo
      echo "rerank_models:"
      echo "  bge-reranker-v2-m3:"
      echo "    endpoint: \"http://rerank:8080\""
      echo "    default: true"
      echo "    runtime: tei"
      echo "    capabilities: [rerank]"
    fi
    if [ -n "$chat_ep" ]; then
      echo
      echo "# 抽取平面 /v1/extract 读的是这一段（不是 Web 的 CHAT_URL）。"
      echo "# 抽值只用文本，所以这里不必真是视觉模型；视觉核对是可选项。"
      echo "vqa_models:"
      # 键必须加引号：Ollama 的模型名普遍带冒号（qwen3:8b），裸写出来不是合法 YAML
      echo "  \"${CHAT_MODEL:-chat}\":"
      echo "    endpoint: \"$chat_ep\""
      echo "    default: true"
      # **必须显式写能力，不能靠段名回填。** vqa_models 段的缺省能力是 [vision]，
      # 而这里的端点是**为抽取选的**（缺省引导用户填 Ollama 之类的文本模型）——
      # 让它被默默标成"看得见图"的话，出处的视觉核对会挑中它，
      # 模型收不到图、只会对着指令自说自话，于是**每条好出处都被判成 parse_mismatch**。
      # 如实声明 [instruct] 之后：抽值照常挑中它，核对挑不到任何视觉模型，
      # 落到可见降级 vision_unavailable —— 说"没法核对"，而不是"这条出处可疑"。
      # 真配了视觉模型想开核对的，在生成的注册表里把这行改成 [instruct, vision]。
      echo "    capabilities: [instruct]"
    fi
  } > "$REGISTRY_FILE"
  ok "$REGISTRY_FILE"
}

write_service_override() {
  local gpu_block="" hostgw_block=""
  if [ "$PROFILE" = "gpu" ]; then
    gpu_block=$'    deploy:\n      resources:\n        reservations:\n          devices:\n            - driver: nvidia\n              count: 1\n              capabilities: [gpu]'
  fi
  if [ "$CHAT_NEEDS_HOSTGW" = 1 ]; then
    hostgw_block=$'    extra_hosts: ["host.docker.internal:host-gateway"]'
  fi

  {
    echo "# 由 init.sh 生成 —— 叠在 DeepDocParse/docker/compose.cpu.yml 之上。"
    echo "# 覆盖层只做三件事：换注册表、按硬件调 TEI 的批量参数、加重启策略。"
    echo "# 路径一律写绝对路径：覆盖文件里的相对路径按“第一个 -f 文件所在目录”解析，容易踩空。"
    echo "name: ddp-service"
    echo "services:"
    for s in gateway arq-worker; do
      echo "  $s:"
      echo "    restart: unless-stopped"
      echo "    environment:"
      echo "      MODELS_CONFIG: /app/models.registry.yaml"
      echo "    volumes:"
      echo "      - $REGISTRY_FILE:/app/models.registry.yaml:ro"
      [ -n "$hostgw_block" ] && echo "$hostgw_block"
    done
    echo "  mcp-server:"
    echo "    restart: unless-stopped"
    echo "  redis:"
    echo "    restart: unless-stopped"
    if [ "$SKIP_MODELS" != 1 ]; then
      echo "  embed:"
      echo "    restart: unless-stopped"
      echo "    image: $TEI_IMAGE"
      # --max-batch-tokens 必须限住：bge-m3 支持 8192 上下文，按最大长度预热会吃爆内存
      echo "    command: >"
      echo "      --model-id /data/bge-m3 --pooling cls --port 8080"
      echo "      --max-batch-tokens $TEI_MAX_BATCH_TOKENS --max-client-batch-size $TEI_CLIENT_BATCH"
      echo "    volumes:"
      echo "      - $MODELS_DIR/bge-m3:/data/bge-m3:ro"
      [ -n "$gpu_block" ] && echo "$gpu_block"
    fi
    if [ "$WITH_RERANK" = 1 ]; then
      echo "  rerank:"
      echo "    restart: unless-stopped"
      echo "    image: $TEI_IMAGE"
      echo "    command: >"
      echo "      --model-id /data/bge-reranker-v2-m3 --port 8080"
      echo "      --max-batch-tokens $TEI_MAX_BATCH_TOKENS --max-client-batch-size $TEI_CLIENT_BATCH"
      echo "    ports: [\"18082:8080\"]"
      echo "    volumes:"
      echo "      - $MODELS_DIR/bge-reranker-v2-m3:/data/bge-reranker-v2-m3:ro"
      [ -n "$gpu_block" ] && echo "$gpu_block"
    fi
    if [ "$WITH_MINERU" = 1 ]; then
      echo "  mineru:"
      echo "    restart: unless-stopped"
      echo "    build:"
      echo "      context: $SERVICE_DIR/docker/mineru"
      echo "    image: mineru:3.4.4"
      echo "    command: mineru-api --host 0.0.0.0 --port 8000"
      echo "    environment:"
      echo "      MINERU_DEVICE_MODE: $MINERU_DEVICE"
      [ -n "$gpu_block" ] && echo "$gpu_block"
    fi
  } > "$SERVICE_OVERRIDE"
  ok "$SERVICE_OVERRIDE"
}

write_nginx_conf() {
  local upstream="127.0.0.1:$BACKEND_PORT"
  cat > "$NGINX_CONF" <<NGINX
# 由 init.sh 生成 —— 重跑 ./init.sh docker configure 会覆盖。
# 前端是 hash 路由（createWebHashHistory），静态托管不需要任何 SPA 回退规则。
server {
    listen $PUBLIC_PORT default_server;
    listen [::]:$PUBLIC_PORT default_server;
    server_name _;

    # 上传体与 backend 的 MAX_UPLOAD_BYTES 对齐；这里小了会在 backend 之前先 413
    client_max_body_size ${MAX_UPLOAD_MB}m;

    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files \$uri \$uri/ /index.html;
        # 构建产物带内容哈希，可长缓存；index.html 不能缓存，否则发新版后打不开
        location = /index.html { add_header Cache-Control "no-store"; }
        location /assets/ { add_header Cache-Control "public, max-age=31536000, immutable"; }
    }

    # /api  前端接口（JWT）      /files 稳定文件 URL（token 即凭证）
    # /v1   对外 API（sk- key）  /mcp   MCP 反代
    # /internal service 的解析回调（SERVICE_TOKEN 鉴权）——
    #   它是 PUBLIC_BASE_URL 的一部分，gateway 容器靠它回访，不能不转
    location ~ ^/(api|files|v1|mcp|internal|healthz|readyz) {
        proxy_pass http://$upstream;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";

        # 问答与抽取都是 SSE 流式：缓冲一开，前端要等整段结束才看到第一个字。
        # 读超时要盖住 CHAT_READ_TIMEOUT(900s) —— 视觉模型在 CPU 上出首字可能要几分钟
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 960s;
        proxy_send_timeout 960s;
    }

    # /metrics 不对外：它是 Prometheus 口径的内部指标，从 127.0.0.1:$BACKEND_PORT 直接抓
}
NGINX
  ok "$NGINX_CONF（对外端口 $PUBLIC_PORT）"
}

# 容器都带 restart: unless-stopped，重启机器会自己回来；宿主上的 backend 不会。
# 单元文件先生成好放着，装不装由 `./init.sh docker systemd` 决定（那一步要 root）
write_systemd_unit() {
  local workers="$BACKEND_WORKERS"
  [ "$NEED_REDIS" = 1 ] || workers=1
  local exec_start="$WEB_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $BACKEND_PORT"
  [ "$workers" -gt 1 ] && exec_start="$exec_start --workers $workers"
  cat > "$SYSTEMD_UNIT_FILE" <<UNIT
# 由 init.sh 生成。安装：./init.sh docker systemd
[Unit]
Description=DeepDocParse-Web backend
After=docker.service network-online.target
Wants=docker.service

[Service]
Type=exec
User=$(id -un)
WorkingDirectory=$WEB_DIR/backend
ExecStart=$exec_start
# 依赖（PG/MinIO/gateway）还没起来时 backend 会起不来：让它重试而不是放弃。
# 结果丢不了 —— 停机期间漏掉的回调由 reconcile 在启动时补
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  ok "$SYSTEMD_UNIT_FILE（要开机自启就跑 ./init.sh docker systemd）"
}

# 装过单元就一切以 systemd 为准：start/stop/status/logs 全部改走 systemctl，
# 否则脚本会再起一个 uvicorn 跟它抢 8080
systemd_managed() { [ -f "/etc/systemd/system/$SYSTEMD_UNIT.service" ]; }

cmd_systemd() {
  step "安装 systemd 单元（开机自启）"
  load_state
  command -v systemctl >/dev/null 2>&1 || die "这台机器没有 systemd"
  [ -f "$SYSTEMD_UNIT_FILE" ] || die "缺 $SYSTEMD_UNIT_FILE，先跑 ./init.sh docker configure"
  need_sudo || die "需要 root 或 sudo"
  $SUDO cp "$SYSTEMD_UNIT_FILE" "/etc/systemd/system/$SYSTEMD_UNIT.service"
  $SUDO systemctl daemon-reload
  # 交给 systemd 管之前先把脚本自己起的那个收掉，否则两个进程抢 8080
  kill_tree backend
  $SUDO systemctl enable --now "$SYSTEMD_UNIT"
  ok "已启用 $SYSTEMD_UNIT.service（此后 start/stop 会自动走 systemctl）"
  dim "看日志：journalctl -u $SYSTEMD_UNIT -f"
}

cmd_configure() {
  step "环境配置"
  mkdir -p "$STATE_DIR" "$LOG_DIR" "$RUN_DIR" "$MODELS_DIR"
  [ -d "$SERVICE_DIR/.git" ] || cmd_fetch

  detect_hardware
  tune_for_hardware
  [ -n "$PUBLIC_HOST" ] || PUBLIC_HOST="$(detect_host_ip)"
  if [ -z "$PUBLIC_BASE_URL" ]; then
    if [ "$PUBLIC_PORT" = 80 ]; then PUBLIC_BASE_URL="http://$PUBLIC_HOST"
    else PUBLIC_BASE_URL="http://$PUBLIC_HOST:$PUBLIC_PORT"; fi
  fi
  case "$PUBLIC_BASE_URL" in
    *127.0.0.1*|*localhost*)
      warn "PUBLIC_BASE_URL=$PUBLIC_BASE_URL 指向回环地址 —— gateway 在容器里，回访会打到容器自己。"
      warn "解析回调与稳定文件 URL 都会失效，请用 --host <本机IP或域名> 重跑 configure" ;;
  esac
  print_plan
  parse_chat_url

  write_web_env
  write_service_env
  write_frontend_env
  write_registry
  write_service_override
  write_nginx_conf
  write_systemd_unit
  save_state

  [ -n "$CHAT_COMPLETIONS" ] || warn "没配 --chat-url：问答、抽取与知识生成会因为拿不到 chat 端点而失败（解析、检索、出处与已有知识读取不受影响）"
}

# ---------------------------------------------------------------- models：模型权重

TORCH_PY=""
find_torch_python() {
  # torch 的 wheel 一直落后解释器一到两个小版本（本项目在 3.14 上就装不上），
  # 所以转换权重这一步单独找一个 3.11~3.13 的解释器，跟 backend 用哪个 python 无关
  local c ver
  for c in python3.13 python3.12 python3.11 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    ver="$("$c" -c 'import sys; print("%d%02d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
    if [ "$ver" -ge 311 ] && [ "$ver" -le 313 ]; then TORCH_PY="$(command -v "$c")"; return 0; fi
  done
  return 1
}

TOOLS_VENV="$STATE_DIR/tools-venv"
ensure_tools_venv() {   # 下载/转换用的独立环境，不污染 backend 的 .venv
  local py="${1:-$PYTHON}"
  if [ ! -x "$TOOLS_VENV/bin/pip" ]; then
    "$py" -m venv "$TOOLS_VENV" || return 1
  fi
  "$TOOLS_VENV/bin/pip" install -q --upgrade pip ${PIP_INDEX:+--index-url "$PIP_INDEX"} >/dev/null 2>&1 || true
}

HF_MIRROR="${HF_ENDPOINT:-https://hf-mirror.com}"
# TEI 真正要读的文件。整仓库下会连 onnx/ 一起拖下来（好几个 GB 的无用文件）
HF_FILES="config.json tokenizer.json tokenizer_config.json special_tokens_map.json sentencepiece.bpe.model"

download_via_modelscope() {   # <repo_id> <dest>
  ensure_tools_venv || return 1
  "$TOOLS_VENV/bin/pip" show modelscope >/dev/null 2>&1 || {
    info "安装 modelscope ..."
    "$TOOLS_VENV/bin/pip" install -q ${PIP_INDEX:+--index-url "$PIP_INDEX"} modelscope || return 1
  }
  # --exclude 不是所有版本都有：先问一遍 --help 再决定，**不要用 2>/dev/null 兜**——
  # 下载进度条走的就是 stderr，吞掉它等于让用户对着一个几 GB 的静默下载干等
  local excl=() help
  help="$("$TOOLS_VENV/bin/modelscope" download --help 2>&1 || true)"
  case "$help" in
    *--exclude*) excl=(--exclude '*.onnx' '*.h5' '*.msgpack' 'onnx/*') ;;
  esac
  info "modelscope download $1 -> $2（断点续传，进度见下）"
  "$TOOLS_VENV/bin/modelscope" download --model "$1" --local_dir "$2" ${excl[@]+"${excl[@]}"}
}

download_via_hf() {   # <repo_id> <dest>：镜像站直下，curl -C - 可续传
  local repo="$1" dest="$2" f
  mkdir -p "$dest"
  for f in $HF_FILES model.safetensors pytorch_model.bin; do
    # safetensors 与 bin 只要有一个就够（bge-m3 官方只发 bin，reranker 两个都有）
    case "$f" in
      model.safetensors) [ -f "$dest/pytorch_model.bin" ] && continue ;;
      pytorch_model.bin) [ -f "$dest/model.safetensors" ] && continue ;;
    esac
    info "下载 $f ..."
    curl -fL --retry 5 --retry-delay 3 -C - --progress-bar \
         -o "$dest/$f" "$HF_MIRROR/$repo/resolve/main/$f" \
      || { rm -f "$dest/$f"; warn "$f 下载失败（该文件在这个仓库可能不存在，继续）"; }
  done
  [ -f "$dest/config.json" ] || return 1
}

fetch_weights() {   # fetch_weights <repo_id> <目录名>
  local repo="$1" dir="$MODELS_DIR/$2"
  if [ -f "$dir/model.safetensors" ] && [ -f "$dir/config.json" ]; then
    ok "$2 权重已就位（$dir）"; return 0
  fi
  mkdir -p "$dir"
  case "$WEIGHTS_SOURCE" in
    modelscope) download_via_modelscope "$repo" "$dir" || die "modelscope 下载失败" ;;
    hf)         download_via_hf "$repo" "$dir" || die "镜像站下载失败" ;;
    *)          download_via_modelscope "$repo" "$dir" \
                  || { warn "modelscope 不可用，改用镜像站 $HF_MIRROR"; download_via_hf "$repo" "$dir"; } \
                  || die "两条下载路径都失败，检查网络或用 --weights-source 指定" ;;
  esac
  convert_to_safetensors "$dir"
}

convert_to_safetensors() {   # TEI 只认 safetensors，而 BAAI/bge-m3 官方只发 .bin
  local dir="$1"
  [ -f "$dir/model.safetensors" ] && { ok "safetensors 已存在，跳过转换"; return 0; }
  [ -f "$dir/pytorch_model.bin" ] || die "$dir 里既没有 model.safetensors 也没有 pytorch_model.bin"

  find_torch_python || die "找不到 3.11~3.13 的 python 来装 torch（转换权重需要）。
        装一个再重跑：apt install python3.12-venv / pacman -S python312 / uv python install 3.12"
  info "用 $TORCH_PY 建转换环境并装 torch（CPU 版，约 200MB）"
  local cvenv="$STATE_DIR/convert-venv"
  [ -x "$cvenv/bin/pip" ] || "$TORCH_PY" -m venv "$cvenv"
  "$cvenv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  "$cvenv/bin/pip" show torch >/dev/null 2>&1 || \
    "$cvenv/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu || \
    die "torch 安装失败"
  "$cvenv/bin/pip" show safetensors >/dev/null 2>&1 || \
    "$cvenv/bin/pip" install -q ${PIP_INDEX:+--index-url "$PIP_INDEX"} safetensors || die "safetensors 安装失败"

  # bge-m3 用仓库自带的脚本：它额外校验 sha256（多来源续传能拼出大小对但内容错的文件）
  if [ "$(basename "$dir")" = "bge-m3" ] && [ -f "$SERVICE_DIR/scripts/prepare_bge_m3.py" ]; then
    info "转换（含 sha256 校验）..."
    ( cd "$SERVICE_DIR" && "$cvenv/bin/python" scripts/prepare_bge_m3.py "$dir" ) || die "权重转换失败"
  else
    info "转换 $dir/pytorch_model.bin -> model.safetensors ..."
    "$cvenv/bin/python" - "$dir" <<'PY' || die "权重转换失败"
import sys, pathlib, torch
from safetensors.torch import save_file
d = pathlib.Path(sys.argv[1])
state = torch.load(d / "pytorch_model.bin", map_location="cpu", weights_only=True)
tensors = {k: v.clone().contiguous() for k, v in state.items() if isinstance(v, torch.Tensor)}
save_file(tensors, d / "model.safetensors", metadata={"format": "pt"})
print(f"已写出 {len(tensors)} 个张量 -> {d / 'model.safetensors'}")
PY
  fi
  ok "$dir/model.safetensors"
}

cmd_models() {
  step "模型权重"
  load_state
  mkdir -p "$MODELS_DIR" "$STATE_DIR"
  find_python || die "找不到 >=3.11 的 python（先跑 ./init.sh docker deps）"

  if [ "$SKIP_MODELS" = 1 ]; then
    warn "--skip-models：不下权重。检索退回关键词路，Web 的索引会标 index_status=failed，问答不可用"
    return 0
  fi

  detect_hardware
  if [ "${DISK_FREE_GB:-0}" -lt 12 ] 2>/dev/null; then
    warn "可用磁盘只有 ${DISK_FREE_GB}GB；bge-m3 下载 + 转换峰值约需 6GB"
  fi

  fetch_weights "BAAI/bge-m3" "bge-m3"
  [ "$WITH_RERANK" = 1 ] && fetch_weights "BAAI/bge-reranker-v2-m3" "bge-reranker-v2-m3"
  ok "权重目录：$MODELS_DIR"
}

# ---------------------------------------------------------------- build：装依赖与构建

cmd_build() {
  step "构建"
  load_state
  find_python || die "找不到 >=3.11 的 python（先跑 ./init.sh docker deps）"

  info "backend venv：$WEB_DIR/.venv"
  [ -x "$WEB_DIR/.venv/bin/python" ] || "$PYTHON" -m venv "$WEB_DIR/.venv"
  "$WEB_DIR/.venv/bin/pip" install -q --upgrade pip ${PIP_INDEX:+--index-url "$PIP_INDEX"}
  # **顺序不能反。** backend 会 import service 仓库的 `ddp_core` 包
  # （分块 / 裁图 / 分词 / 抽取 schema 的唯一一份实现）。
  # 它没写在 backend 的 dependencies 里 —— 路径依赖没有可移植的写法，
  # `[tool.uv.sources]` 只有 uv 读、pip 从不读（写上去 pip 会直接装不上）。
  # 所以在这里显式先装 gateway。理由与替代方案写在 backend/pyproject.toml 末尾。
  [ -d "$SERVICE_DIR/gateway" ] \
    || die "找不到 $SERVICE_DIR/gateway —— 两个仓库必须同级（先跑 ./init.sh docker configure）"
  ( cd "$WEB_DIR" && ./.venv/bin/pip install ${PIP_INDEX:+--index-url "$PIP_INDEX"} \
      -e "$SERVICE_DIR/gateway[corpus]" ) \
    || die "ddp_core（service 仓库的 gateway 包）安装失败"
  ( cd "$WEB_DIR" && ./.venv/bin/pip install ${PIP_INDEX:+--index-url "$PIP_INDEX"} -e "backend[dev]" ) \
    || die "backend 依赖安装失败"
  # 装完当场验一次：漏了 ddp_core 的话后面每个功能都会在 import 期炸，
  # 而那时人已经在排查别的东西了
  "$WEB_DIR/.venv/bin/python" -c "import ddp_core.chunking" 2>/dev/null \
    || die "ddp_core 装上了却 import 不了 —— 检查 $SERVICE_DIR/gateway 是否完整"
  ok "backend 依赖就绪（含 ddp_core）"

  info "前端依赖（registry=$NPM_REGISTRY）"
  # lockfile 的 resolved 全指向 npmmirror，换 registry 会被 npm 当成跨 host 的 remote
  # tarball 直接拒掉（EALLOWREMOTE）。别删 lockfile 重生成，那是仓库文件
  ( cd "$WEB_DIR/frontend" && npm ci --registry="$NPM_REGISTRY" ) \
    || die "npm ci 失败。若报 EALLOWREMOTE，用 --npm-registry https://registry.npmmirror.com 重试"
  info "前端构建（含 vue-tsc 类型检查）"
  ( cd "$WEB_DIR/frontend" && npm run build ) || die "前端构建失败"
  ok "前端产物：$WEB_DIR/frontend/dist"

  info "拉取/构建 service 镜像"
  ( cd "$SERVICE_DIR/docker" && docker compose -f "$SERVICE_COMPOSE" -f "$SERVICE_OVERRIDE" \
      --env-file "$SERVICE_ENV" --env-file "$SERVICE_MCP_ENV" \
      build gateway arq-worker mcp-server ) || die "service 镜像构建失败"
  ok "service 镜像就绪"
}

# ---------------------------------------------------------------- 进程管理

start_bg() {   # start_bg <名字> <工作目录> <命令...>
  local name="$1" dir="$2"; shift 2
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  # 直接 exec：$! 记下的就是进程本身。用 setsid 的话它会 fork，pid 文件当场失效。
  # 套一层 nohup 是为了 ssh 场景：部署完退出登录会给整个会话发 SIGHUP，
  # 不屏蔽的话 backend 跟着一起没。nohup 是原地 exec，pid 不变
  ( cd "$dir" && exec nohup "$@" </dev/null >>"$LOG_DIR/$name.log" 2>&1 ) &
  echo $! > "$RUN_DIR/$name.pid"
  info "$name -> $LOG_DIR/$name.log (pid $!)"
}

alive() { local f="$RUN_DIR/$1.pid"; [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; }

kill_tree() {   # 先收子进程再收本体：uvicorn --workers 会 fork 出一排 worker
  local name="$1" f="$RUN_DIR/$1.pid" pid
  [ -f "$f" ] || return 0
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    info "停 $name (pid $pid)"
  fi
  rm -f "$f"
}

compose_web()     { docker compose -f "$WEB_COMPOSE" --env-file "$WEB_ENV" "$@"; }
# 两份 --env-file：`.env` 是 gateway/worker 的，`.env.mcp` 只有语料 MCP 那几个键。
# 分开的理由见 write_service_env()：混在一起会让裸进程起的 gateway 拒绝启动。
# compose v2 支持多个 --env-file，只用于变量替换，不会整份注入给容器。
compose_service() { docker compose -f "$SERVICE_COMPOSE" -f "$SERVICE_OVERRIDE" \
  --env-file "$SERVICE_ENV" --env-file "$SERVICE_MCP_ENV" "$@"; }
compose_edge()    { docker compose -f "$EDGE_COMPOSE" "$@"; }

service_units() {   # 这次部署该起哪些 service 侧容器（与注册表一一对应）
  local units="gateway arq-worker mcp-server redis"
  [ "$SKIP_MODELS" != 1 ] && units="$units embed"
  [ "$WITH_RERANK" = 1 ] && units="$units rerank"
  [ "$WITH_MINERU" = 1 ] && units="$units mineru"
  printf '%s' "$units"
}

wait_http() {   # wait_http <url> <秒数> <说明>
  local url="$1" limit="${2:-60}" what="${3:-$1}" i=0
  while [ "$i" -lt "$limit" ]; do
    if curl -fsS --noproxy '*' --max-time 3 -o /dev/null "$url" 2>/dev/null; then ok "$what 就绪"; return 0; fi
    i=$((i + 1)); sleep 1
  done
  warn "$what 在 ${limit}s 内没就绪（$url）"
  return 1
}

# curl 连不上时自己就会把 http_code 打成 000 并返回非零 —— 再 `|| echo 000`
# 就会拼出 "000000"。这里只兜住 curl 完全不存在的情况
http_code() {
  local c
  c="$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null || true)"
  printf '%s' "${c:-000}"
}

# ---------------------------------------------------------------- start / stop / status

# compose 项目名固定在仓库的 compose 文件里（ddp-web / ddp-service），一台机器上
# 只能跑一套。**另一个 checkout 再 up 一次会把这一套的同名容器直接换掉** ——
# 这个项目真发生过（起 service 的 redis 顶掉了 Web 的 redis，backend 当场失联）。
# 靠 compose 自己写的 config_files 标签能认出来，起之前先问一句
check_stack_conflict() {
  local name cfg
  for name in $(docker ps -a --filter "label=com.docker.compose.project=ddp-web" \
                             --format '{{.Names}}' 2>/dev/null); do
    cfg="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' \
           "$name" 2>/dev/null || true)"
    [ -n "$cfg" ] || continue
    case "$cfg" in
      *"$WEB_COMPOSE"*) continue ;;
    esac
    warn "容器 $name 属于另一份 checkout（$cfg）"
    warn "compose 项目名是固定的 ddp-web —— 继续下去会把那一套的容器换掉，数据卷也会被接管"
    confirm_risky "确定继续？" || die "已中止。要并存请先停掉另一套：docker compose -f '$cfg' down"
    return 0
  done
}

cmd_start() {
  step "启动"
  load_state
  check_stack_conflict
  [ -f "$WEB_ENV" ] || die "还没配置过，先跑 ./init.sh docker configure"
  [ -f "$SERVICE_OVERRIDE" ] || die "缺 $SERVICE_OVERRIDE，先跑 ./init.sh docker configure"
  [ -x "$WEB_DIR/.venv/bin/python" ] || die "backend venv 不存在，先跑 ./init.sh docker build"
  [ -f "$WEB_DIR/frontend/dist/index.html" ] || die "前端还没构建，先跑 ./init.sh docker build"
  detect_hardware; tune_for_hardware >/dev/null 2>&1 || true
  # 调参值以 .env 里已写好的为准（configure 时算过），这里只重算 worker 数
  local workers; workers="$BACKEND_WORKERS"
  [ -n "$(get_env "$WEB_ENV" REDIS_URL)" ] || workers=1

  info "[1/5] 有状态组件（PostgreSQL 15432 · MinIO 19000/19001$([ "$NEED_REDIS" = 1 ] && echo " · Redis 16379")）"
  local web_units="postgres minio"
  [ -n "$(get_env "$WEB_ENV" REDIS_URL)" ] && web_units="$web_units redis"
  compose_web up -d $web_units || die "Web 数据面启动失败"

  info "[2/5] 等 Postgres 就绪并做迁移"
  local i=0
  while [ "$i" -lt 60 ]; do
    docker exec ddp-web-postgres-1 pg_isready -U ddp >/dev/null 2>&1 && break
    i=$((i + 1)); sleep 1
  done
  [ "$i" -lt 60 ] || die "Postgres 60s 内没起来，看 docker logs ddp-web-postgres-1"
  ( cd "$WEB_DIR/backend" && ../.venv/bin/alembic upgrade head ) || die "数据库迁移失败"
  ok "迁移到 head"

  info "[3/5] service 层（gateway 9000 · mcp 9100 · redis-stack 6379$([ "$SKIP_MODELS" != 1 ] && echo " · TEI 18080")）"
  compose_service up -d $(service_units) || die "service 启动失败"
  wait_http "http://127.0.0.1:9000/healthz" 60 "gateway" || true

  info "[4/5] backend（127.0.0.1:$BACKEND_PORT，$workers 个 worker）"
  kill_tree backend      # 无论走哪条路，先把脚本上一轮起的那个收掉
  if systemd_managed; then
    info "由 systemd 托管，重启 $SYSTEMD_UNIT"
    need_sudo || true
    $SUDO systemctl restart "$SYSTEMD_UNIT" || die "systemctl restart $SYSTEMD_UNIT 失败"
  else
    local uargs=(-m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT")
    [ "$workers" -gt 1 ] && uargs+=(--workers "$workers")
    start_bg backend "$WEB_DIR/backend" ../.venv/bin/python "${uargs[@]}"
  fi
  wait_http "http://127.0.0.1:$BACKEND_PORT/healthz" 60 "backend" \
    || { warn "backend 没起来，最后 30 行日志："
         if systemd_managed; then journalctl -u "$SYSTEMD_UNIT" -n 30 --no-pager >&2 || true
         else tail -30 "$LOG_DIR/backend.log" >&2; fi
         die "启动失败"; }

  info "[5/5] 边缘层 nginx（对外 $PUBLIC_PORT）"
  compose_edge up -d || die "nginx 启动失败（端口 $PUBLIC_PORT 被占用？ss -ltnp | grep :$PUBLIC_PORT）"
  wait_http "http://127.0.0.1:$PUBLIC_PORT/" 30 "前端" || true

  save_state
  printf '\n%s部署完成%s\n' "$C_BOLD$C_GREEN" "$C_RESET"
  info "打开：$PUBLIC_BASE_URL"
  dim "首次使用先在页面上注册一个账号；对外 API key 在「设置 - API Key」里生成"
  dim "自检：./start.sh docker doctor   看日志：./start.sh docker logs backend"
}

cmd_stop() {
  step "停止"
  load_state
  if systemd_managed; then need_sudo || true; $SUDO systemctl stop "$SYSTEMD_UNIT" || true; fi
  kill_tree backend
  sleep 1
  [ -f "$EDGE_COMPOSE" ] && compose_edge stop 2>/dev/null || true
  [ -f "$SERVICE_OVERRIDE" ] && compose_service stop 2>/dev/null || true
  compose_web stop 2>/dev/null || true
  ok "已停（数据卷保留：ddp-web_pgdata / ddp-web_miniodata / ddp-service_redis-data）"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  load_state
  step "容器"
  docker ps --filter "label=com.docker.compose.project=ddp-web" \
            --filter "label=com.docker.compose.project=ddp-service" \
            --filter "label=com.docker.compose.project=ddp-web-edge" \
            --format '    {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null || true
  step "进程"
  if systemd_managed; then
    systemctl is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null \
      && ok "backend 运行中（systemd: $SYSTEMD_UNIT）" || fail "backend 未运行（systemd: $SYSTEMD_UNIT）"
  else
    alive backend && ok "backend 运行中 (pid $(cat "$RUN_DIR/backend.pid"))" || fail "backend 未运行"
  fi
  step "探针"
  printf '    %-26s %s\n' "对外入口 ($PUBLIC_PORT)"   "$(http_code "http://127.0.0.1:$PUBLIC_PORT/")"
  printf '    %-26s %s\n' "backend /healthz"          "$(http_code "http://127.0.0.1:$BACKEND_PORT/healthz")"
  printf '    %-26s %s\n' "backend /readyz"           "$(http_code "http://127.0.0.1:$BACKEND_PORT/readyz")"
  printf '    %-26s %s\n' "gateway /healthz"          "$(http_code http://127.0.0.1:9000/healthz)"
  printf '    %-26s %s\n' "gateway /readyz"           "$(http_code http://127.0.0.1:9000/readyz)"
  [ "$SKIP_MODELS" != 1 ] && printf '    %-26s %s\n' "TEI /health" "$(http_code http://127.0.0.1:18080/health)"
  dim "readyz 非 200 时用 curl -s http://127.0.0.1:9000/readyz | 看是哪一项 down"
}

cmd_logs() {
  local what="${1:-backend}"
  case "$what" in
    backend) if systemd_managed; then journalctl -u "$SYSTEMD_UNIT" -f
             else tail -f "$LOG_DIR/backend.log"; fi ;;
    edge)    compose_edge logs -f ;;
    gateway|arq-worker|mcp-server|embed|rerank|mineru|redis)
             load_state; compose_service logs -f "$what" ;;
    postgres|minio) load_state; compose_web logs -f "$what" ;;
    *) die "不认识的日志名：$what（backend|edge|gateway|arq-worker|mcp-server|embed|postgres|minio）" ;;
  esac
}

# ---------------------------------------------------------------- doctor：自检

DOCTOR_FAILED=0
check() {   # check <说明> <命令...>
  if "${@:2}" >/dev/null 2>&1; then ok "$1"; else fail "$1"; DOCTOR_FAILED=1; fi
}

cmd_doctor() {
  step "自检"
  load_state
  [ -f "$WEB_ENV" ] || die "还没配置过，先跑 ./init.sh docker configure"

  # 1. 密钥。两个仓库的 config.py 都会拒绝带占位密钥启动 —— 提前说清楚是哪一项
  for k in JWT_SECRET SERVICE_TOKEN; do
    if is_placeholder "$(get_env "$WEB_ENV" "$k")"; then
      fail "$k 还是占位值（backend 会拒绝启动）"; DOCTOR_FAILED=1
    else ok "$k 已设置"; fi
  done
  if [ "$(get_env "$WEB_ENV" SERVICE_TOKEN)" = "$(get_env "$SERVICE_ENV" SERVICE_TOKEN)" ]; then
    ok "两侧 SERVICE_TOKEN 一致"
  else
    fail "Web 与 service 的 SERVICE_TOKEN 不一致 —— 所有转发会 401，回调会被拒"; DOCTOR_FAILED=1
  fi

  # 2. 引擎名三处一致。对不上时上传第一步就是 404 unknown_engine（2026-08-19 真踩过）
  local web_engine front_engine
  web_engine="$(get_env "$WEB_ENV" DEFAULT_PARSE_ENGINE)"
  front_engine="$(get_env "$FRONT_ENV" VITE_DEFAULT_ENGINE)"
  if [ "$web_engine" = "$front_engine" ] && grep -qE "^  ${web_engine}:" "$REGISTRY_FILE" 2>/dev/null; then
    ok "解析引擎三处一致：$web_engine（.env / 前端 / 注册表）"
  else
    fail "解析引擎不一致：.env=$web_engine 前端=$front_engine 注册表里$(grep -qE "^  ${web_engine}:" "$REGISTRY_FILE" 2>/dev/null && echo 有 || echo 没有)这个名字"
    DOCTOR_FAILED=1
  fi

  # 3. 回环地址陷阱：gateway 在容器里，PUBLIC_BASE_URL 写 127.0.0.1 会打到容器自己
  local pub; pub="$(get_env "$WEB_ENV" PUBLIC_BASE_URL)"
  case "$pub" in
    *127.0.0.1*|*localhost*) fail "PUBLIC_BASE_URL=$pub 是回环地址，容器回访不到（用 --host <IP> 重跑 configure）"; DOCTOR_FAILED=1 ;;
    *) ok "PUBLIC_BASE_URL=$pub" ;;
  esac

  # 4. 权重
  if [ "$SKIP_MODELS" = 1 ]; then
    warn "--skip-models 模式：向量索引不可用，Web 会把 index_status 标 failed（可见降级，不是故障）"
  elif [ -f "$MODELS_DIR/bge-m3/model.safetensors" ]; then
    ok "bge-m3 权重就位"
  else
    fail "缺 $MODELS_DIR/bge-m3/model.safetensors（跑 ./init.sh docker models）"; DOCTOR_FAILED=1
  fi

  # 5. 活着没
  step "运行状态"
  local codes
  for probe in "对外入口|http://127.0.0.1:$PUBLIC_PORT/" \
               "backend /readyz|http://127.0.0.1:$BACKEND_PORT/readyz" \
               "gateway /readyz|http://127.0.0.1:9000/readyz"; do
    local label="${probe%%|*}" url="${probe#*|}"
    codes="$(http_code "$url")"
    if [ "$codes" = 200 ]; then ok "$label 200"
    else fail "$label $codes"; DOCTOR_FAILED=1; fi
  done
  [ "$(http_code "http://127.0.0.1:$BACKEND_PORT/readyz")" = 200 ] || \
    dim "细节：curl -s http://127.0.0.1:$BACKEND_PORT/readyz"
  [ "$(http_code http://127.0.0.1:9000/readyz)" = 200 ] || \
    dim "细节：curl -s http://127.0.0.1:9000/readyz"

  # 6. 从容器里真的回访一次 —— 这是回调链唯一靠谱的验证方式（gateway 镜像自带 httpx）
  step "容器 -> backend 回访"
  if docker ps --format '{{.Names}}' | grep -q '^ddp-service-gateway-1$'; then
    if docker exec ddp-service-gateway-1 python -c "
import sys, httpx
r = httpx.get('$pub/healthz', timeout=5.0, trust_env=False)
sys.exit(0 if r.status_code == 200 else 1)" >/dev/null 2>&1; then
      ok "gateway 容器能访问 $pub/healthz（解析回调与稳定文件 URL 可用）"
    else
      fail "gateway 容器访问不到 $pub/healthz —— 解析结果回不来，只能靠 60s 的对账兜底"
      dim "多半是 PUBLIC_BASE_URL 不对，或防火墙挡了容器网段到宿主 $PUBLIC_PORT 端口"
      DOCTOR_FAILED=1
    fi
  else
    warn "gateway 容器没在跑，跳过回访检查"
  fi

  # 7. 分词器：换实现会静默毁掉关键词检索路，至少让它显式出现在自检里
  if grep -h "tokenizer backend" "$LOG_DIR/backend.log" 2>/dev/null | tail -1 | grep -q .; then
    dim "$(grep -h "tokenizer backend" "$LOG_DIR/backend.log" | tail -1)"
  fi

  step "结论"
  if [ "$DOCTOR_FAILED" = 0 ]; then ok "全部通过"; else fail "有未通过项，见上"; return 1; fi
}

# ---------------------------------------------------------------- all / help

cmd_all() {
  cmd_deps
  cmd_fetch
  cmd_configure
  cmd_models
  cmd_build
  cmd_start
  cmd_doctor || true
}

cmd_help() {
  cat <<'HELP'
DeepDocParse-Web 一键部署

  ./init.sh docker [子命令] [选项]

子命令（不给就是 all）
  all         deps -> fetch -> configure -> models -> build -> start -> doctor
  deps        检查/安装 docker、python(>=3.11)、node(>=22)
  fetch       clone/更新 service 仓库 DeepDocParse（必须与本仓库同级）
  configure   环境配置 + 优化配置：生成两份 .env、前端 .env.local、注册表、compose 覆盖层、nginx 配置
  models      下载模型权重（bge-m3，必要时转 safetensors）
  build       建 venv、装 backend 依赖、npm ci + 前端构建、构建 service 镜像
  start       启动全部；stop 停止；restart 重启
  status      容器 / 进程 / 探针一览
  logs <名>   backend | edge | gateway | arq-worker | mcp-server | embed | postgres | minio
  doctor      自检（密钥、引擎名一致性、回调可达性、探针）
  systemd     把 backend 装成开机自启的 systemd 单元（要 root；装完 start/stop 自动走 systemctl）

选项
  --host <IP或域名>       对外地址，默认取主网卡 IP。**不能是 127.0.0.1**：
                          service 跑在容器里，要靠这个地址回访 backend
  --port <端口>           对外端口，默认 80
  --public-base-url <url> 显式指定回调用的基地址（域名 + NAT 场景）
  --profile cpu|gpu|auto  默认 auto：探到 NVIDIA 卡就 gpu
  --chat-url <url>        OpenAI 兼容的 chat 端点，如 http://127.0.0.1:11434/v1
                          **不配的话问答与抽取不可用**（解析、检索、出处不受影响）
  --chat-model <名>       chat 模型名，如 qwen3:8b
  --chat-token <token>    chat 端点的 API key
  --with-rerank           启用交叉编码器精排（多下一份权重 + 一个 TEI 容器）
  --with-mineru           启用 MinerU（扫描件/表格/公式，需要 GPU，镜像构建很久）
  --skip-models           不下权重：检索退回关键词路，问答不可用
  --no-rerank / --no-mineru / --with-models
                          上面三个开关的关法。**选项会被记住**（.quickstart/state.env），
                          下次不传也生效，所以关掉要显式关
  --weights-source auto|modelscope|hf
  --service-repo <url>    service 仓库地址（默认官方 GitHub）
  --service-branch <名>
  --npm-registry <url>    默认 npmmirror（lockfile 的 resolved 指向它，换了会 EALLOWREMOTE）
  --pip-index <url>       pip 镜像源
  --no-deps               不自动装系统依赖，缺什么直接报错
  -y, --yes               不交互，一路默认

示例
  ./init.sh docker --host 203.0.113.10 --chat-url http://127.0.0.1:11434/v1 --chat-model qwen3:8b -y
  ./init.sh docker configure --port 8000 && ./start.sh docker restart
HELP
}

# ---------------------------------------------------------------- 入口

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --host)            PUBLIC_HOST="$2"; PUBLIC_BASE_URL=""; shift 2 ;;
      --port)            PUBLIC_PORT="$2"; PUBLIC_BASE_URL=""; shift 2 ;;
      --public-base-url) PUBLIC_BASE_URL="$2"; shift 2 ;;
      --profile)         PROFILE="$2"; shift 2 ;;
      --chat-url)        CHAT_URL="$2"; shift 2 ;;
      --chat-model)      CHAT_MODEL="$2"; shift 2 ;;
      --chat-token)      CHAT_TOKEN="$2"; shift 2 ;;
      # 开关会被记进 state.env 下次接着用，所以每个都要有对应的关法 ——
      # 否则 --skip-models 传过一次就再也回不去了（踩过）
      --with-rerank)     WITH_RERANK=1; shift ;;
      --no-rerank)       WITH_RERANK=0; shift ;;
      --with-mineru)     WITH_MINERU=1; shift ;;
      --no-mineru)       WITH_MINERU=0; shift ;;
      --skip-models)     SKIP_MODELS=1; shift ;;
      --with-models)     SKIP_MODELS=0; shift ;;
      --weights-source)  WEIGHTS_SOURCE="$2"; shift 2 ;;
      --service-repo)    SERVICE_REPO="$2"; shift 2 ;;
      --service-branch)  SERVICE_BRANCH="$2"; shift 2 ;;
      --npm-registry)    NPM_REGISTRY="$2"; shift 2 ;;
      --pip-index)       PIP_INDEX="$2"; shift 2 ;;
      --no-deps)         NO_DEPS=1; shift ;;
      -y|--yes)          ASSUME_YES=1; shift ;;
      -h|--help)         cmd_help; exit 0 ;;
      *)                 die "不认识的选项：$1（./init.sh docker help 看用法）" ;;
    esac
  done
}

main() {
  local cmd="all" logarg=""
  if [ $# -gt 0 ]; then
    case "$1" in
      -*) : ;;
      *)  cmd="$1"; shift
          # logs 的参数是位置参数，先摘出来
          if [ "$cmd" = logs ] && [ $# -gt 0 ]; then case "$1" in -*) : ;; *) logarg="$1"; shift ;; esac; fi ;;
    esac
  fi
  load_state          # 先吃上次的参数
  parse_args "$@"     # 命令行覆盖
  mkdir -p "$STATE_DIR" "$LOG_DIR" "$RUN_DIR"
  [ -n "$SERVICE_COMPOSE" ] || SERVICE_COMPOSE="$SERVICE_DIR/docker/compose.cpu.yml"

  case "$cmd" in
    all)       cmd_all ;;
    deps)      cmd_deps ;;
    fetch)     cmd_fetch ;;
    configure) cmd_configure ;;
    tune)      cmd_configure ;;   # 调参就是重新 configure：两者共用同一条计算链
    models)    cmd_models ;;
    build)     cmd_build ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs "$logarg" ;;
    doctor)    cmd_doctor ;;
    systemd)   cmd_systemd ;;
    help)      cmd_help ;;
    *)         die "不认识的子命令：$cmd（./init.sh docker help 看用法）" ;;
  esac
}

main "$@"
