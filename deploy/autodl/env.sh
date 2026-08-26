#!/usr/bin/env bash
# AutoDL 部署的**唯一配置面**。三个脚本（bootstrap / serve-vllm / verify）都 source 它。
#
# 改配置只改这里。想覆盖某一项而不动文件：先 export 同名变量再跑脚本
# （下面全部用 `${VAR:-默认值}`，外部值优先）。

# ---------------------------------------------------------------- 路径
# 装到哪。AutoDL 的系统盘只有 30G，torch + vLLM + 权重加起来要 ~20G，
# 装在系统盘上很容易在最后一步撑爆。有数据盘就用数据盘。
#
# **注意**：`/root/autodl-tmp` 在 Pro 实例上不一定是独立分区 —— 2026-08-25 实测
# 它只是系统盘上的一个目录（`df` 里看不到独立挂载）。所以真正的解法是
# 建实例时带 `--disk 50`，这个变量只决定放在哪个目录下。
DDP_ROOT="${DDP_ROOT:-$( [ -d /root/autodl-tmp ] && echo /root/autodl-tmp || echo /root )/ddp}"

VENV_DIR="${VENV_DIR:-$DDP_ROOT/venv}"
MODEL_DIR="${MODEL_DIR:-$DDP_ROOT/models/DeepSeek-OCR-2}"
LOG_DIR="${LOG_DIR:-$DDP_ROOT/logs}"

# ---------------------------------------------------------------- 模型与版本
MODEL_ID="${MODEL_ID:-deepseek-ai/DeepSeek-OCR-2}"

# 对外报的模型名。**必须与 models.autodl.yaml 里的 options.model 一致**，
# 否则 gateway 发过来的 model 字段对不上，vLLM 直接 404。
SERVED_NAME="${SERVED_NAME:-deepseek-ocr-2}"

# vLLM 版本。0.27.1 的 registry 里确认有 `DeepseekOCR2ForCausalLM`
# （vllm/model_executor/models/registry.py -> deepseek_ocr2），不需要任何自定义注册代码。
#
# **别随手升级**：社区报告 0.20~0.23 若干版本在部分卡上 CUDA graph 捕获阶段
# worker 直接死（vLLM 论坛 2727 / issue 41468）。升级前先在这台机器上跑一遍
# verify.sh，红了就退回本版本，并把 --enforce-eager 打开（见 serve-vllm.sh）。
VLLM_VERSION="${VLLM_VERSION:-0.27.1}"

# 建 venv 用的 Python。vLLM 0.27 要 3.10+；AutoDL 基础镜像自带的是 3.8，用不了。
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

# ---------------------------------------------------------------- 服务地址
# 端口沿用项目既有约定（18001 = VQA 运行时）。见 CLAUDE.md 的端口速查表。
# 绑 127.0.0.1：AutoDL 实例只有 6006/6008 有对外 URL 映射，模型端口不该对外，
# gateway 与 vLLM 在同一台机器上通过回环通信。
VLLM_HOST="${VLLM_HOST:-127.0.0.1}"
VLLM_PORT="${VLLM_PORT:-18001}"

# ---------------------------------------------------------------- 抽取平面的指令模型
# **不是可选项**（除非你不用 /v1/extract）：抽取平面要一个会遵循指令的模型。
# DeepSeek-OCR-2 只会把图上的字抄出来，拿它抽值抽不出东西，而抽不出来会被记成
# not_found（"文档里没有"）—— 一个看起来像结论的空值。注册表里 OCR-2 因此标了
# no_instruct，抽值路径会跳过它，一个都挑不到时如实报 no_instruct_model。
#
# 选 4B 是因为它要和 OCR-2 挤同一张卡。这个活是"从检索给的几个块里挑出字段值"，
# 上下文已经被限定住了，不需要大模型。
ENABLE_CHAT="${ENABLE_CHAT:-1}"                  # 0 = 只跑识别线，不起这个
CHAT_MODEL_ID="${CHAT_MODEL_ID:-Qwen/Qwen3-4B-Instruct-2507}"
CHAT_MODEL_DIR="${CHAT_MODEL_DIR:-$DDP_ROOT/models/Qwen3-4B-Instruct}"
CHAT_SERVED_NAME="${CHAT_SERVED_NAME:-qwen3-4b-instruct}"   # 要与注册表里的名字一致
CHAT_PORT="${CHAT_PORT:-18002}"

# **4096 不是 8192。** 抽取的 prompt 是"几个检索出来的块 + 字段说明"，很短；
# 而上下文开到 8192 时每条序列要 1.12 GiB KV cache，在与 OCR 挤卡的预算下放不下
# （实测报错：available KV cache memory 0.77 GiB < needed 1.12 GiB，
# vLLM 自己给的建议长度就是 5568）。砍到 4096 KV 需求减半，同样的显存能多跑一倍并发。
CHAT_MAX_MODEL_LEN="${CHAT_MAX_MODEL_LEN:-4096}"

# 抽取模型的 KV cache 大小，**直接写死字节数**（缺省 3 GiB）。
# 设了它就不再看 gpu-memory-utilization —— 这是共卡时唯一算得清的办法，
# 理由见下面「两个服务共卡时」那段。0 = 不写死，回到 utilization 那套。
CHAT_KV_CACHE_BYTES="${CHAT_KV_CACHE_BYTES:-3221225472}"

# ---------------------------------------------------------------- 显存与吞吐
# 以下是**推理优化的旋钮**，含义与取值理由见 README 的「推理优化」一节。
#
# ==== 两个服务共卡时，**不要靠 gpu-memory-utilization 去分**。先读完再改。 ====
#
# 2026-08-25 在 4090D 上把这件事撞明白了：vLLM 对显存有**两道**约束，
# 而它们在共卡时会把对方的占用算两遍，导致怎么调都调不出来：
#
#   ① 启动前置检查：`空闲显存 >= util × 卡容量`
#      —— OCR 占了 8.7G 之后空闲 14.5G，chat 给 0.92 直接被拒：
#         "Free memory on device cuda:0 (14.47/23.52 GiB) is less than
#          desired GPU memory utilization (0.92, 21.64 GiB)"
#   ② KV 预算：`util × 卡容量 − 全卡已用（含别的进程）`
#      —— chat 给 0.50 时算出 "Available KV cache memory: **-6.72 GiB**"
#
# 两条一联立，KV = 卡容量 − 2×对方占用 − 自己权重 − 自己激活，
# **对方的占用被扣了两次**，24G 卡上放 6.7G + 7.5G 两套权重就永远解不出正数。
#
# 正确解法：给第二个服务用 **`--kv-cache-memory-bytes` 直接写死 KV 大小**。
# 官方 docstring：「设了它就忽略 gpu_memory_utilization」——
# 于是 util 只剩下过①那道启动检查的作用，KV 由我们说了算。
#
#   OCR-2（先起）  util 0.38          -> 全卡 ~8.7 GiB
#   Qwen3-4B（后起）util 0.55 只为过检查 + KV 写死 3 GiB
#                                     -> 自身 ~12.5 GiB（权重 7.5 + 激活 ~2 + KV 3）
#   全卡合计 ~21.2 GiB / 23.5 GiB，留 2.3 GiB
#
# **顺序重要**：先 serve-vllm.sh，再 serve-chat.sh。
if [ "${ENABLE_CHAT:-1}" = "1" ]; then
  GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.38}"
  # 这个值**只用来过启动前置检查**（必须 <= 空闲/卡容量）；
  # chat 的 KV 由上面的 CHAT_KV_CACHE_BYTES 写死，不看它
  CHAT_GPU_MEMORY_UTILIZATION="${CHAT_GPU_MEMORY_UTILIZATION:-0.55}"
else
  # 只跑识别线时独占整卡
  GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.85}"
  CHAT_GPU_MEMORY_UTILIZATION="${CHAT_GPU_MEMORY_UTILIZATION:-0.55}"
fi

MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"      # 模型 config 的 max_position_embeddings 就是 8192
MAX_NUM_SEQS="${MAX_NUM_SEQS:-32}"          # 并发序列数；显存紧就调小
BLOCK_SIZE="${BLOCK_SIZE:-256}"             # 官方脚本用的值
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"         # 1 = 关掉 CUDA graph（启动崩溃时的退路）

# ---------------------------------------------------------------- 运行时开关
#
# **关掉 FlashInfer 的采样内核。** 2026-08-25 在 4090D 上实测：不关的话
# vLLM 起到"内存 profiling"那一步会直接崩，报
#
#     FileNotFoundError: [Errno 2] No such file or directory: 'ninja'
#     RuntimeError: Engine core initialization failed.
#
# 原因是 FlashInfer 的采样内核是**首次使用时 JIT 编译**的，需要 ninja + nvcc。
# AutoDL 的基础镜像里没有 ninja，而且它自带的是 CUDA 11.8、torch 带的是更新的
# CUDA —— 就算把 ninja 装上，nvcc 版本也未必对得上。
#
# 关掉它走 vLLM 的 PyTorch 原生采样：不编译、不依赖工具链、启动快。
# 代价是采样这一步略慢，但对我们这种"一页生成 1~2k token"的负载完全不是瓶颈
# （瓶颈在视觉编码和 decode 本身）。
#
# 真要用 FlashInfer 的话：`apt-get install -y ninja-build` 再把这行设成 1，
# 并预留几分钟的首次编译时间。
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

# ---------------------------------------------------------------- 国内网络
# 判据见 CLAUDE.md 陷阱 #5，以及 2026-08-25 在实例上的实测：
# pypi.org 直连 15s，阿里云镜像 4.3s。
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

export PIP_INDEX_URL PIP_TRUSTED_HOST HF_ENDPOINT

# 权重下载方式：modelscope（国内快，缺省）| hf（走 HF_ENDPOINT 镜像）
WEIGHTS_SOURCE="${WEIGHTS_SOURCE:-modelscope}"
