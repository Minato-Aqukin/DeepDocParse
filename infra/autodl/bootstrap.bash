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
# 只算**还没装的**那几样：在已经装好的实例上补装一个组件（比如后加的 MinerU）时，
# 按全量去要空间会把一台本来够用的机器拒掉。
# torch + vLLM 及其 CUDA 依赖 ~10G，OCR-2 权重 ~7G，指令模型 ~8G，
# MinerU 的独立 venv（自带一份 torch）~9G + pipeline 模型 ~3G；另留 ~10G 给解包临时空间。
need_gb=10
[ -x "$VENV_DIR/bin/vllm" ] || need_gb=$((need_gb + 10))
[ -f "$MODEL_DIR/.ddp-download-complete" ] || need_gb=$((need_gb + 7))
if [ "$ENABLE_CHAT" = "1" ] && [ ! -f "$CHAT_MODEL_DIR/.ddp-download-complete" ]; then
  need_gb=$((need_gb + 8))
fi
if [ "$ENABLE_MINERU" = "1" ]; then
  [ -x "$MINERU_VENV_DIR/bin/mineru-api" ] || need_gb=$((need_gb + 9))
  [ -f "$MINERU_MODELS_DIR/.ddp-download-complete" ] || need_gb=$((need_gb + 3))
fi
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
  local complete="$dest/.ddp-download-complete"
  if [ -f "$complete" ]; then
    log "$label 权重已在 $dest，跳过下载"
    return 0
  fi
  # config.json 通常最先下载；只检查它会在 SSH 中断后把数 GB 的
  # `*.incomplete` 误判为完整模型。兼容旧目录时必须同时验证权重。
  if [ -f "$dest/config.json" ] && \
     find "$dest" -maxdepth 1 -type f -name '*.safetensors' -print -quit | grep -q . && \
     ! find "$dest" -maxdepth 1 -type f -name '*.incomplete' -print -quit | grep -q .; then
    touch "$complete"
    log "$label 权重已在 $dest，补写完成标记并跳过下载"
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
  find "$dest" -maxdepth 1 -type f -name '*.safetensors' -print -quit | grep -q . || \
    die "$dest 里没有 safetensors 权重，$label 下载没成功"
  ! find "$dest" -maxdepth 1 -type f -name '*.incomplete' -print -quit | grep -q . || \
    die "$dest 里仍有 incomplete 文件，$label 下载没完成"
  touch "$complete"
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

# ---------------------------------------------------------------- 5. MinerU（pipeline）
# 独立 venv 的理由见 env.bash。模型由 mineru 自己的下载器取（它顺带写好
# MINERU_TOOLS_CONFIG_JSON 里的本地路径，mineru.bash 以 MINERU_MODEL_SOURCE=local 只认这份）。
#
# **torch 一家子钉成 vLLM venv 里的同一版本，再把两边逐字节相同的文件硬链接起来。**
# 不这么做时 MinerU 的 venv 自带另一版 torch + CUDA 库（~5G），而 AutoDL 私有镜像
# 上限 30G —— 2026-10-15 加上 MinerU 后存镜像直接被拒（"镜像大小超过30G限制"）。
# 只钉 torch/torchvision/triton/nvidia-*：transformers 等两边要的版本不同，
# 这正是不能装进同一个 venv 的原因（vLLM 0.27.1 用 transformers 5.x，mineru 会把它换掉）。
if [ "$ENABLE_MINERU" = "1" ]; then
  MPY="$MINERU_VENV_DIR/bin/python"
  [ -x "$MPY" ] || uv venv --python "$PYTHON_VERSION" "$MINERU_VENV_DIR"
  torch_of() { "$1" -c 'import torch; print(torch.__version__)' 2>/dev/null; }
  if [ "$("$MPY" -c 'import mineru.version as v; print(v.__version__)' 2>/dev/null)" != "$MINERU_VERSION" ] \
     || [ "$(torch_of "$MPY")" != "$(torch_of "$PY")" ]; then
    log "装 MinerU $MINERU_VERSION -> $MINERU_VENV_DIR（torch 与 vLLM venv 同版：$(torch_of "$PY")）"
    constraints="$MINERU_VENV_DIR/torch-constraints.txt"
    uv pip freeze --python "$PY" | grep -E '^(torch|torchvision|triton|nvidia-[A-Za-z0-9_.-]+)==' > "$constraints"
    uv pip install --python "$MPY" --index-url "$PIP_INDEX_URL" -c "$constraints" "mineru[core]==$MINERU_VERSION"
  else
    log "MinerU $MINERU_VERSION 已装（torch 与 vLLM venv 同版），跳过"
  fi
  # 同一相对路径、逐字节相同才链（幂等：已是同一 inode 的跳过）。替换走"先链到临时名再 rename"，
  # 中途断掉也不会留下缺文件的 venv。之后任何一边升级包都是写新文件、不改旧 inode，互不影响
  "$MPY" - "$VENV_DIR" "$MINERU_VENV_DIR" <<'PYEOF'
import filecmp, os, sys
from pathlib import Path
src, dst = (Path(p) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            for p in sys.argv[1:3])
saved = linked = 0
for top in ("torch", "torchvision", "triton", "nvidia"):
    for path in (dst / top).rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        twin = src / path.relative_to(dst)
        try:
            a, b = path.stat(), twin.stat()
        except FileNotFoundError:
            continue
        if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) or a.st_size != b.st_size \
                or not filecmp.cmp(path, twin, shallow=False):
            continue
        tmp = path.with_name(path.name + ".ddp-link")
        tmp.unlink(missing_ok=True)  # 上一次中途断掉留下的
        os.link(twin, tmp)
        os.replace(tmp, path)
        saved += a.st_size
        linked += 1
print(f"  与 vLLM venv 硬链接 {linked} 个文件，省 {saved / 2**30:.2f} GiB")
PYEOF
  if [ -f "$MINERU_MODELS_DIR/.ddp-download-complete" ] && [ -f "$MINERU_TOOLS_CONFIG_JSON" ]; then
    log "MinerU pipeline 模型已在 $MINERU_MODELS_DIR，跳过下载"
  else
    mkdir -p "$MINERU_MODELS_DIR"
    case "$WEIGHTS_SOURCE" in
      modelscope) msrc=modelscope ;;
      hf)         msrc=huggingface ;;
      *) die "WEIGHTS_SOURCE 只认 modelscope / hf，收到 $WEIGHTS_SOURCE" ;;
    esac
    log "下 MinerU pipeline 模型（$msrc）-> $MINERU_MODELS_DIR"
    MODELSCOPE_CACHE="$MINERU_MODELS_DIR" HF_HOME="$MINERU_MODELS_DIR" HF_ENDPOINT="$HF_ENDPOINT" \
      "$MINERU_VENV_DIR/bin/mineru-models-download" -s "$msrc" -m pipeline
    grep -q '"pipeline": *"/' "$MINERU_TOOLS_CONFIG_JSON" \
      || die "$MINERU_TOOLS_CONFIG_JSON 里没写上 pipeline 模型路径，MinerU 模型下载没成功"
    touch "$MINERU_MODELS_DIR/.ddp-download-complete"
  fi
else
  warn "ENABLE_MINERU=0：不装 MinerU，注册表里别留 parse_engines.mineru"
fi

log "完成。下一步："
log "  bash $HERE/ocr.bash --daemon     # 识别/VQA 线"
[ "$ENABLE_CHAT" = "1" ] && log "  bash $HERE/chat.bash --daemon      # 抽取线"
[ "$ENABLE_MINERU" = "1" ] && log "  bash $HERE/mineru.bash --daemon    # MinerU 解析引擎"
log "  bash $HERE/verify.bash                  # 验证（别跳过）"
