#!/usr/bin/env python
"""识别质量评测 —— 「多模态文字识别」这条线上的度量。

## 为什么必须有它

`../DeepDocParse-Web/docs/EVAL.md` 的四个指标全部在量**出处**，一个字没量**识别质量**，
而且它自己写明：「ground truth 的坐标终究来自解析器本身……不要拿这个数字去论证解析质量」。
于是「识别」这半边一直没有任何数字支撑 —— 换引擎、升级 mineru、上 vlm-ocr，
都只能靠"看着还行"。这个脚本补的就是那半边。

方法论沿用同一套：**按属性切片，从不报综合分**（借自 OmniDocBench）。
上一轮借了它的方法，没借它的数据；这一轮把数据也接上。

## 三个本机指标 + OmniDocBench 官方指标

| 指标 | 定义 | 适用样本 |
|---|---|---|
| **文本准确率** | `1 - 归一化编辑距离(识别文本, 真值文本)`，按页算再取平均 | 有 `text` 真值的页 |
| **表格单元格 F1** | 把表格拍成 `(行, 列, 单元格文本)` 三元组求 P/R/F1 | 有 `table` 真值的页 |
| **公式编辑准确率** | `1 - 归一化编辑距离(识别 LaTeX, 真值 LaTeX)` | 有公式真值的页 |

**表格这条刻意不是 TEDS。** TEDS 是这个领域的标准指标，但它是 HTML 树的树编辑距离，
一份自己实现的、没有对过官方结果的 TEDS，给出的数字**看起来权威、实际不可比** ——
那比没有数字更糟。单元格 F1 定义清楚、能自己验证、跨引擎可比，
够用来回答"换引擎之后表格识别变好了还是变差了"。
真要报 TEDS，请直接跑 OmniDocBench 官方评测器，不要用这里的数字冒充。
本脚本现在可用 `--export-official` 产出官方评测器所需的 Markdown、子集标注与配置，
再用 `--official-result` 把官方的 TEDS/TEDS-S/CDM 数字并回报告。

## 数据来源

支持两种：

1. **仓库自带 fixture**（默认）：`tests/fixtures/*.pdf` + 同名 `.truth.json`。
   零外部依赖，CI 可跑。
2. **OmniDocBench 格式**：`--dataset <目录>`，目录里是官方的 `*.json` 标注 + 对应图片/PDF。
   适配层在 `load_omnidocbench()` —— 它只读官方标注里的 `text` 与 `html`，
   不依赖官方评测代码。

用法：
    python scripts/eval_ocr.py --engine borndigital
    python scripts/eval_ocr.py --engine mineru --gateway http://127.0.0.1:9000
    python scripts/eval_ocr.py --dataset ~/OmniDocBench/ --engine vlm-ocr --markdown docs/EVAL-ocr-report.md
"""
import argparse
import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))

FIXTURES = ROOT / "tests" / "fixtures"


@dataclass
class PageOutcome:
    sample_id: str
    page_idx: int
    attributes: list[str]
    text_score: float | None = None
    table_f1: float | None = None
    formula_edit: float | None = None
    note: str = ""


@dataclass
class Metric:
    total: float = 0.0
    count: int = 0

    def add(self, value: float | None) -> None:
        if value is None:
            return
        self.total += value
        self.count += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    def __str__(self) -> str:
        return "—" if self.mean is None else f"{self.mean:6.1%} (n={self.count})"


@dataclass
class Slice:
    name: str
    text: Metric = field(default_factory=Metric)
    table: Metric = field(default_factory=Metric)
    formula: Metric = field(default_factory=Metric)
    pages: int = 0


# --------------------------------------------------------------------------- 文本

# 归一化：去掉空白与标点差异。识别出来的标点/空格与真值几乎不可能逐字一致，
# 留着它们等于把排版噪声算成识别错误 —— 那会让这个指标对真正的错字不敏感
_NORMALIZE = re.compile(r"[\s　]+")


def normalize_text(text: str) -> str:
    return _NORMALIZE.sub("", text or "")


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离。滚动数组，O(min(len)) 空间。

    不用 difflib.SequenceMatcher.ratio()：它是 Ratcliff/Obershelp 相似度，
    **不是编辑距离**，对插入/删除的惩罚与领域惯例对不上，报出来的数字没法和
    别的 OCR 评测比较。这个指标的价值全在可比性上。
    """
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1,        # 删除
                               current[j - 1] + 1,     # 插入
                               previous[j - 1] + (ca != cb)))  # 替换
        previous = current
    return previous[-1]


def text_accuracy(predicted: str, truth: str) -> float | None:
    """1 - 归一化编辑距离。真值为空时返回 None（这一页不参与打分）。"""
    want = normalize_text(truth)
    if not want:
        return None
    got = normalize_text(predicted)
    return max(0.0, 1.0 - edit_distance(got, want) / len(want))


# --------------------------------------------------------------------------- 表格

class _TableParser(HTMLParser):
    """把 <table> 拍成 {(行, 列): 文本}。只处理 rowspan/colspan 的基本情况。"""

    def __init__(self) -> None:
        super().__init__()
        self.cells: dict[tuple[int, int], str] = {}
        self._row = -1
        self._col = 0
        self._buf: list[str] = []
        self._in_cell = False
        # 被上面单元格 rowspan 占住的格子，(行, 列) -> 还要占几行
        self._spans: dict[tuple[int, int], int] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr":
            self._row += 1
            self._col = 0
        elif tag in ("td", "th"):
            self._in_cell = True
            self._buf = []
            while (self._row, self._col) in self._spans:
                self._col += 1
            self._span = (int(attributes.get("rowspan") or 1),
                          int(attributes.get("colspan") or 1))

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            text = normalize_text("".join(self._buf))
            rowspan, colspan = self._span
            for dr in range(rowspan):
                for dc in range(colspan):
                    position = (self._row + dr, self._col + dc)
                    if dr == 0 and dc == 0:
                        self.cells[position] = text
                    else:
                        self._spans[position] = 1
            self._col += colspan
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            self._buf.append(data)


def table_cells(html: str) -> dict[tuple[int, int], str]:
    parser = _TableParser()
    parser.feed(html or "")
    return {k: v for k, v in parser.cells.items() if v}


def table_f1(predicted_html: str | None, truth_html: str) -> float | None:
    """单元格三元组的 F1。**不是 TEDS**（理由见模块 docstring）。"""
    want = table_cells(truth_html)
    if not want:
        return None
    got = table_cells(predicted_html or "")
    if not got:
        return 0.0
    matched = sum(1 for position, text in got.items() if want.get(position) == text)
    precision = matched / len(got)
    recall = matched / len(want)
    return 0.0 if not (precision + recall) else 2 * precision * recall / (precision + recall)


def formula_edit_accuracy(predicted: str | None, truth: str) -> float | None:
    """轻量公式回归指标；**不是 CDM**，不可与 OmniDocBench 榜单混用。"""
    want = normalize_text(truth).strip("$")
    if not want:
        return None
    got = normalize_text(predicted or "").strip("$")
    return max(0.0, 1.0 - edit_distance(got, want) / len(want))


# --------------------------------------------------------------------------- 取识别结果

async def recognize(gateway: str, token: str, engine: str, pdf: Path) -> dict | None:
    """经 gateway 跑一次解析，返回 layout_json。失败返回 None。

    走 HTTP 而不是直接 import 引擎：**评测要量的是部署形态下的真实行为**，
    进程内直调会绕开注册表、绕开归一化层，量出来的是另一回事。
    """
    import httpx

    # trust_env=False：本机代理会污染 localhost 调用（同 tests/ 的约定）
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=900.0),
                                 trust_env=False) as http:
        headers = {"Authorization": f"Bearer {token}"}
        # gateway 只接受可下载 URL，本地文件要先起一个静态服务 —— 这里用 file:// 不行，
        # 所以评测要求调用方把 fixture 放到 gateway 能取到的地方（见 --base-url）
        file_url = f"{os.environ.get('EVAL_FILE_BASE', '').rstrip('/')}/{pdf.name}"
        resp = await http.post(f"{gateway}/v1/parse", headers=headers,
                               json={"file_url": file_url, "engine": engine,
                                     "doc_id": f"eval-ocr-{engine}-{pdf.stem}"})
        if resp.status_code != 202:
            return None
        task_id = resp.json()["task_id"]
        for _ in range(300):
            await asyncio.sleep(2)
            status = (await http.get(f"{gateway}/v1/parse/{task_id}",
                                     headers=headers)).json()
            if status["status"] == "succeeded":
                result = await http.get(f"{gateway}/v1/parse/{task_id}/result",
                                        headers=headers)
                return result.json().get("layout_json")
            if status["status"] == "failed":
                return None
    return None


def recognize_inproc(engine: str, pdf: Path) -> dict | None:
    """本地直跑（只支持 borndigital）—— 没有 gateway 也能量一个基线。

    **这条路量出来的不是部署行为**（绕开了注册表与传输层），
    所以报表会标出来。它存在的意义是让"没有任何环境"的机器也有个可跑的下限。
    """
    if engine != "borndigital":
        return None
    from ddp_gateway.services import borndigital, layout

    pages = borndigital.extract_pages(pdf.read_bytes())
    return layout.build(pages, engine="borndigital") if pages else None


# --------------------------------------------------------------------------- 数据集

def load_fixtures() -> list[dict]:
    """仓库自带样本：`<name>.pdf` + `<name>.truth.json`。"""
    samples = []
    for truth_path in sorted(FIXTURES.glob("*.truth.json")):
        pdf = truth_path.with_suffix("").with_suffix(".pdf")
        if not pdf.exists():
            continue
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        samples.append({"id": pdf.stem, "pdf": pdf, "pages": truth["pages"],
                        "attributes": truth.get("attributes", [])})
    return samples


def load_omnidocbench(root: Path, manifest: dict | None = None) -> list[dict]:
    """OmniDocBench 官方标注 -> 本脚本的样本形状。

    只读标注里的 `text` 与 `html`，**不依赖官方评测代码**。
    官方每个样本是一页，`page_info.page_attribute` 里有版式属性 —— 直接拿来做切片，
    这正是"借方法也借数据"的落点。
    """
    samples = []
    seen_images: set[str] = set()
    selected: dict[str, list[str]] = {}
    for slice_name, image_paths in ((manifest or {}).get("slices") or {}).items():
        for image_path in image_paths:
            selected.setdefault(image_path, []).append(slice_name)
    for path in sorted(root.glob("**/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            info = entry.get("page_info") or {}
            image = info.get("image_path")
            if not image:
                continue
            if selected and image not in selected:
                continue
            if image in seen_images:
                continue
            seen_images.add(image)
            attributes = [f"{k}:{v}" for k, v in
                          (info.get("page_attribute") or {}).items()]
            attributes += selected.get(image, [])
            text_parts, tables, formulas = [], [], []
            for block in entry.get("layout_dets") or []:
                if block.get("ignore"):
                    continue
                if block.get("html"):
                    tables.append(block["html"])
                elif block.get("latex") and block.get("category_type") in {
                        "equation_isolated", "equation_semantic"}:
                    formulas.append(block["latex"])
                elif block.get("text"):
                    text_parts.append(block["text"])
            image_path = root / image
            if not image_path.exists():
                image_path = root / "images" / image
            prepared_pdf = root / "documents" / f"{Path(image).stem}.pdf"
            samples.append({
                "id": Path(image).stem,
                "pdf": prepared_pdf if prepared_pdf.exists() else image_path,
                "pages": [{"page_idx": 0, "text": "\n".join(text_parts),
                           "tables": tables, "formulas": formulas}],
                "attributes": attributes,
                "omnidocbench": entry,
            })
    return samples


# --------------------------------------------------------------------------- 判定

def judge(sample: dict, layout_json: dict | None) -> list[PageOutcome]:
    from ddp_gateway.services import layout as layout_mod

    outcomes: list[PageOutcome] = []
    pages = (layout_json or {}).get("pdf_info") or []
    by_index = {p.get("page_idx", i): p for i, p in enumerate(pages)}

    for truth in sample["pages"]:
        index = truth["page_idx"]
        outcome = PageOutcome(sample_id=sample["id"], page_idx=index,
                              attributes=sample["attributes"])
        page = by_index.get(index)
        if page is None:
            # 这一页压根没识别出来。**记 0 分而不是跳过** ——
            # 跳过会让"漏了半份文档"的引擎在报表上看起来和别人一样好
            outcome.text_score = 0.0 if truth.get("text") else None
            outcome.table_f1 = 0.0 if truth.get("tables") else None
            outcome.formula_edit = 0.0 if truth.get("formulas") else None
            outcome.note = "该页没有识别结果"
            outcomes.append(outcome)
            continue

        blocks = page.get("para_blocks") or []
        # 文本指标不能把同页公式/表格再算一遍。否则公式错一次会同时拉低
        # “公式”与“文本”两列，切片归因失真；结构块由各自指标单独负责。
        predicted_text = "\n".join(
            layout_mod.block_text(block) for block in blocks
            if layout_mod.normalize_type(block.get("type")) not in {"table", "equation", "figure"})
        outcome.text_score = text_accuracy(predicted_text, truth.get("text", ""))

        want_tables = truth.get("tables") or []
        if want_tables:
            got = [layout_mod.table_html(b) for b in blocks]
            got = [h for h in got if h]
            # 按顺序一一对应。表格数量对不上时缺的算 0 分 ——
            # 少认出一张表是实打实的识别失败
            scores = [table_f1(got[i] if i < len(got) else None, want)
                      for i, want in enumerate(want_tables)]
            scores = [s for s in scores if s is not None]
            outcome.table_f1 = sum(scores) / len(scores) if scores else None
        want_formulas = truth.get("formulas") or []
        if want_formulas:
            got_formulas = [layout_mod.block_text(b) for b in blocks
                            if layout_mod.normalize_type(b.get("type")) == "equation"]
            scores = [formula_edit_accuracy(
                got_formulas[i] if i < len(got_formulas) else None, want)
                for i, want in enumerate(want_formulas)]
            scores = [s for s in scores if s is not None]
            outcome.formula_edit = sum(scores) / len(scores) if scores else None
        outcomes.append(outcome)
    return outcomes


# --------------------------------------------------------------------------- 报表

def summarize(outcomes: list[PageOutcome]) -> tuple[Slice, list[Slice]]:
    overall = Slice("全部")
    by_attr: dict[str, Slice] = {}
    for outcome in outcomes:
        targets = [overall] + [by_attr.setdefault(a, Slice(a)) for a in outcome.attributes]
        for target in targets:
            target.pages += 1
            target.text.add(outcome.text_score)
            target.table.add(outcome.table_f1)
            target.formula.add(outcome.formula_edit)
    return overall, sorted(by_attr.values(), key=lambda s: s.name)


def _nested(data: dict, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


OFFICIAL_METRICS = [
    ("文本 Edit Distance ↓", ("text_block", "all", "Edit_dist", "ALL_page_avg"), 1),
    ("公式 CDM ↑", ("display_formula", "page", "CDM", "ALL"), 100),
    ("表格 TEDS ↑", ("table", "page", "TEDS", "ALL"), 100),
    ("表格 TEDS-S ↑", ("table", "page", "TEDS_structure_only", "ALL"), 100),
    ("阅读顺序 Edit Distance ↓", ("reading_order", "all", "Edit_dist", "ALL_page_avg"), 1),
]


def _official_values(result: dict) -> list[tuple[str, float, int]]:
    """官方五路指标必须完整且有限；缺依赖/阶段失败不能静默显示破折号。"""
    values = []
    missing = []
    for name, path, scale in OFFICIAL_METRICS:
        value = _nested(result, *path)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            missing.append(f"{name} ({'.'.join(path)})")
        else:
            values.append((name, float(value), scale))
    if missing:
        raise ValueError("官方 OmniDocBench 结果缺少有效指标：" + "；".join(missing))
    debug_failures = []
    for element, payload in result.items():
        if not isinstance(payload, dict):
            continue
        metric_debug = payload.get("metric_debug")
        if not isinstance(metric_debug, dict):
            continue
        for metric_name, debug in metric_debug.items():
            if not isinstance(debug, dict):
                continue
            for kind in ("timeout", "error", "exception"):
                count = debug.get(f"{kind}_case_count", 0)
                cases = debug.get(f"{kind}_cases") or []
                if (isinstance(count, bool) or not isinstance(count, (int, float))
                        or count < 0):
                    debug_failures.append(
                        f"{element}.{metric_name}.{kind}_case_count={count!r}")
                elif count > 0 or cases:
                    debug_failures.append(
                        f"{element}.{metric_name}.{kind}_case_count={int(count)}")
    if debug_failures:
        raise ValueError("官方 OmniDocBench 指标执行存在降级：" + "；".join(debug_failures))
    return values


def _official_section(result: dict) -> list[str]:
    lines = ["", "## OmniDocBench v1.6 官方指标", "",
             "| 指标 | 数值 |", "|---|---:|"]
    for name, value, scale in _official_values(result):
        lines.append(f"| {name} | {value * scale:.4f} |")
    return lines


def load_official_result(path: str) -> dict:
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    _official_values(result)
    return result


def render(outcomes: list[PageOutcome], engine: str, source: str,
           official_result: dict | None = None) -> str:
    overall, slices = summarize(outcomes)
    lines = [f"# 识别质量评测报表（engine={engine}，数据集={source}）", "",
             f"页数 {len(outcomes)}。指标定义见 docs/EVAL-ocr.md。",
             "**从不报综合分**：下面每一行都是一个切片。",
             "轻量表格列不是 TEDS，轻量公式列也不是 CDM；不要与官方数字混用。",
             "", "| 切片 | 页 | 文本准确率 | 表格单元格 F1 | 公式编辑准确率 |",
             "|---|---|---|---|---|"]
    for s in [overall, *slices]:
        lines.append(f"| {s.name} | {s.pages} | {s.text} | {s.table} | {s.formula} |")
    if official_result is not None:
        lines += _official_section(official_result)
    lines += ["", "## 逐页", "", "| 样本 | 页 | 文本 | 表格 | 公式 | 备注 |",
              "|---|---|---|---|---|---|"]
    for o in outcomes:
        text = "—" if o.text_score is None else f"{o.text_score:.1%}"
        table = "—" if o.table_f1 is None else f"{o.table_f1:.1%}"
        formula = "—" if o.formula_edit is None else f"{o.formula_edit:.1%}"
        lines.append(f"| `{o.sample_id}` | {o.page_idx + 1} | {text} | {table} | {formula} | {o.note} |")
    return "\n".join(lines) + "\n"


def layout_markdown(layout_json: dict | None) -> str:
    """DDP-Layout -> OmniDocBench 官方 end-to-end evaluator 的 Markdown 输入。"""
    from ddp_gateway.services import layout as layout_mod

    parts = []
    for page in (layout_json or {}).get("pdf_info") or []:
        for block in page.get("para_blocks") or []:
            kind = layout_mod.normalize_type(block.get("type"))
            if kind == "table" and layout_mod.table_html(block):
                parts.append(layout_mod.table_html(block))
                continue
            text = layout_mod.block_text(block)
            if not text:
                continue
            parts.append(f"$$\n{text.strip('$')}\n$$" if kind == "equation" else text)
    return "\n\n".join(parts) + "\n"


def export_official(samples: list[dict], layouts: dict[str, dict | None], output: Path) -> None:
    """导出官方 v1.6 评测器输入；只对 OmniDocBench 样本生效。"""
    output.mkdir(parents=True, exist_ok=True)
    predictions = output / "predictions"
    predictions.mkdir(exist_ok=True)
    ground_truth = []
    for sample in samples:
        entry = sample.get("omnidocbench")
        if not entry:
            continue
        ground_truth.append(entry)
        image = entry["page_info"]["image_path"]
        (predictions / f"{Path(image).stem}.md").write_text(
            layout_markdown(layouts.get(sample["id"])), encoding="utf-8")
    (output / "ground-truth.json").write_text(
        json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8")
    gt_path = json.dumps(str((output / "ground-truth.json").resolve()))
    prediction_path = json.dumps(str(predictions.resolve()))
    config = """end2end_eval:
  metrics:
    text_block: {metric: [Edit_dist]}
    display_formula: {metric: [Edit_dist, CDM], cdm_workers: 2}
    table: {metric: [TEDS, Edit_dist], teds_workers: 2}
    reading_order: {metric: [Edit_dist]}
  dataset:
    dataset_name: end2end_dataset
    ground_truth: {data_path: __GT_PATH__}
    prediction: {data_path: __PREDICTION_PATH__}
    match_method: quick_match
    match_workers: 2
    quick_match_truncated_timeout_sec: 300
    timeout_fallback_max_chunk_span: 10
    timeout_fallback_order_penalty: 0.10
""".replace("__GT_PATH__", gt_path).replace("__PREDICTION_PATH__", prediction_path)
    (output / "omnidocbench.yaml").write_text(config, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", default="borndigital", help="models.yaml 里的引擎名")
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", ""),
                        help="留空则本地直跑（只支持 borndigital）")
    parser.add_argument("--token", default=os.environ.get("SERVICE_TOKEN", ""))
    parser.add_argument("--dataset", help="OmniDocBench 目录；不给则用仓库自带 fixture")
    parser.add_argument("--manifest", help="只评 manifest 选中的 OmniDocBench v1.6 五域切片")
    parser.add_argument("--export-official", help="导出 OmniDocBench 官方评测器的输入目录")
    parser.add_argument("--official-result", help="官方 metric_result.json；把 TEDS/CDM 并回报告")
    parser.add_argument("--markdown", help="把报表写到这个文件")
    args = parser.parse_args()

    if args.dataset:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8")) if args.manifest else None
        samples = load_omnidocbench(Path(args.dataset).expanduser(), manifest)
        source = "OmniDocBench"
    else:
        samples = load_fixtures()
        source = "fixtures"
    if not samples:
        print("没有样本可评。仓库自带样本需要 tests/fixtures/*.truth.json，"
              "跑 python scripts/make_fixtures.py 生成", file=sys.stderr)
        return 1

    try:
        official_result = load_official_result(args.official_result) if args.official_result else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"官方 OmniDocBench 结果无效：{exc}", file=sys.stderr)
        return 2

    outcomes: list[PageOutcome] = []
    layouts: dict[str, dict | None] = {}
    for sample in samples:
        if args.gateway:
            layout_json = asyncio.run(
                recognize(args.gateway.rstrip("/"), args.token, args.engine, sample["pdf"]))
        else:
            layout_json = recognize_inproc(args.engine, sample["pdf"])
        layouts[sample["id"]] = layout_json
        outcomes.extend(judge(sample, layout_json))

    report = render(outcomes, args.engine, source, official_result)
    if not args.gateway:
        report = ("> ⚠️ 本次是**本地进程内直跑**，绕开了注册表与传输层，"
                  "量的不是部署行为。要量真实行为请加 --gateway。\n\n") + report
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.markdown}")
    if args.export_official:
        export_official(samples, layouts, Path(args.export_official))
        print(f"官方评测输入已写入 {args.export_official}")

    overall, _ = summarize(outcomes)
    # 一页都没量到 = 评测跑歪了（引擎没起、真值缺失），非零退出
    return 0 if (overall.text.count or overall.table.count or overall.formula.count) else 1


if __name__ == "__main__":
    raise SystemExit(main())
