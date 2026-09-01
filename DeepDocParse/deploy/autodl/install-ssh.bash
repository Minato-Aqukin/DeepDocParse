#!/usr/bin/env bash
# 从一台全新的 AutoDL 实例开始，克隆两个私有仓库并安装、启动完整项目。
#
# 推荐用法：先把本文件和 MinIO 二进制上传到服务器同一目录，再执行：
#   chmod +x install-ssh.bash minio
#   ./install-ssh.bash
#
# 可选环境变量：
#   GITHUB_TOKEN=...       GitHub PAT；不提供时在终端安全询问，不写入磁盘或 remote
#   INSTALL_ROOT=/root/CSIE
#   REPO_BRANCH=main
#   MINIO_BIN=/root/minio
#   ENABLE_EMBED=1         1 = 下载 bge-m3 并启动 CPU embedding（推荐）
#   FRONTEND_PORT=6006     AutoDL 对外前端端口
#   BACKEND_PORT=8080      Web API 端口；可设为 AutoDL 的另一个对外端口 6008
#   START_AFTER_INSTALL=1  0 = 只安装，不启动
#   RUN_E2E=0              1 = doctor 通过后继续跑真机 e2e
#   DDP_ROOT=/path         模型、venv、数据和日志目录；缺省由项目 env.bash 决定
#
# AutoDL 实例是非特权容器，不能运行 Docker。本脚本有意复用仓库现有的
# init.sh/start.sh 裸进程入口，不维护第二套服务启动参数。
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/root/CSIE}"
REPO_BRANCH="${REPO_BRANCH:-main}"
ENABLE_EMBED="${ENABLE_EMBED:-1}"
START_AFTER_INSTALL="${START_AFTER_INSTALL:-1}"
RUN_E2E="${RUN_E2E:-0}"
INSTALL_LOG="${INSTALL_LOG:-/root/ddp-install.log}"
FRONTEND_PORT="${FRONTEND_PORT:-6006}"
BACKEND_PORT="${BACKEND_PORT:-8080}"

SERVICE_DIR="$INSTALL_ROOT/DeepDocParse"
WEB_DIR="$INSTALL_ROOT/DeepDocParse-Web"
SERVICE_URL="https://github.com/Minato-Aqukin/DeepDocParse.git"
WEB_URL="https://github.com/Minato-Aqukin/DeepDocParse-Web.git"

green='\033[32m'
yellow='\033[33m'
red='\033[31m'
reset='\033[0m'

log()  { printf "${green}[install]${reset} %s\n" "$*"; }
warn() { printf "${yellow}[install]${reset} %s\n" "$*"; }
die()  { printf "${red}[install] %s${reset}\n" "$*" >&2; exit 1; }

on_error() {
  local rc=$?
  printf "${red}[install] 第 %s 行失败（退出码 %s）。完整日志：%s${reset}\n" \
    "${BASH_LINENO[0]:-?}" "$rc" "$INSTALL_LOG" >&2
  exit "$rc"
}
trap on_error ERR

case "$ENABLE_EMBED" in 0|1) ;; *) die "ENABLE_EMBED 只接受 0 或 1" ;; esac
case "$START_AFTER_INSTALL" in 0|1) ;; *) die "START_AFTER_INSTALL 只接受 0 或 1" ;; esac
case "$RUN_E2E" in 0|1) ;; *) die "RUN_E2E 只接受 0 或 1" ;; esac

[ "$(id -u)" -eq 0 ] || die "请用 root 执行（AutoDL 默认登录用户就是 root）"
command -v nvidia-smi >/dev/null 2>&1 || die "找不到 nvidia-smi：请选择带 NVIDIA GPU 的实例"

mkdir -p "$(dirname "$INSTALL_LOG")"
touch "$INSTALL_LOG"
exec > >(tee -a "$INSTALL_LOG") 2>&1

log "开始时间：$(date -Is)"
log "安装目录：$INSTALL_ROOT"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

install_base_tools() {
  local missing=0 cmd
  for cmd in git curl openssl base64 awk python3; do
    command -v "$cmd" >/dev/null 2>&1 || missing=1
  done
  if command -v python3 >/dev/null 2>&1; then
    python3 -m pip --version >/dev/null 2>&1 || missing=1
  fi
  [ "$missing" -eq 0 ] && return 0

  command -v apt-get >/dev/null 2>&1 || die "缺少基础工具，且系统没有 apt-get"
  log "安装 git/curl/openssl/python3 等基础工具"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git curl ca-certificates openssl coreutils gawk python3 python3-pip
}

install_minio() {
  if command -v minio >/dev/null 2>&1 && minio --version >/dev/null 2>&1; then
    log "MinIO 已安装：$(minio --version | head -1)"
    return 0
  fi

  local candidate="${MINIO_BIN:-}"
  if [ -z "$candidate" ]; then
    for candidate in "$SCRIPT_DIR/minio" /root/minio /root/autodl-tmp/minio; do
      [ -f "$candidate" ] && break
      candidate=""
    done
  fi

  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    log "安装预上传的 MinIO：$candidate"
    install -m 0755 "$candidate" /usr/local/bin/minio
  else
    local tmp_minio
    tmp_minio="$(mktemp /tmp/ddp-minio.XXXXXX)"
    log "未找到预上传的 MinIO，尝试从官方地址下载（最多 5 分钟）"
    if curl -fL --retry 2 --connect-timeout 20 --max-time 300 \
      -o "$tmp_minio" https://dl.min.io/server/minio/release/linux-amd64/minio; then
      install -m 0755 "$tmp_minio" /usr/local/bin/minio
    fi
    rm -f -- "$tmp_minio"
  fi

  /usr/local/bin/minio --version >/dev/null 2>&1 || die \
    "MinIO 下载失败。请把 linux-amd64 的 minio 上传到本脚本同目录后重跑"
  log "MinIO 安装完成：$(/usr/local/bin/minio --version | head -1)"
}

prepare_git_auth() {
  github_token="${GITHUB_TOKEN:-}"
  if [ -z "$github_token" ]; then
    [ -t 0 ] || die "非交互执行时必须通过 GITHUB_TOKEN 提供私有仓库 PAT"
    printf "GitHub PAT（只用于本次 clone，不会保存）：" >&2
    IFS= read -rs github_token
    printf '\n' >&2
  fi
  [ -n "$github_token" ] || die "GitHub PAT 不能为空"
  git_basic="$(printf 'x-access-token:%s' "$github_token" | base64 -w0)"
}

git_with_auth() {
  git -c http.extraheader="Authorization: Basic $git_basic" "$@"
}

clone_repo() {
  local url="$1" dest="$2" label="$3"
  if [ -d "$dest/.git" ]; then
    log "$label 已存在，复用 $dest（安装器不会覆盖服务器上的现有改动）"
    return 0
  fi
  [ ! -e "$dest" ] || die "$dest 已存在但不是 Git 仓库，请移走后重跑"
  log "克隆 $label（分支 $REPO_BRANCH）"
  git_with_auth clone --branch "$REPO_BRANCH" --single-branch "$url" "$dest"
}

download_embedding() {
  # shellcheck source=env.bash
  source "$SERVICE_DIR/deploy/autodl/env.bash"
  local model_dir="$DDP_ROOT/models/bge-m3"
  if [ -f "$model_dir/config.json" ] && \
     { [ -f "$model_dir/pytorch_model.bin" ] || [ -f "$model_dir/model.safetensors" ]; }; then
    log "bge-m3 权重已存在，跳过下载"
  else
    log "下载 bge-m3（CPU embedding；断线后重跑可续传）"
    uv pip install --python "$VENV_DIR/bin/python" --index-url "$PIP_INDEX_URL" -q modelscope
    "$VENV_DIR/bin/modelscope" download --model BAAI/bge-m3 \
      --local_dir "$model_dir" \
      config.json tokenizer.json tokenizer_config.json \
      sentencepiece.bpe.model special_tokens_map.json pytorch_model.bin
  fi
  [ -f "$model_dir/config.json" ] || die "bge-m3 下载后缺少 config.json"

  # embed.bash 正常会从 vLLM 环境继承这两个包；显式补齐可避免它退回调用
  # uv venv 中不存在的 `python -m pip`。
  "$VENV_DIR/bin/python" -c 'import fastapi, uvicorn' >/dev/null 2>&1 || \
    uv pip install --python "$VENV_DIR/bin/python" --index-url "$PIP_INDEX_URL" -q fastapi uvicorn
}

enable_embedding_registry() {
  # web.bash 首次启动时会生成 .env，并把 MODELS_CONFIG 固定指向这个文件。
  # 因此按项目 README 的既定做法启用模板末尾的 embedding 段。仅改服务器
  # checkout 的部署配置；检测顶层键后再写，重复执行不会追加第二份。
  local registry="$SERVICE_DIR/models.autodl.yaml"
  if [ "$ENABLE_EMBED" -eq 1 ] && ! grep -q '^embedding_models:' "$registry"; then
    cat >> "$registry" <<'YAML'

# 由 deploy/autodl/install-ssh.bash 生成：AutoDL 上的 CPU embedding 替身。
embedding_models:
  bge-m3:
    endpoint: "http://127.0.0.1:18080"
    default: true
    runtime: tei
    capabilities: [dense]
YAML
    log "已在 models.autodl.yaml 启用 bge-m3"
  fi
}

verify_ports() {
  local failures=0
  curl -fsS --max-time 10 http://127.0.0.1:9000/readyz >/dev/null \
    && log "gateway /readyz 通过" || { warn "gateway /readyz 未通过"; failures=$((failures + 1)); }
  curl -fsS --max-time 10 "http://127.0.0.1:$BACKEND_PORT/healthz" >/dev/null \
    && log "Web backend :$BACKEND_PORT/healthz 通过" || { warn "Web backend :$BACKEND_PORT/healthz 未通过"; failures=$((failures + 1)); }
  curl -fsS --max-time 10 "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null \
    && log "前端 :$FRONTEND_PORT 通过" || { warn "前端 :$FRONTEND_PORT 未通过"; failures=$((failures + 1)); }
  if [ "$ENABLE_EMBED" -eq 1 ]; then
    curl -fsS --max-time 10 http://127.0.0.1:18080/health >/dev/null \
      && log "embedding /health 通过" || { warn "embedding /health 未通过"; failures=$((failures + 1)); }
  fi
  [ "$failures" -eq 0 ] || die "$failures 个服务健康检查失败，请查看 $DDP_ROOT/logs"
}

install_base_tools
install_minio
mkdir -p "$INSTALL_ROOT"
prepare_git_auth
clone_repo "$SERVICE_URL" "$SERVICE_DIR" "DeepDocParse"
clone_repo "$WEB_URL" "$WEB_DIR" "DeepDocParse-Web"
unset github_token git_basic GITHUB_TOKEN

log "安装模型、Python/Node 依赖和 Web 数据面；预计 25–45 分钟"
(cd "$SERVICE_DIR" && env ENABLE_CHAT=1 bash ./init.sh autodl)

if [ "$ENABLE_EMBED" -eq 1 ]; then
  download_embedding
else
  warn "ENABLE_EMBED=0：索引会失败并显式降级到 BM25"
fi
enable_embedding_registry

if [ "$START_AFTER_INSTALL" -eq 0 ]; then
  log "安装完成，按 START_AFTER_INSTALL=0 未启动"
  log "稍后启动：cd $SERVICE_DIR && env DDP_WITH_EMBED=$ENABLE_EMBED ./start.sh autodl start"
  exit 0
fi

log "启动 OCR → 指令模型 → embedding → Web 全栈"
(cd "$SERVICE_DIR" && env ENABLE_CHAT=1 DDP_WITH_EMBED="$ENABLE_EMBED" \
  FRONTEND_PORT="$FRONTEND_PORT" BACKEND_PORT="$BACKEND_PORT" bash ./start.sh autodl start)

log "运行 DeepSeek-OCR-2 六项静默失效检查"
(cd "$SERVICE_DIR" && env ENABLE_CHAT=1 bash ./start.sh autodl doctor)
verify_ports

if [ "$RUN_E2E" -eq 1 ]; then
  log "运行真机端到端检查"
  (cd "$SERVICE_DIR" && env ENABLE_CHAT=1 bash ./start.sh autodl e2e)
fi

log "安装完成。前端：http://127.0.0.1:$FRONTEND_PORT；后端：http://127.0.0.1:$BACKEND_PORT"
log "日志目录：$DDP_ROOT/logs；安装日志：$INSTALL_LOG"
log "停止服务：cd $SERVICE_DIR && ./start.sh autodl stop"
