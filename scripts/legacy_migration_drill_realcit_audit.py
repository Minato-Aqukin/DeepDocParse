#!/usr/bin/env python3
"""Export the REAL era-run citation audit for the realcit drill leg (T62).

Reads every citation row the era backend wrote (evidence/citations dual-write
via ``app.evidence.record_evidence``) from the realcit source DB at 0012/0013
and writes two files:
  --audit: per-citation pre-migration truth (snippet, page_idx, bbox, digest,
    plus evidence_id/parse_job_id/seq so the verifier can rebuild the era
    locator without guessing)
  --report: run summary (counts, pages, era commits, degradation note)

Unlike ``legacy_migration_drill_seed.py`` (which CONSTRUCTS rows with era
dual-write semantics without running old code), this script only READS rows
the era stack actually wrote over HTTP (register -> upload -> parse ->
index -> 5 QA rounds). It inserts nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--era-backend-commit", default="e6b702a")
    ap.add_argument("--era-gateway-commit", default="2f0e391")
    args = ap.parse_args()

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(args.dsn)
    try:
        async with eng.connect() as conn:
            rev = (await conn.execute(
                text("SELECT version_num FROM alembic_version"))).scalar()
            rows = (await conn.execute(text(
                "SELECT c.id, c.source_kind, c.source_id, c.score, c.similarity,"
                " c.snippet, c.rank, c.content_digest,"
                " e.id AS evidence_id, e.parse_job_id, e.seq,"
                " e.page_idx, e.bbox, e.page_size"
                " FROM citations c JOIN evidence e ON e.id = c.evidence_id"
                " ORDER BY c.created_at, c.rank"))).mappings().all()
            counts = {}
            for t, in (await conn.execute(text(
                    "SELECT 'users:' || count(*) FROM users UNION ALL "
                    "SELECT 'documents:' || count(*) FROM documents UNION ALL "
                    "SELECT 'chunks:' || count(*) FROM chunks UNION ALL "
                    "SELECT 'evidence:' || count(*) FROM evidence UNION ALL "
                    "SELECT 'citations:' || count(*) FROM citations UNION ALL "
                    "SELECT 'assertions:' || count(*) FROM assertions UNION ALL "
                    "SELECT 'messages:' || count(*) FROM messages"))).all():
                k, v = t.split(":")
                counts[k] = int(v)
            degraded = (await conn.execute(text(
                "SELECT DISTINCT coalesce(degraded, '-') FROM messages"
                " WHERE role = 'assistant'"))).scalars().all()
    finally:
        await eng.dispose()

    audit = [
        {"citation_id": r["id"], "source_kind": r["source_kind"],
         "source_id": r["source_id"], "score": r["score"],
         "similarity": r["similarity"], "snippet": r["snippet"],
         "rank": r["rank"], "content_digest": r["content_digest"],
         "evidence_id": r["evidence_id"], "parse_job_id": r["parse_job_id"],
         "seq": r["seq"], "page_idx": r["page_idx"], "bbox": r["bbox"],
         "page_size": r["page_size"]}
        for r in rows
    ]
    pages = sorted({a["page_idx"] for a in audit})
    digits = [bool(a["content_digest"]) for a in audit]
    audit_path = Path(args.audit)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    report = {
        "legacy_revision": rev,
        "era_backend_commit": args.era_backend_commit,
        "era_gateway_commit": args.era_gateway_commit,
        "counts": counts,
        "real_citations": len(audit),
        "citation_pages": pages,
        "citations_with_digest": sum(digits),
        "assistant_degraded": sorted(degraded),
        "note": "every citation row written by era code over HTTP"
                " (register -> upload -> parse -> index -> 5 QA rounds);"
                " this script only reads, never inserts",
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"realcit audit: {len(audit)} citations across pages {pages}"
          f" (digest {sum(digits)}/{len(audit)}) -> {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
