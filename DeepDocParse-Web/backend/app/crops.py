"""出处区域截图：按 bbox 从原件里裁一块图，供 VQA 验证与前端展示。

坐标换算沿用 service 侧 mcp_server 的同一套规则：bbox 基于 layout 的 page_size，
渲染出来的位图是像素，比例 = 像素宽 / page_size 宽。**不能**图省事用 pdfium 的页尺寸——
遇到 CropBox 偏移或旋转页会裁到错误区域（service 侧为此专门把 page_size 存进了 chunk）。

裁剪很贵（渲染整页再切），所以算过一次就存进对象存储，键里带 bbox 摘要。
"""
import asyncio
import hashlib
import io
import json

from app.storage import Storage, crop_key

CROP_MARGIN = 12        # bbox 外扩，避免把边缘文字切掉
RENDER_SCALE = 2.0      # 72dpi 基准 x2 = 144dpi，够 VQA 看清小字


def bbox_digest(bbox: list) -> str:
    return hashlib.sha1(json.dumps(bbox, sort_keys=True).encode()).hexdigest()[:12]


def render_crop(pdf_bytes: bytes, page_idx: int, bbox: list,
                page_size: list | None) -> bytes | None:
    """同步渲染（调用方丢线程池）。失败返回 None —— 裁剪是增强路径，不阻断问答。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            if page_idx >= len(doc):
                return None
            page = doc[page_idx]
            bitmap = page.render(scale=RENDER_SCALE)
            img = bitmap.to_pil()
            sx = img.width / (page_size[0] if page_size else page.get_width())
            sy = img.height / (page_size[1] if page_size else page.get_height())
            x0, y0, x1, y1 = bbox
            box = (max(0, int((x0 - CROP_MARGIN) * sx)), max(0, int((y0 - CROP_MARGIN) * sy)),
                   min(img.width, int((x1 + CROP_MARGIN) * sx)),
                   min(img.height, int((y1 + CROP_MARGIN) * sy)))
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            buf = io.BytesIO()
            img.crop(box).save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return None


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
