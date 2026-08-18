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
import json
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

DATASET = ROOT / "eval" / "citations.json"
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
        if source["kind"] != "local":
            outcomes.append(Outcome(sample["id"], list(sample.get("attributes") or []),
                                    bool((sample.get("expect") or {}).get("answerable", True)),
                                    note="offline 模式跳过外部样本（用 --mode live）"))
            continue
        layout_path = _layout_for(Path(source["path"]))
        if layout_path is None:
            outcomes.append(Outcome(sample["id"], list(sample.get("attributes") or []), True,
                                    note=f"没有可用的版面样本：{source['path']}"))
            continue
        layout = layouts.setdefault(str(layout_path),
                                    json.loads(layout_path.read_text(encoding="utf-8")))

        from app.chunking import layout_to_chunks
        chunks = layout_to_chunks(layout, settings.chunk_max_chars)
        ranked = _keyword_rank(sample["question"], chunks)[:settings.qa_top_k]
        outcomes.append(judge(
            sample,
            pages=[c["page_idx"] for c in ranked],
            bboxes=[c["bbox"] for c in ranked],
            answer="", degraded=None if ranked else "no_hits",
            layout=layout, any_citation=any_citation))
    return outcomes


def _layout_for(pdf_path: Path) -> Path | None:
    """本地样本对应的真实版面产物（`backend/tests/fixtures/layout-<stem>.json`）。"""
    candidate = ROOT / "backend" / "tests" / "fixtures" / f"layout-{pdf_path.stem}.json"
    return candidate if candidate.exists() else None


def _keyword_rank(question: str, chunks: list[dict]) -> list[dict]:
    import re

    terms = [t for t in re.split(r"[\s\W_]+", question.lower()) if t]
    scored = [(sum(c["text"].lower().count(t) for t in terms), c) for c in chunks]
    return [c for score, c in sorted(scored, key=lambda p: p[0], reverse=True) if score > 0]


# --------------------------------------------------------------------------- live

async def run_live(samples: list[dict], web: str, any_citation: bool) -> list[Outcome]:
    import httpx

    outcomes: list[Outcome] = []
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as http:
        user = f"eval_{uuid.uuid4().hex[:8]}"
        resp = await http.post(f"{web}/api/auth/register",
                               json={"username": user, "password": "eval-run-password"})
        resp.raise_for_status()
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        uploaded: dict[str, str] = {}
        for sample in samples:
            source = sample["source"]
            key = source.get("path") or source.get("url")
            if key not in uploaded:
                document_id = await _upload_and_wait(http, web, headers, source)
                if document_id is None:
                    outcomes.append(Outcome(sample["id"],
                                            list(sample.get("attributes") or []), True,
                                            note=f"上传/索引失败：{key}"))
                    continue
                uploaded[key] = document_id
            outcomes.append(await _ask_and_judge(http, web, headers, uploaded[key], sample,
                                                 any_citation))
    return outcomes


async def _upload_and_wait(http, web: str, headers: dict, source: dict) -> str | None:
    if source["kind"] == "local":
        content = (ROOT / source["path"]).resolve().read_bytes()
        name = Path(source["path"]).name
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
                         any_citation: bool) -> Outcome:
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
                 degraded=done.get("degraded"), layout=None, any_citation=any_citation)


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
    lines += ["", "## 逐条", "", "| 样本 | 页码 | bbox | 拒答 | 降级 | 备注 |", "|---|---|---|---|---|---|"]
    mark = {True: "✅", False: "❌", None: "—"}
    for o in outcomes:
        lines.append(f"| `{o.sample_id}` | {mark[o.page_hit]} | {mark[o.bbox_hit]} | "
                     f"{mark[o.refusal_ok]} | {o.degraded or '—'} | {o.note} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--web", default=os.environ.get("WEB_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--markdown", help="把报表写到这个文件")
    parser.add_argument("--any-citation", action="store_true",
                        help="命中判定放宽到「出现在任意一条出处里」（默认只看 top-1）")
    args = parser.parse_args()

    samples = json.loads(Path(args.dataset).read_text(encoding="utf-8"))["samples"]
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
