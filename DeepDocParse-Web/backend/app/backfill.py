"""历史出处回填：老 JSON -> evidence / citations（plan.md 阶段 3）。

**这是全重构最容易产出假出处的一步。** 老记录当年只存了截断过的 snippet，
没有内容指纹；把它们搬进新表时，唯一诚实的做法是**分清哪些验证过、哪些没有**，
而不是给每一条都补一个"当前块的指纹"—— 那等于替历史宣布"它一直指着这里"。

## 三个去处，加起来必须等于老记录总数

    anchored    块还在，且 snippet 对得上  -> 写指纹，从此走**严格**判据
    unanchored  对不上 / 块没了 / 当年就没存 snippet -> 指纹留空，继续走**宽松**判据
    skipped     连定位键都没有，或那次解析已经不存在 -> **建不出证据行**

`anchored + unanchored + skipped == total`，这条恒等式就是 plan.md 要求的
"回填计数对账：一条不丢"。`skipped` 非零不是失败，但**必须被看见** ——
阶段 4 删老列之前，那些出处会随老列一起消失。

## 为什么 unanchored 不是"标失效"

指纹留空时读路径回落到老判据（snippet 包含）。于是：
  - 内容对不上 -> 照样 resolved=False（失效，正确）
  - 当年没存 snippet -> 无从判断，**不冤枉它**（既有行为，原样保留）
把后一种也标成失效的话，一批老回答会突然集体显示"出处已失效"，
而它们其实没有任何问题。

## 为什么用 keyset 分页

`plan-v2.md` 抓到过：OFFSET 在并发写入下**会漏行，且完全静默**。
按主键 `id > :last` 走，漏不掉也不会重复 —— id 是随机 UUID，
顺序无意义但唯一且稳定，keyset 要的正是这两条。

## 与 alembic 的关系

判据从 `ddp_core.anchor` 来（运行时同一份，判据漂移会直接产出假出处）；
**表结构则一律走裸表名 SQL**，不 import ORM 模型 —— 迁移要能在
"模型已经改过好几轮"之后仍然跑出当年的语义，这是 alembic 的常规要求。
"""
import json
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa

from ddp_core.anchor import digest_of, same_content


@dataclass
class BackfillReport:
    total: int = 0
    anchored: int = 0
    unanchored: int = 0
    skipped_no_locator: int = 0
    skipped_no_job: int = 0
    already_present: int = 0
    evidence_created: int = 0
    sources: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def accounted(self) -> int:
        return (self.anchored + self.unanchored + self.already_present
                + self.skipped_no_locator + self.skipped_no_job)

    def check(self) -> None:
        """一条不丢。**对不上就抛** —— 回填悄悄少搬几条正是最难发现的那种错。"""
        if self.accounted != self.total:
            raise RuntimeError(
                f"回填计数对不上：老记录 {self.total} 条，"
                f"去处合计 {self.accounted} 条"
                f"（anchored={self.anchored} unanchored={self.unanchored} "
                f"already={self.already_present} "
                f"no_locator={self.skipped_no_locator} no_job={self.skipped_no_job}）")

    def __str__(self) -> str:
        return (f"回填 {self.sources} 个来源、{self.total} 条老出处："
                f"锚定 {self.anchored} · 未锚定 {self.unanchored} · "
                f"已存在 {self.already_present} · "
                f"无定位键 {self.skipped_no_locator} · 解析已不存在 {self.skipped_no_job}"
                f"；新建证据 {self.evidence_created} 行")


def _as_json(value):
    """JSON 列在不同驱动下读出来可能是 dict/list，也可能是字符串。"""
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _locators(citations) -> dict:
    """与 `app.evidence.locators_of` **同一条规则**（有守卫钉着）。"""
    from app.evidence import locators_of

    return locators_of(citations if isinstance(citations, list) else [])


def _iter_keyset(conn, table: str, columns: str, batch: int):
    """按主键 keyset 翻页。**不用 OFFSET** —— 并发写入下会漏行且静默。"""
    last = ""
    while True:
        rows = conn.execute(
            sa.text(f"SELECT id, {columns} FROM {table} "
                    f"WHERE id > :last ORDER BY id LIMIT :n"),
            {"last": last, "n": batch}).fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1][0]


def backfill(conn, *, batch: int = 500) -> BackfillReport:
    report = BackfillReport()

    live_jobs = {r[0] for r in conn.execute(sa.text("SELECT id FROM parse_jobs"))}

    for row in _iter_keyset(conn, "messages", "citations", batch):
        citations = _as_json(row[1])
        if citations:
            report.sources += 1
            _one_source(conn, report, "message", row[0], citations, live_jobs)

    for row in _iter_keyset(conn, "extraction_items", "fields", batch):
        fields = _as_json(row[1]) or {}
        if not isinstance(fields, dict):
            continue
        for name, cell in fields.items():
            if isinstance(cell, dict) and cell.get("citations"):
                report.sources += 1
                _one_source(conn, report, "extract_field", f"{row[0]}:{name}",
                            cell["citations"], live_jobs)

    report.check()
    return report


def _one_source(conn, report: BackfillReport, source_kind: str, source_id: str,
                citations: list, live_jobs: set) -> None:
    raw = citations if isinstance(citations, list) else []
    report.total += len(raw)
    locators = _locators(raw)
    # 有定位键的之外，剩下的就是"连定位键都没有"的
    report.skipped_no_locator += len(raw) - _counted(raw, locators)

    for rank, ((job_id, seq), citation) in enumerate(locators.items()):
        if job_id not in live_jobs:
            # evidence.parse_job_id 是非空外键，那次解析没了就建不出证据行
            report.skipped_no_job += 1
            continue

        chunk = conn.execute(
            sa.text("SELECT id, document_id, text, page_idx, bbox, page_size, block_type "
                    "FROM chunks WHERE parse_job_id = :job AND seq = :seq"),
            {"job": job_id, "seq": seq}).fetchone()

        # **只有真的验证过才写指纹。** 块没了、或 snippet 对不上，
        # 都留空让读路径回落到老判据 —— 见模块 docstring
        anchored = bool(chunk) and bool(citation.get("snippet")) and same_content(
            snippet=citation.get("snippet") or "", chunk_text=chunk[2], digest="")
        digest = digest_of(chunk[2]) if anchored else ""

        evidence_id = _ensure_evidence(conn, report, job_id, seq, citation, chunk, digest)
        if _has_citation(conn, source_kind, source_id, evidence_id):
            report.already_present += 1
            continue
        conn.execute(
            # created_at 显式给：PG 上这列有 server_default（迁移里写的），
            # 但单测的 SQLite 库是 create_all 建的，模型侧只有 Python 默认值 ——
            # 裸 SQL 绕过 ORM，不给就撞 NOT NULL。两个方言都显式给最稳
            sa.text("INSERT INTO citations (id, evidence_id, source_kind, source_id, "
                    "role, score, similarity, snippet, rank, content_digest, "
                    "created_at) VALUES "
                    "(:id, :ev, :kind, :sid, 'primary', :score, :sim, :snip, :rank, "
                    ":digest, CURRENT_TIMESTAMP)"),
            {"id": uuid.uuid4().hex, "ev": evidence_id, "kind": source_kind,
             "sid": source_id, "score": citation.get("score"),
             "sim": citation.get("similarity"),
             "snip": citation.get("snippet") or "", "rank": rank,
             # 验证过才写。老记录当年没存指纹，凭空补一个等于替历史作证
             "digest": digest})
        if anchored:
            report.anchored += 1
        else:
            report.unanchored += 1


def _counted(raw: list, locators: dict) -> int:
    """`locators` 去过重，所以不能直接用它的长度反推"有定位键的条数"。"""
    from app.evidence import _locator

    return sum(1 for c in raw if isinstance(c, dict) and _locator(c) is not None)


def _ensure_evidence(conn, report: BackfillReport, job_id: str, seq: int,
                     citation: dict, chunk, digest: str) -> str:
    existing = conn.execute(
        sa.text("SELECT id, content_digest, crop_key FROM evidence "
                "WHERE parse_job_id = :job AND seq = :seq AND derived_from IS NULL "
                "ORDER BY created_at LIMIT 1"),
        {"job": job_id, "seq": seq}).fetchone()
    if existing:
        # 已有证据（双写建的，或上一轮回填建的）。**只补空缺，绝不覆盖**：
        # 双写那条是当场记下的，比回填推断出来的更可信
        if digest and not existing[1]:
            conn.execute(sa.text("UPDATE evidence SET content_digest = :d WHERE id = :id"),
                         {"d": digest, "id": existing[0]})
        if citation.get("crop_key") and not existing[2]:
            conn.execute(sa.text("UPDATE evidence SET crop_key = :k WHERE id = :id"),
                         {"k": citation["crop_key"], "id": existing[0]})
        return existing[0]

    evidence_id = uuid.uuid4().hex
    # 块还在就用块上的字段（page_size 只有这里有）；块没了只能回放老 JSON
    # 记下的那份 —— 那正是"这个回答当时拿哪块区域作证"的审计事实
    conn.execute(
        sa.text("INSERT INTO evidence (id, document_id, doc_version, parse_job_id, seq, "
                "atom_key, page_idx, bbox, page_size, kind, crop_key, content_digest, content, "
                "provider, provider_fingerprint, review_state, created_at) VALUES "
                "(:id, :doc, 0, :job, :seq, :atom, :page, :bbox, :psize, :kind, :crop, "
                ":digest, :content, :provider, '', 'unreviewed', CURRENT_TIMESTAMP)"),
        {"id": evidence_id,
         "doc": chunk[1] if chunk else _document_of(conn, job_id),
         "job": job_id, "seq": seq, "atom": f"source:{seq}",
         "page": citation.get("page_idx") if citation.get("page_idx") is not None
                 else (chunk[3] if chunk else 0),
         "bbox": json.dumps(citation.get("bbox") or (_as_json(chunk[4]) if chunk else None)),
         "psize": json.dumps(citation.get("page_size")
                             or (_as_json(chunk[5]) if chunk else None)),
         "kind": (chunk[6] if chunk else None) or "text",
         "crop": citation.get("crop_key"), "digest": digest,
         "content": chunk[2] if chunk else citation.get("snippet", ""),
         "provider": json.dumps({"backfilled": True})})
    report.evidence_created += 1
    return evidence_id


def _document_of(conn, job_id: str) -> str:
    row = conn.execute(sa.text("SELECT document_id FROM parse_jobs WHERE id = :id"),
                       {"id": job_id}).fetchone()
    return row[0]


def _has_citation(conn, source_kind: str, source_id: str, evidence_id: str) -> bool:
    return conn.execute(
        sa.text("SELECT 1 FROM citations WHERE source_kind = :k AND source_id = :s "
                "AND evidence_id = :e AND role = 'primary'"),
        {"k": source_kind, "s": source_id, "e": evidence_id}).fetchone() is not None
