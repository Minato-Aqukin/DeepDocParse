"""born-digital 兜底解析引擎：有文字层的 PDF -> layout_json，不需要 GPU。

**覆盖**：原生 PDF（论文、报告、合同），即有文字层的那一类。
**当前 CPU profile 的限制**：
  - 扫描件 / 无文字层 —— 直接失败并说明原因，不假装解析成功
  - 不恢复表格单元格结构或公式语义；这些需要另选具备对应能力的引擎
  - 版面分析 —— 分栏靠水平投影自然分开（见 _merge_lines），但块之间的阅读序
    只按 (y, x) 排。环绕图文、跨栏标题这类复杂版式会排错，
    这是**已知且写在文档里**的限制，不是 bug（A1 评测集的"中文双栏"切片就是量它的）

本地 Provider 与服务器注册表都调用此实现，不能各自维护解析规则。

坐标系（**最容易错的地方**）：
  pypdfium2 的 charbox/rect 是 PDF 空间 —— 原点左下、y 向上、不含页面旋转；
  layout_json 的 bbox 与 mineru 对齐 —— 原点左上、y 向下、**含**页面旋转。
  裁剪出处图时按 page_size 换算，一旦这里搞错，用户看到的"出处截图"
  会是页面上另一块区域 —— 带着"已做视觉验证"标记的假出处，本项目最恶劣的错误。
"""

import ctypes
import re
from collections.abc import Callable
from statistics import median

from ddp_core.crops import _pdfium_serialized

# 行间距超过行高的这个倍数就断段。1.6 是常见正文行距（1.2~1.5 倍行高）之上、
# 段间距之下的位置；再大会把相邻段落粘成一块，再小会把普通换行切碎
PARAGRAPH_GAP_RATIO = 1.6
# 两行水平投影重叠不足这个比例视为不同栏/不同块，不合并
MIN_HORIZONTAL_OVERLAP = 0.15

_MONO_FONT_PARTS = (
    "courier",
    "mono",
    "consolas",
    "menlo",
    "monaco",
    "sourcecode",
    "liberationmono",
    "dejavusansmono",
    "inconsolata",
    "firacode",
    "jetbrainsmono",
)
_CODE_SYMBOLS = set("{}[]();=<>/\\|&*$#@~`_^:%")
_CODE_TOKEN = re.compile(
    r"(?:\b(?:def|class|function|return|import|from|const|let|var|SELECT|INSERT|UPDATE)\b"
    r"|(?:[A-Za-z_][\w]*\s*=)"
    r"|(?:[A-Za-z_][\w]*\.[A-Za-z_][\w]*)|(?:[A-Za-z_][\w]*\([^)]*\)))"
)


def _font_name(textpage, char_index: int) -> str:
    """取一个字符的字体名。pypdfium2 尚未包这条 PDFium API，失败就返回空串。"""
    try:
        import pypdfium2.raw as pdfium_c

        flags = ctypes.c_int()
        needed = pdfium_c.FPDFText_GetFontInfo(
            textpage.raw, char_index, None, 0, ctypes.byref(flags)
        )
        if not needed:
            return ""
        buf = ctypes.create_string_buffer(needed)
        pdfium_c.FPDFText_GetFontInfo(textpage.raw, char_index, buf, needed, ctypes.byref(flags))
        return buf.value.decode("utf-8", errors="ignore")
    except Exception:  # PDFium 版本/畸形字体不该拖垮整页文字抽取
        return ""


def _char_runs(textpage) -> list[dict]:
    """一页的字符流：[{box(PDF 空间), text, font}]，按 PDFium 原生字符序。

    一次遍历同时拿文本、字符盒与字体名：旧实现先全页扫一遍盒与字体，
    再对每个 rect 做一次全页矩形包含判定（字符数 × 矩形数），大页上
    就是上千字符 × 上百矩形的二次扫描。这里字符只读一次，
    字体信息跟着字符走，后续分组不再做任何全页回查。

    换行符（\\r/\\n）只做分行标记，不进文本、不参与 bbox：
    它们的字符盒是退化的零宽点，纳进来会把行高/行距算坏。
    """
    runs: list[dict] = []
    for i in range(textpage.count_chars()):
        try:
            text = textpage.get_text_range(i, 1) or ""
        except Exception:
            continue
        if text in ("\r", "\n", "\x00"):
            runs.append({"break": True})
            continue
        if text in (" ", "\xa0"):
            # 词间空格：留占位给词间断行用，但不取盒、不取字体
            # （空格盒多为零宽点，且字体对 code 判定没有信息量）。
            # 制表符不走这里：它是 code 缩进信号，要进文本。
            runs.append({"break": False, "space": True})
            continue
        if not text.strip():
            runs.append({"break": False, "box": None, "text": text,
                         "font": _font_name(textpage, i)})
            continue
        try:
            box = textpage.get_charbox(i)
        except Exception:
            continue
        runs.append({"break": False, "box": box, "text": text,
                     "font": _font_name(textpage, i)})
    return runs


def _char_fonts(textpage) -> list[tuple[tuple[float, float, float, float], str]]:
    out = []
    for run in _char_runs(textpage):
        if run.get("break") or run.get("space"):
            continue
        out.append((run["box"], run["font"]))
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
    mono = any(part in name.lower().replace(" ", "") for name in fonts for part in _MONO_FONT_PARTS)
    symbols = sum(ch in _CODE_SYMBOLS for ch in compact) / len(compact) >= 0.12
    syntax = bool(_CODE_TOKEN.search(text))
    return (mono and (indented or symbols or syntax)) or (indented and (symbols or syntax))


def _to_display_bbox(
    rect: tuple[float, float, float, float], unrotated: tuple[float, float], rotation: int
) -> list[float]:
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


def _lines_of_page(page) -> tuple[list[dict], list[float], Callable[[list[float]], list[float]]]:
    """抽出一页的行：[{bbox(阅读坐标系), text}]、该页的 page_size，以及
    阅读坐标系 -> 显示空间的换算函数。

    **行缝合与分段必须在文字自己的阅读坐标系（未旋转、左上原点）里做。**
    旋转页在显示空间里每一行都是一根竖条，并排的两行看起来就像"同一行的左右
    两个碎片"：旧实现在显示空间缝合，把 /Rotate 90 页上的两行倒序拼成一行、
    行间连空格都没有（2026-09-24 版面探针实测）。bbox 最后再换算到显示空间。
    """
    textpage = page.get_textpage()
    try:
        rotation = (page.get_rotation() or 0) % 360
        # get_size() 已经考虑旋转（就是渲染出来的尺寸），page_size 用它；
        # 而 charbox/rect 在未旋转空间里，换算要用未旋转尺寸
        page_size = [float(v) for v in page.get_size()]
        # get_cropbox() does not inherit page-tree boxes and can return a
        # synthetic Letter box for an A4 page. PDFium's effective bounding box
        # resolves inheritance and intersects MediaBox/CropBox before rotation.
        cropbox = page.get_bbox()
        if cropbox is None or cropbox[0] >= cropbox[2] or cropbox[1] >= cropbox[3]:
            raise ValueError("PDF page has no visible bounding box")
        unrotated = (cropbox[2] - cropbox[0], cropbox[3] - cropbox[1])

        # 行基元 = 字符盒的几何聚类，不是 PDFium 的 count_rects()/get_rect()。
        # rect（字体矩形）会横跨多行、叠住混排的异体字片段：
        # 以它为"行"再做 bounded 重提，文本丢序、bbox 退化成整片版心
        # （ESP32 index25 的 CPU 主频句、Attention index4 的 h=8 与
        # dk=dv=dmodel/h=64 就是这么丢的）。字符只读一次（_char_runs），
        # 这里只做线性分组，不再有字符数 × 矩形数的二次扫描。
        # 内联字形/数学符号按字符原文保留，不做公式结构识别。
        runs = _char_runs(textpage)
        lines: list[dict] = []
        pending_space = False
        current: dict | None = None

        def _visible(box) -> dict | None:
            left = max(box[0], cropbox[0])
            bottom = max(box[1], cropbox[1])
            right = min(box[2], cropbox[2])
            top = min(box[3], cropbox[3])
            if left >= right or bottom >= top:
                return None
            return {"box": (left, bottom, right, top)}

        def _emit(box, parts, fonts) -> None:
            raw_text = "".join(parts)
            # PDFium can collapse several leading spaces into one character.
            indented = raw_text.startswith(("\t", " "))
            text = raw_text.strip()
            if text and box is not None:
                lines.append(
                    {
                        "bbox": _to_display_bbox(
                            (
                                box[0] - cropbox[0],
                                box[1] - cropbox[1],
                                box[2] - cropbox[0],
                                box[3] - cropbox[1],
                            ),
                            unrotated,
                            0,
                        ),
                        "text": text,
                        "type": (
                            "code"
                            if _looks_like_code(text, fonts,
                                                indented=indented)
                            else "text"
                        ),
                    }
                )

        def _flush() -> None:
            nonlocal current, pending_space
            if current is None:
                pending_space = False
                return
            _emit(current["box"], current["parts"], current["fonts"])
            current = None
            pending_space = False

        for run in runs:
            if run.get("break"):
                _flush()
                continue
            if run.get("space"):
                # 行首空格保留到 code 判定；词间空格不参与几何盒。
                if current is None:
                    current = {"box": None, "parts": [" "], "fonts": set()}
                elif current["box"] is None:
                    current["parts"].append(" ")
                else:
                    pending_space = True
                continue
            if run.get("box") is None:
                # 制表符等无盒空白：进文本（code 缩进信号），不碰 bbox。
                if current is None:
                    current = {"box": None, "parts": [run["text"]],
                               "fonts": {run["font"]} if run["font"] else set()}
                else:
                    if pending_space:
                        current["parts"].append(" ")
                        pending_space = False
                    current["parts"].append(run["text"])
                    if run["font"]:
                        current["fonts"].add(run["font"])
                continue
            vis = _visible(run["box"])
            if vis is None:
                # 不可见字符直接跳过：旧实现按矩形裁交集还会留半句，
                # 这里字符级裁剪与 cropbox 测试的"CLI 半句保留"一致
                # （保留的是可见字符，不是整句）。
                continue
            box = vis["box"]
            if current is None or current["box"] is None:
                if current is not None:
                    if pending_space:
                        current["parts"].append(" ")
                        pending_space = False
                    current["parts"].append(run["text"])
                    if run["font"]:
                        current["fonts"].add(run["font"])
                    current["box"] = box
                else:
                    current = {"box": box, "parts": [run["text"]],
                               "fonts": {run["font"]} if run["font"] else set()}
                    pending_space = False
                continue
            # 行带判定：字符的底边/顶边落在当前行盒内即同行。
            # 双向包容：上标顶边仍在行盒内（顶边 <= 行顶），
            # 下标底边仍在行盒内（底边 >= 行底）—— 大小字混排不断行；
            # 隔行字两个边都在盒外才算换行。
            # 不用"底边在半行带内"：半带上限遇到版权页式的大行高会被撑爆。
            # 不用"中心在盒内"：混排数学行里大小字中心点上下差 2~3pt，
            # 中心判定会把下标踢出去、把隔行吸进来。
            cbox = current["box"]
            same_row = (cbox[1] - 1.0 <= box[1] <= cbox[3]) or (cbox[1] <= box[3] <= cbox[3] + 1.0)
            if not same_row:
                _flush()
                current = {"box": box, "parts": [run["text"]],
                           "fonts": {run["font"]} if run["font"] else set()}
                pending_space = False
                continue
            if pending_space:
                current["parts"].append(" ")
                pending_space = False
            current["parts"].append(run["text"])
            if run["font"]:
                current["fonts"].add(run["font"])
            current["box"] = (
                min(cbox[0], box[0]),
                min(cbox[1], box[1]),
                max(cbox[2], box[2]),
                max(cbox[3], box[3]),
            )
        _flush()
        # 碎片缝合：同一视觉行的混排/上下标字符会被原生换行拆成多个
        # "字符行"（如 Where 行的 W^Q_i 与 d_model×d_k 各占一个换行段）。
        # 只拼"原生换行两侧、首字 x 单调递增且垂直同带"的碎片；
        # 真正的换行（下一行 x 回到行首、或 y 差超过半行高）不断。
        # 行内空格按字符间隙恢复：碎片首字 x 与上一碎片尾字 x 的
        # 间隙超过半个行高即补一个空格（原生换行不带空格信息）。
        # 缝合只拼文本与外接 bbox，不认公式结构。
        stitched: list[dict] = []
        for line in lines:
            if not stitched:
                stitched.append(line)
                continue
            prev = stitched[-1]
            # 阅读坐标系 y 向下：prev 在上、line 在下时 dy >= 0
            dy = line["bbox"][1] - prev["bbox"][1]
            prev_h = prev["bbox"][3] - prev["bbox"][1]
            same_band = -1.0 <= dy <= max(prev_h / 2, 2.0)
            x_advance = line["bbox"][0] >= prev["bbox"][0]
            x_gap = line["bbox"][0] - prev["bbox"][2]
            near = x_gap <= max(prev_h, 4.0)
            if same_band and x_advance and near and line.get("type") == prev.get("type"):
                if x_gap > max(prev_h / 2, 2.0):
                    prev["text"] = prev["text"] + " " + line["text"]
                else:
                    prev["text"] = prev["text"] + line["text"]
                prev["bbox"] = [
                    min(prev["bbox"][0], line["bbox"][0]),
                    min(prev["bbox"][1], line["bbox"][1]),
                    max(prev["bbox"][2], line["bbox"][2]),
                    max(prev["bbox"][3], line["bbox"][3]),
                ]
                continue
            stitched.append(line)

        def to_display(bbox: list[float]) -> list[float]:
            x0, y0, x1, y1 = bbox
            return _to_display_bbox((x0, unrotated[1] - y1, x1, unrotated[1] - y0), unrotated, rotation)

        return stitched, page_size, to_display
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
                continue  # 不同栏 / 不同块
            if best_gap is None or abs(gap) < abs(best_gap):
                best, best_gap = block, gap
        if best is None:
            open_blocks.append(
                {
                    "bbox": list(box),
                    "last": list(box),
                    "texts": [line["text"]],
                    "type": line.get("type", "text"),
                }
            )
            continue
        union = best["bbox"]
        best["bbox"] = [
            min(union[0], box[0]),
            min(union[1], box[1]),
            max(union[2], box[2]),
            max(union[3], box[3]),
        ]
        best["last"] = list(box)
        best["texts"].append(line["text"])

    blocks = done + open_blocks
    blocks.sort(key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    return [{"bbox": b["bbox"], "text": "\n".join(b["texts"]), "type": b["type"]} for b in blocks]


@_pdfium_serialized
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
            lines, page_size, to_display = _lines_of_page(page)
            # 分段在阅读坐标系里做，块序也是阅读序；最后才把 bbox 换到显示空间
            blocks = [{**block, "bbox": to_display(block["bbox"])} for block in _merge_lines(lines)]
            pages.append({"page_idx": page_idx, "page_size": page_size, "blocks": blocks})
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
