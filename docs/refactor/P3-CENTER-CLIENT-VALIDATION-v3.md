# P3 中心 Client Provider 读取/对账切片（2026-09-13）

本记录是作者自验，未 commit/push，不是提交前独立验收，也不代表 P3 或 P0–P7 完成。协议权威见 `packages/contracts/ddp/client-runtime-format.md` 的 Center metadata windows / accepted-upload receipts。

## 已接线

控制入口提供已认证的 `GET /api/v1/client/snapshot`、`GET .../events?after=`、`GET .../receipts/{operation_key}`、`POST .../query`。会话或带 read scope 的 key 可读；真实用户/组织/凭据 scope 从控制域鉴权结果产生，伪造身份、scope、authority 头不能覆盖它。Go 仅通过固定 corpus HTTP 调用读取业务数据，不查询 corpus SQL。远端 command 明确返回 `approved_plan_required`。

corpus 新增迁移 0025（前序 head 0024），持久保存调用者视图修订、固定元数据快照/分页和真实上传受理回执。PostgreSQL 使用 SERIALIZABLE 事务及范围行锁，序列化冲突有界重试；SQLite 使用写事务。资源、固定版本、固定 ParseJob 与能证明 job 绑定的持久队列任务在同一事务读取。源全文、Task.payload、worker/lease、原始错误消息、配置 URL 和凭据均不进入状态投影。

模型配置与健康是不同字段。健康仍取真实 corpus 能力生产者；只排除观测/心跳时间戳，配置选择和 readiness 的变化进入新的持久修订。即使模型离线、profiles 为空，安全的模型名/开关变化也会出事件。无变化 ACK 复用原 cursor、sequence 和已保存 state，不能一边 ACK 旧水位一边返回新状态。

窗口资源的 `id` 是 resource_id，独立 `version_id` 指定固定版本。首屏资源/任务各至多 100 条、目标 512 KiB；响应最多 3 MiB。state.windows 明确 visible_total、items_loaded、has_more、next_cursor；snapshot_complete 指服务端完整枚举所声明的元数据范围，cache_complete 指首屏是否已含全部元数据。固定 `resource.page`/`task.page` 查询按需替换窗口，不要求拼接无限缓存。超过 20,000 可见版本/绑定队列任务、32 MiB 投影或单项 512 KiB 时显式 507；保留历史每 scope 最多 16 个快照/64 MiB，游标 15 分钟过期。旧页每次重新核对全部固定权限绑定，撤销已缓存首屏的条目也会使后续旧页失效。

`corpus.search` 复用既有检索/证据业务实现；`version_ids=null` 是当前授权固定版本，`[]` 是空范围，最多 1,000 显式版本必须逐一通过批量授权，和单一 resource/version context 冲突则拒绝。文档及 parse-job 授权进入排序/top-k 之前，外部 await 后重新核对命中与返回 scope 中的版本 ID。返回 hits 兼容当前共享调用方。

`evidence.get` 复用既有证据授权，并返回 `{id,version_id,excerpt,evidence,crop}`。顶层 version_id 是当前中心可读副本；envelope 保留独立的 source_version_id、摘要、节点与 locator。当前只为原生中心、摘要已验证的固定解析构造来源 envelope；未知来源/未绑定 Bundle 不能被冒充为新的本地来源。

既有 DocumentSubmitted/ingest 业务入口在 ResourceVersion/ParseJob 受理事务内写 receipt，早于上游解析派发。operation_key 使用控制 upload-session ID；老事件缺 upload_id 时使用原 event ID。回执绑定组织和原始 principal，保存规范请求摘要与固定 resource/version/parse_job。读取只证明受理、不证明成功，未知/他人/不再授权均 404；查询不会再次派发解析或生成。

## 验证

测试仅使用 `ddp-v3-review-pg`（127.0.0.1:15439）：控制认证库 `ddp_v3_discovery`，本次新建专用语料库 `ddp_v3_center_client`。没有连接或修改开发数据库。测试口令只通过环境变量传入，不写入文档。

| 检查 | 结果 |
| --- | --- |
| 完整 corpus 回归检查点 | 501 passed / 1 skipped，日志 `/tmp/ddp-center-client-corpus-all.log`；skip 为显式 opt-in PG 测试，随后已单独真跑。此检查点后的权限 scope、来源降敏和大元数据用例包含在下方最终定向验证。 |
| 最终 client SQLite + PG | **10 passed**，9 项 SQLite/序列化用例 + 1 项 PostgreSQL HTTP/并发用例；日志 `/tmp/ddp-center-client-final.log`。 |
| Go 全套真实控制 PG + Python HTTP | `go test -race -count=1 ./...` PASS，日志 `/tmp/ddp-center-client-go-all.log`。 |
| 最终真实双服务协议 | `TestCenterClientRealCorpusAuthAndScope` PASS，日志 `/tmp/ddp-center-client-cross-final.log`。使用真实 corpus ASGI 路由/服务鉴权及已迁移 PG 表，不用固定 200 替代认证。 |
| 真实能力生产者 → Go | 上述测试实际观察 corpus `/internal/capabilities` HTTP 200 及原响应，再经 Go `ProjectProfiles` 与握手比对。测试网关地址是确定不可达的回环端口，真实生产者诚实返回 unknown/空 profiles；把 user actor 错传给 service-only 端点产生 403 时断言会失败。没有模型健康函数替身。 |
| 游标竞争 | 12 个并发 snapshot 得到相同范围 cursor/sequence；8 个并发撤销后 events 得到相同新修订，previous_sequence 正确，响应不包含已撤销资源。跨主体游标、过期游标均 410。 |
| 权限与幂等负向 | 同字节其他资产的私有 parse 不进入 top-k；任一选定版本未授权返回 404；空列表不变全库；撤销版本不会残留在 hits 或 scope 列表；未知 query/多余字段失败；receipt 有真实 ParseJob 正向且多次查询不增加上游提交计数。 |
| 大元数据 | 实际构造超过 4 MiB 的 5,000 条版本元数据，全部进入稳定分页，总数无丢失，首屏和每页均小于 4 MiB；超大单项显式 `projection_item_too_large`。 |
| 迁移 | 新库从 0001 顺序升级至 **0025** 成功，真实 HTTP 与并发测试使用这套迁移表，未用 create_all 替代迁移；日志 `/tmp/ddp-center-client-migrate.log`。 |
| 静态检查 | 所有本次文件按仓库门禁 F/B ignore 集合通过；`git diff --check` 无错误。 |

复跑所需变量：`CONTROL_TEST_DATABASE_URL`（Go 控制库）与 `CENTER_CLIENT_TEST_DATABASE_URL`（SQLAlchemy asyncpg 语料库）。不设置后者会显式 skip PG/Python 跨服务测试；不能将 skip 当成真实验证通过。

## 尚未完成的出口

这是读取和已受理上传对账切片。远端计划审批、外发上传/原件传输、远端生成/取消接单、交付验收及完整桌面→中心业务 e2e 仍属后续。receipt 目前只覆盖真实 DocumentSubmitted 受理路径，不能据此宣称其他远端写命令可重放或可恢复。

state.projection_scope 明确 all_task_kinds=false：当前任务范围是固定解析及能证明其 job 绑定的队列；缺少固定绑定的历史抽取、知识、GC 任务没有被猜测授权或宣称完整。Bundle 原始证据身份的中心索引/查询仍需单独实现，当前不会以本中心身份改写外来出处。

事件序列是调用者可见的观测修订，不是完整业务 CDC 日志，不承诺展示两次轮询之间所有中间状态，也不承诺历史全文快照。SQLite 客户端缓存仍须遵守 4 MiB projection / 64 MiB scope 上限，按窗口保存，不能把所有分页合并后宣称完整缓存。
