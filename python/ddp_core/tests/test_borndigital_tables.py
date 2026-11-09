"""borndigital-tables 剖面：线框表格恢复 + 普通文本零损失。

约定：
- 夹具全部现场生成（reportlab 画线框 + 写字），不 checked-in 任何 PDF，
  不引用任何真实来源文档的标识/文本/容差。
- 文本断言只认夹具里写死的英文 token；span 断言只认几何结构
  （rowspan/colspan 与 None 覆盖位），不认具体业务语义。
- 依赖由产品和 dev extras 声明，缺失时测试失败；ImportError 降级单独覆盖。
"""

import io
import re

import pytest

import pdfplumber

from ddp_core.application import borndigital as bd
from ddp_core.application import borndigital_tables as bdt
from ddp_core.application import layout as layout_app
from ddp_core.blocks import block_text, table_html
from ddp_core.chunking import layout_to_chunks

W, H = 595.2756, 841.8898  # A4，reportlab 缺省


def _ruled_pdf(*, crop=None, rotate=0, unruled=False):
    """单页夹具：页眉行 + 线框 3x3 表（rowspan + colspan 各一）+ 页脚行。

    表格 (60, H-120) 起，列宽 90、行高 30：
      - 第 0 行第 1/2 列共用一格（colspan=2，跳掉 xs[2] 在该行的竖线段）；
      - 第 0 列第 1/2 行共用一格（rowspan=2，跳掉 ys[2] 在该列的横线段）。
    crop/rotate 通过 pdfminer 读得懂的方式打补丁：reportlab 生成后，
    用 pypdf 改写页字典（pypdf 只是测试夹具工具，不进产品依赖）。
    unruled=True 时只写字不画线。
    """
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setFont("Helvetica", 12)
    c.drawString(60, H - 60, "OUTSIDE-TOP marker alpha")
    x0, top = 60, H - 120
    cw, rh = 90, 30
    xs = [x0 + i * cw for i in range(4)]
    ys = [top - i * rh for i in range(4)]
    if not unruled:
        c.setLineWidth(0.8)
        p = c.beginPath()
        for x in (xs[0], xs[1], xs[3]):
            p.moveTo(x, ys[3])
            p.lineTo(x, ys[0])
        p.moveTo(xs[2], ys[3])
        p.lineTo(xs[2], ys[1])
        for j, y in enumerate(ys):
            if j == 2:
                p.moveTo(xs[1], y)
                p.lineTo(xs[3], y)
            else:
                p.moveTo(xs[0], y)
                p.lineTo(xs[3], y)
        c.drawPath(p, stroke=1, fill=0)
    c.setFont("Helvetica", 9)
    c.drawString(xs[0] + 4, ys[1] + 10, "R0C0")
    c.drawString(xs[1] + 4, ys[1] + 10, "R0C1C2")
    c.drawString(xs[0] + 4, ys[2] + 10, "SPANROWS")
    c.drawString(xs[1] + 4, ys[2] + 10, "R1C1")
    c.drawString(xs[2] + 4, ys[2] + 10, "R1C2")
    c.drawString(xs[1] + 4, ys[3] + 10, "R2C1")
    c.drawString(xs[2] + 4, ys[3] + 10, "R2C2")
    c.drawString(60, ys[3] - 40, "OUTSIDE-BOTTOM marker beta")
    c.showPage()
    c.save()
    data = buf.getvalue()
    if crop is None and not rotate:
        return data
    import pypdf
    from pypdf.generic import RectangleObject

    reader = pypdf.PdfReader(io.BytesIO(data))
    writer = pypdf.PdfWriter()
    page = reader.pages[0]
    if crop is not None:
        page.cropbox = RectangleObject(list(crop))
    if rotate:
        page.rotation = rotate
    writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


TOKENS = ["OUTSIDE-TOP", "alpha", "R0C0", "R0C1C2", "SPANROWS", "R1C1",
          "R1C2", "R2C1", "R2C2", "OUTSIDE-BOTTOM", "beta"]


def _token_multiset(text):
    return sorted(re.findall(r"[A-Za-z0-9-]+", text))


def test_ruled_table_recovers_spans_and_keeps_outside_text():
    pages, notes = bdt.extract_pages(_ruled_pdf())
    assert len(pages) == 1
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    assert built["engine"] == "borndigital-tables"
    blocks = built["pdf_info"][0]["para_blocks"]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1, [b.get("type") for b in blocks]
    table = tables[0]
    x0, y0, x1, y1 = table["bbox"]
    assert 0 <= x0 < x1 <= pages[0]["page_size"][0]
    assert 0 <= y0 < y1 <= pages[0]["page_size"][1]
    html_text = table_html(table)
    assert html_text is not None and html_text.startswith("<table>")
    # rowspan + colspan 各一，且只出现一次（不复制、不捏造）。
    assert html_text.count("rowspan=\"2\"") == 1, html_text
    assert html_text.count("colspan=\"2\"") == 1, html_text
    # 覆盖格不出 td：3x3 网格里 9 个位置，2 个被覆盖 -> 7 个 td。
    assert html_text.count("<td") == 7, html_text
    for token in ("R0C0", "R0C1C2", "SPANROWS", "R1C1", "R1C2", "R2C1", "R2C2"):
        assert html_text.count(token) == 1, (token, html_text)
    # 表外文本仍在普通块里，一字不少。
    texts = [b for b in blocks if b["type"] != "table"]
    flat = " ".join(block_text(b) for b in texts)
    assert "OUTSIDE-TOP" in flat and "alpha" in flat, flat
    assert "OUTSIDE-BOTTOM" in flat and "beta" in flat, flat
    # 表内 token 不许同时出现在普通块里（不重复）。
    for token in ("R0C0", "SPANROWS", "R2C2"):
        assert token not in flat, (token, flat)


def test_no_source_text_lost_or_duplicated_against_plain_profile():
    """与 borndigital 同一字符流：token 多重集必须完全一致。"""
    pdf = _ruled_pdf()
    plain = bd.extract_pages(pdf)
    plain_text = "\n".join(b["text"] for p in plain for b in p["blocks"])
    pages, notes = bdt.extract_pages(pdf)
    built = bdt.layout_json(pages, notes)
    tables_text = " ".join(
        block_text(b) for p in built["pdf_info"] for b in p["para_blocks"])
    assert _token_multiset(tables_text) == _token_multiset(plain_text), (
        tables_text, plain_text)


def test_filled_header_background_does_not_split_cells():
    """填充矩形（灰底表头）的边不算线框：灰底不能把行切碎。

    回归形状：灰底矩形边落在文字行带内时，缺省 `lines` 策略会以它为界
    多切出一行（实测 2 行变 3 行）。本剖面只认 line + 未填充 rect 边，
    灰底只盖底色、不参与成表。
    """
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    x0, top, cw, rh = 60, H - 120, 120, 30
    xs = [x0 + i * cw for i in range(3)]
    ys = [top - i * rh for i in range(3)]
    # 灰底：盖住第 0 行上部，底边落在文字行带内（文字 y 约 133~140，
    # 灰底边在 138）。缺省 `lines` 策略会以它为界多切出一行空行，
    # 本剖面不认填充边，灰底只盖底色。
    c.setFillColorRGB(0.85, 0.85, 0.85)
    c.rect(xs[0], ys[1] + 12, xs[2] - xs[0], ys[0] - ys[1] - 12,
           stroke=0, fill=1)
    c.setLineWidth(0.8)
    p = c.beginPath()
    for x in xs:
        p.moveTo(x, ys[2])
        p.lineTo(x, ys[0])
    for y in ys:
        p.moveTo(xs[0], y)
        p.lineTo(xs[2], y)
    c.drawPath(p, stroke=1, fill=0)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 9)
    c.drawString(xs[0] + 4, ys[1] + 10, "HEADONE")
    c.drawString(xs[1] + 4, ys[1] + 10, "HEADTWO")
    c.drawString(xs[0] + 4, ys[2] + 10, "BODYONE")
    c.drawString(xs[1] + 4, ys[2] + 10, "BODYTWO")
    c.showPage()
    c.save()
    pages, notes = bdt.extract_pages(buf.getvalue())
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    tables = [b for b in built["pdf_info"][0]["para_blocks"]
              if b["type"] == "table"]
    assert len(tables) == 1
    html_text = table_html(tables[0])
    # 2x2 四格，无 span，token 完整不碎。
    assert html_text.count("<td") == 4, html_text
    assert "rowspan" not in html_text and "colspan" not in html_text
    for token in ("HEADONE", "HEADTWO", "BODYONE", "BODYTWO"):
        assert html_text.count(token) == 1, (token, html_text)


def test_thin_filled_rectangles_are_rulings_not_header_backgrounds():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    for x in (60, 150, 240):
        c.rect(x, 600, 0.75, 60, stroke=0, fill=1)
    for y in (600, 630, 660):
        c.rect(60, y, 180, 0.75, stroke=0, fill=1)
    for x, y, text in [(64, 640, "LEFTHEAD"), (154, 640, "RIGHTHEAD"),
                       (64, 610, "LEFTVALUE"), (154, 610, "RIGHTVALUE")]:
        c.drawString(x, y, text)
    c.save()
    pages, notes = bdt.extract_pages(buf.getvalue())
    built = bdt.layout_json(pages, notes)
    tables = [b for p in built["pdf_info"] for b in p["para_blocks"] if b["type"] == "table"]
    assert len(tables) == 1
    html_text = table_html(tables[0])
    assert html_text.count("<td") == 4
    assert "rowspan" not in html_text and "colspan" not in html_text
    assert all(word in html_text for word in ("LEFTHEAD", "RIGHTHEAD", "LEFTVALUE", "RIGHTVALUE"))


def test_nested_frames_do_not_duplicate_inner_text():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.rect(40, 580, 460, 100, stroke=1, fill=0)
    c.line(280, 580, 280, 680)
    c.drawString(300, 640, "OUTER")
    c.rect(60, 600, 180, 60, stroke=1, fill=0)
    c.line(150, 600, 150, 660)
    c.line(60, 630, 240, 630)
    for x, y, text in [(64, 640, "UPPERLEFT"), (154, 640, "UPPERRIGHT"),
                       (64, 610, "LOWERLEFT"), (154, 610, "LOWERRIGHT")]:
        c.drawString(x, y, text)
    c.save()
    data = buf.getvalue()
    pages, notes = bdt.extract_pages(data)
    built = bdt.layout_json(pages, notes)
    actual = " ".join(block_text(b) for p in built["pdf_info"] for b in p["para_blocks"])
    expected = " ".join(b["text"] for p in bd.extract_pages(data) for b in p["blocks"])
    assert _token_multiset(actual) == _token_multiset(expected)


def test_incomplete_grid_does_not_invent_merged_cells():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    # Three closed cells form an L; the top-right position is not a merged cell.
    for x0, y0, x1, y1 in [
        (60, 600, 240, 600), (60, 630, 240, 630), (60, 660, 150, 660),
        (60, 600, 60, 660), (150, 600, 150, 660), (240, 600, 240, 630),
    ]:
        c.line(x0, y0, x1, y1)
    for x, y, text in [(64, 640, "UPPER"), (64, 610, "LOWERLEFT"),
                       (154, 610, "LOWERRIGHT")]:
        c.drawString(x, y, text)
    c.save()
    data = buf.getvalue()
    pages, notes = bdt.extract_pages(data)
    assert not any(b["type"] == "table" for p in pages for b in p["blocks"])
    assert "tables_skipped_page:0:table_detection_failed" in notes
    assert pages == bd.extract_pages(data)


def test_native_order_interleaved_outside_words_keep_their_spaces():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.rect(60, 600, 180, 60, stroke=1, fill=0)
    c.line(150, 600, 150, 660)
    c.line(60, 630, 240, 630)
    c.drawString(64, 640, "FIRST")
    c.drawString(300, 640, "OUTSIDE words remain separate")
    c.drawString(154, 640, "SECOND")
    c.drawString(64, 610, "THIRD")
    c.drawString(154, 610, "FOURTH")
    c.save()
    pages, notes = bdt.extract_pages(buf.getvalue())
    layout = bdt.layout_json(pages, notes)
    outside = " ".join(block_text(b) for b in layout["pdf_info"][0]["para_blocks"]
                       if b["type"] != "table")
    assert "OUTSIDE words remain separate" in outside
    tables = [b for b in layout["pdf_info"][0]["para_blocks"] if b["type"] == "table"]
    assert len(tables) == 1
    assert all(word in table_html(tables[0]) for word in
               ("FIRST", "SECOND", "THIRD", "FOURTH"))


def test_shifted_media_box_never_produces_a_misaligned_table():
    import pypdf
    from pypdf.generic import RectangleObject

    reader = pypdf.PdfReader(io.BytesIO(_ruled_pdf()))
    page = reader.pages[0]
    page.mediabox = RectangleObject([-40, 30, W - 40, H + 30])
    writer = pypdf.PdfWriter()
    writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    pages, notes = bdt.extract_pages(data)
    tables = [b for p in pages for b in p["blocks"] if b["type"] == "table"]
    if tables:
        assert len(tables) == 1
        assert tables[0]["bbox"] == pytest.approx([100, 150, 370, 240], abs=0.01)
        assert all(token in table_html(tables[0]) for token in
                   ("R0C0", "R0C1C2", "SPANROWS", "R1C1", "R1C2", "R2C1", "R2C2"))
    else:
        assert "tables_skipped_page:0:coordinate_mismatch" in notes
        assert pages == bd.extract_pages(data)


def test_unfilled_rect_table_and_empty_frame_ignored():
    """未填充 rect 表成表；无字空框（图例/装饰框）不成表不占位。

    rect 画的 2x2 线框表必须被恢复；同页另一个无字的 rect 空框
    不得产出 table 块（几何成表、内容无字 = 不是表格）。
    """
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    x0, top, cw, rh = 60, H - 120, 90, 30
    # 未填充 rect 画表：外框 + 内十字。
    c.setLineWidth(0.8)
    c.rect(x0, top - 2 * rh, 2 * cw, 2 * rh, stroke=1, fill=0)
    p = c.beginPath()
    p.moveTo(x0 + cw, top - 2 * rh)
    p.lineTo(x0 + cw, top)
    p.moveTo(x0, top - rh)
    p.lineTo(x0 + 2 * cw, top - rh)
    c.drawPath(p, stroke=1, fill=0)
    c.setFont("Helvetica", 9)
    c.drawString(x0 + 4, top - rh + 10, "Q0C0")
    c.drawString(x0 + cw + 4, top - rh + 10, "Q0C1")
    c.drawString(x0 + 4, top - 2 * rh + 10, "Q1C0")
    c.drawString(x0 + cw + 4, top - 2 * rh + 10, "Q1C1")
    # 无字空框：图例/装饰框形状。
    c.rect(400, H - 200, 80, 40, stroke=1, fill=0)
    c.showPage()
    c.save()
    pages, notes = bdt.extract_pages(buf.getvalue())
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    blocks = built["pdf_info"][0]["para_blocks"]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1, [b.get("type") for b in blocks]
    html_text = table_html(tables[0])
    assert html_text.count("<td") == 4, html_text
    for token in ("Q0C0", "Q0C1", "Q1C0", "Q1C1"):
        assert html_text.count(token) == 1, (token, html_text)


def test_table_block_is_standalone_in_chunks():
    pages, notes = bdt.extract_pages(_ruled_pdf())
    chunks = layout_to_chunks(bdt.layout_json(pages, notes))
    table_chunks = [c for c in chunks if c["block_type"] == "table"]
    assert len(table_chunks) == 1, [(c["block_type"], c["text"][:40]) for c in chunks]
    chunk = table_chunks[0]
    assert chunk["table_html"] and chunk["table_html"].startswith("<table>")
    assert "rowspan=\"2\"" in chunk["table_html"]
    assert "OUTSIDE-TOP" not in chunk["text"] and "beta" not in chunk["text"]
    # 表外文本仍可检索。
    flat = " ".join(c["text"] for c in chunks)
    for token in TOKENS:
        assert token in flat, (token, flat)


def test_markdown_embeds_table_html_verbatim():
    pages, _ = bdt.extract_pages(_ruled_pdf())
    markdown = bdt.to_markdown(pages)
    assert "<table>" in markdown and "rowspan=\"2\"" in markdown
    assert "OUTSIDE-TOP" in markdown and "OUTSIDE-BOTTOM" in markdown


def test_cropped_box_page_skips_tables_but_keeps_visible_text():
    """CropBox != MediaBox 的页：两家坐标系理解不一致，表格恢复降级。

    pdfplumber 的表几何在 mediabox 系，borndigital 的显示空间在 cropbox 系；
    实测 pdfplumber 在 CropBox 页上仍按 mediabox 报尺寸（`page.width` 不跟
    CropBox 走），与 PDFium 的 cropbox 尺寸对不上 —— 这正是
    `coordinate_mismatch` 守卫要拦的形状。该页无表格块，但可见文本
    一个不少（与 borndigital 同页输出 token 一致）。
    """
    pdf = _ruled_pdf(crop=[50, 400, 545, 792])
    pages, notes = bdt.extract_pages(pdf)
    assert len(pages) == 1
    assert "tables_skipped_page:0:coordinate_mismatch" in notes, notes
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    assert built["engine_notes"] == notes
    blocks = built["pdf_info"][0]["para_blocks"]
    assert blocks and all(b["type"] != "table" for b in blocks)
    plain = bd.extract_pages(pdf)
    plain_text = "\n".join(b["text"] for p in plain for b in p["blocks"])
    flat = " ".join(block_text(b) for b in blocks)
    assert _token_multiset(flat) == _token_multiset(plain_text), (flat, plain_text)


def test_rotated_page_skips_tables_but_keeps_all_text():
    pdf = _ruled_pdf(rotate=90)
    pages, notes = bdt.extract_pages(pdf)
    assert len(pages) == 1
    assert "tables_skipped_page:0:rotated_page" in notes, notes
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    assert built["engine_notes"] == notes
    blocks = built["pdf_info"][0]["para_blocks"]
    assert [b["type"] for b in blocks] and all(
        b["type"] != "table" for b in blocks)
    flat = " ".join(block_text(b) for b in blocks)
    for token in TOKENS:
        assert token in flat, (token, flat)


def test_unruled_page_reports_no_tables_and_keeps_text():
    pdf = _ruled_pdf(unruled=True)
    pages, notes = bdt.extract_pages(pdf)
    assert notes == ["tables_unavailable: no ruled tables detected"], notes
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    blocks = built["pdf_info"][0]["para_blocks"]
    assert all(b["type"] != "table" for b in blocks)
    # 与 borndigital 同页文本一致（无线框页就是普通剖面）。
    plain = bd.extract_pages(pdf)
    plain_text = "\n".join(b["text"] for p in plain for b in p["blocks"])
    flat = " ".join(block_text(b) for b in blocks)
    assert _token_multiset(flat) == _token_multiset(plain_text)


def test_missing_pdfplumber_keeps_plain_text_with_note(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("no pdfplumber in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    pages, notes = bdt.extract_pages(_ruled_pdf())
    assert notes == ["tables_unavailable: missing pdfplumber dependency"]
    built = bdt.layout_json(pages, notes)
    assert layout_app.validate(built) == []
    flat = " ".join(block_text(b) for p in built["pdf_info"]
                    for b in p["para_blocks"])
    for token in TOKENS:
        assert token in flat, (token, flat)


def test_empty_pdf_returns_no_pages():
    import io as _io

    import pypdfium2 as pdfium

    blank = pdfium.PdfDocument.new()
    blank.new_page(612, 792)
    buf = _io.BytesIO()
    blank.save(buf)
    blank.close()
    pages, notes = bdt.extract_pages(buf.getvalue())
    assert pages == []
    assert notes, "空版面必须有 note，不能静默"
