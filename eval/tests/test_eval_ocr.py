"""OCR 评测器自己的回归用例：指标代码往好里错，比没有指标更糟。"""
import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image
import pypdfium2 as pdfium
import pytest

from ddp_gateway.services import borndigital


from ddp_paths import FIXTURES

EVAL_DIR = Path(__file__).resolve().parents[1]


def _load_eval():
    spec = importlib.util.spec_from_file_location("eval_ocr_metrics", EVAL_DIR / "eval_ocr.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load_eval()


def _load_prepare():
    spec = importlib.util.spec_from_file_location(
        "prepare_eval_corpus", EVAL_DIR / "prepare_eval_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load_prepare()


def test_formula_edit_accuracy_can_fail_independently():
    assert ev.formula_edit_accuracy(r"x^2+y^2", r"x^2+y^2") == 1.0
    assert ev.formula_edit_accuracy(r"x^2-y^2", r"x^2+y^2") < 1.0
    assert ev.formula_edit_accuracy(None, r"x^2+y^2") == 0.0


def test_omnidocbench_loader_keeps_formula_truth_and_manifest_slice(tmp_path):
    entries = [{
        "page_info": {
            "image_path": "picked.png", "page_attribute": {"layout": "double_column"},
        },
        "layout_dets": [
            {"category_type": "text_block", "text": "paper text"},
            {"category_type": "equation_isolated", "latex": r"x^2+y^2"},
            {"category_type": "table", "html": "<table><tr><td>A</td></tr></table>"},
        ],
    }, {
        "page_info": {"image_path": "skipped.png", "page_attribute": {}},
        "layout_dets": [{"category_type": "text_block", "text": "must not load"}],
    }]
    (tmp_path / "OmniDocBench.json").write_text(json.dumps(entries), encoding="utf-8")
    manifest = {"slices": {"论文双栏": ["picked.png"]}}

    samples = ev.load_omnidocbench(tmp_path, manifest)

    assert len(samples) == 1
    assert samples[0]["pages"][0]["formulas"] == [r"x^2+y^2"]
    assert "论文双栏" in samples[0]["attributes"]


def test_missing_formula_page_scores_zero_not_skipped():
    sample = {
        "id": "formula", "attributes": ["公式密集"],
        "pages": [{"page_idx": 0, "formulas": [r"E=mc^2"]}],
    }
    outcome = ev.judge(sample, {"pdf_info": []})[0]
    assert outcome.formula_edit == 0.0


def test_formula_error_does_not_double_count_as_a_text_error():
    sample = {
        "id": "independent-metrics", "attributes": ["公式密集"],
        "pages": [{"page_idx": 0, "text": "plain paragraph", "formulas": ["x+y"]}],
    }
    layout = {"pdf_info": [{"page_idx": 0, "para_blocks": [
        {"type": "text", "lines": [{"spans": [{"content": "plain paragraph"}]}]},
        {"type": "equation", "lines": [{"spans": [{"content": "wrong"}]}]},
    ]}]}
    outcome = ev.judge(sample, layout)[0]
    assert outcome.text_score == 1.0
    assert outcome.formula_edit < 1.0


def test_official_result_is_reported_under_official_names():
    result = {
        "text_block": {"all": {"Edit_dist": {"ALL_page_avg": 0.09}}},
        "display_formula": {"page": {"CDM": {"ALL": 0.91}}},
        "table": {"page": {
            "TEDS": {"ALL": 0.82}, "TEDS_structure_only": {"ALL": 0.88},
        }},
        "reading_order": {"all": {"Edit_dist": {"ALL_page_avg": 0.12}}},
    }
    report = "\n".join(ev._official_section(result))
    assert "公式 CDM" in report and "91.0000" in report
    assert "表格 TEDS" in report and "82.0000" in report


@pytest.mark.parametrize("result", [
    {},
    {
        "text_block": {"all": {"Edit_dist": {"ALL_page_avg": 0.1}}},
        "display_formula": {"page": {"CDM": {}}},
    },
    {
        "text_block": {"all": {"Edit_dist": {"ALL_page_avg": float("nan")}}},
        "display_formula": {"page": {"CDM": {"ALL": 0.9}}},
        "table": {"page": {"TEDS": {"ALL": 0.8},
                              "TEDS_structure_only": {"ALL": 0.85}}},
        "reading_order": {"all": {"Edit_dist": {"ALL_page_avg": 0.1}}},
    },
])
def test_incomplete_official_result_cannot_render_as_a_green_report(result):
    with pytest.raises(ValueError, match="缺少有效指标"):
        ev._official_section(result)


def test_empty_official_result_makes_main_exit_nonzero(tmp_path, monkeypatch, capsys):
    result = tmp_path / "empty.json"
    result.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ev, "load_fixtures", lambda: [{"id": "unused"}])
    monkeypatch.setattr(sys, "argv", ["eval_ocr.py", "--official-result", str(result)])

    assert ev.main() == 2
    assert "结果无效" in capsys.readouterr().err


def test_finite_official_scores_with_cdm_exception_cannot_pass():
    result = {
        "text_block": {"all": {"Edit_dist": {"ALL_page_avg": 0.09}}},
        "display_formula": {
            "page": {"CDM": {"ALL": 0.0}},
            "metric_debug": {"CDM": {
                "exception_case_count": 1,
                "exception_cases": [{"sample_id": "formula-1", "reason": "latex failed"}],
            }},
        },
        "table": {"page": {
            "TEDS": {"ALL": 0.82}, "TEDS_structure_only": {"ALL": 0.88},
        }},
        "reading_order": {"all": {"Edit_dist": {"ALL_page_avg": 0.12}}},
    }

    with pytest.raises(ValueError, match="指标执行存在降级.*exception_case_count=1"):
        ev._official_values(result)


def test_export_official_writes_markdown_ground_truth_and_config(tmp_path):
    sample = {
        "id": "page-one", "omnidocbench": {
            "page_info": {"image_path": "page-one.png"}, "layout_dets": [],
        },
    }
    layout = {"pdf_info": [{"page_idx": 0, "para_blocks": [{
        "type": "equation", "bbox": [0, 0, 10, 10],
        "lines": [{"spans": [{"content": r"x^2+y^2"}]}],
    }]}]}

    ev.export_official([sample], {"page-one": layout}, tmp_path)

    assert "$$" in (tmp_path / "predictions" / "page-one.md").read_text()
    assert json.loads((tmp_path / "ground-truth.json").read_text())[0]["page_info"]["image_path"] == "page-one.png"
    assert "CDM" in (tmp_path / "omnidocbench.yaml").read_text()


def test_domain_manifest_has_four_disjoint_real_slices_and_code_corpus():
    manifest = json.loads(
        (EVAL_DIR / "datasets" / "omnidocbench-v1.6-slices.json").read_text(encoding="utf-8"))
    source = manifest["source"]
    assert "/resolve/main/" not in source["annotation_url"]
    assert "d386947f7fc3bafdcd756c8485845a2f43a19875" in source["annotation_url"]
    assert source["official_evaluator_commit"] == "147cd5ac9472002f5751221d390bf00abdbc0d2f"
    assert "non-commercial" in source["data_usage_terms"]
    assert "license" not in source
    assert "/resolve/main/" not in prepare.HF_IMAGES
    pages = prepare.validate_manifest(manifest)
    assert len(pages) == 40 == len(set(pages))
    assert manifest["synthetic"]["代码密集"]["pages"] == 24


def test_domain_selection_is_proved_by_official_annotations():
    manifest = {"slices": {
        "论文双栏": ["double.png"], "公式密集": ["formula.png"],
        "图表引用": ["chart.png"], "扫描版老手册": ["scan.png"],
    }, "selection_evidence": {"扫描版老手册": {"scan.png": "技术手册"}}}

    def entry(image, attributes, categories):
        return {"page_info": {"image_path": image, "page_attribute": attributes},
                "layout_dets": [{"category_type": kind, "ignore": False}
                                for kind in categories]}

    by_image = {
        "double.png": entry("double.png", {"layout": "double_column"}, []),
        "formula.png": entry("formula.png", {}, ["equation_isolated"] * 20),
        "chart.png": entry("chart.png", {}, ["chart_mask"]),
        "scan.png": entry("scan.png", {"data_source": "book"}, ["text_block"]),
    }
    by_image["scan.png"]["layout_dets"][0]["text"] = "技术手册的操作步骤"
    prepare.validate_selection(manifest, by_image)
    by_image["chart.png"]["layout_dets"] = []
    try:
        prepare.validate_selection(manifest, by_image)
    except ValueError as exc:
        assert "图表引用/chart.png" in str(exc)
    else:
        raise AssertionError("没有 chart_mask 的页不能混进图表引用域")

    by_image["chart.png"]["layout_dets"] = [{
        "category_type": "chart_mask", "ignore": False,
    }]
    by_image["scan.png"]["page_info"]["page_attribute"]["data_source"] = "exam_paper"
    with pytest.raises(ValueError, match="扫描版老手册/scan.png"):
        prepare.validate_selection(manifest, by_image)


def test_prepare_combines_a_slice_into_one_multi_page_pdf(tmp_path):
    images = []
    for index in range(2):
        path = tmp_path / f"page-{index}.png"
        Image.new("RGB", (20, 30), (255, index * 20, 255)).save(path)
        images.append(path)
    output = tmp_path / "slice.pdf"

    prepare.images_to_pdf(images, output)

    assert len(pdfium.PdfDocument(output)) == 2


def test_code_corpus_keeps_all_exact_identifiers_in_the_pdf_text_layer():
    truth = json.loads(
        (FIXTURES / "code-corpus.truth.json").read_text(encoding="utf-8"))
    pages = borndigital.extract_pages(
        (FIXTURES / "code-corpus.pdf").read_bytes())
    assert len(pages) == len(truth["pages"]) == 24
    for page, expected in zip(pages, truth["pages"]):
        text = "\n".join(block["text"] for block in page["blocks"])
        assert expected["identifier"] in text
        assert any(expected["identifier"] in block["text"] and block["type"] == "code"
                   for block in page["blocks"]), \
            f"{expected['identifier']} 仍在文字层，但 code 启发式漏检了"
