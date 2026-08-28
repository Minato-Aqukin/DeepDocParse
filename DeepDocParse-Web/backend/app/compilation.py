"""DDP-Compile v1 的产品层执行器：裁图、VLM 理解与可见降级。"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass

import httpx

from app.config import settings
from app.crops import get_or_create_crops
from app.models import Document, ParseJob
from app.storage import Storage
from app.upstream import chat_request
from ddp_core.compilation import VISUAL_KINDS, code_detection_of, compile_chunks, provider_of

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

VISION_PROMPT = """理解这一个文档视觉原子，只输出 JSON：
{"description":"可检索的客观描述","elements":["关键要素"]}
要求：
- 不补充图中没有的事实；保留数字、单位、变量名和趋势方向
- 代码说明接口/标识符，公式说明变量关系，表格说明行列语义，图表说明轴、图例和趋势
- 看不清就把 description 留空，不要猜测
"""


@dataclass
class CompileOutput:
    chunks: list[dict]
    crop_keys: dict[int, str]
    degraded: list[str]
    provider: dict
    vision_requests: int


def _description(raw: str) -> str | None:
    raw = raw.strip()
    match = _FENCE.search(raw)
    candidate = match.group(1) if match else raw
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    description = str(value.get("description") or "").strip()
    elements = [str(v).strip() for v in (value.get("elements") or [])
                if str(v).strip()] if isinstance(value.get("elements") or [], list) else []
    if not description:
        return None
    return "\n".join([description, *(f"要素：{item}" for item in elements)])


async def _understand(http: httpx.AsyncClient, png: bytes, kind: str,
                      source_text: str) -> tuple[str | None, str | None]:
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    prompt = f"原子类型：{kind}\n已有 OCR/图注：{source_text or '（无）'}\n\n{VISION_PROMPT}"
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "text", "text": prompt},
    ]}]
    try:
        response = await asyncio.wait_for(
            http.send(chat_request(http, messages, stream=False)),
            timeout=settings.compile_vision_timeout)
        if response.status_code != 200:
            return None, "vision_unavailable"
        raw = response.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return None, "vision_unavailable"
    parsed = _description(raw)
    return (parsed, None) if parsed else (None, "vision_invalid_output")


async def compile_document(*, storage: Storage, http: httpx.AsyncClient,
                           document: Document, job: ParseJob, layout: dict) -> CompileOutput:
    provider = provider_of(
        layout=layout, parse_options_hash=job.options_hash,
        embedding_model=settings.embedding_model, vision_model=settings.chat_model)
    base = compile_chunks(layout, max_chars=settings.chunk_max_chars, provider=provider)
    degraded: set[str] = set()
    crop_keys: dict[int, str] = {}
    vision_requests = 0

    if code_detection_of(layout) == "unavailable":
        degraded.add("code_detection_unavailable")
    if not provider["provider_resolved"]:
        degraded.add("provider_unresolved")

    crop_supported = "pdf" in (document.mime or "").lower() and bool(document.object_key)
    crop_keys = await get_or_create_crops(
        storage, job_id=job.id, source_key=document.object_key, mime=document.mime,
        atoms=base) if crop_supported else {}
    for chunk in base:
        if not chunk.get("bbox") or not chunk.get("page_size") or not crop_supported:
            degraded.add("crop_unsupported")
        elif chunk["seq"] not in crop_keys:
            degraded.add("crop_failed")

    visual = [c for c in base if c["block_type"] in VISUAL_KINDS]
    descriptions: dict[int, str] = {}
    if visual and not settings.compile_vision_enabled:
        degraded.add("vision_unavailable")
    elif visual:
        semaphore = asyncio.Semaphore(max(1, settings.compile_vision_concurrency))

        async def one(chunk: dict) -> tuple[int, str | None, str | None, int]:
            key = crop_keys.get(chunk["seq"])
            if not key:
                return chunk["seq"], None, None, 0
            async with semaphore:
                try:
                    png = await storage.get(key)
                except Exception:
                    return chunk["seq"], None, "vision_unavailable", 0
                return (chunk["seq"], *await _understand(
                    http, png, chunk["block_type"], chunk["text"]), 1)

        for seq, description, reason, requested in await asyncio.gather(*(one(c) for c in visual)):
            vision_requests += requested
            if description:
                descriptions[seq] = description
            if reason:
                degraded.add(reason)

    chunks = compile_chunks(layout, max_chars=settings.chunk_max_chars, provider=provider,
                            descriptions=descriptions)
    return CompileOutput(chunks=chunks, crop_keys=crop_keys,
                         degraded=sorted(degraded), provider=provider,
                         vision_requests=vision_requests)
