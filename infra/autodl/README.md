# 在 AutoDL 上部署（无 docker）

> 对外只使用仓库根的 `init.sh` 与 `start.sh`；本目录的 `.bash` 都是内部模块。
>
> ```bash
> ./init.sh autodl
> ./start.sh autodl start
> ./start.sh autodl doctor                 # 六项，别跳过
> ```
>
> **起了 embed.bash 就要把 `models.autodl.yaml` 的 `embedding_models` 段打开**
> （默认注释掉）。不打开的话索引会失败并写 `index_error`、检索退回 BM25 并标
> `degraded=embedding_unavailable` —— 那是可见降级，不是坏了，但问答就没东西可检索。
>
> **MinIO 要自己准备**：dl.min.io 与所有国内镜像、GitHub 代理 2026-08-29 实测
> 全不可用或龟速（20 分钟 6MB）。本地下好后
> `autodl push <id> ./minio /usr/local/bin/minio`，再 `chmod +x`。

## 模型线：DeepSeek-OCR-2

这套脚本把 **VQA 平面 / 抽取平面 / vlm-ocr 解析引擎**——也就是本项目里
「基于大语言模型」的那三条线——跑在一台 AutoDL GPU 实例上，**全部是裸进程，不用 docker**。

| 文件 | 干什么 |
|---|---|
| `env.bash` | 唯一配置面。路径、版本、端口、显存旋钮都在这里 |
| `bootstrap.bash` | 建 venv → 装 vLLM → 下权重。幂等，断线可重跑 |
| `ocr.bash` | 起 DeepSeek-OCR-2（识别 / VQA 线），带全部推理优化参数 |
| `chat.bash` | 起通用指令模型（抽取线）。不起它 `/v1/extract` 用不了 |
| `verify.bash` | **别跳过**。两分钟，专抓四处不会报错的失效 |
| `e2e-llm.bash` | 真机全链路：解析 → 出处 bbox → 抽取 → 视觉核对 |
| `web.bash` | **Web 侧全栈**（PG + MinIO + Redis + gateway + arq worker + 后端 + 前端）。`deploy/docker.bash` 要 docker，这里用不了 |
| `embed.bash` + `embed_shim.py` | bge-m3 的 CPU `/v1/embeddings`。**TEI 的替身，不是等价物** —— 不起它索引会失败、检索退 BM25（可见降级） |
| `chat-template-deepseek-ocr2.jinja` | 模型自己没带 chat template，不给就每个请求 400 |
| `../../models.autodl.yaml` | 配套注册表（endpoint 全是回环地址） |
| `../../scripts/calibrate_verify_threshold.py` | 标定视觉核对阈值（换文档类型就该重标） |

## 已经验过什么（2026-08-25，4090D，约 115 分钟）

这套东西不是纸上谈兵，下面每一条都在真机上跑出来过：

| | 结果 |
|---|---|
| vLLM 0.27.1 认 `DeepseekOCR2ForCausalLM` | ✅ registry 里有，无需自定义注册代码 |
| FlashAttention | ✅ 日志 `Using FLASH_ATTN attention backend`，**没装 flash-attn** |
| 图像分块 | ✅ 整页 1125 prompt token = 6×144 + 256，与模型卡承诺一致 |
| 精度 | ✅ `dtype=torch.bfloat16` |
| grounding 输出 | ✅ `<\|ref\|>text<\|/ref\|><\|det\|>[[94, 50, 339, 68]]<\|/det\|>` + 正文 |
| `/v1/parse` engine=vlm-ocr | ✅ 2 页 18 块，**18/18 有 bbox**，全部落在页内，无 `engine_notes` |
| 表格 | ✅ 块类型判成 `table`，HTML 与 `contract.truth.json` **逐字一致** |
| `/v1/extract` | ✅ `Northwind Trading Company Limited` / `486200.50`，都带出处 |
| 视觉核对 | ✅ 裁图 + 抄写 + 比对跑通，`verified=True` |
| 两个模型共卡 | ✅ 4090D 24G 同时跑 OCR-2 + Qwen3-4B |

真实模型输出已固化成回归夹具 `tests/fixtures/dsocr2-real-output.json`，
`tests/test_layout.py` 里有两条用例对着它跑 —— 解析器不再只对着手写样例编程。

### 阈值标定（此前从没标定过）

`scripts/calibrate_verify_threshold.py` 在真模型上量出来的分布：

| | n | 分布 |
|---|---|---|
| 一致组（块图 vs 自己的文本） | 10 | **全部 1.000** |
| 不一致组（块图 vs 别人的文本） | 90 | p95 = 0.382，max = 0.643 |

原来那个没有依据的 `0.35` 会放过 **5/90** 个该报的不一致。现已改为 **0.55**
（`EXTRACT_MISMATCH_THRESHOLD` / `QA_PARSE_MISMATCH_THRESHOLD`）。
没取脚本建议的中点 0.69，是因为标定样本是 born-digital 英文单栏 —— 最容易的一类，
扫描件/中文上抄写保真度会掉，而这两处的取向是**宁可漏报不要误报**。

**已知的粗糙处**：标定用的是 borndigital 切出来的小块，而 vlm-ocr 的块是合并过的大块，
抄写比值天然更低、会贴着阈值浮动（实测同一个块两次跑出过 `verified` True 和 False）。
要更准就拿你自己的文档、按实际引擎的版面重标一次。

## 为什么是两个模型

一开始的设想是「一个 DeepSeek-OCR-2 通吃」，实际不成立：

| 平面 | 要模型干什么 | OCR-2 行不行 |
|---|---|---|
| 识别（`vlm-ocr`） | 看图，把版面和文字连位置一起吐出来 | ✅ 本行 |
| VQA / 出处核对 | 看一小块图，把上面的字原样抄一遍 | ✅ 本行 |
| **抽取（`/v1/extract`）** | 读一段**文字**，按 schema 挑出字段值并输出 JSON | ❌ **不行** |

DeepSeek-OCR-2 是 **OCR 专用模型**，只在两个官方 prompt 上训练过。
给它"请按 schema 抽取并输出 JSON"，它会继续抄字 —— 而抽不出来会被记成
`not_found`（"文档里没有"）。**系统能力缺失伪装成了事实**，
正是这个项目定义的最危险输出。

所以注册表里 OCR-2 标了 `capabilities: [vision, no_instruct]`，抽值路径会跳过它。
一个可用的指令模型都没有时，`/v1/extract` 如实报 `no_instruct_model` 而不是硬抽。
要让抽取平面真能干活，就得起 `chat.bash`（缺省 Qwen3-4B-Instruct，
挑 4B 是因为它要和 OCR-2 挤同一张卡，而这个活的上下文已经被检索限定住了）。

## 为什么不能用 docker

**AutoDL 的实例本身就是一个非特权 docker 容器。** 2026-08-25 在 4090D 上实测：

| 探测 | 结果 |
|---|---|
| `/.dockerenv` | 存在，PID 1 = `bash` |
| capabilities | `CapEff=00000000a80425fb` —— 无 `CAP_SYS_ADMIN` / `CAP_NET_ADMIN` |
| `mount -t tmpfs` | `permission denied` |
| `/sys/fs/cgroup` | 只读 |
| `iptables` | 不可用 |
| `unshare --user` | `EPERM` |
| `systemctl` | 二进制在，但 systemd 不是 init → `Failed to connect to bus` |

`unshare --user` 那条最要命：它同时堵死了 **dind、rootless docker、podman**。
不是装不上，是内核权限不给。所以 `docker/compose.*.yml` 那套编排在这里没有任何变通余地。

**但架构本身不用改。** 铁律「gateway 从不 import 模型代码，只按注册表访问 HTTP 端点」
在这里救了场：模型跑在容器里还是进程里，对 gateway 是透明的。
换的只是 `models.yaml` 里的 endpoint —— 从 `http://vqa-dsocr:8000` 变成 `http://127.0.0.1:18001`。

## 建实例

```bash
# 4090D 24G 是七个 Pro 规格里最便宜的，跑 3B 的 OCR-2 绰绰有余
autodl create --gpu 4090d --disk 50 --ttl 4h --wait --name ddp-ocr2
```

**`--disk 50` 不是可选项。** 系统盘缺省只有 30G，而 torch + vLLM 及其 CUDA 依赖约 10G、
权重约 7G，pip 解包还要临时空间 —— 30G 会在最后一步撑爆。
另外 `/root/autodl-tmp` 在 Pro 实例上**不是独立数据盘**（实测 `df` 里看不到独立挂载），
指望它腾地方是没用的。

**`--ttl` 也不是可选项。** AutoDL 按开机时长计费，与用不用 GPU 无关 ——
一台跑完了忘记关的机器和一台满载的机器一样烧钱。

## 三步

```bash
./init.sh autodl                           # 约 25~45 分钟（两个模型的下载占大头）
./start.sh autodl start                    # OCR / 抽取 / Web 全栈
./start.sh autodl doctor                   # 不到 2 分钟，6 项检查
./start.sh autodl e2e                      # 真机全链路（起 redis/gateway/worker 跑一遍）
```

`verify.bash` 全绿之前不要往下走。带病跑 e2e 只会烧掉更多 GPU 时间。

只想验识别线、不管抽取的话，两步都显式带同一个开关：
`ENABLE_CHAT=0 ./init.sh autodl`，随后 `ENABLE_CHAT=0 ./start.sh autodl start`。
省 8G 磁盘和一半显存，代价是 `/v1/extract` 一律报 `no_instruct_model`。

> ⚠️ **`ENABLE_CHAT=0` 时要把 `models.autodl.yaml` 里的 `qwen3-4b-instruct`
> 那一段注释掉。** gateway 的 `/readyz` 是 `all(up)` —— 注册了却没起对应服务，
> 探针恒 503，副本永远不接流量。注册表里写了什么，就得真的起什么：
> 这正是 `DeepDocParse-Web/deploy/docker.bash` 的 `write_registry` 特意规避的那个坑
> （它按"这次部署真的起了什么"生成注册表，而不是照抄仓库里的模板）。

### 显存怎么分（24G 卡）

**两个服务共卡时，别指望用 `--gpu-memory-utilization` 去分。** 这条路走不通，
不是参数没调对 —— vLLM 有两道显存约束，它们在共卡时会把对方的占用扣两遍：

| | 约束 | 撞到时的报错 |
|---|---|---|
| ① | 启动前置检查：`空闲显存 >= util × 卡容量` | `Free memory on device cuda:0 (14.47/23.52 GiB) is less than desired GPU memory utilization (0.92, 21.64 GiB)` |
| ② | KV 预算：`util × 卡容量 − 全卡已用（含别的进程）` | `Available KV cache memory: -6.72 GiB` |

两条联立后 `KV = 卡容量 − 2×对方占用 − 自己权重 − 自己激活`：
24G 卡上放 6.7G + 7.5G 两套权重，**永远解不出正数**。
调低 util 过不了②，调高过不了①，2026-08-25 在 4090D 上把 0.42 / 0.50 / 0.92 都试遍了。

**正解：第二个服务用 `--kv-cache-memory-bytes` 直接写死 KV 大小。**
官方 docstring 明说「设了它就忽略 `gpu_memory_utilization`」——
于是 util 只剩下过①那道启动检查的作用，KV 由我们说了算。

| 顺序 | | 参数 | 实测占用 |
|---|---|---|---|
| 1 | DeepSeek-OCR-2 (3B) | `--gpu-memory-utilization 0.38` | 全卡 8.9 GiB |
| 2 | Qwen3-4B-Instruct | `--gpu-memory-utilization 0.55`（只为过①）<br>`--kv-cache-memory-bytes 3221225472`（3 GiB） | 自身 ~12.5 GiB |

**顺序重要**：先 `ocr.bash` 再 `chat.bash`。反过来的话 OCR 那边过不了①。

光按顺序敲还不够 —— `--daemon` 是立刻返回的，而 vLLM 要几分钟才真正吃满显存，
连着敲两条等于让两个进程**并行** profiling，谁先分配全看运气，
而后分配的那个必定算出负的 KV。所以 `chat.bash` 会自己**等 OCR 的 `/health` 通**
再起（最多 10 分钟，`OCR_WAIT_SECONDS=0` 可跳过）。容器版对应 compose 里
`vqa-dsocr` 的 healthcheck + `condition: service_healthy`。

另外「权重放得下」不等于「跑得起来」：预算里要装**权重 + 激活/CUDA context + KV cache**，
最后一样才是能并发的本钱。抽取那边上下文只给 4096（prompt 是"几个检索出来的块 +
字段说明"，很短）—— 开到 8192 时一条序列就要 1.12 GiB KV，白白吃掉一半并发。

`env.bash` 会按 `ENABLE_CHAT` 自动选这套值（独占卡时 OCR 回到 0.85），一般不用手动改。

## 推理优化

### FlashAttention：用上了，但**不要装 `flash-attn`**

官方 model card 写着 `pip install flash-attn==2.7.3 --no-build-isolation`。
那是**给 HF transformers 路径用的**（它显式传 `_attn_implementation='flash_attention_2'`）。
我们走 vLLM，而 vLLM 的实现根本不 import 它 —— 核对 vLLM 0.27.1 源码：

| 组件 | 用什么注意力 |
|---|---|
| 语言塔（DeepSeek-V2 MoE） | vLLM 自带的 `vllm-flash-attn`，**就是 FlashAttention**，随 wheel 装好 |
| 视觉塔（`deepencoder.py`） | `torch.nn.functional.scaled_dot_product_attention` |

`deepseek_ocr2.py` 与 `deepencoder.py` 两个文件里 `flash_attn` 出现 **0 次**。

这条省的是真金白银：`flash-attn` 没有匹配预编译 wheel 时会源码编译，
16 核机器上要 **30~90 分钟**，按 GPU 计费全是白烧。
`verify.bash` 第 5 步会打印 FlashAttention 确实在用的证据，免得后人看见"没装"又去编译一遍。

### 启动参数

每一条都有理由，判据来自 vLLM 官方 recipe 与官方推理脚本，别随手删：

| 参数 | 为什么 |
|---|---|
| `--logits-processors …:NGramPerReqLogitsProcessor` | OCR 模型在表格/页眉这类重复版面上会陷进复读循环，一路吐到 `max_tokens`。挂上它只是**允许**使用，真正生效还要每个请求带 `ngram_size`（见下） |
| `--no-enable-prefix-caching` | 每页图都不一样，前缀缓存命中率约等于 0，白占显存 |
| `--mm-processor-cache-gb 0` | 同理，每页图只用一次 |
| `--max-model-len 8192` | 模型 config 的 `max_position_embeddings` 就是 8192，给大了只是浪费 KV cache |
| `--block-size 256` | 官方脚本用的值 |
| `--limit-mm-per-prompt '{"image": 1}'` | 一次请求只有一页图，写死上限让 vLLM 少预留显存 |
| `--gpu-memory-utilization` | **独占卡时** 0.85（24G 上 ≈ 20G，够 3B BF16 加一大块 KV）；与抽取模型共卡时是 0.38，见上面「显存怎么分」 |
| `--max-num-seqs 32` | continuous batching 的并发上限。这是**吞吐的主旋钮**，显存紧就调小 |

**`--swap-space` 在 0.27.1 已经没有了**（V1 引擎去掉了 CPU swap，`CacheConfig` 里无此字段）。
官方老脚本里的 `swap_space=0` 别照抄，会直接启动失败。

想再压吞吐，按这个顺序调：`--max-num-seqs` ↑ → `models.autodl.yaml` 的 `concurrency` ↑ →
`--gpu-memory-utilization` ↑。三者取小生效，只调一个没用。
（共卡时最后那一项动不了多少，先想清楚要不要把抽取模型挪到别的卡上。）

### 请求侧（gateway 自动带上，列在这里是为了可排查）

```json
{"skip_special_tokens": false,
 "vllm_xargs": {"ngram_size": 20, "window_size": 50,
                "whitelist_token_ids": [128821, 128822]}}
```

- `skip_special_tokens: false` —— **这一行守着整个出处功能**。OpenAI 接口缺省是 `true`，
  而 `<|ref|>` / `<|det|>` 正是特殊 token，不显式关掉的话模型报出来的 bbox
  会在返回前被剥光，我们只看到"每个块 bbox 都是 null"，**没有任何报错**。
- `whitelist_token_ids` 是 `<td>` / `</td>` 的 token id：表格里这两个标签本来就该反复出现，
  不放行的话防复读机制会把表格结构本身掐断。
- `max_tokens` 是 **4096 不是 8192**。官方离线脚本写 8192，那是 `LLM` 类直连的用法；
  走 OpenAI 接口时 `prompt_tokens + max_tokens` 必须 ≤ `max_model_len`(8192)，
  而一页图本身就要 256~1120 个视觉 token —— 照抄 8192 会让**每个请求都 400**，
  错误信息里只说"上下文超了"，看不出是这里抄错了。

## 接 gateway

vLLM 只是模型服务，`/v1/parse`、`/v1/extract`、`ask_document` 还得靠 gateway。

```bash
# Redis：任务状态存这儿。注意 focal 自带的是 redis 5.0.7，**没有 RediSearch** ——
# 解析/抽取用不到它，但向量检索会哑（见 models.autodl.yaml 末尾的说明）
apt-get install -y redis-server && redis-server --daemonize yes --port 6379

cd /root/DeepDocParse/gateway
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
MODELS_CONFIG=../models.autodl.yaml SERVICE_TOKEN=... REDIS_URL=redis://localhost:6379/0 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9000
```

`/readyz` 是 `all(up)`：注册表里注册了什么，就得起什么。`models.autodl.yaml`
注册了**两个**远端 —— OCR-2（:18001，同时兼 vlm-ocr 解析引擎）与指令模型（:18002），
两个都得活着探针才绿。`ENABLE_CHAT=0` 那档只起前一个，
**要连带把 `qwen3-4b-instruct` 那段注释掉**，否则 `/readyz` 恒 503、副本永不接流量。

## 成本控制

AutoDL 按**开机时长**计费，跟 GPU 用不用没关系。

```bash
autodl guard ttl pro-xxx 2h        # 给运行中的实例设/重设定时关机
autodl guard idle pro-xxx --threshold 5 --samples 6 --interval 1m
autodl stop pro-xxx                # 用完立刻关，数据保留
```

关机保留数据，随时 `autodl start` 回来（**但连续关机 15 天会被平台释放并清空**）。
装好的环境值钱：重开一台等于重下 17G，所以宁可关机留着也别随手 `rm`。

## 排查：四处不会报错的失效

这条链路上有四个地方坏了也不吭声，`verify.bash` 就是专门盯它们的：

1. **chat template 没挂上** → 服务起得来，每个 chat 请求 400。
   模型仓库里确实没有 chat_template，必须靠 `--chat-template` 显式给。
2. **模板漏了 BOS** → **服务健康、请求 200、token 数正常，输出却是彻底的垃圾。**
   2026-08-25 在 4090D 上实测撞到过，值得单独记一笔：

   | | 没有 BOS | 有 BOS |
   |---|---|---|
   | `Free OCR.` | `PUBLIC DATA / ## 10 10 10 10 …` 复读到 4096 token 上限 | 143 token 整页准确转写 |
   | grounding | `<|ref|>text compared with in 45 c`（标签都配不齐） | `<|ref|>text<|/ref|><|det|>[[94, 50, 339, 68]]<|/det|>` + 正文 |

   原因：官方脚本 tokenize 时是 `bos=True`，而 vLLM 渲染完模板是用
   `add_special_tokens=False` 分词的 —— 它假定模板自己会写。模板不写，BOS 就丢了。
   排查时极容易怀疑到显存/量化/图像预处理上去（我们逐个排除过：视觉 token
   1125 = 6×144+256 与模型卡一致、dtype bfloat16、FLASH_ATTN 后端正常），
   真正的原因只是模板少了一个 token。
3. **`<image>` 占位符没进 prompt** → 模型在盲猜。
   占位符由 vLLM 按 `image_url` 部件的位置插入，**模板里绝不能自己再写一个**。
   注意 `/detokenize` 打回来的是**展开后**的 prompt，一整页会看到上千个
   `<image>`，那是正常的（(0-6)×144 + 256），不是模板重复。
4. **特殊 token 被剥** → 每个块 bbox 全是 null，全程零报错。
   gateway 侧还有第二道网：`vlm_ocr._engine_notes()` 会在
   "识别出了文字却一个 grounding 标签都没有"时往 `layout_json.engine_notes`
   里写 `dsocr2_no_grounding`，并打一条 warning。排查 bbox 全空时先看这个字段。

其他常见问题：

- **启动崩在 `FileNotFoundError: 'ninja'`** → 已经在 `env.bash` 里用
  `VLLM_USE_FLASHINFER_SAMPLER=0` 关掉了。FlashInfer 的采样内核是首次使用时
  JIT 编译的，要 ninja + 版本匹配的 nvcc，AutoDL 基础镜像两样都不满足。
  2026-08-25 在 4090D 上实测撞到过：权重都加载完了，崩在内存 profiling 那一步。
- **启动时 worker 在 CUDA graph 捕获阶段死掉** → `ENFORCE_EAGER=1 ./start.sh autodl start`。
  社区在 0.20~0.23 若干版本上报过（vLLM 论坛 2727 / issue 41468）。代价是吞吐下降。
- **`/v1/models` 里没有 `deepseek-ocr-2`** → `--served-model-name` 与
  `models.autodl.yaml` 的 `options.model` 不一致，gateway 发过来会 404。
- **pip 慢** → `env.bash` 里已经指到阿里云镜像（实例上实测 4.3s vs pypi 直连 15s）。
- **识别质量差、bbox 乱** → 先确认 `models.autodl.yaml` 里 `dialect: deepseek-ocr2`
  写了。缺省是 `generic-json`，那是给通用视觉模型的 prompt，
  对这个只在两个官方 prompt 上训练过的 OCR 专用模型是错的用法。
