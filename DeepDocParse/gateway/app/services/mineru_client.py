"""mineru-api / mineru-router 客户端（二者接口完全兼容，只是 URL 不同）。

参考：MinerU 3.x 提供异步任务接口 POST /tasks（提交/查状态/取结果）。
实现 M1 时以锁定版本的 mineru-api 实际 OpenAPI 文档为准
（启动 mineru-api 后访问其 /docs 抄参数），并在 tests/test_contract.py 固化断言。
"""
import httpx


class MineruClient:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def submit(self, endpoint: str, file_url: str, options: dict) -> str:
        """提交解析任务，返回 mineru 侧 task_id。

        TODO(M1): 按 mineru-api 实际契约实现。要点：
        - 文件从 file_url 下载后上传，或 mineru 支持 URL 输入则直接透传
        - options 透传（backend=pipeline|vlm、lang 等）
        """
        raise NotImplementedError

    async def status(self, endpoint: str, mineru_task_id: str) -> dict:
        """TODO(M1): GET /tasks/{id} 透传，归一化为契约的 status 枚举。"""
        raise NotImplementedError

    async def fetch_result(self, endpoint: str, mineru_task_id: str) -> dict:
        """TODO(M1): 取回 markdown / layout_json / images。
        layout_json 必须保留（页码+bbox 是 ask_document 裁剪验证和 v2 分块索引的数据源）。
        """
        raise NotImplementedError
