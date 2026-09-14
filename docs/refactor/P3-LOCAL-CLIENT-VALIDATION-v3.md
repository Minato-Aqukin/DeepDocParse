# P3 本地 Client API 接线验证

后续模型安装、真实 CPU 生成与 OOM 验证见 [P3-LOCAL-MODEL-VALIDATION-v3.md](P3-LOCAL-MODEL-VALIDATION-v3.md)。下文测试数字和未完成项保留为该轮历史记录，不能替代后续模型报告或全阶段验收。

2026-09-12。限定为已有本地运行时接入共享连接层所需的握手、投影、断点和对账接口；不代表 Electron 整体验收完成。

## 已落地契约

- `GET /api/v1/client/handshake`：`protocol_version=ddp-client/1`，持久 workspace/environment/authority 身份，稳定本地主体和接口能力列表；沿用所有 HTTP 鉴权边界。
- `GET /api/v1/client/snapshot`：返回 `{cursor,sequence,state:{resources,tasks,capabilities}}`。SQLite WAL 的同一读事务固定游标与资源/任务状态，其他连接在读取中途提交不会被混入旧游标的投影。
- `GET /api/v1/client/events?after=...`：单条完整投影批次携带 `previous_sequence`；无变化也返回同序号 ACK。非法、未来、跨 workspace 或已超出保留区间的 cursor 返回 `410 cursor_expired`。
- `GET /api/v1/client/receipts/{key}`：按精确 operation key 查询既有任务，未知返回 404，不创建或重发任务。含斜杠的键通过 URL 编码后也可对账。
- Cursor 格式为 `local.<workspace_id>.<event_seq>`，消费者当作 opaque。复用现有 events 与 SQLite 自增序号高水位；未另建持久投影或事件总状态。
- HTTP listener 每次启动写入 `runtime.started`，让更换 Provider 配置后的恢复强制取得新的投影。心跳字段 `lease_until` / `updated_at` 不进入 client task 投影；普通 task/receipt 接口仍返回它们。

stdout bootstrap 的完整非秘密形状：

```json
{
  "url": "http://127.0.0.1:<random-port>",
  "token_file": "/absolute/path/to/private-session.json",
  "protocol_version": "ddp-client/1",
  "identity": {
    "environment_id": "local-<persistent-id>",
    "workspace_id": "<persistent-id>",
    "authority_node_id": "local-<persistent-id>"
  },
  "profile": {"issuer": "local-<persistent-id>", "subject": "workspace:<persistent-id>"},
  "capabilities": ["resource.list", "resource.upload", "task.read", "task.cancel",
    "corpus.retrieve", "evidence.read", "bundle.export", "bundle.import",
    "rag.answer.cited", "wiki.build", "client.snapshot", "client.events", "client.receipt"]
}
```

Token 只在权限 0600 的文件中，文件 JSON 为 `{url,token,pid}`。HTTP handshake 是上述 bootstrap 去掉 `url` / `token_file`。能力列表表达接口支持，实际模型可用状态在 `state.capabilities`，不会凭这张列表声称模型已运行。

## 实测

`python/ddp_local/tests/test_client_api.py` 的 3 条新测试通过：

1. 实际子进程 + 随机回环 HTTP，检查非秘密 bootstrap、0600 token、鉴权、上传/CPU 解析、批次事件、ACK、重启后身份与 receipt 保持、序号推进、另一个 workspace 拒绝旧 cursor。
2. 两个真实 SQLite 连接，在旧 snapshot 的多次读取之间提交新资源；旧投影仍保持旧序号和空资源/任务，下一次 snapshot 才看见新提交。
3. 模拟事件保留区间失效，确认旧游标 410，高水位不回退，新 snapshot 可继续 ACK；未来/非法游标拒绝。

全 local suite **26 passed**，日志 `/tmp/ddp-local-client-full.log`；新接口单独跑见 `/tmp/ddp-local-client-api.log`。ruff F/B 与枚举守卫通过。P2 CPU 与服务器适配器 smoke 继续通过，仍明确显示没有实际生成模型。

真实退出用例发现并修正了此前单测漏掉的缺陷：uvicorn 0.52.4 在 ASGI shutdown 后重放 SIGTERM，会在 CLI 外层 finally 前结束进程，留下会话凭证文件。现将凭证清理挂到 ASGI lifespan shutdown，并保留外层 finally；实际 SIGTERM 退出后文件消失。

## 剩余边界

- SIGKILL/系统崩溃无法执行 shutdown；Electron main 仍需按进程所有权清理失效凭证和子进程，不能删除另一个活动会话的文件。当前 API 没有阻止两个 listener 同时打开同一个 workspace；单进程所有权须由桌面宿主补齐。
- Snapshot 当前返回整个本地资源/任务投影，没有分页和按响应字节限制；大型工作区的预算、压力和内存验收尚未完成。
- Listener 参数、健康与断开状态属于当前进程；接线代码只提供真实已支持的接口，不代表模型安装、GPU profile、Linux 凭证后端或完整桌面打包已完成。
- 生成/Wiki 真实模型验收、语义引用质量和中心 HTTPProvider 的端到端接入继续按总计划执行；本报告没有把这些计为通过。
