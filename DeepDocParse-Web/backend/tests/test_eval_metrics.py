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


def _load_extraction_eval():
    spec = importlib.util.spec_from_file_location(
        "eval_extraction_metrics", ROOT / "scripts" / "eval_extraction.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_eval()
extract_ev = _load_extraction_eval()
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


def test_live_ground_truth_loader_keeps_text_anchor_bbox_measurable():
    layout = ev._ground_truth_layout({
        "kind": "local", "path": "../DeepDocParse/tests/fixtures/long-doc.pdf",
    })
    assert layout is not None
    assert ev._anchor_bbox(layout, "launch code of project Zephyr") is not None


def test_offline_keyword_rank_uses_the_production_cjk_tokenizer():
    from ddp_core.tokenize import tokenized

    chunks = [
        {"page_idx": 0, "text": "无关英文内容", "text_tokenized": tokenized("无关英文内容")},
        {"page_idx": 6, "text": "浦发银行的不良率最高，为0.58%",
         "text_tokenized": tokenized("浦发银行的不良率最高，为0.58%")},
    ]
    ranked = ev._keyword_rank("哪家银行的住房按揭贷款不良率最高？", chunks)
    assert ranked and ranked[0]["page_idx"] == 6


def test_identifier_report_measures_same_set_before_and_after_code_route():
    chunks = [
        {"page_idx": 0, "bbox": [0, 0, 90, 30], "block_type": "code",
         "text": "# preserve fetchUser when rendering a long explanatory comment",
         "text_tokenized": "preserve fetchuser rendering explanatory comment"},
        {"page_idx": 1, "bbox": [0, 40, 90, 60], "block_type": "code",
         "text": "def fetchUser(user_id):", "text_tokenized": "def fetchuser user id"},
    ]
    question = "Where is fetchUser defined?"
    before = ev._keyword_rank(question, chunks, code_boost=False)
    after = ev._keyword_rank(question, chunks)
    assert before[0]["page_idx"] == 0
    assert after[0]["page_idx"] == 1

    outcome = ev.Outcome("code", ["标识符精确查询"], True,
                         page_hit=True, bbox_hit=True,
                         legacy_page_hit=False, legacy_bbox_hit=False)
    report = ev.render([outcome], "offline")
    assert "同集合改造前后" in report
    assert "+100.0%" in report


def test_wrong_page_never_counts_as_a_bbox_hit():
    """不同页的块坐标当然可能重叠（版式一样）。往好里错的指标不能要。"""
    page_idx, want = ev._anchor_bbox(LAYOUT, "launch code of project Zephyr")
    sample = {"id": "t", "question": "q", "attributes": [],
              "expect": {"answerable": True, "page_idx": page_idx, "bbox": want}}
    outcome = ev.judge(sample, pages=[page_idx + 1], bboxes=[want], answer="",
                       degraded=None, layout=None, any_citation=False)
    assert outcome.page_hit is False and outcome.bbox_hit is False


def test_omnidocbench_citation_slices_are_disjoint_and_ten_pages_each():
    dataset = json.loads(
        (ROOT / "eval" / "omnidocbench-citations-v1.6.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT.parent / "DeepDocParse" / "eval" / "omnidocbench-v1.6-slices.json")
        .read_text(encoding="utf-8"))
    by_slice = {}
    for sample in dataset["samples"]:
        source = sample["source"]
        by_slice.setdefault(source["slice"], []).append(sample)
    assert set(by_slice) == set(manifest["slices"])
    assert len({sample["source"]["image_path"] for sample in dataset["samples"]}) == 40
    for slice_name, images in manifest["slices"].items():
        samples = by_slice[slice_name]
        assert len(samples) == 10
        assert [sample["source"]["image_path"] for sample in samples] == images
        assert [sample["expect"]["page_idx"] for sample in samples] == list(range(10))
    assert all(len(sample["expect"].get("bbox") or []) == 4
               for sample in by_slice["图表引用"])


def test_code_layout_golden_matches_all_generated_identifiers():
    layout = json.loads((FIXTURES / "layout-code-corpus.json").read_text(encoding="utf-8"))
    truth = json.loads(
        (ROOT.parent / "DeepDocParse" / "tests" / "fixtures" / "code-corpus.truth.json")
        .read_text(encoding="utf-8"))
    assert len(layout["pdf_info"]) == len(truth["pages"]) == 24
    assert layout["code_detection"] == "heuristic"
    for expected in truth["pages"]:
        located = ev._anchor_bbox(layout, expected["identifier"])
        assert located is not None
        assert located[0] == expected["page_idx"]
        page = layout["pdf_info"][expected["page_idx"]]
        assert any(block["type"] == "code" and expected["identifier"] in " ".join(
            str(span.get("content") or "")
            for line in (block.get("lines") or [])
            for span in (line.get("spans") or []))
                   for block in page["para_blocks"])


def test_omnidocbench_adapter_preserves_formula_table_and_page_order(tmp_path):
    from ddp_core.blocks import table_html

    entries = [
        {
            "page_info": {"image_path": "formula.png", "width": 100, "height": 200},
            "layout_dets": [{
                "category_type": "equation_isolated", "poly": [1, 2, 9, 2, 9, 8, 1, 8],
                "order": 0, "latex": "x^2 + y^2 = 1", "ignore": False,
            }],
        },
        {
            "page_info": {"image_path": "table.png", "width": 300, "height": 400},
            "layout_dets": [{
                "category_type": "table", "poly": [10, 20, 90, 20, 90, 80, 10, 80],
                "order": None, "html": "<table><tr><td>Model</td><td>Score</td></tr>"
                                         "<tr><td>DDP</td><td>0.9</td></tr></table>",
                "ignore": False,
            }],
        },
    ]
    (tmp_path / "OmniDocBench.subset.json").write_text(
        json.dumps(entries), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slices": {"test": ["formula.png", "table.png"]}}),
                        encoding="utf-8")

    layout = ev._omnidocbench_slice_layout("test", tmp_path, manifest)
    assert [page["page_idx"] for page in layout["pdf_info"]] == [0, 1]
    formula = layout["pdf_info"][0]["para_blocks"][0]
    table = layout["pdf_info"][1]["para_blocks"][0]
    assert formula["type"] == "equation" and formula["bbox"] == [1, 2, 9, 8]
    assert ev._anchor_bbox(layout, "x^2 + y^2") == (0, [1, 2, 9, 8])
    assert table["type"] == "table" and "<table>" in table_html(table)
    assert "table_html" not in table
    assert ev._anchor_bbox(layout, "DDP 0.9") == (1, [10, 20, 90, 80])


def test_omnidocbench_adapter_preserves_empty_chart_as_a_bbox_atom(tmp_path):
    entry = {
        "page_info": {"image_path": "chart.png", "width": 100, "height": 200},
        "layout_dets": [{
            "category_type": "chart_mask", "poly": [10, 20, 90, 20, 90, 80, 10, 80],
            "order": 0, "ignore": False,
        }],
    }
    (tmp_path / "OmniDocBench.subset.json").write_text(
        json.dumps([entry]), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slices": {"test": ["chart.png"]}}), encoding="utf-8")

    layout = ev._omnidocbench_slice_layout("test", tmp_path, manifest)

    assert layout["pdf_info"][0]["para_blocks"] == [{
        "type": "figure", "bbox": [10, 20, 90, 80], "lines": [],
    }]


def test_failed_upload_is_cached_and_not_retried(monkeypatch):
    calls = 0

    async def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(ev, "_upload_and_wait", fail)
    cache = {}
    source = {"kind": "omnidocbench", "slice": "图表引用"}

    async def twice():
        first = await ev._upload_once(None, "http://web", {}, source, cache)
        second = await ev._upload_once(None, "http://web", {}, source, cache)
        return first, second

    first, second = __import__("asyncio").run(twice())
    assert first == second == ("图表引用", None)
    assert calls == 1


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


def _record(**values):
    return {"fields": {name: {"status": "found", "value": value, "citations": []}
                       for name, value in values.items()}}


def _record_sample(rows):
    return {"id": "rows", "attributes": ["多记录"], "expect": {"records": rows}}


def test_array_records_are_matched_row_by_row_without_order_sensitivity():
    sample = _record_sample([
        {"item": "bearing", "quantity": 12},
        {"item": "seal", "quantity": 4},
    ])
    outcomes = extract_ev.judge_records(sample, [
        _record(item="seal", quantity=4),
        _record(item="bearing", quantity=12),
    ])
    assert [outcome.ok for outcome in outcomes] == [True, True]


def test_array_record_count_mismatch_cannot_score_as_perfect():
    sample = _record_sample([{"item": "bearing"}, {"item": "seal"}])
    missing = extract_ev.judge_records(sample, [_record(item="bearing")])
    extra = extract_ev.judge_records(
        _record_sample([{"item": "bearing"}]),
        [_record(item="bearing"), _record(item="invented")],
    )
    assert [outcome.ok for outcome in missing] == [True, False]
    assert [outcome.ok for outcome in extra] == [True, False]
    assert "多返回" in extra[-1].note


def test_array_record_matching_preserves_duplicate_counts():
    sample = _record_sample([{"item": "same"}, {"item": "same"}])
    outcomes = extract_ev.judge_records(sample, [_record(item="same")])
    assert [outcome.ok for outcome in outcomes] == [True, False]


def test_array_record_wrong_value_or_non_found_status_is_wrong():
    sample = _record_sample([{"item": "bearing", "quantity": 12}])
    wrong_value = extract_ev.judge_records(sample, [_record(item="bearing", quantity=13)])
    non_found = {"fields": {
        "item": {"status": "found", "value": "bearing"},
        "quantity": {"status": "error", "value": None},
    }}
    wrong_status = extract_ev.judge_records(sample, [non_found])
    assert wrong_value[0].ok is False
    assert wrong_status[0].ok is False


def test_array_api_items_are_reconstructed_as_records_instead_of_dropping_all_but_first():
    sample = {"schema": {"type": "array"}}
    items = [
        {"status": "ok", "fields": _record(item="bearing")["fields"]},
        {"status": "ok", "fields": _record(item="seal")["fields"]},
    ]
    payload = extract_ev.result_payload(sample, items)
    assert payload["fields"] == {}
    assert [extract_ev._record_values(row) for row in payload["records"]] == [
        {"item": "bearing"}, {"item": "seal"},
    ]


def test_stage5_atom_hit_rates_are_always_reported_separately():
    outcomes = [
        ev.Outcome("c", ["代码密集"], True, page_hit=True, bbox_hit=True),
        ev.Outcome("e", ["公式密集"], True, page_hit=True, bbox_hit=False),
        ev.Outcome("t", ["操作表格"], True, page_hit=True, bbox_hit=True),
        ev.Outcome("f", ["图表引用"], True, page_hit=False, bbox_hit=False),
    ]
    metrics = ev.summarize_atoms(outcomes)
    assert list(metrics) == ["code", "equation", "table", "figure"]
    assert metrics["code"].rate == 1.0
    assert metrics["equation"].rate == 0.0
    report = ev.render(outcomes, "offline")
    for kind in metrics:
        assert f"| `{kind}` |" in report
