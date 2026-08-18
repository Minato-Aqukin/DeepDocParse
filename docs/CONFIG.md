# DeepDocParse（service 层）配置参考

gateway 的全部配置项。取自 `gateway/app/config.py`，**本文件由脚本生成，不要手改**——
改注释请改源码，然后重跑 `python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > `gateway/.env` > 下表默认值。

共 **12** 项。

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
| `ALLOW_INSECURE_DEFAULTS` | `bool` | `False` | 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权 |

<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->
