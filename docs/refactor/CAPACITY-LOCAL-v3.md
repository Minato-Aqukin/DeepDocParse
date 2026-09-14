# 本地容量基线（v3，CPU-only）

> **本地 CPU、无 GPU、单机 dev 栈，不是生产能力声明。**
> 这里的所有数字都是一次真实运行的结果，报告 JSON 与日志路径附在每节。
> 用途：给"这台机器"和"这台机器的降级行为"留一个可复算的基线；
> 容量目标仍待用户确认（`docs/refactor/STATUS.md` §18 第 5 条）。

## 0. 环境与运行

| 项 | 值 |
|---|---|
| 主机 | CachyOS(kernel 7.2.4) / AMD Ryzen 7 255 / 16 线程 / 24.4 GB / 无独显 |
| 栈 | `scripts/dev.sh up`（无 GPU 档，`MODELS_CONFIG=models.cpu.yaml`） |
| 入口 | `http://127.0.0.1:8080`（control-api；PG 15432、MinIO 19000、redis 16379） |
| 服务 | control-api / corpus-api / corpus-worker(**单副本**) / model-gateway / mcp / PG / MinIO / redis |
| 夹具 | `tests/fixtures/sample.pdf`，439 字节，1 页，`sha256=d5af21db…` |
| 压测工具 | `scripts/loadtest.py`（argparse + httpx，无新依赖） |
| 主报告 | `dist/loadtest/loadtest-66ddf85b.json`（run 66ddf85b，2026-09-13T14:46:53Z） |
| 主日志 | `/tmp/opencode/loadtest-run2.log` |
| CPU 佐证报告 | `/tmp/opencode/loadtest-cpu-confirm.json`（level=4，带 `/proc/stat` 采样） |

复算命令：

```bash
scripts/dev.sh up
.venv/bin/python scripts/loadtest.py --levels 1,4,16 --iterations 3,8,16
.venv/bin/python scripts/loadtest.py --self-test        # 不连网络的聚合自检
```

每个流程：注册（每次运行一个唯一账号）→ 每档登录一次（同档 worker 复用
会话，真实客户端不会每条请求重登）→ 预签名直传 + finalize + 服务端摘要 →
outbox 建文档 → 解析轮询到终态 → `/api/search` → 联邦
`task-intents`（失败时如实记错误码）。每档的流程内容都带 `c<档位>f<序号>`
后缀，保证 digest 唯一、解析真的执行（早期版本跨档重复 digest 会被幂等
复用，已修）。

## 1. 主运行数字（run 66ddf85b）

每格 `n / 失败 / p50 / p95 / p99 / max`，单位毫秒；`parse` 的 n 是文档数。

| 步骤 | 并发 1（3 流程，墙钟 158.6s） | 并发 4（8 流程，151.5s） | 并发 16（16 流程，104.2s） |
|---|---|---|---|
| auth_login | 1 / 0 / 198.7 / 198.7 / 198.7 / 198.7 | 1 / 0 / 187.2 / 187.2 / 187.2 / 187.2 | 1 / 0 / 185.5 / 185.5 / 185.5 / 185.5 |
| upload | 3 / 0 / 1316 / 2859 / 2859 / 2859 | 8 / 0 / 1660 / 3194 / 3194 / 3194 | 16 / 0 / 6196 / 12121 / 12121 / 12121 |
| doc_ingest | 3 / 0 / 973 / 983 / 983 / 983 | 8 / 0 / 1952 / 2330 / 2330 / 2330 | 16 / 0 / 1809 / 3267 / 3267 / 3267 |
| parse | 3 / 0 / **56498** / 58397 / 58397 / 58397 | 8 / 0 / **56509** / 73834 / 73834 / 73834 | 16 / 0 / **59395** / 64941 / 64941 / 64941 |
| search | 3 / 0 / 3657 / 3665 / 3665 / 3665 | 8 / 0 / 7377 / 11380 / 11380 / 11380 | 16 / 0 / **18575** / 34059 / 34059 / 34059 |
| federation_intent | 3 / **3** / 11.6 / 11.9 / 11.9 / 11.9 | 8 / **8** / 12.0 / 16.4 / 16.4 / 16.4 | 16 / **16** / 12.7 / 3798.8 / 3798.8 / 3798.8 |

注册（每次运行一次）：`201`，189 ms。主运行里 auth/upload/doc_ingest/parse/
search **零 5xx、零超时**；唯一的"错误"是联邦意图的显式 503（§2.1）。

## 2. 失败与降级（都可见，不是静默）

### 2.1 联邦任务：本机被显式拒绝（503 `node_identity_unconfigured`）

主运行 27 次 `POST /api/v1/task-intents` 全部返回：

```json
{"error":{"message":"configure a persistent node identity (BUNDLE_NODE_ID)",
          "type":"server_error","code":"node_identity_unconfigured"}}
```

dev 栈没有配置 `BUNDLE_NODE_ID`（`docker compose … exec ddp-corpus-api-1 env`
里没有它）。所以"submit→poll"在真实栈上**只能走到提交被拒**：
`loadtest.py` 已实现 intent→plan→approve→submit→poll 全链，配置了节点身份
的部署会继续往下跑；本机记录的是被拒绝的事实，不伪造成成功。

第二个已知门槛：`collection.publish` 需要
`members_state(..., public=True) == "ready"`，而本机索引是
`index_status=failed`（没有 embedding）→ 409 `collection_not_publishable`。
两件事都在 `packaging` 之外的部署配置里，未在本轮改。

### 2.2 search：100% `degraded=embedding_unavailable`

27/27 次搜索返回 HTTP 200 且 `"degraded":"embedding_unavailable"`
（可见降级）。model-gateway 日志里对应一批
`POST /v1/embeddings → 502 embedding runtime unreachable`；
`models.cpu.yaml` 本来就没有 embedding 端点。搜索仍是"关键词路跑完"的
真实请求，只是语义路缺席。

### 2.3 parse：终态被 60 秒对账周期量化

所有解析都 `parse_status=succeeded`，但主运行读到的
`index_status` 是 `pending/indexing`——索引失败要再等一轮才落到
`index_status=failed`（DB 里随后可见 `向量化失败：… embedding_unreachable`）。
解析时延从并发 1 到 16 几乎不变（56.5→59.4s p50），波形像定时器而不像
排队：`reconcile_interval=60`（`services/corpus-api/ddp_corpus/config.py:107`，
循环在 `reconcile.py:110`），gateway 完成回调没配置时，终态要等下一轮
对账才被发现。**这 ~56-60s 是轮询粒度，不是 CPU 解析耗时**；有回调或把
`RECONCILE_INTERVAL` 调小的部署会完全不同。

CPU 佐证（`/tmp/opencode/loadtest-cpu-confirm.json`，并发 4、4 个流程）：
整机 CPU busy **mean 11.7% / max 15.2%**（79 个采样），loadavg 1.7→2.35。
机器根本没吃满，与"时延由定时器/上游等待主导"一致。

## 3. 什么主导了时延

| 现象 | 主因（有据） |
|---|---|
| parse ~56-60s、并发不敏感 | 60s 对账周期（config.py:107 / reconcile.py:110） |
| search 3.7s→7.4s→18.6s（p50）随并发翻倍增长 | `embed_one` 的 embedding-unavailable 路径与重试；并发越高排队越久 |
| upload p50 1.3s→1.7s→6.2s | MinIO 预签名直传 + finalize 摘要校验，并发下 MinIO/PG 排队 |
| doc_ingest 约 1-2s、并发基本不涨 | control outbox → corpus 落库的固定开销 |
| 联邦 12ms 即返 | 身份未配置，在入口/语料侧就短路了 |

## 4. 早期一次被作废的运行（保留证据）

`dist/loadtest/loadtest-f5a6423e.json`：并发 16 时 8 个流程里 6 个
`429 rate_limited`（登录限速 `LOGIN_RATE_LIMIT_PER_MIN=10`），且跨档
digest 重复被幂等复用，`parse` 出现假的 43ms。两个都是 harness 设计问题，
已修：同档登录一次并复用会话；流程 nonce 带上档位。上面的主运行是修复后
的结果；旧报告留档只为说明"怎么发现并修掉的"。

## 5. 明确边界

- 单机、单副本 corpus-worker、开发档镜像；没有 GPU/TEI/rerank/mineru/VQA。
- 439 字节 1 页 PDF、单用户；不能用它推生产 QPS/并发容量。
- 联邦链路本机只到"提交被拒"；`BUNDLE_NODE_ID` 与可发布集合是前置。
- 报告里的注册响应体已做脱敏（原始响应含会话 token；harness 现在不落
  任何响应体）。
