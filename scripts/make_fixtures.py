"""生成 e2e 用的测试素材（确定性，可重复）。

产物写入 tests/fixtures/：
  long-doc.pdf   5 页文本 PDF，第 3/5 页各埋一条唯一事实，用于验证检索定位与出处页码
  vqa-test.png   含文字的图片，用于验证图片直答路径
  contract.pdf   2 页合同样本，字段值与表格行**已知**，用于抽取评测（DDP-Extract）
  code-corpus.pdf 24 页代码密集小集，公开基准缺口的自建回归集

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

    return _write_pdf(page_lines, leading=34)


def _write_pdf(page_lines: list[list[str]], *, leading: int = 34,
               font: str = "Helvetica") -> bytes:
    """把每页的文本行写成一份合法 PDF（不引入额外依赖）。

    **xref 表不可省**：缺 xref 的 PDF 解析器只能读出第一页，
    表现为 mineru 只返回 1 页版面、检索永远找不到后面页的事实（真踩过）。
    """
    pages = len(page_lines)
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
            # PDF literal string：反斜杠与括号必须转义。旧实现直接删除，
            # 对代码/公式样本会把原文真值悄悄改掉。
            safe = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            parts.append(f"BT /F1 12 Tf 60 {y} Td ({safe}) Tj ET")
            y -= leading
        stream = "\n".join(parts).encode()
        objects[content_nums[i]] = (b"<</Length " + str(len(stream)).encode() + b">>stream\n"
                                    + stream + b"\nendstream")
    objects[font_num] = f"<</Type/Font/Subtype/Type1/BaseFont/{font}>>".encode()

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


# 抽取评测的真值。**放在这里而不是评测数据集里**：PDF 由本脚本生成，
# 真值和生成它的代码放在一起才不会漂 —— 改了合同内容忘了改期望值，
# 表现会是"抽取准确率突然掉了"，而实际上是评测集过期了。
# 注意故意**没有违约金条款**：那是空值正确率指标的样本
# （"文档里确实没有"必须报 not_found，而不是编一个数）。
CONTRACT_FIELDS = {
    "buyer_name": "Northwind Trading Company Limited",
    "seller_name": "Contoso Manufacturing Group",
    "contract_no": "NW-2026-0817",
    "signed_at": "2026-03-14",
    "total_amount": 486200.5,
    "currency": "USD",
    "payment_days": 45,
}
# 第 2 页的货品表。表格行 = 多记录抽取（顶层 array）的真值
CONTRACT_ITEMS = [
    {"item": "Industrial bearing 6204", "quantity": 1200, "unit_price": 18.5},
    {"item": "Hydraulic seal kit HS-9", "quantity": 340, "unit_price": 62.0},
    {"item": "Precision shaft PS-220", "quantity": 75, "unit_price": 410.4},
]


def build_contract_pdf() -> bytes:
    """两页合同样本：第 1 页是字段，第 2 页是货品表。

    刻意用英文：这个 PDF 由本脚本手写，字体是 base-14 的 Helvetica，
    塞中文进去 pypdfium2 抽出来会是乱码 —— 而抽取评测要的是**可复现的真值**，
    不是好看的样本。真实中文文档的抽取质量要靠真实语料评，不是靠这个 fixture。
    """
    return _write_pdf([_contract_page1(CONTRACT_FIELDS), _contract_page2()], leading=26)


def _contract_page1(f: dict) -> list[str]:
    return [
        "PURCHASE AGREEMENT",
        "",
        f"Contract No.: {f['contract_no']}",
        f"Date of signature: {f['signed_at']}",
        "",
        f"Buyer: {f['buyer_name']}",
        "Registered address: 88 Harbour Road, Wellington",
        "",
        f"Seller: {f['seller_name']}",
        "Registered address: 21 Foundry Street, Sheffield",
        "",
        "Article 1 - Price",
        f"The total contract price is {f['currency']} {f['total_amount']:,.2f},",
        "inclusive of all taxes and delivery charges.",
        "",
        "Article 2 - Payment",
        f"The Buyer shall pay the full amount within {f['payment_days']} days",
        "after receipt of the goods and a valid invoice.",
        "",
        "Article 3 - Delivery",
        "Delivery shall be made to the Buyer's warehouse in Wellington.",
    ]


def _contract_page2() -> list[str]:
    lines = ["Schedule A - Goods", "", "Item                       Quantity   Unit price"]
    for row in CONTRACT_ITEMS:
        lines.append(f"{row['item']:<26} {row['quantity']:>8} {row['unit_price']:>11.2f}")
    return lines + ["", "End of Schedule A."]


def contract_truth() -> dict:
    """识别评测的真值（scripts/eval_ocr.py 读它）。

    **真值来自生成 PDF 的那份源文本，不是解析器的输出。**
    这一点很要紧：`../DeepDocParse-Web/docs/EVAL.md` 里已经点名过一次同类问题 ——
    出处评测的 bbox 真值终究来自解析器本身，所以那个数字只能说明"检索指对了块"，
    不能说明"解析得准"。识别评测要是也拿解析器输出当真值，就是纯粹的自我印证。
    这里的合同 PDF 由本脚本写出，源文本就是绝对真值。
    """
    f, page1, page2 = CONTRACT_FIELDS, [], []
    for line in _contract_page1(f):
        if line:
            page1.append(line)
    for line in _contract_page2():
        if line:
            page2.append(line)
    # 表格真值用 HTML 表达（与 DDP-Layout 的 table_html 同一形态）
    rows = "".join(
        f"<tr><td>{r['item']}</td><td>{r['quantity']}</td>"
        f"<td>{r['unit_price']:.2f}</td></tr>" for r in CONTRACT_ITEMS)
    table = ("<table><tr><td>Item</td><td>Quantity</td><td>Unit price</td></tr>"
             + rows + "</table>")
    return {
        "attributes": ["英文单栏", "born-digital", "合同"],
        "pages": [
            {"page_idx": 0, "text": "\n".join(page1)},
            # born-digital 不做表格识别（它没有版面模型），这一页的表格分必然是 0 ——
            # **这正是要量的东西**：换成 mineru / vlm-ocr 之后这个数字应该起来
            {"page_idx": 1, "text": "\n".join(page2), "tables": [table]},
        ],
    }


# 公开文档解析基准几乎不量代码块。这里自建 24 页，覆盖驼峰、下划线、点号、
# 命名空间、路径、泛型与 shell flag；每页一个唯一标识符，方便阶段 5 做精确查询回归。
CODE_IDENTIFIERS = [
    "HttpRequestParser", "parse_job_id", "registry.default_of", "DDP_CORE_PATH",
    "std::vector<Result>", "com.example.deepdoc.Indexer", "--skip-special-tokens",
    "QA_PARSE_MISMATCH_THRESHOLD", "loadCitationTargets", "evidence.content_digest",
    "asyncio.to_thread", "DocumentUpload.uploaded_by", "uq_documents_doc_origin",
    "VLLM_USE_FLASHINFER_SAMPLER", "SearchIndex.min_similarity", "layout_to_chunks",
    "code_detection_unavailable", "CitationRole.REJECTED", "graph_neighbors",
    "WikiSentence.evidence_ids", "RRF_K", "EXTRACT_MAX_RECORD_BLOCKS",
    "get_evidence", "entity_merge_uncertain",
]


def _code_page(index: int, identifier: str) -> list[str]:
    return [
        f"// DeepDocParse code benchmark page {index + 1:02d}",
        "from __future__ import annotations",
        "",
        f"TARGET_IDENTIFIER = \"{identifier}\"",
        "",
        "async def resolve_document(document_id: str, *, min_score: float = 0.55):",
        "    candidates = await index.search(document_id, min_similarity=min_score)",
        "    for candidate in candidates:",
        "        if candidate.evidence_id and candidate.bbox:",
        "            yield {\"id\": candidate.evidence_id, \"bbox\": candidate.bbox}",
        "",
        "class EvidenceCompiler(Generic[T]):",
        "    def compile_atom(self, atom: T) -> tuple[str, list[float]]:",
        "        provider = f\"{self.engine.name}:{self.engine.version}\"",
        "        return provider, [atom.x0, atom.y0, atom.x1, atom.y1]",
        "",
        "# Exact lookup must preserve snake_case, CamelCase, dotted.names, and --flags.",
        f"assert normalize_identifier(TARGET_IDENTIFIER)  # {identifier}",
    ]


def build_code_corpus_pdf() -> bytes:
    return _write_pdf([_code_page(i, identifier)
                       for i, identifier in enumerate(CODE_IDENTIFIERS)],
                      leading=20, font="Courier")


def code_corpus_truth() -> dict:
    return {
        "attributes": ["代码密集", "born-digital", "自建代码基准"],
        "pages": [{"page_idx": i, "text": "\n".join(_code_page(i, identifier)),
                   "identifier": identifier}
                  for i, identifier in enumerate(CODE_IDENTIFIERS)],
    }


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
    import json

    contract = FIXTURES / "contract.pdf"
    contract.write_bytes(build_contract_pdf())
    truth = FIXTURES / "contract.truth.json"
    truth.write_text(json.dumps(contract_truth(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    print(f"wrote {contract} ({contract.stat().st_size} bytes) + {truth.name}")
    code_pdf = FIXTURES / "code-corpus.pdf"
    code_truth = FIXTURES / "code-corpus.truth.json"
    code_pdf.write_bytes(build_code_corpus_pdf())
    code_truth.write_text(json.dumps(code_corpus_truth(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"wrote {code_pdf} ({code_pdf.stat().st_size} bytes) + {code_truth.name}")
    pdf = FIXTURES / "long-doc.pdf"
    png = FIXTURES / "vqa-test.png"
    pdf.write_bytes(build_long_pdf())
    png.write_bytes(build_test_png())
    print(f"wrote {pdf} ({pdf.stat().st_size} bytes)")
    print(f"wrote {png} ({png.stat().st_size} bytes)")
