# DeepDocParse 语料 API 配置参考

语料 API 的全部配置项。取自 `services/corpus-api/ddp_corpus/config.py`，
**本文件由脚本生成，不要手改** —— 改注释请改源码，然后重跑
`python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > 服务自己的 `.env` > 下表默认值。

账号、API key、配额、限速那一层的配置**不在这里** —— 它们属于
`services/control-api`（Go），见 `services/control-api/CONFIG.md`。

前端另有构建期变量（`apps/web/.env*`，不在下表）：`VITE_API_TARGET`
（dev server 代理到的后端地址）、`VITE_API_BASE`（打包后请求的前缀），
以及 `VITE_DEFAULT_ENGINE`（上传对话框预选的解析引擎，留空取 `ENGINES` 第一条）。
**`VITE_DEFAULT_ENGINE` 要与 `DEFAULT_PARSE_ENGINE`、`infra/registry/models.yaml`
三者对齐** —— 任一处对不上，上传会在网关侧收 404 unknown_engine。

共 **73** 项。

## 本层资源

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | `str` | `'postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse'` | PostgreSQL（必须带 pgvector 扩展；单测走 SQLite in-memory，不读这一项）。 端口 15432 是为了不和别处的 PG 撞车 |
| `MINIO_INTERNAL_ENDPOINT` | `str` | `'127.0.0.1:19000'` | MinIO：service 与浏览器走不同 endpoint —— 预签名 URL 的签名覆盖 host， 两边 host 不同则签名不同，必须分开生成（见 CLAUDE.md 部署陷阱）。 端口 19000/19001：9000 被 gateway 占用，且要避开 Windows 保留段 7964-8063。 service / 本进程可达 |
| `MINIO_PUBLIC_ENDPOINT` | `str` | `'127.0.0.1:19000'` | 浏览器可达 |
| `MINIO_ACCESS_KEY` | `str` | `'minioadmin'` | 对象存储凭据（生产必须改） |
| `MINIO_SECRET_KEY` | `str` | `'minioadmin'` | 同上 |
| `MINIO_SECURE` | `bool` | `False` | 走 https 则置 true |
| `MINIO_PUBLIC_SECURE` | `bool \| None` | `None` | **公网那一侧**的 scheme。内网明文回环、公网由反代或隧道终结 TLS 时两侧不一样： 预签名的签名覆盖 host，给浏览器的那条必须签成 https，否则 https 页面里 发出的是 http 请求，浏览器按混合内容直接拦掉。而只有 minio_secure 一个 开关时把它打开，内网 client 也会去 https 连回环 —— 表现是 ensure_bucket 就连不上。留空（None）跟随 minio_secure，既有部署行为不变 |
| `MINIO_REGION` | `str` | `'us-east-1'` | 区域。**必须显式给** —— 不给的话 SDK 在签名前先向该 endpoint 问一次区域， 而 public endpoint 在容器里连不上，表现是签名接口 502 而其他一切正常 |
| `MINIO_BUCKET` | `str` | `'deepdocparse'` | 桶名。原件、解析结果、出处裁剪图都在这一个桶里，靠对象键前缀分区 |
| `SOURCE_URL_TTL_SECONDS` | `int` | `300` | `/download?format=source` 302 过去的那条直读 URL 的有效期。 **短**是有意的：它是一条带凭证的地址，转发出去就等于转发了原件。 别调到小时级 —— 那样它就变成了事实上的公开链接 |

## 对 service（DeepDocParse）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SERVICE_URL` | `str` | `'http://127.0.0.1:9000'` | DeepDocParse gateway 的地址。解析平面必须走它，embedding/chat 缺省也回落到它 |
| `CONTROL_URL` | `str` | `'http://127.0.0.1:8080'` | 控制面（services/control-api）。本服务向它要两样东西： 稳定文件 URL 的凭证、actor 显示名 —— 两者都住在 control schema， 而 corpus 对那个 schema 没有任何权限（企业边界 5） |
| `SERVICE_TOKEN` | `str` | `'change-me'` | 内网服务凭据，**三个服务必须一致**（control-api / model-gateway / 本服务）。 它是本服务唯一的门禁：actor 上下文头之所以可信，前提就是 "只有持有它的调用方能进来"。占位值会被拒绝启动 |
| `DEFAULT_PARSE_ENGINE` | `str` | `'mineru'` | 上传/重解析没有显式指定引擎时用哪个。**名字必须在 service 的 models.yaml 里存在**， 否则 service 返回 404 unknown_engine —— 这正是无 GPU 环境踩到的： models.cpu.yaml 只注册了 borndigital，本层却按名字写死 mineru，第一步就断。 与注册表驱动一致：换引擎 = 改这一行配置，不改代码 |
| `EMBEDDING_URL` | `str` | `''` | 解析平面必须走 service（那是它的本职）；但 embedding 与 chat 只要求 OpenAI 兼容， 留独立配置以免把本层绑死在 DeepDocParse 的部署形态上（ADR #17）。 留空则回落到 {service_url}/v1/...，dev 下什么都不用配。 如直连 TEI：http://127.0.0.1:18080/v1/embeddings |
| `EMBEDDING_TOKEN` | `str` | `''` | 留空用 service_token |
| `EMBEDDING_MODEL` | `str` | `''` | 留空由上游注册表选 default |
| `CHAT_URL` | `str` | `''` | 如直连 vLLM：http://.../v1/chat/completions |
| `CHAT_TOKEN` | `str` | `''` | 留空用 service_token |
| `CHAT_MODEL` | `str` | `''` | 留空由上游注册表选 default |
| `PUBLIC_BASE_URL` | `str` | `'http://127.0.0.1:8081'` | 本服务对模型网关可达的地址：解析回调用它拼。 宿主机混合模式用 127.0.0.1:8081，全容器模式用服务名（http://corpus-api:8081） |
| `REDIS_URL` | `str` | `''` | 多副本部署必须配：对账选主靠它。留空 = 单实例模式。 **限速不在这里** —— 整体迁去了 control-api |
| `MAX_UPLOAD_BYTES` | `int` | `200 * 1024 * 1024` | 单文件上限。**真正的把关在 control-api**（它签发预签名前就校验）， 这里保留是给"外部提交"路径与展示用 —— 两处的值应当一致。 字节流不再经过本进程（不变式 6），所以它不再是 OOM 防线 |
| `UPLOAD_CHUNK_BYTES` | `int` | `1024 * 1024` | 分片读取的粒度：边读边累计，超限立刻中断，不等整个文件落地 |

## 归档与对账

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `RESULT_TTL` | `int` | `86400` | 必须 <= service 的 RESULT_TTL(24h)：超过这个窗口结果就被 service 清了，补取不回来 |
| `RECONCILE_INTERVAL` | `int` | `60` | 对账扫描间隔（秒）。回调是尽力而为的，恰好在重启时丢掉就得靠这条自愈路径 |
| `GC_GRACE_SECONDS` | `int` | `3600` | 软删除后多久才允许真正回收对象。删对象不可逆，而"删了又重新上传"会复活同一行 （documents.upload 的复活分支）—— 宽限期把"回收与复活撞车"从一个真实的竞态 变成实际不可达，剩下的由 gc 里的 claim 兜住 |

## 检索与问答

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `EMBEDDING_DIM` | `int` | `1024` | bge-m3 是 1024 维。换模型维度变了必须整体 reindex（关系库不能像 Redis 那样按维度分索引名） |
| `EMBEDDING_BATCH_SIZE` | `int` | `16` | 单次 embeddings 请求的最大条数。必须低于运行时的 max-client-batch-size（TEI 默认 32）， 否则长文档整批被拒 413（service 侧 worker 已经踩过） |
| `CHUNK_MAX_CHARS` | `int` | `800` | 分块上限，影响出处粒度 |
| `COMPILE_VISION_ENABLED` | `bool` | `True` | DDP-Compile v1：视觉原子在入库时裁图并由视觉模型生成派生理解。 关掉不是“等价的轻量模式”：文档仍可索引，但 compile_degraded 会明确记录 vision_unavailable，图表类问题不会假装已获得视觉理解。 |
| `COMPILE_VISION_CONCURRENCY` | `int` | `2` | 单文档同时调用 VLM 的原子数 |
| `COMPILE_VISION_TIMEOUT` | `float` | `120.0` | 每个原子的 VLM 超时（秒） |

## 持久任务队列

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `TASK_LEASE_SECONDS` | `int` | `300` | 领取后多久没续租就允许被接管。**太短会让慢任务被反复抢**（每次接管 generation +1，旧的那次算完也写不进去，纯浪费）；太长会让崩溃后的 恢复变慢。取值应当明显大于单个任务的心跳周期 |
| `TASK_HEARTBEAT_SECONDS` | `int` | `30` | worker 的续租周期。必须显著小于 lease，否则一次 GC 停顿就够超时 |
| `TASK_CONCURRENCY_INDEX` | `int` | `2` | 每种任务各自的并发。**不能共用一个无量纲总并发**（§10）： embedding 是网络等待、编译要打视觉模型、抽取是 N 次串行调用， 三者的合理并发差一个数量级 索引：分块 + 批量向量化，主要是网络等待 |
| `TASK_CONCURRENCY_COMPILE` | `int` | `1` | 编译：每个视觉原子打一次 VLM。**默认 1** —— 显存是硬约束， 两份编译并发很容易把 GPU 打满，而排队比 OOM 好 |
| `TASK_CONCURRENCY_EXTRACT` | `int` | `2` | 抽取：一次 = N 个字段 × (检索 + 模型调用)，本身已经是串行的长任务 |
| `TASK_CONCURRENCY_DEFAULT` | `int` | `2` | 其它种类（知识生成、GC、解析轮询）的并发 |
| `TASK_POLL_INTERVAL` | `float` | `1.0` | 空转时的轮询间隔。调大省数据库连接，调小降低任务延迟 |
| `INDEX_LEASE_SECONDS` | `int` | `300` | worker 无 heartbeat 后多久允许接管 |
| `INDEX_HEARTBEAT_SECONDS` | `int` | `30` | 活 worker 的续租周期 |
| `QA_TOP_K` | `int` | `4` | 进 prompt 的 chunk 数 |
| `QA_CANDIDATES` | `int` | `8` | 每路检索的候选数（融合前） |
| `QA_CONTEXT_CHARS` | `int` | `6000` | 资料段总字符上限 |
| `QA_MIN_SIMILARITY` | `float` | `0.45` | 余弦相似度下限。没有它的话 top-k 永远返回东西：问一个与文档无关的问题 也会拿到几条不相干的"出处"，还带着"已做视觉验证"的标记——出处就成了假证据。  0.45 来自真 bge-m3(TEI, CLS pooling) 的实测分布： 相关问题的最佳命中 0.725~0.786，无关问题 0.246~0.381，分离点约 0.55。 取 0.45 两侧都有余量。**往上调是安全的（真实命中还有 0.27 余量），往下调才危险** ——0.35 会放过一部分无关问题（实测有无关提问以 0.352/0.381 越线）。 |
| `QA_LOW_SIMILARITY` | `float` | `0.6` | "低相关"提示线：命中过了下限、但离典型真实命中还差得远时，界面主动提醒用户 "这个回答的依据不太牢靠"，而不是替他判断。取 0.60 = 下限 0.45 与实测真实命中 0.725~0.786 之间偏下的位置：宁可多提醒，也不要让勉强及格的出处看起来同样可信。 **必须 > qa_min_similarity**，否则永远不触发。 |
| `QA_HISTORY_TURNS` | `int` | `6` | 带入的历史消息条数 |
| `QA_CROP_COUNT` | `int` | `1` | 做视觉验证的区域数 |
| `QA_DECISION_ENABLED` | `bool` | `True` | DDP-Agent v1 是否让模型先判断本轮需不需要检索。判定失败必须保守检索， 绝不能退回模型常识；测试基线会显式关闭，另有专门契约用例覆盖开启路径。 |
| `QA_DECISION_TIMEOUT` | `float` | `20.0` | 判定先于 SSE 首帧，必须单独限时；超时按 decision_unavailable 保守执行检索。 |
| `QA_VERIFY_PARSE` | `bool` | `True` | 出处一致性核对（A4）：把裁出来的区域图让视觉模型原样抄一遍，与 chunk 文本比对。 补的是七种降级里唯一的洞 ——「解析本身错了」。那时 chunk 文本是错的， 但语义相似度照样过阈值、照样裁图、照样标 verified，产出这个类别最恶劣的错误： **带着"已做视觉验证"标记的假出处**。核对与回答并发跑，不增加首字延迟。 |
| `QA_PARSE_MISMATCH_THRESHOLD` | `float` | `0.55` | 相似度低于它就判定解析与原图对不上（difflib 比值，0~1）。  **2026-08-25 在 4090D + DeepSeek-OCR-2 上标定过**（此前是没有依据的 0.35）： 一致组（块图 vs 自己的文本）  n=10，全部 1.000 不一致组（块图 vs 别人的文本）n=90，p95=0.382，max=0.643 旧的 0.35 会放过 5/90 个该报的不一致。标定脚本在 service 仓库： `DeepDocParse/scripts/calibrate_verify_threshold.py`。  取 0.55 而不是脚本建议的中点 0.69：标定样本是 born-digital 英文单栏， 是最容易的一类；扫描件/中文上抄写保真度会掉。仍然**宁可漏报不要误报**： 误报会把好出处打成"存疑"，比不报更伤信任。  **这个数字的来源与本层的实际用法并不完全对得上，用之前先读这段。** 标定跑的是 service 侧：DeepSeek-OCR-2 + 它的原生 prompt `Free OCR.`。 而本层 qa.TRANSCRIBE_PROMPT 写死的是一句**中文指令**，模型由 CHAT_MODEL 决定 （quickstart 缺省引导用户填一个通用文本/视觉模型）—— service 侧那套 "按注册表 options.transcribe_prompt 换成模型听得懂的话"**没有移植到本层**。 也就是说：把阈值从 0.35 提到 0.55 在本层是朝着**误报**方向动的， 而上面刚说过本层的既定取向是宁可漏报。之所以仍然跟着提，是因为两处同源、 分叉会更难解释；但**本层至今没有自己的标定数据**。 拿到 GPU 机器后要做的是：用本层真实的 CHAT_MODEL + 中文 prompt 重跑一次 calibrate_verify_threshold.py，按那个分布定本层自己的值 （或者把 transcribe_prompt 那套移植过来，让两层真的同源）。 |
| `QA_VERIFY_TIMEOUT` | `float` | `20.0` | 等核对结果的上限（秒）。核对与回答并发跑，正常情况下回答先结束、这里几乎不等； 但视觉模型在 CPU 上抄一段文字可能要几分钟（read 超时是 900s）， **没有上限的话 done 帧会被硬生生拖后几分钟**，用户看着答案已经出完却迟迟不落定。 超时就当"没测出来"——宁可不打标，也不能让核对拖垮体验 |
| `KNOWLEDGE_ENABLED` | `bool` | `True` | 可整体关掉知识层，旧检索/问答路径不受影响 |
| `KNOWLEDGE_MAX_EVIDENCE` | `int` | `50` | 单次生成送入模型的证据原子上限 |

## 重排序（D1）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `RERANK_URL` | `str` | `''` | 交叉编码器精排。留空 = 回落到 {service_url}/v1/rerank； **service 侧没注册 rerank_models 时那个端点返回 404**，本层据此打 degraded="rerank_unavailable" 并照常返回融合名次 —— 可见降级，不是静默跳过 |
| `RERANK_TOKEN` | `str` | `''` | 留空用 service_token |
| `RERANK_MODEL` | `str` | `''` | 留空由上游注册表选 default |
| `RERANK_ENABLED` | `bool` | `False` | 关掉就完全不调 rerank。默认关：没部署 rerank 容器的人不该每次问答都吃一个 404 往返 |
| `RERANK_CANDIDATES` | `int` | `24` | 送进重排的候选数。**必须显著大于 qa_top_k**，否则无米下锅 —— 精排的价值全在"从更大的候选池里挑"，候选=top_k 时它只是把 4 条重新排了个序 |
| `RERANK_TIMEOUT` | `float` | `20.0` | 等重排结果的上限（秒）。交叉编码器每个候选一次前向，CPU 上 24 条要几秒； 超时就当"没重排"并打 rerank_unavailable —— 宁可不精排也不能把问答拖死 |

## 结构化抽取（v1.1）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `EXTRACT_CANDIDATES` | `int` | `4` | 每个字段进模型的候选块数。抽取是"一个字段一次定位"，候选给多了纯属烧钱： 模型要在 N 个块里挑一个，块越多挑错的机会越大 |
| `EXTRACT_MAX_FIELDS` | `int` | `64` | 一次抽取最多处理多少字段。schema 由用户给，没有上限时一个 200 字段的 schema 就是 200 次检索 + 200 次模型调用 |
| `EXTRACT_MAX_RECORD_BLOCKS` | `int` | `8` | 多记录（表格）抽取时最多看多少个候选块 |
| `EXTRACT_MAX_RETRIES` | `int` | `2` | 模型输出不合 schema 时的重试次数。用尽仍不合规打 schema_violation， **绝不静默把该字段当成"文档里没有"** —— 那会让系统故障伪装成事实 |
| `EXTRACT_CONCURRENCY` | `int` | `4` | 一次抽取里并发跑多少个字段。字段互不依赖，串行跑 30 个字段要等半分钟； 但也不能敞开 —— 上游是同一个 chat 端点，打满只会一起变慢 |
| `EXTRACT_DOC_CONCURRENCY` | `int` | `2` | 批量抽取里同时处理多少份文档。乘以 extract_concurrency 才是真实并发， 两个都调大很容易把上游打挂 |
| `EXTRACT_MAX_DOCUMENTS` | `int` | `200` | 一次批量最多多少份文档。没有上限时"全选"就能提交几千份 |
| `EXTRACT_VERIFY` | `bool` | `True` | 出处一致性核对：裁出区域图让视觉模型原样抄一遍（沿用问答平面 A4 的做法）。 抽取默认**开**（与 service 侧默认关相反）：产品层有原件、有裁剪管线， 而抽取结果是要被当数据用的，核对的价值比问答那边更高 |
| `EXTRACT_VERIFY_FIELDS` | `int` | `3` | 核对的字段数上限。每个字段核对一次 = 一次渲染 + 一次视觉模型调用， 全量核对会让一次 30 字段的抽取变成 60 次模型调用 |
| `CHAT_READ_TIMEOUT` | `float` | `900.0` | 视觉模型在 CPU 上出第一个 token 可能要几分钟（dev 机常态），读超时要留够 |
| `ALLOW_INSECURE_DEFAULTS` | `bool` | `False` | 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权 |

<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->
