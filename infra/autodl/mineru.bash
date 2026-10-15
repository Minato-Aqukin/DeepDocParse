#!/usr/bin/env bash
# 起 MinerU 的 mineru-api（pipeline 后端），给注册表 parse_engines.mineru 用。
#
#   bash infra/autodl/mineru.bash --daemon
#
# 装环境与下模型归 bootstrap.bash（ENABLE_MINERU=1 时一起做）。
#
# **显存不靠启动顺序分**：pipeline 的模型在第一份解析任务到来时才加载
# （mineru-api 只预载 vlm 后端，且要显式 --enable-vlm-preload），起进程本身不占显存。
# 两套 vLLM 起来时已经把各自的显存预分配完，mineru 只能用剩下的余量 ——
# MINERU_VRAM_GB 按余量定它的批大小。超了是那一次解析 OOM 失败（可见的任务失败），
# 不会反过来挤掉 vLLM。
#
# 只听 127.0.0.1：gateway 先下载 file_url 再 multipart 转传（见
# docs/upstream/mineru-api-contract.md），mineru 不需要、也不该对外。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.bash
source "$HERE/env.bash"

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

if [ "$ENABLE_MINERU" != "1" ]; then
  echo "[mineru] ENABLE_MINERU=0，跳过（注册表里别留 parse_engines.mineru，否则网关探针恒红）" >&2
  exit 0
fi

[ -x "$MINERU_VENV_DIR/bin/mineru-api" ] || {
  echo "[mineru] mineru 没装（$MINERU_VENV_DIR），先跑 bootstrap.bash" >&2; exit 1; }
# 配置文件里写着 pipeline 模型的本地路径；没有它 mineru 会在第一份任务时现场下载
[ -f "$MINERU_TOOLS_CONFIG_JSON" ] || {
  echo "[mineru] 缺 $MINERU_TOOLS_CONFIG_JSON（模型路径），先跑 bootstrap.bash" >&2; exit 1; }

mkdir -p "$LOG_DIR" "$MINERU_OUTPUT_DIR"
# MINERU_MODEL_SOURCE=local：只认 mineru.json 里的本地路径。不设的话缺文件时会去
# HuggingFace 现下 —— 国内直连拿不到，表现是第一份任务挂很久再失败。
CMD=(env MINERU_MODEL_SOURCE=local MINERU_DEVICE_MODE=cuda
     MINERU_VIRTUAL_VRAM_SIZE="$MINERU_VRAM_GB"
     MINERU_TOOLS_CONFIG_JSON="$MINERU_TOOLS_CONFIG_JSON"
     MINERU_API_OUTPUT_ROOT="$MINERU_OUTPUT_DIR"
     MINERU_API_TASK_RETENTION_SECONDS="$MINERU_RETENTION_SECONDS"
     MINERU_API_MAX_CONCURRENT_REQUESTS="$MINERU_CONCURRENCY"
     "$MINERU_VENV_DIR/bin/mineru-api" --host 127.0.0.1 --port "$MINERU_PORT")

echo "[mineru] $("$MINERU_VENV_DIR/bin/python" -c 'import mineru.version as v; print(v.__version__)') -> http://127.0.0.1:$MINERU_PORT（显存上限 ${MINERU_VRAM_GB}G）"

if [ "$DAEMON" = "1" ]; then
  # setsid -f：nohup 会随 SSH 会话被回收（见 stack.bash 里 start_bg 的同一条注释）
  ( cd "$LOG_DIR" && setsid -f "${CMD[@]}" > "$LOG_DIR/mineru.log" 2>&1 < /dev/null )
  echo "[mineru] 后台启动，日志 $LOG_DIR/mineru.log"
else
  cd "$LOG_DIR" && exec "${CMD[@]}"
fi
