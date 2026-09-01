"""mineru-api / mineru-router 客户端（二者接口完全兼容，只是 URL 不同）。

按 mineru 3.4.4 实测契约实现（见 docs/mineru-api-contract.md）：
  POST /tasks                  multipart 提交，202 返回 {task_id, status, ...}
  GET  /tasks/{id}             状态：pending|processing|completed|failed
  GET  /tasks/{id}/result      未完成 202 / 失败 409 / 成功 200 返回 results

mineru 只收 multipart 文件上传（不支持 URL 输入），因此 submit 先从
file_url 下载再转传 —— 文件流只过内存，不落盘（无状态原则）。
"""
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx

# mineru 状态 -> openapi.yaml 契约四态
_STATUS_MAP = {
    "pending": "pending",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
}

# 后处理链需要的输出：markdown + 版面 JSON（页码/bbox）+ 图片
_RESULT_FIELDS = {
    "return_md": "true",
    "return_middle_json": "true",
    "return_content_list": "true",
    "return_images": "true",
}


class MineruTaskNotFound(Exception):
    """mineru 侧任务不存在（已被清理或 id 无效）。"""


class MineruClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def submit(self, endpoint: str, file_url: str, options: dict) -> str:
        """提交解析任务，返回 mineru 侧 task_id。"""
        file_resp = await self._http.get(file_url, follow_redirects=True)
        file_resp.raise_for_status()
        filename = PurePosixPath(urlparse(file_url).path).name or "document.pdf"
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        data: dict = dict(_RESULT_FIELDS)
        for key, value in options.items():
            data[key] = str(value).lower() if isinstance(value, bool) else str(value)

        resp = await self._http.post(
            f"{endpoint}/tasks",
            files=[("files", (filename, file_resp.content, mime))],
            data=data,
        )
        resp.raise_for_status()
        return resp.json()["task_id"]

    async def status(self, endpoint: str, mineru_task_id: str) -> dict:
        """GET /tasks/{id}，归一化为契约的 status 枚举。"""
        resp = await self._http.get(f"{endpoint}/tasks/{mineru_task_id}")
        if resp.status_code == 404:
            raise MineruTaskNotFound(mineru_task_id)
        resp.raise_for_status()
        payload = resp.json()
        return {
            "status": _STATUS_MAP.get(payload.get("status"), "running"),
            "error": payload.get("error"),
        }

    async def fetch_result(self, endpoint: str, mineru_task_id: str) -> dict | None:
        """取回结果并整形为契约形态 {markdown, layout_json, images}。

        未就绪（202）返回 None；失败（409）抛 RuntimeError。
        layout_json 保留 mineru middle_json 原文（页码+bbox 是 ask_document
        裁剪验证和 v2 分块索引的数据源）。
        """
        import json as _json

        resp = await self._http.get(f"{endpoint}/tasks/{mineru_task_id}/result")
        if resp.status_code == 202:
            return None
        if resp.status_code == 404:
            raise MineruTaskNotFound(mineru_task_id)
        if resp.status_code == 409:
            payload = resp.json()
            raise RuntimeError(payload.get("error") or "mineru task failed")
        resp.raise_for_status()

        payload = resp.json()
        # results: {filename: {md_content, middle_json, content_list, images:{name: dataURI}}}
        first = next(iter(payload.get("results", {}).values()), {})
        middle = first.get("middle_json")
        return {
            "markdown": first.get("md_content") or "",
            "layout_json": _json.loads(middle) if isinstance(middle, str) and middle else (middle or {}),
            "images": [
                {"name": name, "url": data_uri}
                for name, data_uri in (first.get("images") or {}).items()
            ],
        }
