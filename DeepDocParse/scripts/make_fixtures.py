"""生成 e2e 用的测试素材（确定性，可重复）。

产物写入 tests/fixtures/：
  long-doc.pdf   5 页文本 PDF，第 3/5 页各埋一条唯一事实，用于验证检索定位与出处页码
  vqa-test.png   含文字的图片，用于验证图片直答路径

用法：python scripts/make_fixtures.py
"""
import random
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# 埋点事实：页索引 -> 内容（检索必须能定位到，且出处页码要对得上）
FACTS = {
    2: "The launch code of project Zephyr is 8712.",
    4: "The annual revenue of Acme Corp reached 42 million dollars in 2025.",
}
FILLER_WORDS = "system data engine result vector index chunk page block layout parse".split()


def build_long_pdf(pages: int = 5) -> bytes:
    """手写多页文本 PDF（不引入额外依赖），带合法 xref 表。

    注意：**xref 表不可省**。缺 xref 的 PDF 解析器只能读出第一页，
    表现为 mineru 只返回 1 页版面、检索永远找不到后面页的事实（真踩过）。
    """
    random.seed(7)  # 固定种子：同样的输入永远得到同样的 PDF
    page_lines: list[list[str]] = []
    for p in range(pages):
        lines = [f"Page {p + 1} section heading"]
        for i in range(18):
            lines.append(" ".join(random.choices(FILLER_WORDS, k=10)) + f" line {i}.")
        if p in FACTS:
            lines.insert(9, FACTS[p])
        page_lines.append(lines)

    # 对象编号：1=Catalog 2=Pages，之后每页占两个（page/content），最后是字体
    page_nums = [3 + 2 * i for i in range(pages)]
    content_nums = [4 + 2 * i for i in range(pages)]
    font_num = 3 + 2 * pages

    objects: dict[int, bytes] = {1: b"<</Type/Catalog/Pages 2 0 R>>"}
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objects[2] = f"<</Type/Pages/Kids[{kids}]/Count {pages}>>".encode()
    for i, lines in enumerate(page_lines):
        objects[page_nums[i]] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            f"/Contents {content_nums[i]} 0 R"
            f"/Resources<</Font<</F1 {font_num} 0 R>>>>>>"
        ).encode()
        y = 740
        parts = []
        for ln in lines:
            safe = ln.replace("(", "").replace(")", "")
            parts.append(f"BT /F1 12 Tf 60 {y} Td ({safe}) Tj ET")
            y -= 34
        stream = "\n".join(parts).encode()
        objects[content_nums[i]] = (b"<</Length " + str(len(stream)).encode() + b">>stream\n"
                                    + stream + b"\nendstream")
    objects[font_num] = b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj".encode() + objects[num] + b"endobj\n"

    xref_offset = len(out)
    last = max(objects)
    out += f"xref\n0 {last + 1}\n".encode()
    out += b"0000000000 65535 f \n"          # 每条 xref 记录必须恰好 20 字节
    for num in range(1, last + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (f"trailer<</Size {last + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref_offset}\n%%EOF\n").encode()
    return bytes(out)


# PIL 默认字体是 ~11px 位图字体，OCR 完全读不出（踩过：VQA 一直返回乱码）。
# 必须用 TrueType 且字号够大；按平台依次尝试。
FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def build_test_png() -> bytes:
    """含文字的 PNG（VQA 直答路径的输入）。Pillow 是 mcp_server 的既有依赖。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    font = None
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, 48)
            break
        except OSError:
            continue
    if font is None:
        raise RuntimeError(f"找不到可用 TrueType 字体，尝试过：{FONT_CANDIDATES}")

    img = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 50), "DeepDocParse VQA test", fill="black", font=font)
    draw.text((40, 140), "Answer: 42", fill="black", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    pdf = FIXTURES / "long-doc.pdf"
    png = FIXTURES / "vqa-test.png"
    pdf.write_bytes(build_long_pdf())
    png.write_bytes(build_test_png())
    print(f"wrote {pdf} ({pdf.stat().st_size} bytes)")
    print(f"wrote {png} ({png.stat().st_size} bytes)")
