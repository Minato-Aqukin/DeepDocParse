#!/usr/bin/env bash
# 起抽取平面用的通用指令模型（缺省 Qwen3-4B-Instruct）。
#
#   bash deploy/autodl/serve-chat.sh --daemon
#
# 与 serve-vllm.sh 是**两个独立的 vLLM 进程，挤同一张卡**，
# **必须在它之后起**。
#
# 共卡的显存不是靠 `gpu-memory-utilization` 分的 —— 那条路走不通：它算的是
# **整卡**已用显存，再叠上启动前置检查（`空闲 >= util × 卡容量`），
# 对方的占用会被扣两遍，24G 卡上放 6.7G + 7.5G 两套权重怎么调都解不出正数。
# 这里的办法是 `--kv-cache-memory-bytes` 直接写死 KV 大小（设了它就忽略 util），
# util 只留着过那道启动检查。完整推导见 env.sh 的「两个服务共卡时」一节。
#
# 为什么不能省：DeepSeek-OCR-2 是 OCR 专用模型，只会把看到的字抄出来。
# 给它"请按 schema 抽取并输出 JSON"的指令，抽不出东西 —— 而抽不出来会被记成
# not_found（"文档里没有"），一个看起来像结论的空值。注册表里 OCR-2 标了
# no_instruct，抽值路径会跳过它；一个可用的都没有时 /v1/extract 会如实报
# no_instruct_model（不是硬抽），也就是说不起这个服务 = 抽取平面等于没有。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

if [ "$ENABLE_CHAT" != "1" ]; then
  echo "[chat] ENABLE_CHAT=0，跳过。注意：/v1/extract 会一律报 no_instruct_model" >&2
  exit 0
fi

[ -x "$VENV_DIR/bin/vllm" ] || { echo "vLLM 没装，先跑 bootstrap.sh" >&2; exit 1; }
[ -f "$CHAT_MODEL_DIR/config.json" ] || {
  echo "指令模型不在 $CHAT_MODEL_DIR —— 先跑 bootstrap.sh（它会一起下）" >&2; exit 1; }

mkdir -p "$LOG_DIR"

# ---- 等 OCR 那个服务真的分配完显存再起 ----
# **"先起 serve-vllm.sh"这句话不够。** `--daemon` 是 nohup 立刻返回的，
# 而 vLLM 从进程启动到真正吃满显存要几分钟（加载权重 + memory profiling +
# CUDA graph 捕获）。照 README 三步连着敲，两个进程实际上是**并行** profiling 的，
# 谁先分配完全看运气 —— 而按上面那段推导，**后分配的那个必定算出负的 KV**。
# 不等的话失败从"必然"变成"50/50"，比必然更难排查。
# 容器版对应的是 compose 里 vqa-dsocr 的 healthcheck + service_healthy。
wait_for_ocr() {
  local url="http://$VLLM_HOST:$VLLM_PORT/health" waited=0
  # 10 分钟：冷启动要下/读权重 + 捕获 CUDA graph，给足
  local limit="${OCR_WAIT_SECONDS:-600}"
  # 参数写错要当场说是参数写错。不校验的话 `[ "$waited" -lt "abc" ]` 每轮报
  # "需要整数"，最后打出来的却是"600s 内没有就绪" —— 把配置错误报成超时
  case "$limit" in ''|*[!0-9]*)
    echo "[chat] OCR_WAIT_SECONDS 必须是非负整数，收到：$limit" >&2; return 1 ;;
  esac
  # 探测靠这个 python，它不在的话每轮都会被当成"还没就绪"，白等满 limit 才失败
  [ -x "$VENV_DIR/bin/python" ] || {
    echo "[chat] $VENV_DIR/bin/python 不存在，先跑 bootstrap.sh" >&2; return 1; }
  echo "[chat] 等 OCR 服务就绪（$url，最多 ${limit}s）…"
  while [ "$waited" -lt "$limit" ]; do
    if "$VENV_DIR/bin/python" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$url', timeout=3)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      echo "[chat] OCR 服务已就绪（等了 ${waited}s），开始起指令模型"
      return 0
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  echo "[chat] OCR 服务 ${limit}s 内没有就绪。**不继续起** —— 现在起只会两个都算不出" >&2
  echo "[chat] 先看 $LOG_DIR/vllm.log；只想单独跑指令模型就设 OCR_WAIT_SECONDS=0" >&2
  return 1
}

# 写成显式 if 而不是 `[ ... ] && { ... }`：后者在 set -e 下能不能安全短路
# 取决于 shell 对 AND-OR 列表的处理细节，这种地方不值得省两行
if [ "${OCR_WAIT_SECONDS:-600}" != "0" ]; then
  wait_for_ocr || exit 1
fi

# 参数比 OCR 那边简单得多：
#   - Qwen3 自带 chat_template，**不用** --chat-template（OCR-2 才需要，它没带）
#   - 不用 grounding、不用防复读 logits processor（那是 OCR 版面识别的问题）
#   - 前缀缓存**开着**：抽取的 prompt 前半段（system + 字段说明）在同一份 schema 的
#     多个字段之间高度重复，命中率很高 —— 与 OCR 那边正好相反
ARGS=(
  "$CHAT_MODEL_DIR"
  --served-model-name "$CHAT_SERVED_NAME"
  --host "$VLLM_HOST" --port "$CHAT_PORT"
  --max-model-len "$CHAT_MAX_MODEL_LEN"
  # util 在这里**只用来过 vLLM 的启动前置检查**（要求 空闲 >= util×卡容量）；
  # KV 大小由下面那行说了算（官方 docstring：设了 kv-cache-memory-bytes
  # 就忽略 gpu-memory-utilization）。共卡时这是唯一算得清的办法，见 env.sh。
  --gpu-memory-utilization "$CHAT_GPU_MEMORY_UTILIZATION"
  --max-num-seqs "$MAX_NUM_SEQS"
)
[ "${CHAT_KV_CACHE_BYTES:-0}" != "0" ] && ARGS+=(--kv-cache-memory-bytes "$CHAT_KV_CACHE_BYTES")
[ "$ENFORCE_EAGER" = "1" ] && ARGS+=(--enforce-eager)

echo "[chat] $CHAT_MODEL_DIR -> http://$VLLM_HOST:$CHAT_PORT （对外名 $CHAT_SERVED_NAME）"
echo "[chat] util $CHAT_GPU_MEMORY_UTILIZATION（只为过启动检查） + KV 写死 $((CHAT_KV_CACHE_BYTES / 1024 / 1024)) MiB"

if [ "$DAEMON" = "1" ]; then
  nohup "$VENV_DIR/bin/vllm" serve "${ARGS[@]}" >"$LOG_DIR/chat.log" 2>&1 &
  echo $! > "$LOG_DIR/chat.pid"
  echo "[chat] 后台启动，pid $(cat "$LOG_DIR/chat.pid")，日志 $LOG_DIR/chat.log"
else
  exec "$VENV_DIR/bin/vllm" serve "${ARGS[@]}"
fi
