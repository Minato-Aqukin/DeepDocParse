"""Real CropBox and rotation cases: extracted anchors must hit visible glyphs."""

import io
import subprocess
import sys

import pytest
from PIL import Image, ImageChops

from ddp_core.application.borndigital import extract_pages
from ddp_core.crops import render_crop
from ddp_paths import FIXTURES


def cropped_pdf(rotation):
    stream = (
        b"BT /F1 12 Tf 100 200 Td (VISIBLE) Tj ET\n"
        b"BT /F1 12 Tf 10 20 Td (OUTSIDE) Tj ET\n"
        b"BT /F1 12 Tf 330 350 Td (CLIPPED_MARKER) Tj ET"
    )
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            f"/CropBox[50 100 350 500]/Rotate {rotation}"
            f"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        ).encode(),
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        result += f"{offset:010d} 00000 n \n".encode()
    result += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
        f"startxref\n{xref}\n%%EOF"
    ).encode()
    return bytes(result)


@pytest.mark.parametrize(
    "rotation,corner", [(0, (True, False)), (90, (True, True)),
                        (180, (False, True)), (270, (False, False))]
)
def test_cropbox_translation_rotation_and_visible_intersection(rotation, corner):
    pdf = cropped_pdf(rotation)
    page = extract_pages(pdf)[0]
    width, height = page["page_size"]
    assert (width, height) == ((400, 300) if rotation in (90, 270) else (300, 400))
    text = " ".join(block["text"] for block in page["blocks"])
    assert "OUTSIDE" not in text and "CLIPPED_MARKER" not in text
    assert "CLI" in text, "retain text inside the crop even when a line crosses the edge"
    for block in page["blocks"]:
        x0, y0, x1, y1 = block["bbox"]
        assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
    for block in page["blocks"]:
        if block["text"] == "VISIBLE":
            x0, y0, _, _ = block["bbox"]
            assert (x0 < width / 2, y0 < height / 2) == corner
            png = render_crop(pdf, 0, block["bbox"], page["page_size"])
            assert png is not None
            image = Image.open(io.BytesIO(png)).convert("RGB")
            ink = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
            assert ink.getbbox() is not None, "anchor crop must contain the rendered source text"
            break
    else:
        pytest.fail("the complete visible marker was lost")


def test_parse_and_render_share_pdfium_lock_in_one_process():
    # PDFium corruption kills its process; isolate this regression from pytest.
    program = """
import asyncio, pathlib
from ddp_core.application.borndigital import extract_pages
from ddp_core.crops import render_page

pdf = pathlib.Path(sys.argv[1]).read_bytes()
async def main():
    results = await asyncio.gather(*[
        asyncio.to_thread(extract_pages, pdf),
        asyncio.to_thread(render_page, pdf, 0, 2.0),
        asyncio.to_thread(extract_pages, pdf),
        asyncio.to_thread(render_page, pdf, 1, 2.0),
    ])
    assert len(results[0]) == len(results[2]) == 5
    assert results[1] and results[3]
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", "import sys\n" + program, str(FIXTURES / "long-doc.pdf")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
