"""出处区域截图：按 bbox 从原件里裁一块图。

坐标规则与 mcp_server / Web 层的裁剪完全一致，**这套规则只有一个正确写法**：
比例 = 渲染位图的像素宽 / layout 的 `page_size` 宽。

**不能图省事用 pdfium 报的页尺寸**：遇到 CropBox 偏移或旋转页会裁到错误区域，
产出"带着已验证标记的假出处" —— 这是这个项目最不能接受的一种错误
（同一句警告写在 docs/layout-format.md 的坐标系一节）。

渲染是 CPU 密集的同步代码，调用方一律 `asyncio.to_thread`。
"""
import io

CROP_MARGIN = 12        # bbox 外扩，避免把边缘文字切掉（页面坐标单位）
RENDER_SCALE = 2.0      # 72dpi 基准 x2 = 144dpi，够视觉模型看清小字


def render_crop(pdf_bytes: bytes, page_idx: int, bbox: list,
                page_size: list | None) -> bytes | None:
    """同步渲染并裁剪，返回 PNG 字节。

    失败一律返回 None —— 裁剪是增强路径，不该阻断抽取本身；
    但调用方必须把"裁不出来"打成 crop_failed / crop_unsupported，不许当没发生。
    """
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            if page_idx >= len(doc):
                return None
            page = doc[page_idx]
            img = page.render(scale=RENDER_SCALE).to_pil()
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


def render_page(pdf_bytes: bytes, page_idx: int, scale: float = RENDER_SCALE) -> bytes | None:
    """整页渲染成 PNG —— vlm-ocr 引擎的输入（见 services/vlm_ocr.py）。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            if page_idx >= len(doc):
                return None
            buf = io.BytesIO()
            doc[page_idx].render(scale=scale).to_pil().save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return None


def page_sizes(pdf_bytes: bytes) -> list[tuple[float, float]]:
    """每页的 [宽, 高]（PDF 点）。vlm-ocr 要用它把归一化坐标还原成 bbox。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            return [(doc[i].get_width(), doc[i].get_height()) for i in range(len(doc))]
        finally:
            doc.close()
    except Exception:
        return []
