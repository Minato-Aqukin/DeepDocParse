# mineru-api 实测契约（锁定版本 3.4.4）

> 来源：官方镜像（本仓库 `docker/mineru/Dockerfile`，基于 vllm-openai:v0.21.0 + `mineru[core]==3.4.4`）
> 实测方式：容器 `mineru-api --host 0.0.0.0 --port 8000`，抓取 `/openapi.json` 并真实提交任务验证。
> 实测日期：2026-07-14。升级 mineru 版本前必须先按本文档跑绿 `tests/`（铁律 4）。

## 端点总览

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/tasks` | 提交异步解析任务（multipart） |
| GET | `/tasks/{task_id}` | 查任务状态 |
| GET | `/tasks/{task_id}/result` | 取任务结果 |
| POST | `/file_parse` | 同步解析（gateway 不用——大文件会长时间占连接） |
| GET | `/health` | 健康检查（readyz 探针用） |

## POST /tasks —— 提交

`multipart/form-data`，**只收文件上传，不支持 URL 输入**（`server_url` 字段在 0.0.0.0 监听时被安全策略禁用，见启动日志 SSRF 警告）。gateway 因此先从 `file_url` 下载再转传（内存流，不落盘）。

关键表单字段（实测默认值）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `files` | file[] | 必填 | 可多文件；gateway 每任务只传一个 |
| `backend` | str | `hybrid-engine` | dev 用 `pipeline`（8GB 显存）；prod 用 vlm 系 |
| `lang_list` | str[] | `['ch']` | 语言 |
| `parse_method` | str | `auto` | |
| `formula_enable` / `table_enable` / `image_analysis` | bool | true | |
| `return_md` | bool | true | **gateway 显式传 true** |
| `return_middle_json` | bool | false | **gateway 显式传 true**（页码+bbox 数据源） |
| `return_content_list` | bool | false | **gateway 显式传 true** |
| `return_images` | bool | false | **gateway 显式传 true** |
| `start_page_id` / `end_page_id` | int | 0 / 99999 | 透传 options 可用 |

响应 `202`：

```json
{"task_id": "35447197-…", "status": "pending", "backend": "pipeline",
 "file_names": ["sample"], "created_at": "…", "started_at": null,
 "completed_at": null, "error": null, "status_url": "…", "result_url": "…",
 "queued_ahead": 0, "message": "Task submitted successfully"}
```

## GET /tasks/{task_id} —— 状态

- `200`：同上结构；`status` 枚举实测为 `pending | processing | completed | failed`
  - gateway 归一化：`pending→pending`，`processing→running`，`completed→succeeded`，`failed→failed`
- `404`：`{"detail": "Task not found"}`（任务不存在/已清理 → `MineruTaskNotFound`）

## GET /tasks/{task_id}/result —— 结果

- `202`：未就绪，body 同状态结构（`message: "Task result is not ready yet"`）→ 客户端返回 `None` 继续轮询
- `200`：

```json
{"backend": "pipeline", "version": "3.4.4",
 "results": {"<不含扩展名的文件名>": {
     "md_content": "# …",
     "middle_json": "<JSON 字符串（需二次解析）>",
     "content_list": [...],
     "images": {"<name>.jpg": "data:image/jpeg;base64,…"}}}}
```

  - `middle_json` 是**字符串**，解析后 `pdf_info[]` 每页含 `page_idx`、`page_size`、
    `para_blocks[]`（块带 `type` + `bbox[4]`）——ask_document 裁剪验证与 v2 分块索引的数据源
- `404`：任务不存在
- `409`：任务失败（源码确认；未现场复现）→ 客户端抛 `RuntimeError`

## 与 gateway 的映射（app/services/mineru_client.py）

- `submit()`：下载 file_url → `POST /tasks`（options 透传覆盖表单默认值，return_* 四项恒为 true）→ 存 `task_id`
- `status()`：`GET /tasks/{id}` → 状态归一化四态
- `fetch_result()`：`GET /tasks/{id}/result` → 整形为契约 `{markdown, layout_json(对象), images[]}`

## 已知注意事项

1. `results` 的 key 是**去扩展名的文件名**（`sample.pdf → "sample"`），客户端取首个 value，不按名索引
2. 监听 0.0.0.0 时 mineru 禁用 `*-http-client` 后端与 `server_url`（SSRF 防护）——URL 输入不可用是有意行为
3. 宿主机 Windows 上 8000 端口在保留段内，dev 调试映射用 18000+；compose 网络内部不受影响
