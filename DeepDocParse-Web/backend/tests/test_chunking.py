"""分块规则：出处定位的地基，改动前先看这里为什么这么切。

分块逻辑在本层（ADR #16，与 service 解耦），所以 mineru 版面格式的假设必须由测试固化：
mineru 升级后如果 middle_json 结构变了，这个文件先红。
"""
from app.chunking import layout_to_chunks, page_count_of


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


def test_empty_layout_is_not_an_error():
    assert layout_to_chunks({}) == []
    assert page_count_of({}) == 0
    assert page_count_of({"pdf_info": [{}, {}]}) == 2


def test_seq_is_document_wide():
    layout = {"pdf_info": [_page(0, [("a" * 100, [0, 0, 1, 1])]),
                           _page(1, [("b" * 100, [0, 0, 1, 1])])]}
    chunks = layout_to_chunks(layout, max_chars=50)
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
