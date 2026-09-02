"""分块规则：出处定位的地基，改动前先看这里为什么这么切。

分块逻辑在本层（ADR #16，与 service 解耦），所以版面格式的假设必须由测试固化。

**下面大部分用例用的是 `_page()` 合成的样本**——合成样本测得了分块规则，
但测不出上游格式漂移（它永远长成我们以为的样子）。真实格式由
`tests/fixtures/layout-*.json` 里的**真机产物**固化，见本文件末尾那组用例。
格式本身的契约写在 ../DeepDocParse/docs/layout-format.md。
"""
import json
from pathlib import Path

from ddp_core.chunking import layout_to_chunks, page_count_of

from ddp_paths import FIXTURES

FIXTURES = FIXTURES


def _page(page_idx: int, blocks: list[tuple[str, list]]) -> dict:
    return {
        "page_idx": page_idx,
        "page_size": [612, 792],
        "para_blocks": [{"bbox": bbox, "lines": [{"spans": [{"content": text}]}]}
                        for text, bbox in blocks],
    }


def test_chunks_never_span_pages():
    """出处必须能落到唯一页码，所以 chunk 绝不能跨页——哪怕两页都很短。"""
    layout = {"pdf_info": [_page(0, [("短", [0, 0, 10, 10])]),
                           _page(1, [("也短", [0, 0, 10, 10])])]}
    chunks = layout_to_chunks(layout, max_chars=10_000)
    assert [c["page_idx"] for c in chunks] == [0, 1]


def test_merges_until_limit_and_unions_bbox():
    layout = {"pdf_info": [_page(0, [("A" * 30, [10, 10, 50, 20]),
                                     ("B" * 30, [12, 30, 80, 40]),
                                     ("C" * 30, [0, 60, 20, 70])])]}
    chunks = layout_to_chunks(layout, max_chars=70)
    assert len(chunks) == 2
    # 前两块合并，bbox 取外接矩形
    assert chunks[0]["bbox"] == [10, 10, 80, 40]
    assert chunks[0]["text"].startswith("A" * 30)
    assert chunks[1]["bbox"] == [0, 60, 20, 70]


def test_carries_page_size_for_cropping():
    """缺 page_size 会让裁剪在 CropBox 偏移/旋转页上裁错区域，必须随块带走。"""
    layout = {"pdf_info": [_page(3, [("正文", [1, 2, 3, 4])])]}
    chunk = layout_to_chunks(layout)[0]
    assert chunk["page_size"] == [612, 792] and chunk["page_idx"] == 3
    assert chunk["char_len"] == len(chunk["text"])


def test_tolerates_missing_fields():
    """版面数据不完整时仍要出块（只是不能裁剪），不能整篇文档变成不可检索。"""
    layout = {"pdf_info": [
        {"page_idx": 0, "para_blocks": [
            {"lines": [{"spans": [{"content": "无 bbox 的块"}]}]},
            {"bbox": [0, 0, 1, 1], "lines": [{"spans": [{}]}]},          # 空内容，跳过
            {"bbox": [0, 0, 1, 1]},                                       # 无 lines，跳过
        ]},
    ]}
    chunks = layout_to_chunks(layout)
    assert len(chunks) == 1
    assert chunks[0]["bbox"] is None and chunks[0]["page_size"] is None


def test_single_oversized_block_is_split():
    """回归：单块超过 max_chars 必须切开。

    不切的话它会被原样送进 embedding 运行时，由后者按模型最大长度静默截断——
    块尾内容从此检索不到，而且没有任何报错。
    """
    body = "这是一段很长的正文。" * 200          # 2000 字，远超 max_chars
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"bbox": [10, 10, 100, 500], "lines": [{"spans": [{"content": body}]}]}]}]}

    chunks = layout_to_chunks(layout, max_chars=300)

    assert len(chunks) > 1, "超长块没有被切开"
    assert all(c["char_len"] <= 300 for c in chunks), \
        f"切完仍有超限块：{[c['char_len'] for c in chunks]}"
    # 内容不能丢：拼回去要覆盖原文的全部字符
    assert "".join(c["text"] for c in chunks).replace("\n", "") == body
    # 出处三件套要跟着每一段走，否则切出来的块无法定位
    assert all(c["page_idx"] == 0 and c["bbox"] == [10, 10, 100, 500]
               and c["page_size"] == [612, 792] for c in chunks)
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))


def test_oversized_block_prefers_sentence_boundaries():
    """能在句读处断就别硬切 —— 硬切会把一句话劈成两半，检索与出处都变难看。"""
    body = "。".join(f"第{i}句话内容" for i in range(60)) + "。"
    chunks = layout_to_chunks(
        {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
            {"bbox": [0, 0, 1, 1], "lines": [{"spans": [{"content": body}]}]}]}]},
        max_chars=120)

    assert len(chunks) > 1
    assert all(c["text"].endswith("。") for c in chunks), \
        f"应当在句号处断开：{[c['text'][-12:] for c in chunks]}"


def test_no_split_when_block_fits():
    """回归护栏：没超限的块不许被切 —— 否则出处粒度会平白变碎。"""
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"bbox": [0, 0, 1, 1], "lines": [{"spans": [{"content": "短短一句话。"}]}]}]}]}
    assert [c["text"] for c in layout_to_chunks(layout, max_chars=300)] == ["短短一句话。"]


def test_empty_layout_is_not_an_error():
    assert layout_to_chunks({}) == []
    assert page_count_of({}) == 0
    assert page_count_of({"pdf_info": [{}, {}]}) == 2


def test_seq_is_document_wide():
    layout = {"pdf_info": [_page(0, [("a" * 100, [0, 0, 1, 1])]),
                           _page(1, [("b" * 100, [0, 0, 1, 1])])]}
    chunks = layout_to_chunks(layout, max_chars=50)
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# 真实版面样本：合成样本测不出上游格式漂移，这组用例才是"格式变了先红"的那一道
# ---------------------------------------------------------------------------

REAL_LAYOUT = json.loads((FIXTURES / "layout-long-doc.json").read_text(encoding="utf-8"))
# 样本来源：DeepDocParse/tests/fixtures/long-doc.pdf（make_fixtures.py 生成的 5 页文本 PDF），
# 由 **born-digital** 引擎真实解析产出。埋点事实在第 3 页（page_idx=2）。
#
# **它盯得住 born-digital 的格式漂移，盯不住 mineru 的**：那需要一份真实 mineru
# middle_json 样本，而 mineru 要 GPU，本机产不出来。有 GPU 的机器上补一份
# `layout-<name>.json`（`"engine": "mineru"`）放进这个目录，下面的用例参数化一下即可。
# 在那之前，别把这组用例当成"mineru 升级会先红"的保证。
FACT_ANCHOR = "The launch code of project Zephyr"


def test_real_layout_still_has_every_promised_field():
    """契约承诺的字段在真机产物里一个不少。

    这些字段任何一个消失，出处定位就会以某种安静的方式坏掉：
    没有 page_idx 就落不到页，没有 page_size 就裁错区域，没有 spans[].content
    就没有可检索的文本。所以这里逐个盯死，而不是只看"能不能跑通"。
    """
    assert REAL_LAYOUT["pdf_info"], "版面里一页都没有"
    for page in REAL_LAYOUT["pdf_info"]:
        assert isinstance(page["page_idx"], int)
        assert len(page["page_size"]) == 2 and all(v > 0 for v in page["page_size"])
        for block in page["para_blocks"]:
            assert len(block["bbox"]) == 4
            assert any(span.get("content")
                       for line in block["lines"] for span in line["spans"])


def test_real_layout_chunks_keep_facts_on_the_right_page():
    """真实版面切出来的块，埋点事实必须落在它真正所在的那一页。

    页码错了整套"可验证出处"就是假的 —— 而这种错误不会抛任何异常。
    """
    chunks = layout_to_chunks(REAL_LAYOUT, max_chars=800)
    assert page_count_of(REAL_LAYOUT) == 5
    hits = [c["page_idx"] for c in chunks if FACT_ANCHOR in c["text"]]
    assert hits == [2], f"事实应只出现在第 3 页，实际 {hits}"


def test_real_layout_chunks_are_croppable():
    """每个块都要带得出 bbox 与 page_size，否则"出处截图"这条路走不通。"""
    chunks = layout_to_chunks(REAL_LAYOUT, max_chars=800)
    assert chunks
    assert all(c["bbox"] and len(c["bbox"]) == 4 for c in chunks)
    assert all(c["page_size"] for c in chunks)
    # bbox 必须落在页内（左上原点、y 向下）
    for chunk in chunks:
        width, height = chunk["page_size"]
        x0, y0, x1, y1 = chunk["bbox"]
        assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height, chunk["bbox"]
