# DeepDocParse（service 层）配置参考

gateway 的全部配置项。取自 `gateway/app/config.py`，**本文件由脚本生成，不要手改**——
改注释请改源码，然后重跑 `python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > `gateway/.env` > 下表默认值。

共 **21** 项。

## 通用

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SERVICE_TOKEN` | `str` | `'change-me'` | 唯一鉴权凭据：所有 /v1/* 都验它，且必须与 DeepDocParse-Web 的 SERVICE_TOKEN 一致。 用户 API key 在 Web 层校验，service 完全不感知用户 |
| `REDIS_URL` | `str` | `'redis://localhost:6379/0'` | ARQ 队列 + 任务暂存 + 向量索引都用它。**不是缓存**：结果丢了就得重新解析 |
| `MODELS_CONFIG` | `str` | `'models.yaml'` | 模型注册表路径。加模型 = 加容器 + 在这个文件里加一行（铁律 3） |
| `PARSE_QUEUE_MAX` | `int` | `200` | 在途解析任务的水位上限，超了 /v1/parse 直接 429，让调用方退避而不是把 mineru 压垮 |
| `VQA_MAX_CONCURRENCY` | `int` | `8` | VQA 同步通道的并发闸。视觉模型显存吃紧，超了返回 429 而不是排队等到超时 |
| `RESULT_TTL` | `int` | `86400` | 解析结果在 service 侧的暂存时长（秒）。永久归档是 Web 层的事， 这个窗口只保证"backend 有足够时间来取"。调小会让对账补取不回来 |
| `POLL_INITIAL_DELAY` | `float` | `1.0` | ARQ 轮询 mineru 的退避起点（秒） |
| `POLL_MAX_DELAY` | `float` | `10.0` | 轮询退避上限（秒）：指数退避到这里就不再变长 |
| `POLL_TIMEOUT` | `float` | `1800.0` | 单个解析任务的轮询总时限（秒），超时判失败并释放水位。 必须 < QUEUE_INFLIGHT_TTL，否则水位先被淘汰、任务还在跑 |
| `QUEUE_INFLIGHT_TTL` | `float` | `2400.0` | 在途任务在水位集合里的存活上限。worker 最迟在 poll_timeout 把任务判失败并释放， 所以超过 poll_timeout + 余量还挂着的一定是"释放丢了"（worker 被杀/Redis 重启）， 淘汰它才能让水位自愈 —— 否则 /v1/parse 会永久 429（见 task_store.QUEUE_INFLIGHT_KEY） |
| `EMBEDDING_BATCH_SIZE` | `int` | `16` | v2 分块索引：单次 embeddings 请求的最大 chunk 数。 必须留在运行时的 max-client-batch-size 之下（TEI 默认 32），否则长文档整批被拒 413 |

## 抽取平面（v1.1）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `EXTRACT_CANDIDATES` | `int` | `4` | 每个字段进模型的候选块数。抽取是"一个字段一次定位"，候选给多了纯属烧钱： 模型要在 N 个块里挑一个，块越多挑错的机会越大 |
| `EXTRACT_MIN_SIMILARITY` | `float` | `0.45` | 余弦相似度下限。**与问答平面同一把尺子**：Web 层实测无关问题 0.246~0.381、 真实命中 0.725~0.786。没有它的话每个"文档里其实没有"的字段都会被硬塞一个 最相似的噪声块当出处 —— 而带着出处的假值比空值危险得多 |
| `EXTRACT_LOW_SIMILARITY` | `float` | `0.6` | 低相关提示线：过了下限但没到这里的，界面要提醒"这条依据不牢"。**必须 > 下限** |
| `EXTRACT_MAX_RETRIES` | `int` | `2` | 模型输出不合 schema 时的重试次数。用尽仍不合规打 schema_violation， **绝不静默把该字段当成"文档里没有"** —— 那会让系统故障伪装成事实 |
| `EXTRACT_MAX_FIELDS` | `int` | `64` | 一次抽取最多处理多少字段。schema 是调用方给的，没有上限时一个 200 字段的 schema 就是 200 次检索 + 200 次模型调用 |
| `EXTRACT_MAX_RECORD_BLOCKS` | `int` | `8` | 多记录（表格）抽取时最多看多少个候选块 |
| `EXTRACT_CONCURRENCY` | `int` | `4` | 一次抽取里并发跑多少个字段。字段之间互不依赖，串行跑一个 30 字段的 schema 要等半分钟；但也不能敞开 —— 上游是同一个模型运行时，打满只会一起变慢 |
| `EXTRACT_VERIFY` | `bool` | `False` | 出处一致性核对：裁出区域图让视觉模型原样抄一遍，与块文本比对（沿用问答平面 A4）。 请求方可用 options.verify 覆盖。没有 file_url 或未注册 VQA 模型时打 vision_unavailable |
| `EXTRACT_MISMATCH_THRESHOLD` | `float` | `0.55` | 抄写相似度低于它判定解析与原图对不上。与 Web 层 qa_parse_mismatch_threshold 同源。  **2026-08-25 在 4090D + DeepSeek-OCR-2 上标定过**（此前是没有依据的 0.35）： 一致组（块图 vs 自己的文本）  n=10，全部 1.000 不一致组（块图 vs 别人的文本）n=90，p95=0.382，max=0.643 用旧的 0.35 会放过 5/90 个**该报的不一致**；脚本建议取两组中点 0.69。  这里取 **0.55** 而不是 0.69：标定用的是 tests/fixtures/contract.pdf —— born-digital、英文、单栏，是最容易的一类，一致组才会齐刷刷 1.000。 扫描件/中文/多栏上抄写保真度一定往下掉，阈值定太高会把好出处打成"存疑"， 而这两处的既定取向是**宁可漏报不要误报**（误报比不报更伤信任）。  换文档类型前重标一次： python scripts/calibrate_verify_threshold.py --pdf <你的文档> \ --endpoint http://127.0.0.1:18001 --model deepseek-ocr-2 \ --models-config models.autodl.yaml |
| `ALLOW_INSECURE_DEFAULTS` | `bool` | `False` | 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权 |

<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->
