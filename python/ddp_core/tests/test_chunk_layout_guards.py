"""分块/版面守卫：hang-guard、畸形坐标跳过、[0,0] 拒收。"""
import pytest

from ddp_core.application import layout as layout_app
from ddp_core.chunking import _adjacent, _furniture_key, _line_height, _split_oversized, layout_to_chunks


def _page(page_size, blocks):
    return {"page_idx": 0, "page_size": page_size, "para_blocks": blocks}


def _block(text, bbox):
    return {"type": "text", "bbox": bbox,
            "lines": [{"spans": [{"content": text}]}]}


def _layout(text="正文内容足够长可以切分测试文本内容", bbox=None, page_size=None):
    block = _block(text, bbox or [10, 100, 590, 120])
    return {"pdf_info": [_page(page_size or [612, 792], [block])]}


def test_split_oversized_rejects_non_positive_max_chars():
    with pytest.raises(ValueError):
        _split_oversized("x" * 10, 0)
    with pytest.raises(ValueError):
        _split_oversized("x" * 10, -3)


def test_layout_to_chunks_rejects_non_positive_max_chars():
    with pytest.raises(ValueError):
        layout_to_chunks(_layout(), max_chars=0)
    with pytest.raises(ValueError):
        layout_to_chunks(_layout(), max_chars=-1)


def test_layout_to_chunks_still_splits_long_block():
    chunks = layout_to_chunks(_layout(text="汉" * 100), max_chars=10)
    assert len(chunks) > 1
    assert all(c["char_len"] <= 10 + len("标题") for c in chunks)


def test_oversized_prose_keeps_complete_sentences_together():
    sentences = [
        "The active sensors sample each incoming channel.",
        "Only the selected buffer accepts writes during the transfer.",
        "Other buffers retain their previous contents.",
    ]
    body = " ".join(sentences)
    chunks = layout_to_chunks(_layout(text=body), max_chars=80)
    texts = [chunk["text"] for chunk in chunks]
    assert all(any(sentence in text for text in texts) for sentence in sentences)
    assert " ".join(texts) == body
    assert all(len(text) <= 80 for text in texts)


@pytest.mark.parametrize("value,max_chars", [
    ("3.1415926535897932384626433832795028841971", 64),
    ("3.14159", 38),  # The period is exactly at the end of the candidate window.
])
def test_oversized_prose_does_not_treat_decimal_point_as_sentence_end(value, max_chars):
    body = f"Measured output remains in the range {value} units."
    pieces = _split_oversized(body, max_chars=max_chars)
    assert any(value in piece for piece in pieces)
    assert " ".join(pieces) == body
    assert all(len(piece) <= max_chars for piece in pieces)


@pytest.mark.parametrize("bbox", [None, [], [1, 2, 3], [1, 2, "x", 4], [1, 2, float("nan"), 4]])
@pytest.mark.parametrize("page_size", [[612, 792], [0, 0], None])
def test_furniture_key_skips_malformed_geometry(bbox, page_size):
    page = _page(page_size, [])
    assert _furniture_key(_block("页脚文字", bbox), page) is None


def test_furniture_key_skips_zero_height_page():
    page = _page([612, 0], [])
    assert _furniture_key(_block("页脚文字", [10, 10, 590, 20]), page) is None


def test_line_height_tolerates_malformed_bbox():
    assert _line_height(_block("t", [1, 2])) == 0.0
    assert _line_height(_block("t", [1, 2, "x", 4])) == 0.0
    assert _line_height(_block("t", None)) == 0.0


def test_adjacent_treats_malformed_bbox_as_not_adjacent():
    prev = _block("上一段", [10, 100, 590, 120])
    bad = _block("下一段", [10])
    assert _adjacent(prev, bad) is False
    assert _adjacent(bad, prev) is False


def test_layout_to_chunks_tolerates_malformed_bbox():
    chunks = layout_to_chunks(_layout(bbox=[10]))
    assert len(chunks) == 1


def test_build_pages_rejects_missing_or_zero_page_size():
    with pytest.raises(ValueError):
        layout_app.build_pages([{"page_idx": 0, "para_blocks": []}], engine="t")
    with pytest.raises(ValueError):
        layout_app.build_pages([{"page_idx": 0, "page_size": [0, 0], "para_blocks": []}],
                               engine="t")
    with pytest.raises(ValueError):
        layout_app.build_pages([{"page_idx": 0, "page_size": [612, -1], "para_blocks": []}],
                               engine="t")
    with pytest.raises(ValueError):
        layout_app.build_pages([{"page_idx": 0, "page_size": "612x792", "para_blocks": []}],
                               engine="t")


def test_build_pages_keeps_valid_page_size_and_validates_clean():
    built = layout_app.build_pages(
        [{"page_idx": 0, "page_size": [612, 792], "para_blocks": []}], engine="t")
    assert built["pdf_info"][0]["page_size"] == [612, 792]
    assert layout_app.validate(built) == []
