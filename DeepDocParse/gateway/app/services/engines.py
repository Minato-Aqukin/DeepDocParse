"""解析引擎适配层 —— 注册表驱动的落点。

一直以来 `/v1/parse` 是直接调 MineruClient 的：传输层号称引擎无关，
代码里却写死了一个引擎。这一层把"引擎"变成真正可替换的东西：

    ParseEngine 协议 = submit -> status -> fetch_result
    fetch_result 必须返回**归一化后**的结果（layout.py），不是引擎原生格式

选哪个适配器由注册表的 `runtime` 决定（models.yaml），缺省按段名推断，
所以老的 models.yaml 不用改也照跑（向后兼容）。

现有三个实现，覆盖三种形态：
  mineru-api    外部任务型：提交后轮询远端状态（mineru-api / mineru-router）
  borndigital   进程内同步型：无外部依赖、无 GPU，靠 pypdfium2 取文字层
  vlm-ocr       模型调用型：整页渲染成图交给视觉语言模型识别（v1.1）

第二个引擎的存在本身就是验收：铁律 3「加引擎 = 加容器 + 一行配置」
在此之前从来没有被第二个引擎走通过。第三个引擎是复验：
**vlm-ocr 没有改动这一层的任何既有代码，只加了一个分支和一个 normalizer** ——
接缝留对了的话，加引擎就应该是这个成本。
"""
import asyncio
from typing import Protocol

import httpx

from app.services import borndigital, layout, vlm_ocr
from app.services.mineru_client import MineruClient

# born-digital 下载 PDF 的字节上限。**必须有**：它跑在 worker 进程内，
# 整份文件要进内存，没有上限时一份超大文件就能把 worker 打爆 ——
# 而它没有容器隔离（那是"零依赖启动"换来的代价，见 docs/layout-format.md）
BORNDIGITAL_MAX_BYTES = 200 * 1024 * 1024

MINERU_RUNTIME = "mineru-api"
BORNDIGITAL_RUNTIME = "borndigital"
VLM_OCR_RUNTIME = "vlm-ocr"

# 进程内引擎的 endpoint 前缀。它不是一个可以打 HTTP 的地址 —— 探针、健康检查
# 这类"挨个去连"的代码必须先认出它，否则会把"没有远端"当成"远端挂了"
INPROC_SCHEME = "inproc://"


def is_inprocess(entry) -> bool:
    """这个条目是不是进程内引擎（没有远端可探）。

    **vlm-ocr 不算**：它虽然也跑在 worker 进程里，但 endpoint 指向一个真实的
    模型容器，探针该去连它。把它算进来会让"视觉模型挂了"这件事在 /readyz 上看不见。
    """
    return (getattr(entry, "runtime", "") == BORNDIGITAL_RUNTIME
            or str(getattr(entry, "endpoint", "")).startswith(INPROC_SCHEME))


class ParseEngine(Protocol):
    async def submit(self, endpoint: str, file_url: str, options: dict) -> str: ...

    async def status(self, endpoint: str, native_id: str) -> dict: ...

    async def fetch_result(self, endpoint: str, native_id: str) -> dict | None: ...


class MineruEngine:
    """外部任务型：包一层 MineruClient，并把结果过一遍 normalizer。"""

    def __init__(self, client: MineruClient):
        self._client = client

    async def submit(self, endpoint: str, file_url: str, options: dict) -> str:
        return await self._client.submit(endpoint, file_url, options)

    async def status(self, endpoint: str, native_id: str) -> dict:
        return await self._client.status(endpoint, native_id)

    async def fetch_result(self, endpoint: str, native_id: str) -> dict | None:
        result = await self._client.fetch_result(endpoint, native_id)
        if result is None:
            return None
        result["layout_json"] = layout.from_mineru(result.get("layout_json"), engine="mineru")
        return result


class BornDigitalEngine:
    """进程内同步型：没有远端任务，也没有 GPU。

    `submit` 不做事，只把 file_url 当作 native_id 带下去 —— 真正的抽取在
    `fetch_result` 里做，而那是在 **worker 进程**里跑的（poll_and_archive）。
    这样 API 进程不会被一份 200 页的 PDF 卡住，也不需要在两个进程之间
    共享任何中间状态（无状态原则）。

    抽取本身是 CPU 密集的同步代码，丢线程池，别阻塞事件循环。
    """

    def __init__(self, http: httpx.AsyncClient):
        self._http = http

    async def submit(self, endpoint: str, file_url: str, options: dict) -> str:
        return file_url

    async def status(self, endpoint: str, native_id: str) -> dict:
        # 没有排队也没有远端：受理即"可以取结果了"。
        # 路由层会把 succeeded 对外报成 running 直到 worker 真的归档完
        return {"status": "succeeded", "error": None}

    async def fetch_result(self, endpoint: str, native_id: str) -> dict | None:
        pdf_bytes = await _download(self._http, native_id)
        if not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
            raise RuntimeError(
                "borndigital 引擎只处理 PDF；这份文件不是 PDF，请改用 mineru")

        pages = await asyncio.to_thread(borndigital.extract_pages, pdf_bytes)
        if not pages:
            # 扫描件走到这里。**不返回空版面**：那会让下游以为解析成功了，
            # 只是这份文档"恰好没有内容"——正是这个项目最忌讳的静默降级
            raise RuntimeError(
                "borndigital 引擎没有从这份 PDF 里提取到任何文字层"
                "（多半是扫描件/纯图片）。扫描件需要 OCR，请改用 mineru 引擎")

        return {
            "markdown": borndigital.to_markdown(pages),
            "layout_json": layout.build(pages, engine="borndigital"),
            "images": [],       # 不抽图：born-digital 的定位是文字层兜底，不是全功能解析
        }



class VlmOcrEngine:
    """模型调用型：整页渲染成图 -> 视觉语言模型 -> DDP-Layout。

    与 BornDigitalEngine 同构（进程内、无远端任务），差别只在 fetch_result 里
    做的事：那边是抽文字层，这边是打模型。因此下载 PDF 的那套上限逻辑照抄
    —— 它同样跑在 worker 进程内，同样没有容器隔离。

    endpoint 指向的就是 VQA 那个容器：**不用为解析再起一个**，
    注册表里同一个 endpoint 可以既是 vqa_models 也是 parse_engines 的条目。
    """

    def __init__(self, http: httpx.AsyncClient, entry):
        self._http = http
        self._entry = entry

    async def submit(self, endpoint: str, file_url: str, options: dict) -> str:
        return file_url

    async def status(self, endpoint: str, native_id: str) -> dict:
        # 没有排队也没有远端：受理即"可以取结果了"（与 borndigital 同）
        return {"status": "succeeded", "error": None}

    async def fetch_result(self, endpoint: str, native_id: str) -> dict | None:
        pdf_bytes = await _download(self._http, native_id)
        if not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
            raise RuntimeError("vlm-ocr 引擎目前只处理 PDF；这份文件不是 PDF")

        options = dict(self._entry.options)
        model = self._entry.adapter or options.pop("model", "") or ""
        if not model:
            raise RuntimeError(
                "vlm-ocr 引擎必须在 models.yaml 里指定 options.model（视觉模型名）"
                " —— 缺它时运行时会自己挑一个，识别质量无从复现")
        layout_json = await vlm_ocr.recognize(
            self._http, endpoint=endpoint, model=model,
            pdf_bytes=pdf_bytes, options=options)
        return {
            "markdown": vlm_ocr.to_markdown(layout_json),
            "layout_json": layout_json,
            "images": [],       # 模型识别的是文字，不抽图
        }


async def _download(http: httpx.AsyncClient, url: str) -> bytes:
    """边下边计字节数，超限立刻中断 —— 不等整个文件落进内存再判断。

    borndigital 与 vlm-ocr 共用：两者都跑在 worker 进程内、都要整份文件进内存，
    上限的理由一模一样，没必要各写一份（各写一份的下场是改了一处忘了另一处）。
    """
    chunks: list[bytes] = []
    total = 0
    async with http.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > BORNDIGITAL_MAX_BYTES:
                raise RuntimeError(
                    f"文件超过进程内引擎的处理上限"
                    f"（{BORNDIGITAL_MAX_BYTES // 1024 // 1024}MB）。"
                    f"它跑在 worker 进程内、整份文件进内存，请改用 mineru 引擎")
            chunks.append(chunk)
    return b"".join(chunks)


def runtime_of(entry) -> str:
    """注册表条目用哪个 runtime。没写就按"解析引擎默认是 mineru 任务协议"推断。"""
    return getattr(entry, "runtime", "") or MINERU_RUNTIME


def resolve(entry, *, mineru_client: MineruClient, http: httpx.AsyncClient) -> ParseEngine:
    runtime = runtime_of(entry)
    if runtime == BORNDIGITAL_RUNTIME:
        return BornDigitalEngine(http)
    if runtime == VLM_OCR_RUNTIME:
        return VlmOcrEngine(http, entry)
    if runtime == MINERU_RUNTIME:
        return MineruEngine(mineru_client)
    raise LookupError(f"unknown parse runtime: {runtime}")
