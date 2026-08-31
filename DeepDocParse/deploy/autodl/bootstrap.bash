#!/usr/bin/env bash
# 在 AutoDL 实例上装好 DeepSeek-OCR-2 的推理环境。**幂等**，可以重复跑。
#
#   bash deploy/autodl/bootstrap.bash
#
# 干三件事：建 Python 3.12 venv -> 装 vLLM -> 下模型权重。
# 每一步都先检查"是不是已经做过了"，断线重跑不会从头再来一遍
# （这很重要：AutoDL 按开机时长计费，重下 8GB 就是白烧钱）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.bash
source "$HERE/env.bash"

log()  { printf '\033[32m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[bootstrap]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[bootstrap] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 环境体检
# 全在装东西之前做完。装到一半才发现磁盘不够，那 10 分钟的下载就白花了。
log "环境体检"

command -v nvidia-smi >/dev/null || die "没有 nvidia-smi —— 这台机器没有 GPU，跑不了 vLLM"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

# AutoDL 实例是**非特权容器**（2026-08-25 实测：无 CAP_SYS_ADMIN、mount 被拒、
# cgroup 只读、unshare userns 返回 EPERM）。docker / podman / dind 一概跑不起来，
# 所以这套部署全部是裸进程。这里只做提示，不作为失败条件 ——
# 万一将来平台放开了，脚本不该因此拒绝运行。
if [ -f /.dockerenv ]; then
  warn "本机是容器内环境：docker compose 那套编排在这里用不了，本脚本走裸进程路线"
fi

mkdir -p "$DDP_ROOT" "$LOG_DIR"
avail_gb=$(df -BG --output=avail "$DDP_ROOT" | tail -1 | tr -dc '0-9')
# torch + vLLM 及其 CUDA 依赖 ~10G，OCR-2 权重 ~7G，指令模型 ~8G，
# pip 解包过程还要临时空间。
need_gb=25
[ "$ENABLE_CHAT" = "1" ] && need_gb=35
log "安装目录 $DDP_ROOT，可用 ${avail_gb}G（需要 ${need_gb}G）"
if [ "${avail_gb:-0}" -lt "$need_gb" ]; then
  die "可用空间不足 ${need_gb}G（当前 ${avail_gb}G）。AutoDL 系统盘缺省只有 30G ——
      重建实例时带 --disk 50，或把 DDP_ROOT 指到更大的分区；
      只跑识别线可以 ENABLE_CHAT=0（省 8G，但 /v1/extract 用不了）"
fi

# ---------------------------------------------------------------- 1. uv
# 用 uv 而不是 pip/conda：vLLM 的依赖树很大，uv 解析+下载快一个数量级。
# 在按分钟计费的机器上，这个差别是真金白银。
if ! command -v uv >/dev/null 2>&1; then
  log "装 uv"
  python3 -m pip install --quiet --index-url "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" uv
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || die "uv 装完了却不在 PATH 里，检查 $HOME/.local/bin"
log "uv $(uv --version)"

# ---------------------------------------------------------------- 2. venv
# 基础镜像自带 Python 3.8（AutoDL 的公共镜像最新也只到 py38），vLLM 0.27 要 3.10+。
# uv 会自己下一份独立的 CPython，不碰系统 Python，也不碰 conda。
if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "建 venv（Python $PYTHON_VERSION）：$VENV_DIR"
  # 国内下 python-build-standalone 慢的话，取消下面这行的注释换镜像：
  # export UV_PYTHON_INSTALL_MIRROR=https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download
  uv venv --python "$PYTHON_VERSION" "$VENV_DIR"
else
  log "venv 已存在，跳过"
fi
PY="$VENV_DIR/bin/python"

# ---------------------------------------------------------------- 3. vLLM
#
# **这里最重要的是"没装什么"：不装 flash-attn。**
#
# 官方 model card 写着 `pip install flash-attn==2.7.3 --no-build-isolation`，
# 那是**给 HF transformers 路径用的**（它显式传 _attn_implementation='flash_attention_2'）。
# 我们走 vLLM，vLLM 的实现不 import flash_attn：
#   - 语言塔用 vLLM 自带的 vllm-flash-attn（随 wheel 一起装好，就是 FlashAttention）
#   - 视觉塔（deepencoder）用 torch 的 scaled_dot_product_attention
# 2026-08-25 核对 vLLM 0.27.1 的 deepseek_ocr2.py / deepencoder.py 源码确认，
# 两个文件里 flash_attn 出现 0 次。
#
# 这不是省事，是省钱：flash-attn 没有匹配的预编译 wheel 时会源码编译，
# 16 核机器上要 30~90 分钟 —— 按 GPU 计费，纯烧。
# FlashAttention 照样在用，只是由 vLLM 自带的那份提供（verify.bash 会打印证据）。
if ! "$PY" -c "import vllm" 2>/dev/null; then
  log "装 vLLM $VLLM_VERSION（不装 flash-attn，理由见脚本注释）"
  VIRTUAL_ENV="$VENV_DIR" uv pip install --python "$PY" \
    --index-url "$PIP_INDEX_URL" \
    "vllm==$VLLM_VERSION"
else
  log "vLLM $("$PY" -c 'import vllm; print(vllm.__version__)') 已装，跳过"
fi

# ninja：保险。缺省我们关掉了 FlashInfer 采样（env.bash 里的 VLLM_USE_FLASHINFER_SAMPLER=0），
# 所以正常路径用不到它；但 vLLM 里还有别的 JIT 路径，装上很小、省得再撞一次
# "FileNotFoundError: 'ninja'"。它只有几 MB。
uv pip install --python "$PY" -q --index-url "$PIP_INDEX_URL" ninja 2>/dev/null || \
  warn "ninja 没装上（不影响缺省路径，见 env.bash 的 VLLM_USE_FLASHINFER_SAMPLER）"

installed=$("$PY" -c 'import vllm; print(vllm.__version__)')
[ "$installed" = "$VLLM_VERSION" ] || warn "装到的是 vLLM $installed，与期望的 $VLLM_VERSION 不一致"

# 模型架构必须被 vLLM 认识。这一步是**纯 CPU 检查**，几秒钟，
# 但能在下 7GB 权重之前就把"这个版本不支持这个模型"挡掉。
log "确认 vLLM 认识 DeepseekOCR2ForCausalLM"
"$PY" - <<'PYEOF' || die "这个 vLLM 版本不支持 DeepSeek-OCR-2，换版本（见 env.bash 的 VLLM_VERSION）"
from vllm.model_executor.models.registry import ModelRegistry
archs = set(ModelRegistry.get_supported_archs())
assert "DeepseekOCR2ForCausalLM" in archs, "registry 里没有 DeepseekOCR2ForCausalLM"
print("  OK: DeepseekOCR2ForCausalLM 在 registry 里")
PYEOF

# ---------------------------------------------------------------- 4. 权重
# 断点续传由下载器自己管，重跑不会从头来（OCR-2 约 7G，Qwen3-4B 约 8G）。
fetch_weights() {
  local repo="$1" dest="$2" label="$3"
  if [ -f "$dest/config.json" ]; then
    log "$label 权重已在 $dest，跳过下载"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  case "$WEIGHTS_SOURCE" in
    modelscope)
      log "从 ModelScope 下 $label -> $dest"
      "$VENV_DIR/bin/modelscope" download --model "$repo" --local_dir "$dest"
      ;;
    hf)
      log "从 $HF_ENDPOINT 下 $label -> $dest"
      HF_ENDPOINT="$HF_ENDPOINT" "$VENV_DIR/bin/hf" download "$repo" --local-dir "$dest"
      ;;
    *) die "WEIGHTS_SOURCE 只认 modelscope / hf，收到 $WEIGHTS_SOURCE" ;;
  esac
  [ -f "$dest/config.json" ] || die "$dest 里没有 config.json，$label 下载没成功"
}

# 注意：**必须用 uv pip，不能用 `$PY -m pip`** —— uv 建的 venv 缺省不装 pip
# （除非 --seed），`python -m pip` 会直接 ModuleNotFoundError。
case "$WEIGHTS_SOURCE" in
  modelscope) uv pip install --python "$PY" --index-url "$PIP_INDEX_URL" -q modelscope ;;
  hf)         uv pip install --python "$PY" --index-url "$PIP_INDEX_URL" -q huggingface_hub ;;
esac

fetch_weights "$MODEL_ID" "$MODEL_DIR" "DeepSeek-OCR-2"
# 架构名对不上的话，后面 vLLM 会挑一个错的实现或直接失败，在这里拦住更省事
grep -q "DeepseekOCR2ForCausalLM" "$MODEL_DIR/config.json" \
  || warn "config.json 里没找到 DeepseekOCR2ForCausalLM —— 下到的可能不是 OCR-2"

if [ "$ENABLE_CHAT" = "1" ]; then
  # 抽取平面要它。不下的话 /v1/extract 会一律报 no_instruct_model（如实报错，
  # 但抽取平面等于没有）—— 理由见 chat.bash 的头注释
  fetch_weights "$CHAT_MODEL_ID" "$CHAT_MODEL_DIR" "指令模型（抽取平面用）"
else
  warn "ENABLE_CHAT=0：不下指令模型，/v1/extract 会一律报 no_instruct_model"
fi

log "完成。下一步："
log "  bash $HERE/ocr.bash --daemon     # 识别/VQA 线"
[ "$ENABLE_CHAT" = "1" ] && log "  bash $HERE/chat.bash --daemon      # 抽取线"
log "  bash $HERE/verify.bash                  # 验证（别跳过）"
