#!/usr/bin/env python
"""出处评测：**这个回答的出处对不对**，而不是"跑通没有"。

现有验收很厚（114 例单测 + e2e 约 40 条断言），但几乎全部在问"跑通没有"：

    回答非空          bool(answer.strip())          没问对不对
    回答带出处        bool(citations)               没问出处对不对
    出处页码在范围内   0 <= page < page_count        50 页文档答案在 37 页，给 0 页也通过
    出处裁剪图可取回   len(crop) > 100               没问图里有没有答案

这个脚本补的就是"对不对"。方法借自 OmniDocBench（借方法，不是借基准）：
**从不报一个综合分**，而是按属性切片给分。综合分只告诉你"变好了 3%"，
切片才告诉你"双栏页的出处命中率只有 40%"。

四个指标
--------
1. 出处页码命中率   期望页是否出现在出处里（默认看 top-1，--any-citation 放宽到任一条）
2. bbox 包含率      期望区域与出处 bbox 是否重叠（需要样本标了 bbox 或 text_anchor）
3. 拒答正确率       answerable=false 的样本是否真的拒答了（零出处 / no_hits / 明说未找到）
4. 降级标记准确率   实际降级标记是否与期望一致（没期望值时只统计分布）

两种模式
--------
offline  只用本地 layout.json + 本层分块 + 关键词检索。不需要任何模型、不需要服务，
         量的是**定位链路本身**（分块粒度、页码归属、bbox 精度）。
         在没有 GPU 的机器上，这是唯一跑得动的那一半，也正好是差异化所在的那一半。
live     打真实 Web 后端（注册 -> 上传 -> 等索引 -> 提问），四个指标全量。
         需要 backend + PG + MinIO + embedding + chat 都在跑。

用法
----
    python scripts/eval_citations.py --mode offline
    python scripts/eval_citations.py --mode live --web http://127.0.0.1:8080
    python scripts/eval_citations.py --mode offline --markdown docs/EVAL-report.md

指标定义与如何加样本见 docs/EVAL.md。
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

DATASET = ROOT / "eval" / "citations.json"
OMNIDOCBENCH_ROOT = Path(os.environ.get(
    "EVAL_OMNIDOCBENCH_ROOT",
    ROOT.parent / "DeepDocParse" / ".eval-cache" / "omnidocbench-v1.6",
))
OMNIDOCBENCH_MANIFEST = ROOT.parent / "DeepDocParse" / "eval" / "omnidocbench-v1.6-slices.json"
REFUSAL_MARKERS = ("未找到", "没有找到", "文档中未", "not found", "no information")


@dataclass
class Outcome:
    sample_id: str
    attributes: list[str]
    answerable: bool
    page_hit: bool | None = None        # None = 该样本不适用这个指标
    bbox_hit: bool | None = None
    refusal_ok: bool | None = None
    degraded: str | None = None
    degraded_ok: bool | None = None
    legacy_page_hit: bool | None = None
    legacy_bbox_hit: bool | None = None
    note: str = ""


@dataclass
class Metric:
    hit: int = 0
    total: int = 0

    def add(self, ok: bool | None) -> None:
        if ok is None:
            return
        self.total += 1
        self.hit += int(ok)

    @property
    def rate(self) -> float | None:
        return self.hit / self.total if self.total else None

    def __str__(self) -> str:
        return "—" if self.rate is None else f"{self.rate:6.1%} ({self.hit}/{self.total})"


@dataclass
class Slice:
    name: str
    page: Metric = field(default_factory=Metric)
    bbox: Metric = field(default_factory=Metric)
    refusal: Metric = field(default_factory=Metric)
    degraded: Metric = field(default_factory=Metric)
    samples: int = 0


ATOM_KINDS = ("code", "equation", "table", "figure")


def atom_kind_of(attributes: list[str]) -> str | None:
    values = set(attributes)
    if "代码密集" in values or "标识符精确查询" in values:
        return "code"
    if "公式密集" in values or "独立公式" in values or "公式行内" in values:
        return "equation"
    if "操作表格" in values or "表格" in values:
        return "table"
    if "图表引用" in values or values & {
            "图注", "示意图", "几何图", "信息图", "物理图像", "统计图", "走势图",
            "表内配图", "多子图"}:
        return "figure"
    return None


def summarize_atoms(outcomes: list[Outcome]) -> dict[str, Metric]:
    """四类原子固定分报；有 bbox 真值时用 bbox，否则退回页码。"""
    metrics = {kind: Metric() for kind in ATOM_KINDS}
    for outcome in outcomes:
        kind = atom_kind_of(outcome.attributes)
        if kind is None:
            continue
        hit = outcome.bbox_hit if outcome.bbox_hit is not None else outcome.page_hit
        metrics[kind].add(hit)
    return metrics


# --------------------------------------------------------------------------- 判定

# 出处 bbox 至少要盖住期望区域的这个比例才算命中。
# **不能用"相交"**：相交的门槛低到几乎恒真（同页的块本来就挤在一块版心里），
# 那样这个指标就只是页码指标的影子，永远不会独立变红 —— 会高估的指标不能用来做决策
MIN_BBOX_COVERAGE = 0.5


def _coverage(want: list[float] | None, got: list[float] | None) -> float:
    """出处框盖住了期望区域的多大比例（交集面积 / 期望区域面积）。"""
    if not want or not got:
        return 0.0
    inter_w = min(want[2], got[2]) - max(want[0], got[0])
    inter_h = min(want[3], got[3]) - max(want[1], got[1])
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    want_area = (want[2] - want[0]) * (want[3] - want[1])
    return (inter_w * inter_h) / want_area if want_area > 0 else 0.0


def _anchor_bbox(layout: dict, anchor: str) -> tuple[int, list[float]] | None:
    """在版面里找到含 anchor 文本的**原始块**，返回 (页码, bbox)。

    这是**半自动标注**的关键：人只需要给出"答案原文长什么样"，
    页码与 bbox 由版面算出来 —— 30~50 条标注的成本从"逐条量坐标"降到"抄一句原文"。

    **必须找原始 para_block，不能走 layout_to_chunks**。曾经这里用
    `layout_to_chunks(layout, max_chars=100000)` 取 ground truth：那个上限会把
    整页所有块并成一个 chunk，bbox 就是整片版心 —— 于是同页的任何出处都必然
    "覆盖"它，bbox 指标恒等于页码指标，永远不会独立变红（2026-08-18 验收抓到）。

    还剩一处循环性，用之前要知道：ground truth 的坐标终究来自解析器。
    所以这个指标衡量的是"检索有没有指到正确的块"，**不是**"解析器切得准不准"。
    后者要人工核对原件。见 docs/EVAL.md。
    """
    for page in layout.get("pdf_info") or []:
        for block in page.get("para_blocks") or []:
            text = " ".join(
                str(span.get("content") or "")
                for line in (block.get("lines") or [])
                for span in (line.get("spans") or []))
            if anchor in text and block.get("bbox"):
                return page.get("page_idx", 0), list(block["bbox"])
    return None


def judge(sample: dict, *, pages: list[int], bboxes: list[list[float]],
          answer: str, degraded: str | None, layout: dict | None,
          any_citation: bool) -> Outcome:
    expect = sample.get("expect") or {}
    answerable = bool(expect.get("answerable", True))
    outcome = Outcome(sample_id=sample["id"], attributes=list(sample.get("attributes") or []),
                      answerable=answerable, degraded=degraded)

    if not answerable:
        # 拒答正确的三种形态，按可信度从高到低：
        #   1. 压根没给出处            —— 最干净
        #   2. 明确标了 no_hits        —— 检索层自己判定无命中
        #   3. 回答里明说"文档中未找到" —— 给了出处但话说清楚了
        # **给了出处、又没标 no_hits、还没明说没找到，就是错的**：
        # 出处是断言，不是装饰，凭空给出处比不回答更糟。
        said_not_found = any(m in answer.lower() for m in REFUSAL_MARKERS)
        outcome.refusal_ok = not pages or degraded == "no_hits" or said_not_found
        outcome.note = f"citations={len(pages)}"
        return outcome

    want_page = expect.get("page_idx")
    want_bbox = expect.get("bbox")
    if want_bbox is None and layout is not None and expect.get("text_anchor"):
        located = _anchor_bbox(layout, expect["text_anchor"])
        if located:
            anchor_page, want_bbox = located
            if want_page is None:
                want_page = anchor_page

    if want_page is not None:
        candidates = pages if any_citation else pages[:1]
        outcome.page_hit = want_page in candidates
        outcome.note = f"want p{want_page + 1}, got {[p + 1 for p in pages] or '无出处'}"
    if want_bbox is not None:
        # **必须同页比**：不同页的两个块坐标当然可能重叠（版式一样），
        # 只比矩形会让"页码错了但 bbox 对了"这种自相矛盾的结果出现，
        # 而且是往好里错 —— 指标一旦会往好里错，就不能拿来做决策了
        pairs = list(zip(pages, bboxes))
        candidates = pairs if any_citation else pairs[:1]
        outcome.bbox_hit = any(page == want_page
                               and _coverage(want_bbox, got) >= MIN_BBOX_COVERAGE
                               for page, got in candidates)
    if "degraded" in expect:
        outcome.degraded_ok = (expect["degraded"] or None) == degraded
    return outcome


# --------------------------------------------------------------------------- offline

def run_offline(samples: list[dict], any_citation: bool) -> list[Outcome]:
    """只用本地版面 + 本层分块 + 关键词检索，量定位链路本身。

    检索用的是纯词面打分（与 MemoryIndex 的关键词路同规则），**没有向量**：
    没有 GPU 的机器上这是唯一跑得动的部分。数字要和 live 分开看 ——
    它是"关键词路单打独斗"的下界，不是产品实际表现。
    """
    from app.config import settings

    layouts: dict[str, dict] = {}
    outcomes: list[Outcome] = []
    for sample in samples:
        source = sample["source"]
        if source["kind"] == "local":
            layout_path = _layout_for(Path(source["path"]))
            if layout_path is None:
                outcomes.append(Outcome(sample["id"], list(sample.get("attributes") or []), True,
                                        note=f"没有可用的版面样本：{source['path']}"))
                continue
            layout = layouts.setdefault(str(layout_path),
                                        json.loads(layout_path.read_text(encoding="utf-8")))
        elif source["kind"] == "omnidocbench":
            key = source["slice"]
            try:
                if key not in layouts:
                    layouts[key] = _omnidocbench_slice_layout(key)
                layout = layouts[key]
            except (FileNotFoundError, KeyError, ValueError) as exc:
                outcomes.append(Outcome(sample["id"], list(sample.get("attributes") or []), True,
                                        note=f"OmniDocBench 版面不可用：{exc}"))
                continue
        else:
            outcomes.append(Outcome(sample["id"], list(sample.get("attributes") or []),
                                    bool((sample.get("expect") or {}).get("answerable", True)),
                                    note="offline 模式跳过外部样本（用 --mode live）"))
            continue

        from ddp_core.chunking import layout_to_chunks
        chunks = layout_to_chunks(layout, settings.chunk_max_chars)
        ranked = _keyword_rank(sample["question"], chunks)[:settings.qa_top_k]
        outcome = judge(
            sample,
            pages=[c["page_idx"] for c in ranked],
            bboxes=[c["bbox"] for c in ranked],
            answer="", degraded=None if ranked else "no_hits",
            layout=layout, any_citation=any_citation)
        if "标识符精确查询" in (sample.get("attributes") or []):
            legacy = _keyword_rank(
                sample["question"], chunks, code_boost=False)[:settings.qa_top_k]
            baseline = judge(
                sample,
                pages=[c["page_idx"] for c in legacy],
                bboxes=[c["bbox"] for c in legacy],
                answer="", degraded=None if legacy else "no_hits",
                layout=layout, any_citation=any_citation)
            outcome.legacy_page_hit = baseline.page_hit
            outcome.legacy_bbox_hit = baseline.bbox_hit
        outcomes.append(outcome)
    return outcomes


def _layout_for(pdf_path: Path) -> Path | None:
    """本地样本对应的真实版面产物（`backend/tests/fixtures/layout-<stem>.json`）。"""
    candidate = ROOT / "backend" / "tests" / "fixtures" / f"layout-{pdf_path.stem}.json"
    return candidate if candidate.exists() else None


def _poly_bbox(poly: list[float]) -> list[float]:
    if len(poly) < 4 or len(poly) % 2:
        raise ValueError(f"非法 polygon：{poly}")
    xs, ys = poly[0::2], poly[1::2]
    return [min(xs), min(ys), max(xs), max(ys)]


def _plain_table(table_html: str) -> str:
    """给关键词路一份可检索的表格文本；行列关系仍由 table_html 保留。"""
    with_breaks = re.sub(r"</(?:td|th|tr|li)\s*>", " ", table_html, flags=re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", with_breaks)).split())


def _omnidocbench_page(entry: dict, page_idx: int) -> dict:
    type_map = {
        "title": "title", "header": "title",
        "table": "table", "table_caption": "table", "table_footnote": "table",
        "figure": "figure", "chart_mask": "figure",
        "figure_caption": "figure", "figure_footnote": "figure",
        "equation_isolated": "equation", "equation_semantic": "equation",
        "list_group": "list",
    }
    blocks = []
    for det in sorted(entry.get("layout_dets") or [],
                      key=lambda item: item.get("order")
                      if isinstance(item.get("order"), (int, float)) else float("inf")):
        if det.get("ignore"):
            continue
        category = str(det.get("category_type") or "text_block")
        table_html = str(det.get("html") or "") if category == "table" else ""
        content = str(det.get("text") or det.get("latex") or "")
        if table_html:
            content = _plain_table(table_html)
        is_visual_atom = category in {"chart_mask", "figure"}
        if not content.strip() and not is_visual_atom:
            continue
        block = {
            "type": type_map.get(category, "text"),
            "bbox": _poly_bbox(list(det.get("poly") or [])),
            # 视觉原子没有臆造文字：它仍以 figure+bbox 进入版面，检索不到时
            # bbox 指标应如实变红，而不是让评测适配器先把原子删掉。
            "lines": [],
        }
        if content.strip():
            span = {"content": content}
            if table_html:
                # DDP-Layout v1.1 的可选承诺在 spans[].html，**不是**块顶层。
                # 放错位置会让生产 table_html() 与抽取评测都看不到行列结构。
                span["html"] = table_html
            block["lines"] = [{"spans": [span]}]
        blocks.append(block)

    info = entry["page_info"]
    return {
        "page_idx": page_idx,
        "page_size": [info["width"], info["height"]],
        "para_blocks": blocks,
    }


def _omnidocbench_layout(image_path: str, root: Path | None = None) -> dict:
    """把一页官方标注适配成 DDP-Layout（主要用于适配器单测）。"""
    corpus_root = root or OMNIDOCBENCH_ROOT
    entries = json.loads(
        (corpus_root / "OmniDocBench.subset.json").read_text(encoding="utf-8"))
    entry = next((item for item in entries
                  if (item.get("page_info") or {}).get("image_path") == image_path), None)
    if entry is None:
        raise KeyError(image_path)
    return {
        "layout_version": "ddp-layout/1",
        "provider": {"name": "OmniDocBench", "version": "1.6"},
        "pdf_info": [_omnidocbench_page(entry, 0)],
    }


def _omnidocbench_slice_layout(slice_name: str, root: Path | None = None,
                                manifest_path: Path | None = None) -> dict:
    """按 manifest 固定顺序组装一个 10 页域，页码指标因此不会天然命中。"""
    corpus_root = root or OMNIDOCBENCH_ROOT
    manifest_file = manifest_path or OMNIDOCBENCH_MANIFEST
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    images = (manifest.get("slices") or {}).get(slice_name)
    if not images:
        raise KeyError(slice_name)
    entries = json.loads(
        (corpus_root / "OmniDocBench.subset.json").read_text(encoding="utf-8"))
    by_image = {(entry.get("page_info") or {}).get("image_path"): entry for entry in entries}
    missing = [image for image in images if image not in by_image]
    if missing:
        raise KeyError(f"{slice_name}: {missing}")
    return {
        "layout_version": "ddp-layout/1",
        "provider": {"name": "OmniDocBench", "version": "1.6"},
        "pdf_info": [_omnidocbench_page(by_image[image], page_idx)
                     for page_idx, image in enumerate(images)],
    }


def _keyword_rank(question: str, chunks: list[dict], *, code_boost: bool = True) -> list[dict]:
    # 与生产 PgVectorIndex / MemoryIndex 用**同一套 tokenizer 与 OR 语义**。
    # 旧实现按标点切，整句中文会变成一个超长 token，中文切片大量 no_hits，
    # 量到的是评测器自己的缺陷而不是产品关键词路。
    from ddp_core.tokenize import query_string

    terms = [term for term in query_string(question).split() if term]
    scored: list[tuple[int, int, dict]] = []
    for index, chunk in enumerate(chunks):
        indexed = (chunk.get("text_tokenized") or query_string(chunk["text"])).lower().split()
        scored.append((sum(indexed.count(term) for term in terms), index, chunk))
    keyword = [(index, chunk) for score, index, chunk
               in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
    if not code_boost:
        return [chunk for _, chunk in keyword]

    # 与生产一样：原关键词名次 + code 精确命中名次做 RRF，不把词频
    # 和向量分数硬加。保留 code_boost=False 作同一评测集上的改造前对照。
    from ddp_core.search import EXACT_CODE_WEIGHT, RRF_K, exact_code_ids
    by_id = {str(index): chunk for index, chunk in keyword}
    kw_ids = list(by_id)
    code_ids = exact_code_ids([
        (str(index), chunk.get("block_type", "text"), chunk.get("text", ""))
        for index, chunk in keyword], question)
    fused: dict[str, float] = {}
    for ranked in (kw_ids, *([code_ids] * EXACT_CODE_WEIGHT)):
        for rank, cid in enumerate(ranked):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return [by_id[cid] for cid in sorted(fused, key=fused.get, reverse=True)]


# --------------------------------------------------------------------------- live

async def _upload_once(http, web: str, headers: dict, source: dict,
                       cache: dict[str, str | None]) -> tuple[str, str | None]:
    """同一文档成功或失败都只尝试一次；失败必须被缓存为可见状态。"""
    key = source.get("path") or source.get("url") or source.get("slice")
    if key not in cache:
        cache[key] = await _upload_and_wait(http, web, headers, source)
    return key, cache[key]


async def run_live(samples: list[dict], web: str, any_citation: bool) -> list[Outcome]:
    import httpx

    outcomes: list[Outcome] = []
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as http:
        user = f"eval_{uuid.uuid4().hex[:8]}"
        resp = await http.post(f"{web}/api/auth/register",
                               json={"username": user, "password": "eval-run-password"})
        resp.raise_for_status()
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        uploaded: dict[str, str | None] = {}
        truth_layouts: dict[str, dict | None] = {}
        for sample in samples:
            source = sample["source"]
            key, document_id = await _upload_once(http, web, headers, source, uploaded)
            if document_id is None:
                outcomes.append(Outcome(sample["id"],
                                        list(sample.get("attributes") or []), True,
                                        note=f"上传/索引失败：{key}"))
                continue
            if key not in truth_layouts:
                truth_layouts[key] = _ground_truth_layout(source)
            outcomes.append(await _ask_and_judge(http, web, headers, document_id, sample,
                                                 any_citation, truth_layouts[key]))
    return outcomes


def _ground_truth_layout(source: dict) -> dict | None:
    """live 也必须有独立真值版面，否则 text_anchor 的 bbox 列会整列“不适用”。"""
    if source["kind"] == "local":
        path = _layout_for(Path(source["path"]))
        return json.loads(path.read_text(encoding="utf-8")) if path else None
    if source["kind"] == "omnidocbench":
        return _omnidocbench_slice_layout(source["slice"])
    return None


async def _upload_and_wait(http, web: str, headers: dict, source: dict) -> str | None:
    if source["kind"] == "local":
        content = (ROOT / source["path"]).resolve().read_bytes()
        name = Path(source["path"]).name
    elif source["kind"] == "omnidocbench":
        path = OMNIDOCBENCH_ROOT / "slice-documents" / f"{source['slice']}.pdf"
        content = path.read_bytes()
        name = path.name
    else:
        # 外部样本只记 URL，不进仓库 —— 再分发别人的 PDF 是另一回事
        content = (await http.get(source["url"], follow_redirects=True)).content
        name = source["url"].rsplit("/", 1)[-1] or "sample.pdf"

    resp = await http.post(f"{web}/api/documents", headers=headers,
                           files={"file": (name, content, "application/pdf")})
    if resp.status_code != 202:
        return None
    document_id = resp.json()["id"]
    for _ in range(120):
        doc = (await http.get(f"{web}/api/documents/{document_id}", headers=headers)).json()
        if doc["index_status"] == "ready":
            return document_id
        if doc["status"] == "failed" or doc["index_status"] == "failed":
            return None
        await asyncio.sleep(5)
    return None


async def _ask_and_judge(http, web: str, headers: dict, document_id: str, sample: dict,
                         any_citation: bool, layout: dict | None = None) -> Outcome:
    cid = (await http.post(f"{web}/api/documents/{document_id}/conversations",
                           headers=headers)).json()["id"]
    events: list[tuple[str, dict]] = []
    async with http.stream("POST", f"{web}/api/conversations/{cid}/ask", headers=headers,
                           json={"question": sample["question"]}) as resp:
        buffer = ""
        async for piece in resp.aiter_text():
            buffer += piece
    for block in buffer.split("\n\n"):
        lines = block.splitlines()
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))

    answer = "".join(d.get("text", "") for n, d in events if n == "delta")
    done = dict(events).get("done", {})
    citations = dict(events).get("citations", {}).get("citations", [])
    return judge(sample, pages=[c["page_idx"] for c in citations],
                 bboxes=[c.get("bbox") for c in citations], answer=answer,
                 degraded=done.get("degraded"), layout=layout, any_citation=any_citation)


# --------------------------------------------------------------------------- 报表

def summarize(outcomes: list[Outcome]) -> tuple[Slice, list[Slice]]:
    overall = Slice("全部")
    by_attr: dict[str, Slice] = {}
    for outcome in outcomes:
        targets = [overall] + [by_attr.setdefault(a, Slice(a)) for a in outcome.attributes]
        for target in targets:
            target.samples += 1
            target.page.add(outcome.page_hit)
            target.bbox.add(outcome.bbox_hit)
            target.refusal.add(outcome.refusal_ok)
            target.degraded.add(outcome.degraded_ok)
    return overall, sorted(by_attr.values(), key=lambda s: s.name)


def render(outcomes: list[Outcome], mode: str) -> str:
    overall, slices = summarize(outcomes)
    lines = [f"# 出处评测报表（mode={mode}）", "",
             f"样本 {len(outcomes)} 条。指标定义见 docs/EVAL.md。", "",
             "| 切片 | 样本 | 页码命中率 | bbox 包含率 | 拒答正确率 | 降级标记准确率 |",
             "|---|---|---|---|---|---|"]
    for s in [overall, *slices]:
        lines.append(f"| {s.name} | {s.samples} | {s.page} | {s.bbox} | {s.refusal} | "
                     f"{s.degraded} |")
    atoms = summarize_atoms(outcomes)
    lines += ["", "## 四类原子命中率（固定分报）", "",
              "| 原子 | 命中率 |", "|---|---|"]
    for kind in ATOM_KINDS:
        lines.append(f"| `{kind}` | {atoms[kind]} |")
    identifier = [outcome for outcome in outcomes
                  if outcome.legacy_page_hit is not None or outcome.legacy_bbox_hit is not None]
    if identifier:
        before_page, after_page = Metric(), Metric()
        before_bbox, after_bbox = Metric(), Metric()
        for outcome in identifier:
            before_page.add(outcome.legacy_page_hit)
            after_page.add(outcome.page_hit)
            before_bbox.add(outcome.legacy_bbox_hit)
            after_bbox.add(outcome.bbox_hit)
        lines += ["", "## 标识符精确查询（同集合改造前后）", "",
                  "| 指标 | 改造前（通用关键词路） | 改造后（code 精确路） | 绝对提升 |",
                  "|---|---|---|---|",
                  f"| 页码命中率 | {before_page} | {after_page} | "
                  f"{_delta(before_page, after_page)} |",
                  f"| bbox 命中率 | {before_bbox} | {after_bbox} | "
                  f"{_delta(before_bbox, after_bbox)} |"]
    lines += ["", "## 逐条", "", "| 样本 | 页码 | bbox | 拒答 | 降级 | 备注 |", "|---|---|---|---|---|---|"]
    mark = {True: "✅", False: "❌", None: "—"}
    for o in outcomes:
        lines.append(f"| `{o.sample_id}` | {mark[o.page_hit]} | {mark[o.bbox_hit]} | "
                     f"{mark[o.refusal_ok]} | {o.degraded or '—'} | {o.note} |")
    return "\n".join(lines) + "\n"


def _delta(before: Metric, after: Metric) -> str:
    if before.rate is None or after.rate is None:
        return "—"
    return f"{after.rate - before.rate:+.1%}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dataset", action="append",
                        help="评测集 JSON；可重复传入并合并（缺省 eval/citations.json）")
    parser.add_argument("--web", default=os.environ.get("WEB_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--markdown", help="把报表写到这个文件")
    parser.add_argument("--any-citation", action="store_true",
                        help="命中判定放宽到「出现在任意一条出处里」（默认只看 top-1）")
    args = parser.parse_args()

    datasets = args.dataset or [str(DATASET)]
    samples = [sample for dataset in datasets for sample in
               json.loads(Path(dataset).read_text(encoding="utf-8"))["samples"]]
    if args.mode == "offline":
        outcomes = run_offline(samples, args.any_citation)
    else:
        outcomes = asyncio.run(run_live(samples, args.web.rstrip("/"), args.any_citation))

    report = render(outcomes, args.mode)
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"报表已写入 {args.markdown}")

    overall, _ = summarize(outcomes)
    # 有指标但一条都没命中 = 评测本身跑歪了（数据集写错、服务没起），非零退出
    measured = [m for m in (overall.page, overall.bbox, overall.refusal) if m.total]
    return 0 if any(m.hit for m in measured) or not measured else 1


if __name__ == "__main__":
    raise SystemExit(main())
