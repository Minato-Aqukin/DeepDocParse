"""ddp_core —— 语料核心逻辑，**只此一份**。

## 为什么存在

重构前，这些逻辑在两个仓库里各有一份**逐字复制品**（`plan.md` §1 体检结论：
600 行承重逻辑靠注释同步，已经静默出错三次 —— 关键词路 AND/OR 语义、
重建索引指错块、抽取平面从不打 `vision_unavailable`）。
分块判据两边漂一点点，同一份版面就会切出不同的块，
而出处的稳定定位键 `seq` 按块序算 —— **历史出处会指到错误的块**。

搬进来之后只剩一份，两侧 import 同一个模块，物理上不可能再漂。

## 为什么是 `ddp_core` 而不是 `app.services`

`deepdocparse-gateway` 与 `deepdocparse-web-backend` **两个发行包都声明了
`packages = ["app"]`** —— Web 装上 gateway 之后两个 `app` 顶层包直接撞车
（实测：Web 的 venv 里 `app` 解析到它自己的 `backend/app/`，
`import app.services.chunking` 报 `ModuleNotFoundError: No module named 'app.services'`）。
所以共享代码单独占一个顶层包名，与两边的 `app` 都不冲突。

## 谁在用

- gateway：`app/services/*` 与 `app/routers/*`
- Web 后端：`backend/app/*`（通过路径依赖装 `deepdocparse-gateway`）
- mcp_server：裁图那份

**往这里加东西之前先想清楚**：它是不是两边都要用？只有一边用的留在各自的 `app/` 里。
"""
