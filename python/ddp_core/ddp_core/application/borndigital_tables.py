"""born-digital 表格剖面：有文字层 + 有线框表格的 PDF -> 带表格块的页。

**与 `borndigital.py` 的关系**：那份是 CPU 文字层解析器，行为冻结。
本文件只调它的 `_char_runs` / `_merge_lines` 等 helpers，外加
`_lines_of_page` 的 keyword-only `exclude` 钩子做字符级表格区剔除
（该钩子缺省 `None`，borndigital 自身行为一字不变）。本文件只做一件事：
用 pdfplumber 的线框几何（显式线框边的 `explicit` 策略）在普通行/段
流程之外，额外产出 `type: table` 块（contract `table_html` +
`rowspan`/`colspan`）。

**线框的定义**：页面 line、未填充矩形，以及厚度不超过 pdfplumber 默认
snap 容差的填充矩形（PDF 常用这种细矩形绘制边线）。较厚的填充区域
（灰色表头底、图例色块）和 curve 装饰边不参与成表，避免穿字的假边界。
全部检测容差取 pdfplumber 缺省，不按具体文档拟合。

**覆盖与不覆盖**（`engine_notes` 里如实报，不静默）：
  - 覆盖：机器生成、有文字层、有**线框**的表格。合并单元格按几何
    span 如实表达，不猜语义、不复制单元格文本。
  - 不覆盖：无文字层（扫描件）—— 整份失败，与 borndigital 一致；
    无线框/纯文本对齐的"表格" —— 那是 `text` 策略的猜测，不做，
    对应文本走普通段落流程原样保留；
    旋转页（`/Rotate != 0`）—— pdfplumber 与 PDFium 的坐标系在旋转页上
    不一致（一个按显示旋转、一个在未旋转空间），该页表格恢复整体降级，
    文本仍走普通流程；
    CropBox/坐标系对不上的页 —— 同上，降级保文本。

坐标系：pdfplumber 的表几何在 mediabox 系（左上原点、y 向下）；
borndigital 的显示空间是 cropbox 系（同样左上原点、y 向下）。
两系只差一个平移 `(cropbox[0], page_h - cropbox[3])`（无旋转时），
换算由 `_plumber_to_display` 做，换算完的表 bbox 必须整体落在
`page_size` 内，否则该页表格恢复作废。

文本是**单一来源**：单元格文字一律来自 PDFium `_char_runs`
（与 borndigital 同一字符流、同一原生顺序、同一空格语义），
不用 pdfplumber 的 `extract()`（它会丢空格、且字形来源是另一套解析器）。
"""

import html
import io

from ddp_core.application import borndigital as _bd
from ddp_core.application.layout import ENGINE_NOTES
from ddp_core.crops import _pdfium_serialized

ENGINE = "borndigital-tables"


# 只有这两个 note code 前缀。含义：
#   tables_unavailable:<reason>  整份 PDF 里 pdfplumber 没产出任何表
#     （无依赖 / 解析失败 / 全是无线框页）。有这个 note 不代表失败：
#     文本流程照常。
#   tables_skipped_page:<idx>:<reason>  某一页的表格恢复被跳过，
#     文本仍走普通流程。reason 是 rotated_page / coordinate_mismatch /
#     table_detection_failed 之一。
TABLES_UNAVAILABLE = "tables_unavailable"
TABLES_SKIPPED_PAGE = "tables_skipped_page"


def _plumber_to_display(bbox, cropbox, page_h):
    """pdfplumber bbox（mediabox 系，左上原点）-> borndigital 显示空间。

    无旋转时两系只差 cropbox 原点的平移：x 减 cropbox 左缘，
    y 减 mediabox 顶到 cropbox 顶的距离（= page_h - cropbox[3]）。
    """
    dx = cropbox[0]
    dy = page_h - cropbox[3]
    x0, top, x1, bottom = bbox
    return [x0 - dx, top - dy, x1 - dx, bottom - dy]


def _ruled_tables(plumber_page):
    """Detect ruled cells without treating broad background fills as boundaries.

    Painted rules are thin filled rectangles at the detector's own snapping
    resolution, not necessarily PDF line objects. Glyph-less grids are omitted;
    geometry alone does not establish that a grid is a semantic data table.
    """
    from pdfplumber import utils as _plumber_utils
    from pdfplumber.table import TableSettings

    edges = [_plumber_utils.line_to_edge(line) for line in plumber_page.lines]
    rule_thickness = TableSettings().snap_tolerance
    for rect in plumber_page.rects:
        if rect.get("fill") and min(rect["width"], rect["height"]) > rule_thickness:
            continue
        edges.extend(_plumber_utils.rect_to_edges(rect))
    vertical = [e for e in edges if e["orientation"] == "v"]
    horizontal = [e for e in edges if e["orientation"] == "h"]
    if len(vertical) < 2 or len(horizontal) < 2:
        return []
    tables = plumber_page.find_tables({
        "vertical_strategy": "explicit",
        "horizontal_strategy": "explicit",
        "explicit_vertical_lines": vertical,
        "explicit_horizontal_lines": horizontal,
    })
    chars = plumber_page.chars
    kept = []
    for table in tables:
        x0, top, x1, bottom = table.bbox
        has_text = False
        for char in chars:
            text = char.get("text") or ""
            if len(text) != 1 or not text.isalnum():
                continue
            h_mid = (char["x0"] + char["x1"]) / 2
            v_mid = (char["top"] + char["bottom"]) / 2
            if x0 <= h_mid < x1 and top <= v_mid < bottom:
                has_text = True
                break
        if has_text:
            kept.append(table)
    return kept


def _grid_spans(table):
    """Recover spans from cell rectangles, not from ambiguous missing cells.

    A missing grid position may be a merged cell or an unclosed hole. Only an
    actual cell rectangle can own it. Overlaps and holes are not expressible as
    this flat rectangular table and must preserve the page's plain text instead.
    """
    cells = table.cells
    xs = sorted({x for cell in cells for x in (cell[0], cell[2])})
    ys = sorted({y for cell in cells for y in (cell[1], cell[3])})
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    if n_rows < 1 or n_cols < 1:
        return None
    x_index = {x: j for j, x in enumerate(xs)}
    y_index = {y: i for i, y in enumerate(ys)}
    out = [[None] * n_cols for _ in range(n_rows)]
    for cell in cells:
        left, right = x_index[cell[0]], x_index[cell[2]]
        top, bottom = y_index[cell[1]], y_index[cell[3]]
        if left >= right or top >= bottom:
            return None
        for i in range(top, bottom):
            for j in range(left, right):
                if out[i][j] is not None:
                    return None
                out[i][j] = ((cell, bottom - top, right - left)
                             if (i, j) == (top, left) else (top, left))
    if any(cell is None for row in out for cell in row):
        return None
    return n_rows, n_cols, out


def _cell_text(indexed):
    """一格的 [(下标, run)]（原生顺序）-> 文本。

    `space` 占位表示词间空格（多个压成一个），`break` 表示原生换行
    （格内多行用空格连接，不丢词）；首尾空白 strip。
    下标不连续（原生流中途去了别的格再回来，如两行的表头共用一格）
    时补一个空格：那是源字符流里两个独立的文本段，不是同一个词。
    """
    parts = []
    pending_space = False
    prev_index = None
    for index, run in indexed:
        if prev_index is not None and index > prev_index + 1 and parts:
            pending_space = True
        prev_index = index
        if run.get("break"):
            pending_space = bool(parts)
            continue
        if run.get("space"):
            pending_space = bool(parts)
            continue
        if pending_space:
            parts.append(" ")
            pending_space = False
        parts.append(run.get("text", ""))
    return "".join(parts).strip()


def _table_block(table_bbox, grid_text, spans):
    """(表 bbox, 首格文本, span 网格) -> DDP-Layout para_block（dict）。

    block_text 读的是拍平的行文本（逐行、逐格，覆盖格不重复）；
    结构走 span 上 `html` 字段里的 `<table>`（与 mineru/dsocr2 同一个位置，
    `layout.table_html` 不用改）。覆盖格在 HTML 里不出 `<td>`，
    由首格的 rowspan/colspan 承载。
    """
    n_rows, n_cols, out = spans
    html_rows = []
    flat_lines = []
    for i in range(n_rows):
        tds = []
        flat_cells = []
        for j in range(n_cols):
            info = out[i][j]
            if not (isinstance(info, tuple) and len(info) == 3):
                continue
            _, rs, cs = info
            text = grid_text[i][j] or ""
            attrs = ""
            if rs > 1:
                attrs += f" rowspan=\"{rs}\""
            if cs > 1:
                attrs += f" colspan=\"{cs}\""
            tds.append(f"<td{attrs}>{html.escape(text)}</td>")
            if text:
                flat_cells.append(text)
        html_rows.append("<tr>" + "".join(tds) + "</tr>")
        if flat_cells:
            flat_lines.append(" ".join(flat_cells))
    block = {
        "type": "table",
        "bbox": [float(v) for v in table_bbox],
        "lines": [
            {"spans": [{"content": line}]}
            for line in flat_lines
            if line.strip()
        ],
    }
    block["lines"].append(
        {"spans": [{"content": "",
                    "html": "<table>" + "".join(html_rows) + "</table>"}]}
    )
    return block


def _page_tables_to_blocks(pdfium_page, plumber_page, runs):
    """一页 -> (table blocks, 表内字符下标集合, skip_reason|None)。

    runs 是本页的 `_char_runs`（PDFium 原生顺序）。表内字符按下标返回，
    调用方把它喂给 `_lines_of_page(exclude=...)` 做字符级剔除。
    旋转页/几何对不上/归属失败时返回 reason，调用方保留全部普通文本。
    """
    rotation = (pdfium_page.get_rotation() or 0) % 360
    if rotation != 0:
        return ([], set(), "rotated_page")
    cropbox = pdfium_page.get_bbox()
    page_size = [float(v) for v in pdfium_page.get_size()]
    page_w, page_h = page_size
    # 无旋转时 pdfium get_size 就是 mediabox 尺寸；plumber 侧也一样。
    # 两边尺寸对不上（>0.5pt）说明坐标系理解有误，整页降级。
    if (abs(plumber_page.width - page_w) > 0.5
            or abs(plumber_page.height - page_h) > 0.5):
        return ([], set(), "coordinate_mismatch")
    tables = _ruled_tables(plumber_page)
    if not tables:
        return ([], set(), None)
    # runs 的显示空间盒（与 _lines_of_page 的可见裁剪同一判据）。
    unrotated = (cropbox[2] - cropbox[0], cropbox[3] - cropbox[1])
    h0 = unrotated[1]
    display_boxes: list = []
    for run in runs:
        box = run.get("box")
        if box is None:
            display_boxes.append(None)
            continue
        left = max(box[0], cropbox[0])
        bottom = max(box[1], cropbox[1])
        right = min(box[2], cropbox[2])
        top = min(box[3], cropbox[3])
        if left >= right or bottom >= top:
            display_boxes.append(None)
            continue
        display_boxes.append([left - cropbox[0], h0 - (top - cropbox[1]),
                              right - cropbox[0], h0 - (bottom - cropbox[1])])
    blocks = []
    excluded: set[int] = set()
    for table in tables:
        disp_bbox = _plumber_to_display(table.bbox, cropbox, page_h)
        if not (0 <= disp_bbox[0] < disp_bbox[2] <= page_w + 0.5
                and 0 <= disp_bbox[1] < disp_bbox[3] <= page_h + 0.5):
            # 野表（线框延伸出页，如页眉装饰线）：只跳过这张表，
            # 不连累页内其它表。它的"字符"本就不在页内，无需剔除。
            continue
        spans = _grid_spans(table)
        if spans is None:
            return ([], set(), "table_detection_failed")
        n_rows, n_cols, out = spans
        primaries: dict[tuple[int, int], list] = {}
        for i in range(n_rows):
            for j in range(n_cols):
                info = out[i][j]
                if isinstance(info, tuple) and len(info) == 3:
                    primaries[(i, j)] = _plumber_to_display(
                        info[0], cropbox, page_h)
        # 表 bbox 内的可见字符下标（中心点判定）。
        inside = set()
        for k, (run, box) in enumerate(zip(runs, display_boxes)):
            if run.get("break") or run.get("space") or box is None:
                continue
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            if (disp_bbox[0] <= cx < disp_bbox[2]
                    and disp_bbox[1] <= cy < disp_bbox[3]):
                inside.add(k)
        if not inside:
            continue
        if inside & excluded:
            # Nested/overlapping grids cannot emit the same source glyph twice.
            return ([], set(), "table_detection_failed")
        # 可见字符 -> 首格（中心点），一字一格；落在表内但进不了
        # 任何首格的字符（线框与文字错位）让整张表降级 —— 宁可整表
        # 回落普通文本，也不让个别字符凭空消失。
        # 空格/换行占位符没有盒：它归属"原生序前后最近可见字符同属的那一格"，
        # 跨格的占位（格间边界）两边都不收 —— 免得一个空格同时进两格造成
        # 文本重复，也免得格内词间空格丢失。
        cell_of: dict[int, tuple[int, int]] = {}
        failed = False
        for k in sorted(inside):
            run = runs[k]
            if run.get("break") or run.get("space"):
                continue
            box = display_boxes[k]
            if box is None:
                continue
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            hit = None
            for key, cell in primaries.items():
                if cell[0] <= cx < cell[2] and cell[1] <= cy < cell[3]:
                    hit = key
                    break
            if hit is None:
                failed = True
                break
            cell_of[k] = hit
        if failed:
            return ([], set(), "table_detection_failed")
        per_cell: dict[tuple[int, int], list] = {k: [] for k in primaries}
        previous = None
        pending = []
        for k, run in enumerate(runs):
            if run.get("break") or run.get("space"):
                pending.append((k, run))
                continue
            cell = cell_of.get(k)
            if cell is not None:
                # Delimiters belong to the table only when the two immediately
                # adjacent native glyphs do. Outside words may be interleaved
                # between cell draws and must retain their original spaces.
                if previous in cell_of:
                    inside.update(index for index, _ in pending)
                    if cell_of[previous] == cell:
                        per_cell[cell].extend(pending)
                per_cell[cell].append((k, run))
            pending.clear()
            previous = k
        grid_text = [[None] * n_cols for _ in range(n_rows)]
        for (i, j) in primaries:
            grid_text[i][j] = _cell_text(per_cell[(i, j)])
        blocks.append(_table_block(disp_bbox, grid_text, spans))
        excluded |= inside
    return (blocks, excluded, None)


@_pdfium_serialized
def extract_pages(pdf_bytes: bytes) -> tuple[list[dict], list[str]]:
    """PDF 字节 -> ([页], notes)。

    页：{page_idx, page_size, blocks, printed_page_label}。blocks 里
    `type: table` 的块是 DDP-Layout 形状（含 lines/spans/html），其余与
    borndigital 同形（{bbox, text, type}）。调用方用 `to_layout_pages`
    转成 DDP-Layout 形状再 `build_pages`。

    没有任何一页有文字且无表时返回 ([], notes) —— 调用方据此报
    "没有文字层"，而不是产出空版面。
    """
    import pypdfium2 as pdfium

    notes: list[str] = []
    try:
        import pdfplumber
    except ImportError:
        notes.append(f"{TABLES_UNAVAILABLE}: missing pdfplumber dependency")
        return (_bd.extract_pages(pdf_bytes), notes)
    document = pdfium.PdfDocument(pdf_bytes)
    try:
        try:
            plumber = pdfplumber.open(io.BytesIO(pdf_bytes))
        except Exception as exc:
            notes.append(f"{TABLES_UNAVAILABLE}: pdfplumber open failed "
                         f"({type(exc).__name__})")
            return (_bd.extract_pages(pdf_bytes), notes)
        try:
            if len(plumber.pages) != len(document):
                notes.append(f"{TABLES_UNAVAILABLE}: page count mismatch "
                             f"(pdfium={len(document)}, "
                             f"pdfplumber={len(plumber.pages)})")
                return (_bd.extract_pages(pdf_bytes), notes)
            out = []
            any_table = False
            for page_idx in range(len(document)):
                page = document[page_idx]
                textpage = page.get_textpage()
                try:
                    runs = _bd._char_runs(textpage)
                finally:
                    textpage.close()
                try:
                    table_blocks, excluded, reason = _page_tables_to_blocks(
                        page, plumber.pages[page_idx], runs)
                except Exception:
                    table_blocks, excluded, reason = ([], set(),
                                                      "table_detection_failed")
                if reason is not None:
                    notes.append(f"{TABLES_SKIPPED_PAGE}:{page_idx}:{reason}")
                    table_blocks, excluded = [], set()
                any_table = any_table or bool(table_blocks)
                lines, page_size, to_display = _bd._lines_of_page(
                    page, exclude=excluded)
                blocks = [{**b, "bbox": to_display(b["bbox"])}
                          for b in _bd._merge_lines(lines)]
                # 表块按 y 序并入：绝不重排普通块之间的相对顺序。
                merged = []
                pending = sorted(table_blocks,
                                 key=lambda b: (round(b["bbox"][1], 1),
                                                b["bbox"][0]))
                for block in blocks:
                    while (pending
                           and (round(pending[0]["bbox"][1], 1),
                                pending[0]["bbox"][0])
                           < (round(block["bbox"][1], 1), block["bbox"][0])):
                        merged.append(pending.pop(0))
                    merged.append(block)
                merged.extend(pending)
                label = document.get_page_label(page_idx) or None
                out.append({"page_idx": page_idx, "page_size": page_size,
                            "blocks": merged,
                            "printed_page_label": label})
            if not any_table:
                notes.append(f"{TABLES_UNAVAILABLE}: no ruled tables detected")
            if not any(p["blocks"] for p in out):
                return ([], notes)
            return (out, notes)
        finally:
            plumber.close()
    finally:
        document.close()


def to_layout_pages(pages: list[dict]) -> list[dict]:
    """extract_pages 的页 -> build_pages 可收的 DDP-Layout 形状页。"""
    layout_pages = []
    for page in pages:
        para_blocks = []
        for block in page["blocks"]:
            if block.get("type") == "table" and isinstance(
                    block.get("lines"), list):
                para_blocks.append(block)
                continue
            para_blocks.append({
                "type": block.get("type", "text"),
                "bbox": list(block["bbox"]),
                "lines": [{"spans": [{"content": line}]}
                          for line in block.get("text", "").splitlines()
                          if line.strip()],
            })
        layout_pages.append({
            "page_idx": page["page_idx"],
            "page_size": list(page["page_size"]),
            "printed_page_label": page.get("printed_page_label"),
            "para_blocks": [b for b in para_blocks
                            if b.get("type") == "table" or b.get("lines")],
        })
    return layout_pages


def to_markdown(pages: list[dict]) -> str:
    """页 -> markdown。表格块嵌 HTML（与 vlm-ocr 的 to_markdown 同约定）。"""
    from ddp_core.blocks import table_html

    parts: list[str] = []
    for page in pages:
        if parts:
            parts.append("\n---\n")
        for block in page["blocks"]:
            if block.get("type") == "table" and isinstance(
                    block.get("lines"), list):
                html_text = table_html(block)
                if html_text:
                    parts.append(html_text)
                    continue
            text = block.get("text", "").replace("\n", " ").strip()
            if text:
                parts.append(text)
    return "\n\n".join(p for p in parts if p) + "\n"


def layout_json(pages: list[dict], notes: list[str]) -> dict:
    """extract_pages 产物 -> 完整 layout_json（含 engine_notes）。"""
    from ddp_core.application.layout import build_pages

    built = build_pages(to_layout_pages(pages), engine=ENGINE,
                        code_detection="heuristic")
    if notes:
        built[ENGINE_NOTES] = list(notes)
    return built
