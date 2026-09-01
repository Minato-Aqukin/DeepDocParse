"""探针。healthz=进程存活；readyz=依赖就绪（Redis + 各注册引擎连通）。"""
import asyncio

import httpx
from fastapi import APIRouter, Request, Response

from app.services.engines import is_inprocess

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


async def _probe(http: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await http.get(url, timeout=3.0)
        return "up" if resp.status_code < 500 else f"down ({resp.status_code})"
    except httpx.HTTPError as exc:
        return f"down ({type(exc).__name__})"


# runtime -> 探针路径。**按 runtime 而不是按段名推断**：vlm-ocr 注册在
# parse_engines 段里，但它的 endpoint 是个 OpenAI 兼容的模型运行时，没有 /health。
# 按段名推断会去打一个不存在的路径，把一个健康的容器报成 down —— 而 readyz 恒 503
# 的后果是这个副本永远不接流量。这也是铁律 3 的应有之义：路由层不认识具体引擎，
# 只按注册表声明的 runtime 行事
_RUNTIME_PROBE_PATH = {
    "mineru-api": "/health",
    "vlm-ocr": "/v1/models",     # OpenAI 协议运行时
    "tei": "/health",
}


def _probe_path(entry, fallback: str) -> str:
    """**只在条目显式声明了 runtime 时才查表**，否则用段名的缺省路径。

    不能用 engines.runtime_of()：那是**解析引擎**的助手，留空时默认返回
    "mineru-api" —— 拿它去问 vqa/embedding 条目，会把它们的探针路径
    全改成 /health，把一整排健康的 OpenAI 运行时报成 down。
    段名的缺省值本来就是对的，只有"段名猜不准"的条目（vlm-ocr 挂在
    parse_engines 段却说 OpenAI 协议）才需要 runtime 来纠正。
    """
    runtime = getattr(entry, "runtime", "") or ""
    return _RUNTIME_PROBE_PATH.get(runtime, fallback)


@router.get("/readyz")
async def readyz(request: Request, response: Response):
    state = request.app.state
    checks: dict[str, str] = {}

    try:
        await state.redis.ping()
        checks["redis"] = "up"
    except Exception as exc:  # redis.exceptions 体系庞杂，探针一律兜住
        checks["redis"] = f"down ({type(exc).__name__})"

    # 进程内条目没有远端可探：它的就绪性就等于本进程的就绪性。不特判的话
    # httpx 会对 inproc:// 抛 UnsupportedProtocol（HTTPError 子类），这一项永远 down、
    # /readyz 恒 503 —— 而 models.cpu.yaml 里 born-digital 是唯一的解析引擎，
    # 无 GPU 路径的就绪探针会永远红。四段都走同一判据，别只修用到的那一段
    probes: dict[str, str] = {}
    for prefix, section, fallback in (
        ("engine", state.registry.parse_engines, "/health"),
        ("vqa", state.registry.vqa_models, "/v1/models"),
        ("embed", state.registry.embedding_models, "/health"),
        ("rerank", state.registry.rerank_models, "/health"),
    ):
        for name, entry in section.items():
            if is_inprocess(entry):
                checks[f"{prefix}:{name}"] = "up"
            else:
                probes[f"{prefix}:{name}"] = f"{entry.endpoint}{_probe_path(entry, fallback)}"

    # 同一个 endpoint 只探一次。**vlm-ocr 与 vqa 指向同一个容器**（注册表允许
    # 一个 endpoint 出现在多个段里），不去重就是对同一个模型容器每次探两遍 ——
    # 而模型容器的健康检查往往不便宜
    unique = {url: None for url in probes.values()}
    results = dict(zip(unique, await asyncio.gather(
        *(_probe(state.http, url) for url in unique))))
    checks.update({name: results[url] for name, url in probes.items()})

    ready = all(v == "up" for v in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "degraded", "checks": checks}
