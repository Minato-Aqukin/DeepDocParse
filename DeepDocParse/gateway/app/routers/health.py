"""探针。healthz=进程存活；readyz=依赖就绪（Redis + 各注册引擎连通）。"""
import asyncio

import httpx
from fastapi import APIRouter, Request, Response

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

    # parse_engines 走 mineru 的 /health（实测契约，见 docs/mineru-api-contract.md）；
    # vqa_models 走 OpenAI 的 /v1/models；embedding_models 走 TEI 的 /health
    probes: dict[str, str] = {}
    for name, entry in state.registry.parse_engines.items():
        probes[f"engine:{name}"] = f"{entry.endpoint}/health"
    for name, entry in state.registry.vqa_models.items():
        probes[f"vqa:{name}"] = f"{entry.endpoint}/v1/models"
    for name, entry in state.registry.embedding_models.items():
        probes[f"embed:{name}"] = f"{entry.endpoint}/health"
    results = await asyncio.gather(*(_probe(state.http, url) for url in probes.values()))
    checks.update(dict(zip(probes.keys(), results)))

    ready = all(v == "up" for v in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "degraded", "checks": checks}
