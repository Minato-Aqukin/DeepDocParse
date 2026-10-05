#!/usr/bin/env python3
"""Seed mixed-ownership + real old-era citations on a COPY of the web snapshot.

Ownership fixtures (for verifier 1d -- recorded org vs assigned org):
  - UNAMBIGUOUS: one document whose single uploader's recorded
    documents.organization_id is a synthetic sentinel org (NOT real snapshot data) -> the backfilled resource MUST
    keep that org (never quarantine a decided case).
  - AMBIGUOUS: one pre-existing document with >= 2 uploaders (web snapshot
    already has these) -> additional-uploader rows MUST be quarantined to
    ``migration:unresolved`` and MUST NOT carry the first uploader's org.
  The seed additionally stamps one fresh unambiguous document itself so the
  cit dataset always has >= 1 of each kind even if the snapshot changes.

Citation fixtures (for verifier clause 3):
  - chunks compiled with the CURRENT borndigital path (same deterministic
    chunking the 0012 era used via ddp_core) from tests/fixtures/long-doc.pdf;
    no old code is executed (the era stack needs an unavailable gateway).
  - 2 matching citations land via the era dual-write path (per-citation
    digest = digest_of(chunk text)) -> backfill classifies them anchored.
  - 1 adversarial citation (snippet NOT in the chunk) is seeded with an EMPTY
    content_digest and NO pre-existing evidence/citation row -> the history
    backfill must classify it unanchored (unanchored >= 1), never forged.

Usage:
  .venv/bin/python scripts/legacy_migration_drill_seed.py \
      --dsn 'postgresql+asyncpg://ddp:ddp@127.0.0.1:15509/deepdocparse' \
      --pdf tests/fixtures/long-doc.pdf --report <path.json>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "services" / "corpus-api"))
sys.path.insert(0, str(ROOT / "services" / "model-gateway"))
sys.path.insert(0, str(ROOT / "python" / "ddp_core" / "src"))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ddp_core.application.borndigital import extract_pages, to_markdown
    from ddp_gateway.services import layout as layoutmod
    from ddp_core.compilation import compile_chunks, provider_of

    raw = Path(args.pdf).read_bytes()
    assert raw[:5] == b"%PDF-", "not a PDF"
    pages = extract_pages(raw)
    assert pages, "borndigital found no text layer"
    lay = layoutmod.build(pages, engine="borndigital", code_detection="heuristic")
    prov = provider_of(layout=lay, parse_options_hash="h",
                       embedding_model="test", vision_model="test")
    chunks = compile_chunks(lay, max_chars=800, provider=prov)
    assert chunks and any(c.get("search_text") for c in chunks), "no indexable chunks"
    _ = to_markdown(pages)

    doc_id = hashlib.sha256(raw).hexdigest()
    now = datetime.now(UTC).isoformat()
    eng = create_async_engine(args.dsn)
    # The drill runs the seed when the cit DB is at 0013 (post control-migrate
    # + alembic 0012->0013, pre migrate.py): documents.organization_id exists
    # (0013 adds it, default ''), control.organizations does NOT (control
    # schema lands later via control-migrate up). So the unambiguous fixture
    # stamps a stable sentinel org id (not the stamped default, not ''), and
    # migrate.py's stamp_organization skips it (only '' rows are stamped).
    SENTINEL_ORG = "c17seedorg0000000000000000000001"
    try:
        async with eng.begin() as conn:
            users = (await conn.execute(text("SELECT id FROM users ORDER BY created_at"))).fetchall()
            assert len(users) >= 2, "snapshot needs >= 2 users for mixed fixtures"
            owner_a, owner_b = users[0][0], users[1][0]
            # UNAMBIGUOUS fixture: synthetic doc_id (NOT the PDF hash, so it
            # cannot collide with the snapshot's long-doc row under the
            # (doc_id, origin) unique constraint), single uploader, stamped
            # with the sentinel org BEFORE migration (recorded org). 0015
            # must keep it; migrate.py's stamp_organization only touches ''
            # rows. Chunks still come from the real PDF parse above.
            real_org = SENTINEL_ORG
            did_u = hashlib.sha256(f"cit-unambig:{doc_id}".encode()).hexdigest()[:32]
            doc_id_u = hashlib.sha256(f"cit-unambig-doc:{doc_id}".encode()).hexdigest()
            jid = hashlib.sha256(f"cit-job:{doc_id}".encode()).hexdigest()[:32]
            cid = hashlib.sha256(b"cit-conv").hexdigest()[:32]
            await conn.execute(text(
                "INSERT INTO documents (id, uploaded_by, organization_id, doc_id, origin,"
                " filename, mime, size_bytes, object_key, index_status, current_job_id,"
                " created_at, updated_at)"
                " VALUES (:id, :u, :org, :doc, 'web', 'cit-unambiguous.pdf',"
                " 'application/pdf', :sz, '', 'ready', :job, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                " ON CONFLICT (id) DO NOTHING"),
                {"id": did_u, "u": owner_a, "org": real_org, "doc": doc_id_u,
                 "sz": len(raw), "job": jid})
            await conn.execute(text(
                "INSERT INTO document_uploads (id, document_id, user_id, created_at)"
                " VALUES (:id, :doc, :u, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"),
                {"id": hashlib.sha256(b"cit-upl-u").hexdigest()[:32], "doc": did_u, "u": owner_a})
            # AMBIGUOUS fixture: reuse the snapshot's long-doc.pdf document, which
            # already has >= 1 uploader; add a SECOND uploader with no recorded org.
            # 0015 must quarantine the second row (never the first uploader's org).
            existing = (await conn.execute(text(
                "SELECT id, uploaded_by FROM documents WHERE doc_id = :doc AND origin = 'web'"),
                {"doc": doc_id})).fetchone()
            if existing:
                did = existing[0]
            else:
                did = hashlib.sha256(f"cit-doc:{doc_id}".encode()).hexdigest()[:32]
                await conn.execute(text(
                    "INSERT INTO documents (id, uploaded_by, doc_id, origin, filename, mime,"
                    " size_bytes, object_key, index_status, current_job_id, created_at, updated_at)"
                    " VALUES (:id, :u, :doc, 'web', 'long-doc.pdf', 'application/pdf', :sz,"
                    " '', 'ready', :job, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    " ON CONFLICT (id) DO NOTHING"),
                    {"id": did, "u": owner_a, "doc": doc_id, "sz": len(raw), "job": jid})
                await conn.execute(text(
                    "INSERT INTO document_uploads (id, document_id, user_id, created_at)"
                    " VALUES (:id, :doc, :u, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"),
                    {"id": hashlib.sha256(b"cit-upl-a").hexdigest()[:32], "doc": did, "u": owner_a})
            await conn.execute(text(
                "INSERT INTO document_uploads (id, document_id, user_id, created_at)"
                " VALUES (:id, :doc, :u, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"),
                {"id": hashlib.sha256(b"cit-upl-b").hexdigest()[:32], "doc": did, "u": owner_b})
            await conn.execute(text(
                "INSERT INTO parse_jobs (id, document_id, engine, options, options_hash, status,"
                " page_count, result_prefix, initiated_by, document_version, created_at, updated_at)"
                " VALUES (:id, :doc, 'borndigital', CAST('{}' AS JSON), 'h', 'succeeded',"
                " 5, '', :u, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                " ON CONFLICT (id) DO NOTHING"),
                {"id": jid, "doc": did, "u": owner_a})
            fp_full = json.dumps(prov)
            fp = fp_full if len(fp_full) <= 64 else hashlib.sha256(fp_full.encode()).hexdigest()
            for seq, c in enumerate(chunks):
                await conn.execute(text(
                    "INSERT INTO chunks (id, document_id, parse_job_id, seq, page_idx, bbox,"
                    " page_size, text, char_len, block_type, text_tokenized, search_text,"
                    " provider, provider_fingerprint)"
                    " VALUES (:id, :doc, :job, :seq, :page, CAST(:bbox AS JSON),"
                    " CAST(:psize AS JSON), :text, :clen, :bt, :tok, :stext,"
                    " CAST(:prov AS JSON), :fp)"
                    " ON CONFLICT (id) DO NOTHING"),
                    {"id": hashlib.sha256(f"cit-chunk:{jid}:{seq}".encode()).hexdigest()[:32],
                     "doc": did, "job": jid, "seq": seq, "page": c["page_idx"],
                     "bbox": json.dumps(c["bbox"]), "psize": json.dumps(c["page_size"]),
                     "text": c["text"], "clen": c.get("char_len", len(c["text"])),
                     "bt": c.get("block_type", "text"), "tok": c.get("text_tokenized", ""),
                     "stext": c.get("search_text", c["text"]),
                     "prov": json.dumps(c.get("provider", {})), "fp": fp})
            # 0013 renamed conversations.user_id -> actor_id (seed runs at 0013).
            await conn.execute(text(
                "INSERT INTO conversations (id, document_id, actor_id, title, created_at, updated_at)"
                " VALUES (:id, :doc, :u, 'cit-seed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                " ON CONFLICT (id) DO NOTHING"),
                {"id": cid, "doc": did, "u": owner_a})
            # Legacy citations JSON: 2 matching + 1 adversarial (snippet NOT in
            # the chunk). The adversarial row is seeded with an EMPTY digest
            # and NO evidence/citation row, so the history backfill must
            # classify it unanchored (unanchored >= 1), never forged.
            cites = []
            for seq in (0, 1):
                c = chunks[seq]
                cites.append({
                    "chunk_id": "legacy-dangling", "parse_job_id": jid, "seq": seq,
                    "page_idx": c["page_idx"], "bbox": c["bbox"], "crop_key": None,
                    "score": 0.03, "similarity": 0.7, "rank": seq,
                    "snippet": c["text"][:20], "page_size": c["page_size"]})
            bad = chunks[2]
            cites.append({
                "chunk_id": "legacy-dangling", "parse_job_id": jid, "seq": 2,
                "page_idx": bad["page_idx"], "bbox": bad["bbox"], "crop_key": None,
                "score": 0.03, "similarity": 0.7, "rank": 2,
                "snippet": "这段文字根本不在原文里，不该被锚定", "page_size": bad["page_size"]})
            mid = hashlib.sha256(b"cit-msg").hexdigest()[:32]
            await conn.execute(text(
                "INSERT INTO messages (id, conversation_id, role, content, verified,"
                " model_meta, created_at) VALUES (:id, :cid, 'assistant', '答',"
                " FALSE, CAST('{}' AS JSON), CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING"),
                {"id": mid, "cid": cid})
            # Era dual-write for the 2 MATCHING rows only (legacy_mode):
            # evidence from chunk fields, per-citation digest from chunk text.
            from ddp_core.anchor import digest_of as _digest_of
            for c in cites[:2]:
                ch = chunks[c["seq"]]
                ev = hashlib.sha256(f"cit-ev:{jid}:{c['seq']}".encode()).hexdigest()[:32]
                await conn.execute(text(
                    "INSERT INTO evidence (id, document_id, parse_job_id, seq,"
                    " atom_key, page_idx, bbox, page_size, kind, content_digest, content,"
                    " provider, provider_fingerprint, review_state, created_at)"
                    " VALUES (:id, :doc, :job, :seq, :atom, :page, CAST(:bbox AS JSON),"
                    " CAST(:psize AS JSON), :kind, :digest, :content,"
                    " CAST(:prov AS JSON), :fp, 'unreviewed', CURRENT_TIMESTAMP)"
                    " ON CONFLICT (id) DO NOTHING"),
                    {"id": ev, "doc": did, "job": jid, "seq": c["seq"],
                     "atom": f"source:{c['seq']}:{_digest_of(ch['text'])[:16]}",
                     "page": ch["page_idx"], "bbox": json.dumps(ch["bbox"]),
                     "psize": json.dumps(ch["page_size"]), "kind": ch.get("block_type", "text"),
                     "digest": _digest_of(ch["text"]), "content": ch["text"],
                     "prov": json.dumps(ch.get("provider", {})), "fp": fp})
                await conn.execute(text(
                    "INSERT INTO citations (id, evidence_id, source_kind, source_id, role,"
                    " score, similarity, snippet, rank, content_digest, created_at)"
                    " VALUES (:id, :ev, 'message', :sid, 'primary', :score, :sim, :snip,"
                    " :rank, :digest, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING"),
                    {"id": hashlib.sha256(f"cit-cite:{mid}:{c['seq']}".encode()).hexdigest()[:32],
                     "ev": ev, "sid": mid, "score": c.get("score"), "sim": c.get("similarity"),
                     "snip": c.get("snippet") or "", "rank": c["seq"],
                     "digest": _digest_of(ch["text"])})
            cites_path = Path(args.report).parent / "cit-legacy-citations.json"
            cites_path.write_text(json.dumps(cites, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    finally:
        await eng.dispose()

    report = {
        "seeded_at": now, "pdf": args.pdf, "doc_sha256": doc_id,
        "unambiguous_document_id": did_u, "unambiguous_owner": owner_a,
        "unambiguous_org": real_org,
        "ambiguous_document_id": did, "ambiguous_second_uploader": owner_b,
        "document_id": did, "parse_job_id": jid, "conversation_id": cid,
        "message_id": mid, "chunks": len(chunks),
        "legacy_citations": len(cites), "matching": 2, "mismatching": 1,
        "note": "unambiguous doc stamped with a real recorded org; ambiguous doc"
                " gains a second uploader with no recorded org; adversarial citation"
                " seeded with empty digest and no pre-existing row (backfill must"
                " classify it unanchored)",
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"seeded {len(chunks)} chunks + {len(cites)} legacy citations -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
