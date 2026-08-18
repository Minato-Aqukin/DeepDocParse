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
    # 无 GPU 路径的就绪探针会永远红。三段都走同一判据，别只修用到的那一段
    probes: dict[str, str] = {}
    for prefix, section, path in (
        ("engine", state.registry.parse_engines, "/health"),      # mineru 的实测契约
        ("vqa", state.registry.vqa_models, "/v1/models"),          # OpenAI 协议
        ("embed", state.registry.embedding_models, "/health"),     # TEI
    ):
        for name, entry in section.items():
            if is_inprocess(entry):
                checks[f"{prefix}:{name}"] = "up"
            else:
                probes[f"{prefix}:{name}"] = f"{entry.endpoint}{path}"
    results = await asyncio.gather(*(_probe(state.http, url) for url in probes.values()))
    checks.update(dict(zip(probes.keys(), results)))

    ready = all(v == "up" for v in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "degraded", "checks": checks}
