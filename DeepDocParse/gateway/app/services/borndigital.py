"""born-digital 兜底解析引擎：有文字层的 PDF -> layout_json，不需要 GPU。

**为什么要有它**（B2）：没有 GPU 就完全跑不起来，是本项目最大的单点采用阻塞。
MarkItDown 能成为 MCP 生态下载量第一不是因为能力强（扫描件直接返回空），
而是 pip install 即用、无 GPU —— 这是全类别最被低估的采用变量。

**覆盖**：原生 PDF（论文、报告、合同），即有文字层的那一类。
**不覆盖，且刻意不扩张**：
  - 扫描件 / 无文字层 —— 直接失败并说明原因，不假装解析成功
  - 表格结构、公式 —— 那是 mineru 的本职，重写它是"明确不做"里的一条
  - 版面分析 —— 分栏靠水平投影自然分开（见 _merge_lines），但块之间的阅读序
    只按 (y, x) 排。环绕图文、跨栏标题这类复杂版式会排错，
    这是**已知且写在文档里**的限制，不是 bug（A1 评测集的"中文双栏"切片就是量它的）

它同时是铁律 3「注册表驱动」的第一次真正验证：在此之前注册表里只有 mineru
一个解析引擎，"加引擎 = 加容器 + 一行配置"从未被第二个引擎走通过。

坐标系（**最容易错的地方**）：
  pypdfium2 的 charbox/rect 是 PDF 空间 —— 原点左下、y 向上、不含页面旋转；
  layout_json 的 bbox 与 mineru 对齐 —— 原点左上、y 向下、**含**页面旋转。
  裁剪出处图时按 page_size 换算，一旦这里搞错，用户看到的"出处截图"
  会是页面上另一块区域 —— 带着"已做视觉验证"标记的假出处，本项目最恶劣的错误。
"""
import ctypes
import re
from statistics import median

# 行间距超过行高的这个倍数就断段。1.6 是常见正文行距（1.2~1.5 倍行高）之上、
# 段间距之下的位置；再大会把相邻段落粘成一块，再小会把普通换行切碎
PARAGRAPH_GAP_RATIO = 1.6
# 两行水平投影重叠不足这个比例视为不同栏/不同块，不合并
MIN_HORIZONTAL_OVERLAP = 0.15

_MONO_FONT_PARTS = (
    "courier", "mono", "consolas", "menlo", "monaco", "sourcecode",
    "liberationmono", "dejavusansmono", "inconsolata", "firacode", "jetbrainsmono",
)
_CODE_SYMBOLS = set("{}[]();=<>/\\|&*$#@~`_^:%")
_CODE_TOKEN = re.compile(
    r"(?:\b(?:def|class|function|return|import|from|const|let|var|SELECT|INSERT|UPDATE)\b"
    r"|(?:[A-Za-z_][\w]*\s*=)"
    r"|(?:[A-Za-z_][\w]*\.[A-Za-z_][\w]*)|(?:[A-Za-z_][\w]*\([^)]*\)))")


def _font_name(textpage, char_index: int) -> str:
    """取一个字符的字体名。pypdfium2 尚未包这条 PDFium API，失败就返回空串。"""
    try:
        import pypdfium2.raw as pdfium_c
        flags = ctypes.c_int()
        needed = pdfium_c.FPDFText_GetFontInfo(
            textpage.raw, char_index, None, 0, ctypes.byref(flags))
        if not needed:
            return ""
        buf = ctypes.create_string_buffer(needed)
        pdfium_c.FPDFText_GetFontInfo(
            textpage.raw, char_index, buf, needed, ctypes.byref(flags))
        return buf.value.decode("utf-8", errors="ignore")
    except Exception:  # PDFium 版本/畸形字体不该拖垮整页文字抽取
        return ""


def _char_fonts(textpage) -> list[tuple[tuple[float, float, float, float], str]]:
    out = []
    for i in range(textpage.count_chars()):
        try:
            out.append((textpage.get_charbox(i), _font_name(textpage, i)))
        except Exception:
            continue
    return out


def _fonts_in_rect(char_fonts, rect) -> set[str]:
    left, bottom, right, top = rect
    names = set()
    for box, name in char_fonts:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if left - 0.5 <= cx <= right + 0.5 and bottom - 0.5 <= cy <= top + 0.5 and name:
            names.add(name)
    return names


def _looks_like_code(text: str, fonts: set[str], *, indented: bool) -> bool:
    """等宽字体 + 缩进 + 符号密度三信号，至少两个同意才标 code。"""
    compact = "".join(text.split())
    if len(compact) < 8:
        return False
    mono = any(part in name.lower().replace(" ", "")
               for name in fonts for part in _MONO_FONT_PARTS)
    symbols = sum(ch in _CODE_SYMBOLS for ch in compact) / len(compact) >= 0.12
    syntax = bool(_CODE_TOKEN.search(text))
    return (mono and (indented or symbols or syntax)) or (indented and (symbols or syntax))


def _to_display_bbox(rect: tuple[float, float, float, float],
                     unrotated: tuple[float, float], rotation: int) -> list[float]:
    """PDF 空间矩形 (left, bottom, right, top) -> 显示空间 [x0, y0, x1, y1]（左上原点）。

    rotation 是页面的显示旋转（顺时针度数）。不处理它的话，横排页（rotation=90）
    的 bbox 全部错位 —— 而错位的 bbox 会裁出一张与文本无关的"出处截图"。
    """
    left, bottom, right, top = rect
    w0, h0 = unrotated
    if rotation == 90:
        return [bottom, left, top, right]
    if rotation == 180:
        return [w0 - right, bottom, w0 - left, top]
    if rotation == 270:
        return [h0 - top, w0 - right, h0 - bottom, w0 - left]
    return [left, h0 - top, right, h0 - bottom]


def _lines_of_page(page) -> tuple[list[dict], list[float]]:
    """抽出一页的行：[{bbox(显示空间), text}]，以及该页的 page_size。"""
    textpage = page.get_textpage()
    try:
        rotation = (page.get_rotation() or 0) % 360
        # get_size() 已经考虑旋转（就是渲染出来的尺寸），page_size 用它；
        # 而 charbox/rect 在未旋转空间里，换算要用未旋转尺寸
        page_size = [float(v) for v in page.get_size()]
        cropbox = page.get_cropbox() or (0.0, 0.0, *page.get_size())
        unrotated = (cropbox[2] - cropbox[0], cropbox[3] - cropbox[1])

        lines: list[dict] = []
        char_fonts = _char_fonts(textpage)
        for i in range(textpage.count_rects()):
            rect = textpage.get_rect(i)
            raw_text = textpage.get_text_bounded(*rect) or ""
            indented = any(line.startswith(("\t", "  ")) for line in raw_text.splitlines())
            text = raw_text.strip()
            if not text:
                continue
            fonts = _fonts_in_rect(char_fonts, rect)
            lines.append({
                "bbox": _to_display_bbox(rect, unrotated, rotation),
                "text": text,
                "type": "code" if _looks_like_code(text, fonts, indented=indented) else "text",
            })
        return lines, page_size
    finally:
        textpage.close()


def _horizontal_overlap(a: list[float], b: list[float]) -> float:
    """两个 bbox 的水平投影重叠占较窄者的比例。"""
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    narrower = min(a[2] - a[0], b[2] - b[0])
    return overlap / narrower if narrower > 0 else 0.0


def _merge_lines(lines: list[dict]) -> list[dict]:
    """行 -> 段。

    **不是"和上一行比"那么简单**：按 y 排序后，左右两栏的行会交替出现
    （左1、右1、左2、右2…），只跟前一行比较的话每一栏的段落都会被对方切碎。
    所以维护一组"还开着的块"，每来一行找竖直最接近、且水平投影重叠的那个块续上。

    **水平重叠必须拿块里最后一行来比，不能拿块的并集 bbox。** 用并集的话：
    一个整页宽的标题先并进左栏第一行，块的并集就变成整页宽，此后左右两栏的每一行
    都与它"重叠"，于是整页塌成一个块 —— 文本左右交错，bbox 退化成整片版心，
    "bbox 级出处"这个卖点在双栏文档上直接失效。（这是 2026-08-18 验收抓到的真 bug，
    回归用例：test_borndigital_keeps_two_columns_apart_under_a_full_width_title）

    仍然不做的是版面分析：块之间的阅读序只按 (y, x) 排，跨栏标题会被并进它下面
    那一栏的第一段。已知限制，见 docs/layout-format.md。
    """
    if not lines:
        return []
    lines = sorted(lines, key=lambda ln: (round(ln["bbox"][1], 1), ln["bbox"][0]))
    heights = [ln["bbox"][3] - ln["bbox"][1] for ln in lines]
    typical = median(heights) if heights else 12.0
    max_gap = typical * PARAGRAPH_GAP_RATIO

    done: list[dict] = []
    open_blocks: list[dict] = []
    for line in lines:
        box = line["bbox"]
        # 行已按 y 排序：底边离当前行超过 max_gap 的块再也接不上任何后续行，
        # 直接退休。既是正确性（不会被远处的块吸走），也让复杂度回到线性
        still_open = []
        for block in open_blocks:
            (still_open if box[1] - block["last"][3] <= max_gap else done).append(block)
        open_blocks = still_open

        best, best_gap = None, None
        for block in open_blocks:
            gap = box[1] - block["last"][3]
            if block["type"] != line.get("type", "text"):
                continue
            # 拿最后一行比，不是并集 —— 见 docstring
            if _horizontal_overlap(block["last"], box) < MIN_HORIZONTAL_OVERLAP:
                continue        # 不同栏 / 不同块
            if best_gap is None or abs(gap) < abs(best_gap):
                best, best_gap = block, gap
        if best is None:
            open_blocks.append({"bbox": list(box), "last": list(box),
                                "texts": [line["text"]],
                                "type": line.get("type", "text")})
            continue
        union = best["bbox"]
        best["bbox"] = [min(union[0], box[0]), min(union[1], box[1]),
                        max(union[2], box[2]), max(union[3], box[3])]
        best["last"] = list(box)
        best["texts"].append(line["text"])

    blocks = done + open_blocks
    blocks.sort(key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    return [{"bbox": b["bbox"], "text": "\n".join(b["texts"]), "type": b["type"]}
            for b in blocks]


def extract_pages(pdf_bytes: bytes) -> list[dict]:
    """PDF 字节 -> [{page_idx, page_size, blocks}]。**同步**，调用方丢线程池。

    没有任何一页有文字时返回空列表 —— 调用方据此报"这份文档没有文字层"，
    而不是产出一份空版面让下游以为解析成功了。
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_bytes)
    try:
        pages = []
        for page_idx in range(len(document)):
            page = document[page_idx]
            lines, page_size = _lines_of_page(page)
            pages.append({"page_idx": page_idx, "page_size": page_size,
                          "blocks": _merge_lines(lines)})
        return pages if any(page["blocks"] for page in pages) else []
    finally:
        document.close()


def to_markdown(pages: list[dict]) -> str:
    """段落之间空行，页之间加分隔线。

    刻意不猜标题层级：born-digital 只看得到字号与坐标，猜错了会把正文变成 H1，
    比不猜更糟。要标题结构就用 mineru。
    """
    parts: list[str] = []
    for page in pages:
        if parts:
            parts.append("\n---\n")
        for block in page["blocks"]:
            parts.append(block["text"].replace("\n", " ").strip())
    return "\n\n".join(p for p in parts if p) + "\n"
