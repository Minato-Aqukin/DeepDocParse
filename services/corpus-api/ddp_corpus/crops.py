"""出处区域截图的**对象存储缓存层**。

真正的坐标换算与渲染在 `ddp_core.crops` —— 那套规则曾经有三份复制品
（gateway / 本层 / mcp_server），靠注释互相叮嘱"只有一个正确写法"，
而写错的后果是裁出一张与文本无关的图并带着"已验证"标记。现在只剩一份。

本模块只管这里独有的那件事：**裁剪很贵**（渲染整页再切），
所以算过一次就存进对象存储，键里带 bbox 摘要。
"""
import asyncio
import hashlib
import json

from ddp_corpus.storage import Storage, crop_key
from ddp_core.crops import CROP_MARGIN, RENDER_SCALE, render_crop, render_crops  # noqa: F401

__all__ = [
    "CROP_MARGIN", "RENDER_SCALE", "bbox_digest", "get_or_create_crop",
    "get_or_create_crops", "render_crop",
]


def bbox_digest(bbox: list) -> str:
    return hashlib.sha1(json.dumps(bbox, sort_keys=True).encode()).hexdigest()[:12]




async def get_or_create_crop(storage: Storage, *, job_id: str, source_key: str, mime: str,
                             page_idx: int, bbox: list | None,
                             page_size: list | None) -> str | None:
    """返回对象键；不支持裁剪（非 PDF / 无 bbox / 渲染失败）时返回 None。"""
    if not bbox or not source_key:
        return None
    if "pdf" not in (mime or "").lower():
        return None            # 只有 PDF 能按坐标裁；图片类文档直接用原图更合适

    key = crop_key(job_id, page_idx, bbox_digest(bbox))
    if await storage.exists(key):
        return key
    try:
        pdf_bytes = await storage.get(source_key)
    except Exception:
        return None
    png = await asyncio.to_thread(render_crop, pdf_bytes, page_idx, bbox, page_size)
    if png is None:
        return None
    await storage.put(key, png, "image/png")
    return key


async def get_or_create_crops(storage: Storage, *, job_id: str, source_key: str, mime: str,
                              atoms: list[dict]) -> dict[int, str]:
    """编译期批量裁图：PDF 只读一次，每页只渲染一次。"""
    if not source_key or "pdf" not in (mime or "").lower():
        return {}
    usable = [atom for atom in atoms if atom.get("bbox") and atom.get("page_size")]
    if not usable:
        return {}

    found: dict[int, str] = {}
    missing: list[tuple[dict, str]] = []
    for atom in usable:
        key = crop_key(job_id, atom["page_idx"], bbox_digest(atom["bbox"]))
        if await storage.exists(key):
            found[atom["seq"]] = key
        else:
            missing.append((atom, key))
    if not missing:
        return found

    try:
        pdf_bytes = await storage.get(source_key)
    except Exception:
        return found
    requests = [(atom["page_idx"], atom["bbox"], atom["page_size"])
                for atom, _ in missing]
    rendered = await asyncio.to_thread(render_crops, pdf_bytes, requests)
    for (atom, key), png in zip(missing, rendered):
        if png is None:
            continue
        await storage.put(key, png, "image/png")
        found[atom["seq"]] = key
    return found
