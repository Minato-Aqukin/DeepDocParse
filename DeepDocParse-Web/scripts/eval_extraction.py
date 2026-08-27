#!/usr/bin/env python
"""抽取评测 —— 「结构化信息提取」这条线上的度量。

沿用 docs/EVAL.md 的方法论，一个字不改：**按属性切片，从不报综合分**。
综合分只告诉你「变好了 3%」，切片才告诉你「表格类字段的准确率只有 40%」——
前者没法指导任何决策，后者直接指向下一步该修哪儿。

## 四个指标

| 指标 | 定义 | 适用字段 |
|---|---|---|
| **字段准确率** | 抽出来的值与期望值一致（按类型归一化后比较） | 期望 `status=found` 的字段 |
| **字段出处命中率** | 该字段的出处落在期望页码上（默认只看 top-1 出处） | 同上，且样本标了 page |
| **空值正确率** | 文档里确实没有的字段，是否真的报了 not_found | 期望 `status=not_found` 的字段 |
| **schema 合规率** | 结果整体是否符合 DDP-Extract v1（自检无问题）。**负样本反着算**：标了 `expect.schema_valid=false` 的样本，被守卫拦下才算通过 | 每个样本一次 |

**空值正确率是这套评测的核心**，不是凑数的第四个指标。抽取里最危险的输出是
"看起来像结论的空值"：把"我们的检索挂了"报成"文档里没有"，用户会直接拿去用。
这一条对应 docs/EVAL.md 里的「拒答正确率」，方法论上是同一件事。

## 两种模式

| 模式 | 依赖 | 量的是什么 |
|---|---|---|
| `offline` | 只要本地版面样本 + 本层分块与 schema 代码 | **schema 层与定位链路**：字段是否检索得到、schema 校验是否严格。**不调模型**，所以量不到抽值准确率 |
| `live` | backend + PG + MinIO + embedding + chat 全在跑 | 四个指标全量，就是用户真实拿到的东西 |

没有 GPU 的机器上只跑得动 `offline` —— 而它恰好覆盖差异化所在的那一半（定位与契约），
不是模型能力那一半。**offline 的数字不能代表产品表现，别混着引用。**

用法：
    python scripts/eval_extraction.py --mode offline
    python scripts/eval_extraction.py --mode live --web http://127.0.0.1:8080
    python scripts/eval_extraction.py --mode offline --markdown docs/EVAL-extraction-report.md
"""
import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

DATASET = ROOT / "eval" / "extractions.json"


@dataclass
class FieldOutcome:
    sample_id: str
    field: str
    attributes: list[str]
    value_ok: bool | None = None        # None = 该字段不适用这个指标
    citation_ok: bool | None = None
    empty_ok: bool | None = None
    note: str = ""


@dataclass
class SampleOutcome:
    sample_id: str
    attributes: list[str]
    schema_ok: bool | None = None
    fields: list[FieldOutcome] = field(default_factory=list)
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
    value: Metric = field(default_factory=Metric)
    citation: Metric = field(default_factory=Metric)
    empty: Metric = field(default_factory=Metric)
    schema: Metric = field(default_factory=Metric)
    samples: int = 0


# --------------------------------------------------------------------------- 判定

# 数值比较的相对容差。抽出来的 "12,345.00 元" 与期望的 12345 应该算一致；
# 但 12345 与 12346 不算 —— 抽取结果是要被当数据用的，不能糊
_NUMBER_TOLERANCE = 1e-6
_STRIP = re.compile(r"[\s,，。、；;:：]+")


def values_equal(want, got) -> bool:
    """期望值与抽出来的值是否一致。

    比较前做**最小限度**的归一化：数字去掉千分位与空白，字符串去掉空白与标点。
    **不做同义词、不做模糊匹配** —— 那会把"差不多对"算成对，而这个指标存在的
    全部意义就是把"差不多"和"对"分开。
    """
    if want is None or got is None:
        return want is got
    if isinstance(want, bool) or isinstance(got, bool):
        return bool(want) is bool(got)
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) <= _NUMBER_TOLERANCE * max(abs(float(want)), 1.0)
        except (TypeError, ValueError):
            return False
    return _STRIP.sub("", str(want)).lower() == _STRIP.sub("", str(got)).lower()


def judge_fields(sample: dict, fields: dict, *, any_citation: bool) -> list[FieldOutcome]:
    """拿一份抽取结果对着期望逐字段判定。"""
    outcomes: list[FieldOutcome] = []
    attributes = sample.get("attributes", [])
    for name, expect in (sample.get("expect", {}).get("fields") or {}).items():
        got = fields.get(name) or {}
        status = got.get("status")
        outcome = FieldOutcome(sample_id=sample["id"], field=name, attributes=attributes)

        if expect.get("status") == "not_found":
            # 空值正确率：**error 不算对**。"我们没能抽出来"和"文档里没有"是两回事，
            # 把前者算成后者正是这个指标要抓的错误
            outcome.empty_ok = status == "not_found"
            if status == "found":
                outcome.note = f"编了一个值：{got.get('value')!r}"
            elif status == "error":
                outcome.note = f"报了 error（{got.get('degraded')}）而不是 not_found"
            outcomes.append(outcome)
            continue

        # 期望 found
        if status != "found":
            outcome.value_ok = False
            outcome.citation_ok = False if expect.get("page") is not None else None
            outcome.note = f"没抽到（status={status}，degraded={got.get('degraded')}）"
            outcomes.append(outcome)
            continue

        outcome.value_ok = values_equal(expect.get("value"), got.get("value"))
        if not outcome.value_ok:
            outcome.note = f"期望 {expect.get('value')!r}，得到 {got.get('value')!r}"

        want_page = expect.get("page")
        if want_page is not None:
            citations = got.get("citations") or []
            pages = ([c.get("page_idx") for c in citations] if any_citation
                     else [citations[0].get("page_idx")] if citations else [])
            outcome.citation_ok = want_page in pages
            if not outcome.citation_ok:
                outcome.note = (outcome.note + "；" if outcome.note else "") + \
                    f"出处页 {pages} 不含期望页 {want_page}"
        outcomes.append(outcome)
    return outcomes


# --------------------------------------------------------------------------- offline

def run_offline(samples: list[dict], any_citation: bool) -> list[SampleOutcome]:
    """不调模型，只量 schema 层与定位链路。

    做两件事：
    1. **schema 校验**：样本的 schema 必须通过 validate_schema。
       坏 schema 会在生产上表现成"抽不到"，看起来像模型不行 —— 先在这里挡住
    2. **可定位性**：对每个期望 found 的字段，用关键词路在版面派生的块里找一遍，
       看它的期望页是否进得了候选。这是抽值准确率的**上界** ——
       检索都到不了那一页，模型再强也抽不出来
    """
    from ddp_core.chunking import layout_to_chunks
    from ddp_core.extract_format import parse_schema, validate_schema
    from ddp_core.tokenize import tokens as tokenize

    outcomes: list[SampleOutcome] = []
    for sample in samples:
        result = SampleOutcome(sample_id=sample["id"], attributes=sample.get("attributes", []))
        problems = validate_schema(sample.get("schema") or {})
        # 样本可以**期望 schema 非法**（负样本：验证守卫真的会拦）。
        # 不区分的话这类样本永远显示 0% 合规，看起来像 bug，而它恰恰是通过了
        want_valid = sample.get("expect", {}).get("schema_valid", True)
        result.schema_ok = (not problems) == want_valid
        if problems:
            result.note = ("按预期被拦下：" if not want_valid else "") + problems[0]
        if problems or not want_valid:
            outcomes.append(result)
            continue

        layout_path = ROOT / sample["layout"]
        if not layout_path.exists():
            result.note = f"版面样本不存在：{sample['layout']}"
            result.schema_ok = None
            outcomes.append(result)
            continue

        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        chunks = layout_to_chunks(layout)
        spec = parse_schema(sample["schema"])
        by_name = {f.name: f for f in spec.fields}

        for name, expect in (sample.get("expect", {}).get("fields") or {}).items():
            field_spec = by_name.get(name)
            outcome = FieldOutcome(sample_id=sample["id"], field=name,
                                   attributes=result.attributes)
            if field_spec is None:
                outcome.note = "期望里的字段不在 schema 中"
                result.fields.append(outcome)
                continue

            hits = _keyword_rank(field_spec.query, chunks, tokenize)
            pages = [c["page_idx"] for c in hits[:4]]
            want_page = expect.get("page")

            if expect.get("status") == "not_found":
                # **offline 判不了空值正确率，所以这里什么都不判。**
                #
                # 原来写的是 `not hits if not hits else None` —— 取值只可能是
                # True 或 None，**永远不会是 False**，于是这一列结构上恒等于 100%。
                # 那是 EVAL.md 里「bbox 指标恒等于页码指标」那个先例的重演：
                # 一个永远不会红的指标不能用来做决策，摆在报表上只会制造虚假的安心。
                #
                # 检索零命中确实意味着下游必然 not_found，但那量的是"检索没给
                # 编造的机会"，不是"模型没编"——把它算进空值正确率是偷换概念。
                # 真正的空值正确率只有 live 模式量得到。
                outcome.note = ("检索零命中（下游必然 not_found）" if not hits
                                else "检索有命中，能否正确拒答只有 live 量得到")
            elif want_page is not None:
                outcome.citation_ok = want_page in pages
                if not outcome.citation_ok:
                    outcome.note = f"关键词路候选页 {pages} 不含期望页 {want_page}"
            result.fields.append(outcome)
        outcomes.append(result)
    return outcomes


def _keyword_rank(query: str, chunks: list[dict], tokenize) -> list[dict]:
    """纯词面打分。与 offline 模式的定位一样，是**下界**而不是产品表现。"""
    import math

    q = set(tokenize(query))
    if not q:
        return []
    docs = [set(tokenize(c["text"])) for c in chunks]
    n = len(chunks) or 1
    idf = {t: math.log(1 + n / (1 + sum(1 for d in docs if t in d))) for t in q}
    scored = [(sum(idf[t] for t in q if t in d), c) for c, d in zip(chunks, docs)]
    scored = [(s, c) for s, c in scored if s > 0]
    scored.sort(key=lambda p: p[0], reverse=True)
    return [c for _, c in scored]


# --------------------------------------------------------------------------- live

async def run_live(samples: list[dict], web: str, any_citation: bool) -> list[SampleOutcome]:
    """全链路：上传 -> 等索引 -> 建 run -> 等完成 -> 判定。"""
    import httpx

    from ddp_core.extract_format import validate_result

    outcomes: list[SampleOutcome] = []
    # trust_env=False 是契约的一部分（铁律 7）：本机 SOCKS 代理会污染 localhost 调用
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0),
                                 trust_env=False) as http:
        headers = await _login(http, web)
        for sample in samples:
            result = SampleOutcome(sample_id=sample["id"],
                                   attributes=sample.get("attributes", []))
            try:
                document_id = await _upload_and_index(http, web, headers, sample)
                if document_id is None:
                    result.note = "上传或索引失败"
                    outcomes.append(result)
                    continue
                items = await _run_extraction(http, web, headers, sample, document_id)
                if not items:
                    result.note = "抽取没有产出结果"
                    outcomes.append(result)
                    continue
                fields = items[0].get("fields") or {}
                # 结果自检：形状不合 DDP-Extract v1 就是我们的问题，不是模型的
                problems = validate_result({
                    "extract_version": "ddp-extract/1", "status": items[0].get("status", "ok"),
                    "degraded": items[0].get("degraded"), "fields": fields,
                })
                result.schema_ok = not problems
                if problems:
                    result.note = problems[0]
                result.fields = judge_fields(sample, fields, any_citation=any_citation)
            except Exception as exc:  # noqa: BLE001
                result.note = f"{type(exc).__name__}: {exc}"
            outcomes.append(result)
    return outcomes


async def _login(http, web: str) -> dict:
    user = f"eval_extract_{uuid.uuid4().hex[:8]}"
    await http.post(f"{web}/api/auth/register",
                    json={"username": user, "password": "eval-password-1"})
    resp = await http.post(f"{web}/api/auth/login",
                           json={"username": user, "password": "eval-password-1"})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _upload_and_index(http, web: str, headers: dict, sample: dict) -> str | None:
    path = ROOT / sample["source"]
    files = {"file": (path.name, path.read_bytes(), "application/pdf")}
    resp = await http.post(f"{web}/api/documents", headers=headers, files=files,
                           data={"engine": os.environ.get("EVAL_ENGINE", "borndigital"),
                                 "options": "{}"})
    resp.raise_for_status()
    document_id = resp.json()["id"]
    for _ in range(120):
        await asyncio.sleep(2)
        info = (await http.get(f"{web}/api/documents/{document_id}", headers=headers)).json()
        if info.get("index_status") == "ready":
            return document_id
        if info.get("index_status") == "failed" or info.get("status") == "failed":
            return None
    return None


async def _run_extraction(http, web: str, headers: dict, sample: dict,
                          document_id: str) -> list[dict]:
    resp = await http.post(f"{web}/api/extractions/runs", headers=headers, json={
        "document_ids": [document_id], "schema_json": sample["schema"],
        "name": f"eval/{sample['id']}"})
    resp.raise_for_status()
    run_id = resp.json()["id"]
    for _ in range(180):
        await asyncio.sleep(2)
        detail = (await http.get(f"{web}/api/extractions/runs/{run_id}",
                                 headers=headers)).json()
        if detail["run"]["status"] in ("succeeded", "partial", "failed"):
            return detail["items"]
    return []


# --------------------------------------------------------------------------- 报表

def summarize(outcomes: list[SampleOutcome]) -> tuple[Slice, list[Slice]]:
    overall = Slice("全部")
    by_attr: dict[str, Slice] = {}
    for sample in outcomes:
        targets = [overall] + [by_attr.setdefault(a, Slice(a)) for a in sample.attributes]
        for target in targets:
            target.samples += 1
            target.schema.add(sample.schema_ok)
            for f in sample.fields:
                target.value.add(f.value_ok)
                target.citation.add(f.citation_ok)
                target.empty.add(f.empty_ok)
    return overall, sorted(by_attr.values(), key=lambda s: s.name)


def render(outcomes: list[SampleOutcome], mode: str) -> str:
    overall, slices = summarize(outcomes)
    lines = [f"# 抽取评测报表（mode={mode}）", "",
             f"样本 {len(outcomes)} 条。指标定义见 docs/EVAL-extraction.md。",
             "**从不报综合分**：下面每一行都是一个切片。", ""]
    if mode == "offline":
        lines += ["> offline 模式不调模型，量的是 schema 层与关键词路的可定位性。",
                  "> 「字段准确率」这一列必然为空 —— 它需要 live。", ""]
    lines += ["| 切片 | 样本 | 字段准确率 | 出处命中率 | 空值正确率 | schema 合规率 |",
              "|---|---|---|---|---|---|"]
    for s in [overall, *slices]:
        lines.append(f"| {s.name} | {s.samples} | {s.value} | {s.citation} | {s.empty} | "
                     f"{s.schema} |")

    lines += ["", "## 逐字段", "", "| 样本 | 字段 | 值 | 出处 | 空值 | 备注 |",
              "|---|---|---|---|---|---|"]
    mark = {True: "✅", False: "❌", None: "—"}
    for sample in outcomes:
        if sample.note:
            lines.append(f"| `{sample.sample_id}` | — | — | — | — | {sample.note} |")
        for f in sample.fields:
            lines.append(f"| `{f.sample_id}` | {f.field} | {mark[f.value_ok]} | "
                         f"{mark[f.citation_ok]} | {mark[f.empty_ok]} | {f.note} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--web", default=os.environ.get("WEB_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--markdown", help="把报表写到这个文件")
    parser.add_argument("--any-citation", action="store_true",
                        help="出处命中放宽到「出现在任意一条出处里」（默认只看 top-1）")
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
    # 有指标但一条都没命中 = 评测本身跑歪了（数据集写错、服务没起），非零退出。
    # 与 eval_citations.py 同一个判据
    measured = [m for m in (overall.value, overall.citation, overall.empty, overall.schema)
                if m.total]
    return 0 if any(m.hit for m in measured) or not measured else 1


if __name__ == "__main__":
    raise SystemExit(main())
