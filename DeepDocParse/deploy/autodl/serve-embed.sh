#!/usr/bin/env bash
# 起 bge-m3 的 /v1/embeddings（CPU），给没有 TEI 的机器用。
#
#   bash deploy/autodl/serve-embed.sh --daemon
#
# `models.autodl.yaml` 的 embedding_models 段本来就写着这条出路
# （"得下 TEI 的裸二进制或用 sentence-transformers 包一个 /v1/embeddings"）。
# 这里选后者，但不引 sentence-transformers —— vLLM 的 venv 里已经有
# transformers + torch，bge-m3 的 dense 向量就是 CLS 池化后 L2 归一化。
#
# **它是 TEI 的替身，不是等价物**：CPU 上跑、无批处理优化、没有 sparse/colbert
# 两路输出。存在的理由只有一个 —— 让没有 TEI 的机器也能把
# 「上传 → 索引 → 检索 → 问答」这条产品路径走完。
# **质量数字仍然要在有 TEI 的部署上量**，别拿这里的结果当基线。
#
# 不起它的后果是**可见的既有降级**，不是坏了：索引会失败并写 index_error，
# 检索退回 BM25 并标 degraded=embedding_unavailable。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

EMBED_PORT="${EMBED_PORT:-18080}"
EMBED_MODEL_DIR="${EMBED_MODEL_DIR:-$DDP_ROOT/models/bge-m3}"
DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

if [ ! -f "$EMBED_MODEL_DIR/config.json" ]; then
  echo "[embed] 权重不在 $EMBED_MODEL_DIR。下它（只取需要的文件，别拉 onnx）：" >&2
  echo "  $VENV_DIR/bin/pip install modelscope" >&2
  echo "  $VENV_DIR/bin/modelscope download --model BAAI/bge-m3 \\" >&2
  echo "    --local_dir $EMBED_MODEL_DIR config.json tokenizer.json \\" >&2
  echo "    tokenizer_config.json sentencepiece.bpe.model special_tokens_map.json pytorch_model.bin" >&2
  exit 1
fi

"$VENV_DIR/bin/python" -c "import fastapi, uvicorn" 2>/dev/null \
  || "$VENV_DIR/bin/pip" install -q fastapi uvicorn

mkdir -p "$LOG_DIR"
CMD=("$VENV_DIR/bin/python" -m uvicorn embed_shim:app --host 127.0.0.1 --port "$EMBED_PORT")

if [ "$DAEMON" = "1" ]; then
  # setsid -f：nohup 会随 SSH 会话被回收（见 serve-web.sh 里的同一条注释）
  ( cd "$HERE" && EMBED_MODEL_DIR="$EMBED_MODEL_DIR" \
    setsid -f "${CMD[@]}" > "$LOG_DIR/embed.log" 2>&1 < /dev/null )
  echo "[embed] 后台启动 -> http://127.0.0.1:$EMBED_PORT，日志 $LOG_DIR/embed.log"
  echo "[embed] 记得把 models.autodl.yaml 的 embedding_models 段打开（默认是注释掉的）"
else
  cd "$HERE" && EMBED_MODEL_DIR="$EMBED_MODEL_DIR" exec "${CMD[@]}"
fi
