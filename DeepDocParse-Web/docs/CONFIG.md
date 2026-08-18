# DeepDocParse-Web（产品层）配置参考

后端的全部配置项。取自 `backend/app/config.py`，**本文件由脚本生成，不要手改**——
改注释请改源码，然后重跑 `python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > `backend/.env` > 仓库根 `.env` > 下表默认值。

前端另有两个构建期变量（`frontend/.env*`，不在下表）：
`VITE_API_TARGET`（dev server 代理到的后端地址）与 `VITE_API_BASE`（打包后请求的前缀）。

共 **45** 项。

## 本层资源

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | `str` | `'postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse'` | PostgreSQL（必须带 pgvector 扩展；单测走 SQLite in-memory，不读这一项）。 端口 15432 是为了不和别处的 PG 撞车 |
| `JWT_SECRET` | `str` | `'change-me'` | 签发/校验用户会话的密钥。**占位值会被拒绝启动**：它是 change-me 等于任何人都能给任意 user_id 伪造一个有效会话，且运行时不报任何错 |
| `JWT_TTL_MINUTES` | `int` | `60 * 24 * 7` | 登录态有效期（分钟），默认 7 天 |
| `BCRYPT_ROUNDS` | `int` | `12` | bcrypt 成本因子。**生产不要调低** —— 它就是抗离线爆破的全部本钱。 单测把它降到 4：默认 12 时一次 hash+verify 要 0.37s，而几乎每个用例都要注册一个 用户，光这一项就占掉整个套件大半时间（见 tests/conftest.py） |
| `MINIO_INTERNAL_ENDPOINT` | `str` | `'127.0.0.1:19000'` | MinIO：service 与浏览器走不同 endpoint —— 预签名 URL 的签名覆盖 host， 两边 host 不同则签名不同，必须分开生成（见 CLAUDE.md 部署陷阱）。 端口 19000/19001：9000 被 gateway 占用，且要避开 Windows 保留段 7964-8063。 service / 本进程可达 |
| `MINIO_PUBLIC_ENDPOINT` | `str` | `'127.0.0.1:19000'` | 浏览器可达 |
| `MINIO_ACCESS_KEY` | `str` | `'minioadmin'` | 对象存储凭据（生产必须改） |
| `MINIO_SECRET_KEY` | `str` | `'minioadmin'` | 同上 |
| `MINIO_SECURE` | `bool` | `False` | 走 https 则置 true |
| `MINIO_BUCKET` | `str` | `'deepdocparse'` | 桶名。原件、解析结果、出处裁剪图都在这一个桶里，靠对象键前缀分区 |

## 对 service（DeepDocParse）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SERVICE_URL` | `str` | `'http://127.0.0.1:9000'` | DeepDocParse gateway 的地址。解析平面必须走它，embedding/chat 缺省也回落到它 |
| `MCP_URL` | `str` | `'http://127.0.0.1:9100'` | service 的 MCP 平面，/mcp 反代的上游 |
| `SERVICE_TOKEN` | `str` | `'change-me'` | 与 service 之间的内网令牌，**必须与 DeepDocParse/.env 的 SERVICE_TOKEN 一致**。 它同时也是 /internal/* 回调端点的凭据 —— 占位值会被拒绝启动 |
| `EMBEDDING_URL` | `str` | `''` | 解析平面必须走 service（那是它的本职）；但 embedding 与 chat 只要求 OpenAI 兼容， 留独立配置以免把本层绑死在 DeepDocParse 的部署形态上（ADR #17）。 留空则回落到 {service_url}/v1/...，dev 下什么都不用配。 如直连 TEI：http://127.0.0.1:18080/v1/embeddings |
| `EMBEDDING_TOKEN` | `str` | `''` | 留空用 service_token |
| `EMBEDDING_MODEL` | `str` | `''` | 留空由上游注册表选 default |
| `CHAT_URL` | `str` | `''` | 如直连 vLLM：http://.../v1/chat/completions |
| `CHAT_TOKEN` | `str` | `''` | 留空用 service_token |
| `CHAT_MODEL` | `str` | `''` | 留空由上游注册表选 default |
| `PUBLIC_BASE_URL` | `str` | `'http://127.0.0.1:8080'` | 本服务对 service 可达的外部地址：稳定文件 URL 与解析回调都用它拼。 宿主机混合模式用 127.0.0.1:8080，全容器模式用服务名（http://web-backend:8080）。 |
| `REDIS_URL` | `str` | `''` | 多副本部署必须配：限速计数与对账选主都靠它。留空 = 单实例模式（进程内计数） |
| `MAX_UPLOAD_BYTES` | `int` | `200 * 1024 * 1024` | 单次上传的字节上限。**必须有**：上传体要整个进内存（算内容 sha256 当 doc_id， 再原样 put 进 MinIO），没有上限时任意登录用户传个大文件就能把进程打爆 ——dev 机 WSL 只有 ~7.7GB，门槛极低。超限返回 413。 |
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
| `QA_TOP_K` | `int` | `4` | 进 prompt 的 chunk 数 |
| `QA_CANDIDATES` | `int` | `8` | 每路检索的候选数（融合前） |
| `QA_CONTEXT_CHARS` | `int` | `6000` | 资料段总字符上限 |
| `QA_MIN_SIMILARITY` | `float` | `0.45` | 余弦相似度下限。没有它的话 top-k 永远返回东西：问一个与文档无关的问题 也会拿到几条不相干的"出处"，还带着"已做视觉验证"的标记——出处就成了假证据。  0.45 来自真 bge-m3(TEI, CLS pooling) 的实测分布： 相关问题的最佳命中 0.725~0.786，无关问题 0.246~0.381，分离点约 0.55。 取 0.45 两侧都有余量。**往上调是安全的（真实命中还有 0.27 余量），往下调才危险** ——0.35 会放过一部分无关问题（实测有无关提问以 0.352/0.381 越线）。 |
| `QA_LOW_SIMILARITY` | `float` | `0.6` | "低相关"提示线：命中过了下限、但离典型真实命中还差得远时，界面主动提醒用户 "这个回答的依据不太牢靠"，而不是替他判断。取 0.60 = 下限 0.45 与实测真实命中 0.725~0.786 之间偏下的位置：宁可多提醒，也不要让勉强及格的出处看起来同样可信。 **必须 > qa_min_similarity**，否则永远不触发。 |
| `QA_HISTORY_TURNS` | `int` | `6` | 带入的历史消息条数 |
| `QA_CROP_COUNT` | `int` | `1` | 做视觉验证的区域数 |
| `QA_VERIFY_PARSE` | `bool` | `True` | 出处一致性核对（A4）：把裁出来的区域图让视觉模型原样抄一遍，与 chunk 文本比对。 补的是七种降级里唯一的洞 ——「解析本身错了」。那时 chunk 文本是错的， 但语义相似度照样过阈值、照样裁图、照样标 verified，产出这个类别最恶劣的错误： **带着"已做视觉验证"标记的假出处**。核对与回答并发跑，不增加首字延迟。 |
| `QA_PARSE_MISMATCH_THRESHOLD` | `float` | `0.35` | 相似度低于它就判定解析与原图对不上（difflib 比值，0~1）。 **这个默认值还没有在真视觉模型上标定过**（本机无 GPU），拿到 GPU 机器后 按实测分布重定 —— 与 qa_min_similarity 当年的定法一样。宁可漏报不要误报： 误报会把好出处打成"存疑"，比不报更伤信任 |
| `QA_VERIFY_TIMEOUT` | `float` | `20.0` | 等核对结果的上限（秒）。核对与回答并发跑，正常情况下回答先结束、这里几乎不等； 但视觉模型在 CPU 上抄一段文字可能要几分钟（read 超时是 900s）， **没有上限的话 done 帧会被硬生生拖后几分钟**，用户看着答案已经出完却迟迟不落定。 超时就当"没测出来"——宁可不打标，也不能让核对拖垮体验 |
| `QA_RATE_PER_MIN` | `int` | `20` | 每用户问答限速 |
| `CHAT_READ_TIMEOUT` | `float` | `900.0` | 视觉模型在 CPU 上出第一个 token 可能要几分钟（dev 机常态），读超时要留够 |

## 额度默认值（新建 key 时的初值）

| 环境变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `DEFAULT_QUOTA_PAGES` | `int | None` | `1000` | None = 不限 |
| `DEFAULT_RATE_LIMIT_PER_MIN` | `int` | `60` | 新建 key 的默认限速（次/分钟） |
| `ALLOW_INSECURE_DEFAULTS` | `bool` | `False` | 只有明确知道自己在做什么才打开（一次性容器、CI）。生产打开等于没有鉴权 |
| `CORS_ORIGINS` | `str` | `'http://localhost:5173,http://127.0.0.1:5173'` | 允许跨源访问的前端地址，逗号分隔。默认是 dev 的 Vite（5173）—— 换部署形态时必须能改配置而不是改代码 |

<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->
