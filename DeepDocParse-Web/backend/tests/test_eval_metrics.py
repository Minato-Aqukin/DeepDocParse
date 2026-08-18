"""评测脚本的判定逻辑（A1）。

评测本身也是代码，也会错 —— 而**会高估的指标比没有指标更糟**：
它让人以为已经度量过了。这个文件盯的就是"指标能不能真的变红"。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_eval():
    spec = importlib.util.spec_from_file_location(
        "eval_citations", ROOT / "scripts" / "eval_citations.py")
    module = importlib.util.module_from_spec(spec)
    # 必须先进 sys.modules 再 exec：@dataclass 会用 sys.modules[cls.__module__]
    # 去解析类型注解，模块不在表里就直接 AttributeError
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_eval()
LAYOUT = json.loads((FIXTURES / "layout-long-doc.json").read_text(encoding="utf-8"))


def test_anchor_resolves_to_one_original_block_not_the_whole_page():
    """ground truth 必须是**原始块**，不能是整页。

    曾经这里走 `layout_to_chunks(max_chars=100000)` 取 ground truth —— 那个上限
    把整页并成一个 chunk，bbox 就是整片版心，于是同页任何出处都必然"覆盖"它，
    **bbox 指标恒等于页码指标**，永远不会独立变红（2026-08-18 验收抓到）。
    """
    located = ev._anchor_bbox(LAYOUT, "launch code of project Zephyr")
    assert located is not None
    page_idx, bbox = located
    assert page_idx == 2

    page = LAYOUT["pdf_info"][page_idx]
    page_width, page_height = page["page_size"]
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    assert area < page_width * page_height * 0.25, \
        f"ground truth 覆盖了整页的 {area / (page_width * page_height):.0%}，那是整片版心不是一个块"


def test_bbox_metric_can_fail_while_page_metric_passes():
    """同一页里指错块，页码算命中、bbox 必须算不命中。

    两列数字永远相同的指标就是页码指标的影子。
    """
    page_idx, want = ev._anchor_bbox(LAYOUT, "launch code of project Zephyr")
    others = [b["bbox"] for b in LAYOUT["pdf_info"][page_idx]["para_blocks"]
              if ev._coverage(want, b["bbox"]) < ev.MIN_BBOX_COVERAGE]
    assert others, "这一页只有一个块，换个锚点再测"

    sample = {"id": "t", "question": "q", "attributes": [],
              "expect": {"answerable": True, "page_idx": page_idx, "bbox": want}}
    outcome = ev.judge(sample, pages=[page_idx], bboxes=[others[0]], answer="",
                       degraded=None, layout=None, any_citation=False)
    assert outcome.page_hit is True
    assert outcome.bbox_hit is False


def test_bbox_metric_passes_on_the_right_block():
    page_idx, want = ev._anchor_bbox(LAYOUT, "launch code of project Zephyr")
    sample = {"id": "t", "question": "q", "attributes": [],
              "expect": {"answerable": True, "page_idx": page_idx, "bbox": want}}
    outcome = ev.judge(sample, pages=[page_idx], bboxes=[want], answer="",
                       degraded=None, layout=None, any_citation=False)
    assert outcome.page_hit is True and outcome.bbox_hit is True


def test_wrong_page_never_counts_as_a_bbox_hit():
    """不同页的块坐标当然可能重叠（版式一样）。往好里错的指标不能要。"""
    page_idx, want = ev._anchor_bbox(LAYOUT, "launch code of project Zephyr")
    sample = {"id": "t", "question": "q", "attributes": [],
              "expect": {"answerable": True, "page_idx": page_idx, "bbox": want}}
    outcome = ev.judge(sample, pages=[page_idx + 1], bboxes=[want], answer="",
                       degraded=None, layout=None, any_citation=False)
    assert outcome.page_hit is False and outcome.bbox_hit is False


@pytest.mark.parametrize("pages,degraded,answer,expected", [
    ([], None, "随便说点什么", True),                 # 零出处 = 拒答了
    ([1], "no_hits", "", True),                       # 检索层判定无命中
    ([1], None, "文档中未找到相关内容", True),         # 给了出处但话说清楚了
    ([1], None, "答案是 42", False),                  # 凭空给出处 —— 比不回答更糟
])
def test_refusal_judgement(pages, degraded, answer, expected):
    sample = {"id": "t", "question": "q", "attributes": [], "expect": {"answerable": False}}
    outcome = ev.judge(sample, pages=pages, bboxes=[None] * len(pages), answer=answer,
                       degraded=degraded, layout=None, any_citation=False)
    assert outcome.refusal_ok is expected
