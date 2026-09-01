#!/usr/bin/env bash
# 起 vLLM 的 OpenAI 兼容服务，喂 DeepSeek-OCR-2。
#
#   bash deploy/autodl/ocr.bash            # 前台跑，Ctrl-C 停
#   bash deploy/autodl/ocr.bash --daemon   # 后台跑，日志在 $LOG_DIR/vllm.log
#
# 起来之后跑 verify.bash 验证 —— **别跳过那一步**，有两处失效是不报错的
# （chat template 缺失、特殊 token 被剥），verify.bash 就是专门抓它们的。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.bash
source "$HERE/env.bash"

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

[ -x "$VENV_DIR/bin/vllm" ] || { echo "vLLM 没装，先跑 bootstrap.bash" >&2; exit 1; }
[ -f "$MODEL_DIR/config.json" ] || { echo "权重不在 $MODEL_DIR，先跑 bootstrap.bash" >&2; exit 1; }

TEMPLATE="$HERE/chat-template-deepseek-ocr2.jinja"
[ -f "$TEMPLATE" ] || { echo "chat template 不见了：$TEMPLATE" >&2; exit 1; }

mkdir -p "$LOG_DIR"

# --------------------------------------------------------------------------
# 启动参数。**每一条都有理由，别随手删。**
# 判据来源：vLLM 官方 recipe（recipes.vllm.ai/deepseek-ai/DeepSeek-OCR-2）
# 与官方推理脚本（DeepSeek-OCR2-vllm/run_dpsk_ocr2_pdf.py）。
# 全部参数已对着 vLLM 0.27.1 的 arg_utils.py / cli_args.py 核过存在性。
# --------------------------------------------------------------------------
ARGS=(
  "$MODEL_DIR"

  # 对外报的模型名。gateway 发来的 model 字段按这个匹配，
  # 与 models.autodl.yaml 的 options.model 必须一致
  --served-model-name "$SERVED_NAME"
  --host "$VLLM_HOST" --port "$VLLM_PORT"

  # 模型带 custom code（modeling_deepseekocr2.py 等），不开这个加载不了
  --trust-remote-code

  # ---- chat template：模型自己没带，不给就每个 chat 请求都 400 ----
  # content-format 显式钉成 string：让 vLLM 先把 [image_url, text] 拍平成
  # 一个字符串（占位符 `<image>` 由它按部件位置插入）再套模板。
  # 留 auto 的话行为随模板内容变化，是个不该有的不确定性。
  --chat-template "$TEMPLATE"
  --chat-template-content-format string

  # ---- 防复读：OCR 模型在表格/页眉这类重复版面上会陷进循环，一路吐到 max_tokens ----
  # 挂上它只是**允许**使用；真正生效还要每个请求带 ngram_size
  # （vLLM 的 validate_params：没传就整个跳过）。gateway 侧在
  # vlm_ocr._dsocr2_body() 里通过 vllm_xargs 传，两边缺一不可。
  # 注意模块路径是 deepseek_ocr（v1）不是 deepseek_ocr2 —— 处理器定义在那个文件里。
  --logits-processors vllm.model_executor.models.deepseek_ocr:NGramPerReqLogitsProcessor

  # ---- 官方 recipe 明确要求的两条 ----
  # OCR 每页图都不一样，前缀缓存命中率约等于 0，白占显存
  --no-enable-prefix-caching
  # 多模态预处理缓存同理：每页图只用一次
  --mm-processor-cache-gb 0

  # ---- 容量与吞吐 ----
  --max-model-len "$MAX_MODEL_LEN"        # 模型 config 的 max_position_embeddings 就是 8192
  --block-size "$BLOCK_SIZE"              # 官方脚本用的值
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-num-seqs "$MAX_NUM_SEQS"          # continuous batching 的并发上限
  # 一次请求只有一页图。写死上限能让 vLLM 少预留显存
  --limit-mm-per-prompt '{"image": 1}'
)

# CUDA graph 捕获阶段崩溃时的退路（社区在 0.20~0.23 若干版本上报过）。
# 代价是吞吐下降，所以缺省关着 —— 真崩了再开。
[ "$ENFORCE_EAGER" = "1" ] && ARGS+=(--enforce-eager)

# 注意：0.27.1 **没有** --swap-space 了（V1 引擎去掉了 CPU swap，
# CacheConfig 里已无此字段）。官方老脚本里的 swap_space=0 别照抄，会启动失败。

echo "[serve] vLLM $("$VENV_DIR/bin/python" -c 'import vllm; print(vllm.__version__)')"
echo "[serve] 模型 $MODEL_DIR -> http://$VLLM_HOST:$VLLM_PORT （对外名 $SERVED_NAME）"

if [ "$DAEMON" = "1" ]; then
  # 没有 systemd（AutoDL 容器里 PID 1 是 bash，systemctl 连不上 bus），
  # 后台化只能靠 nohup。实例关机即进程消失，不需要考虑重启策略。
  nohup "$VENV_DIR/bin/vllm" serve "${ARGS[@]}" >"$LOG_DIR/vllm.log" 2>&1 &
  echo $! > "$LOG_DIR/vllm.pid"
  echo "[serve] 后台启动，pid $(cat "$LOG_DIR/vllm.pid")，日志 $LOG_DIR/vllm.log"
  echo "[serve] 就绪后跑：bash $HERE/verify.bash"
else
  exec "$VENV_DIR/bin/vllm" serve "${ARGS[@]}"
fi
