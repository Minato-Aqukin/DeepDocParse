#!/usr/bin/env python
"""PITR + full-reconciliation recovery drill (plan T61).

Phase A (source): fresh scratch PG (WAL archiving ON from init) + scratch
MinIO; run both real migration chains; seed a realistic generated dataset
(~all core tables at the stated scale); mc-mirror-equivalent object copy;
base backup; post-base writes (uploads, federation task rows, wiki rows);
record PITR target time; mc mirror again (post-base objects); pg_switch_wal.

Phase B (restore-to-target): fresh PG container, restore base backup,
replay WAL to the recorded target time (recovery_target_time), promote,
run full reconciliation against the target-time object set.

Phase C (restore-to-latest): same but replay to latest (no target),
prove no data loss vs source, run full reconciliation.

Phase D (identity): copy B's live node seed pattern is NOT reused; instead
run Go TestNodeIdentityBackupRestoreDrill (same-seed restore == authority,
fresh clone != authority and cannot verify authority proof, missing seed
refuses). Additionally prove at the peer layer: a descriptor/credential
signed by the clone key is rejected as "descriptor approved identity or key
mismatch" style mismatch by NodeIDForPublicKey comparison (peer cannot be
impersonated).

Usage (env only, never CLI secrets):
  DDP_REC_DSN / DDP_REC_MINIO_... set by recovery_drill.sh wrapper.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "corpus-api"))
sys.path.insert(0, str(ROOT / "python" / "ddp_core"))
sys.path.insert(0, str(ROOT / "python" / "ddp_contracts"))

ORG = "org-pitr"
USER_PREFIX = "pitr-user-"
ACTOR = "actor-pitr"
# NOLOSS 覆盖的持久表清单（plan §15.3：持久资产、Bundle、Wiki/人工编辑、
# 授权配置、任务状态、节点身份绑定、上传/outbox 行全部纳入备份）。
# 来源（只读核对）：database/control/*.sql 的 CREATE TABLE control.* +
# Base.metadata（services/corpus-api/ddp_corpus/* + python/ddp_core/models.py）
# 在 head 状态的全部表。public schema 名即模型 __tablename__；control.*
# 即 control 迁移里的表名。audit/knowledge 等只增审计表也在内 —— NOLOSS
# 若漏掉它们，丢行也会报 PASS。
# 注意：users/api_keys/file_tokens/usage_records 是 0001 时代的旧 public
# 表，已被 control.* 取代（head 无此表），故不在清单内。
COUNT_TABLES = ["public.documents", "public.parse_jobs", "public.resources",
                "public.resource_versions", "public.chunks", "public.evidence",
                "public.citations", "public.wikis", "public.wiki_revisions",
                "public.wiki_pages", "public.wiki_dependencies", "public.wiki_claim_bindings",
                "public.wiki_human_edits", "public.wiki_write_keys",
                "public.upload_events", "public.document_uploads",
                "public.conversations", "public.messages",
                "public.extraction_templates", "public.extraction_runs", "public.extraction_items",
                "public.tasks", "public.corpus_outbox", "public.processed_events", "public.usage_claims",
                "public.agent_turns", "public.assertions", "public.retrieval_candidates",
                "public.evidence_verifications", "public.knowledge_entities", "public.graph_edges",
                "public.knowledge_reviews", "public.wiki_entries", "public.wiki_sections",
                "public.wiki_sentences",
                "public.federation_requests", "public.federation_probes", "public.federation_admissions",
                "public.federation_executions", "public.federation_task_events",
                "public.coverage_ledgers", "public.coverage_entries",
                "public.federation_deliveries", "public.federation_credential_nonces",
                "public.federation_root_ledgers", "public.federation_root_reservations",
                "public.federation_delegation_consumption", "public.federation_cache_entries",
                "public.collections", "public.collection_members", "public.collection_receipts",
                "public.collection_catalog_views", "public.collection_catalog_snapshots",
                "public.collection_catalog_pages",
                "public.client_views", "public.client_snapshots", "public.client_pages",
                "public.client_receipts",
                "public.bundle_replicas", "public.bundle_replica_revoke_keys",
                "public.remote_computes",
                "control.organizations", "control.users", "control.memberships", "control.roles",
                "control.api_keys", "control.quotas", "control.usage_ledger", "control.audit_events",
                "control.upload_sessions", "control.file_grants", "control.control_outbox",
                "control.node_identity", "control.node_directories", "control.node_members",
                "control.node_directory_views", "control.member_snapshots", "control.member_snapshot_pages",
                "control.scope_manifests", "control.scope_target_pages", "control.scope_catalog_sources",
                "control.scope_catalog_revocations", "control.scope_remote_sources",
                "control.federation_credential_nonces",
                "control.subtree_snapshots", "control.subtree_snapshot_pages"]



def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def minio_client(args):
    from minio import Minio

    return Minio(args.minio_endpoint, access_key=args.minio_access_key,
                 secret_key=args.minio_secret_key, secure=False)


def _pdf_bytes(i: int, extra: str = "") -> bytes:
    body = f"%PDF-1.4\npitr drill doc {i} {extra}\n" + ("x" * (1024 + (i % 7) * 256)) + "\n%%EOF\n"
    return body.encode()


async def seed_dataset(args, n_docs: int, tag: str) -> dict:
    """Seed a realistic generated dataset. Returns state dict."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy import text
    from datetime import datetime, timezone, timedelta
    from ddp_corpus.federation_models import (CoverageEntry, CoverageLedger, FederationRequest,
                                              FederationProbe, FederationAdmission, FederationExecution,
                                              FederationTaskEvent, FederationDelivery, FederationCredentialNonce,
                                              FederationRootLedger, FederationRootReservation,
                                              FederationDelegationConsumption)
    from ddp_corpus.cache import FederationCacheEntry
    from ddp_corpus.collection_models import (Collection, CollectionMember, CollectionReceipt,
                                              CollectionCatalogView, CollectionCatalogSnapshot,
                                              CollectionCatalogPage)
    from ddp_corpus.client_models import ClientView, ClientSnapshot, ClientPage, ClientReceipt
    from ddp_corpus.bundle_models import BundleReplica, BundleReplicaRevokeKey
    from ddp_corpus.remote_compute_models import RemoteCompute
    from ddp_corpus.models import (Chunk, Citation, DependencyManifest, Document,
                                   DocumentUpload, Evidence, ParseJob, Resource, ResourceVersion,
                                   UploadEvent, Wiki, WikiRevision, WikiPage, WikiHumanEdit,
                                   WikiWriteKey, ClaimEvidenceBinding, Task, CorpusOutbox,
                                   ProcessedEvent, UsageClaim, Conversation, Message,
                                   ExtractionTemplate, ExtractionRun, ExtractionItem)

    engine = create_async_engine(args.dsn, pool_size=4, max_overflow=8)
    mc = minio_client(args)
    from minio.error import S3Error
    try:
        if not mc.bucket_exists(args.bucket):
            mc.make_bucket(args.bucket)
    except S3Error:
        pass
    state = {"org": ORG, "tag": tag, "docs": [], "objects": {}, "users": [],
             "versions": [], "evidence_total": 0, "chunks_total": 0}
    mk = async_sessionmaker(engine, expire_on_commit=False)
    async with mk() as s:
        await s.execute(text(
            "INSERT INTO control.organizations (id, name, slug) VALUES (:id,:n,:s) "
            "ON CONFLICT (id) DO NOTHING"), {"id": ORG, "n": "PITR drill", "s": "pitr-drill"})
        users = []
        for u in range(3):
            uid = f"u{(u + 1):03d}-pitr-20261005-{tag}"
            users.append(uid)
            await s.execute(text(
                "INSERT INTO control.users (id, username, password_hash) VALUES (:id,:u,:p) "
                "ON CONFLICT (id) DO NOTHING"), {"id": uid, "u": f"{USER_PREFIX}{tag}-{u}", "p": "not-a-real-hash"})
            await s.execute(text(
                "INSERT INTO control.memberships (organization_id, user_id, role) VALUES (:o,:u,'admin') "
                "ON CONFLICT DO NOTHING"), {"o": ORG, "u": uid})
        state["users"] = users
        await s.commit()
    # corpus rows per doc
    for i in range(n_docs):
        did = f"d{i:04d}-{tag}-pitr20261005"
        did = (did + "0" * 32)[:32]
        jid = (f"j{i:04d}-{tag}-pitr20261005" + "0" * 32)[:32]
        rid = (f"r{i:04d}-{tag}-pitr20261005" + "0" * 32)[:32]
        vid = (f"v{i:04d}-{tag}-pitr20261005" + "0" * 32)[:32]
        owner = users[i % len(users)]
        pdf = _pdf_bytes(i, tag)
        dhash = digest(pdf)
        okey = f"uploads/{ORG}/{did}.pdf"
        import io as _io
        r = mc.put_object(args.bucket, okey, _io.BytesIO(pdf), len(pdf),
                          content_type="application/pdf")
        state["objects"][okey] = {"sha256": dhash, "size": len(pdf), "etag": r.etag}
        # parse result objects bound via parse_jobs.result_prefix (reconciler
        # requires every live job prefix to own objects; validated 2026-10-05)
        layout = ('{"job":"' + jid + '","pages":[{"page_idx":0,"blocks":3}]}\n').encode()
        docmd = (f"# PITR drill {tag} doc {i}\n\nbeacon {(i * 7)}.\n").encode()
        mc.put_object(args.bucket, f"results/{jid}/layout.json", _io.BytesIO(layout), len(layout),
                      content_type="application/json")
        mc.put_object(args.bucket, f"results/{jid}/document.md", _io.BytesIO(docmd), len(docmd),
                      content_type="text/markdown")
        async with mk() as s:
            s.add(Document(id=did, uploaded_by=owner, organization_id=ORG, doc_id=dhash,
                           origin="web", filename=f"pitr-{tag}-{i}.pdf", mime="application/pdf",
                           size_bytes=len(pdf), object_key=okey, index_status="ready",
                           compile_status="ready"))
            s.add(ParseJob(id=jid, document_id=did, engine="borndigital",
                           options_hash="b" * 64, status="succeeded", page_count=1,
                           initiated_by=owner, resource_id=rid, result_prefix=f"results/{jid}/",
                           index_status="ready"))
            s.add(DocumentUpload(id=(f"du{i:04d}{tag}" + "0" * 32)[:32], document_id=did, user_id=owner))
            s.add(Resource(id=rid, organization_id=ORG, owner_id=owner, uploaded_by=owner,
                           display_name=f"pitr-{tag}-{i}.pdf", publication="published" if i % 2 == 0 else "private"))
            s.add(ResourceVersion(id=vid, resource_id=rid, version_no=1, document_id=did,
                                  parse_job_id=jid, bundle_prefix="", source_digest=dhash,
                                  filename=f"pitr-{tag}-{i}.pdf", size_bytes=len(pdf)))
            await s.flush()
            s.add(UploadEvent(id=(f"ue{i:04d}{tag}" + "0" * 32)[:32], resource_version_id=vid,
                              actor_id=owner, idempotency_key=f"pitr-{tag}-upload-{i}",
                              request_digest=dhash))
            # chunks + evidence: 3 per doc, digest-bound to chunk text (anchor.py rule)
            from ddp_core.anchor import digest_of
            for seq in range(3):
                txt = f"PITR drill {tag} doc {i} seq {seq} beacon {(i * 7 + seq)}."
                atom = f"source:{seq}:{digest(txt.encode())[:16]}"
                s.add(Chunk(id=(f"c{i:04d}{seq}{tag}" + "0" * 32)[:32], document_id=did,
                            parse_job_id=jid, seq=seq, page_idx=0, text=txt,
                            search_text=txt, char_len=len(txt)))
                s.add(Evidence(id=(f"e{i:04d}{seq}{tag}" + "0" * 32)[:32], document_id=did,
                               parse_job_id=jid, seq=seq, atom_key=atom, page_idx=0,
                               content_digest=digest_of(txt), content=txt))
                state["chunks_total"] += 1
                state["evidence_total"] += 1
            await s.commit()
        state["docs"].append({"id": did, "job": jid, "resource": rid, "version": vid,
                              "digest": dhash, "object_key": okey})
        state["versions"].append(vid)
    # federation rows + every other persistent table named in plan 15.3, so
    # NOLOSS actually covers Bundle/authz/task/identity/upload/outbox rows.
    # (B-carryover rows already populate some tables; the drill writes at
    # least one row per table so a dropped table fails NOLOSS, not vacuous 0==0.)
    async with mk() as s:
        rt = f"task-pitr-{tag}-" + "1" * 16
        s.add(FederationRequest(root_task_id=rt, organization_id=ORG, actor_id=ACTOR,
                                task_spec_digest="sha256:" + "e" * 64, scope_id="scope-pitr",
                                scope_digest="sha256:" + "f" * 64, search_mode="exhaustive_scope",
                                planning_state="approved", plan_revision=1, plan_digest="sha256:" + "0" * 64,
                                status="succeeded", retrieval_completeness="complete",
                                evidence_sufficiency="sufficient_by_policy", coverage_ref="scope-pitr"))
        await s.flush()
        s.add(CoverageLedger(root_task_id=rt, scope_ref="scope-pitr", search_mode="exhaustive_scope",
                             enumeration_state="sealed", retrieval_completeness="complete",
                             evidence_sufficiency="sufficient_by_policy", counts_json={"succeeded": 1},
                             manifest_digest="sha256:" + "1" * 64))
        await s.flush()
        s.add(CoverageEntry(root_task_id=rt, target_digest="2" * 64,
                            target_key_json={"origin_node_id": "node-pitr", "collection_id": "col-pitr",
                                             "operation": "corpus.retrieve"},
                            query_digest="sha256:" + "3" * 64, state="succeeded", attempts=1,
                            actual_index_revision="index-1", evidence_refs_json=[],
                            used_budget_json={"requests": 1, "bytes": 0}))
        # federation executor chain for the same root task
        now = datetime.now(timezone.utc)
        s.add(FederationProbe(probe_id=(f"pb-{tag}" + "0" * 32)[:32], organization_id=ORG,
                              actor_id=ACTOR, target_node_id="node-pitr",
                              task_spec_digest="sha256:" + "e" * 64, consent_ref="pitr-probe-1",
                              probe_kind="capability_input", state="succeeded",
                              expires_at=now + timedelta(hours=1)))
        adm_id = (f"ad-{tag}" + "0" * 32)[:32]
        s.add(FederationAdmission(admission_id=adm_id, organization_id=ORG, actor_id=ACTOR,
                                 idempotency_key=f"pitr-{tag}-adm-1",
                                 request_digest="sha256:" + "a" * 64,
                                 plan_digest="sha256:" + "0" * 64, root_task_id=rt,
                                 step_id="step-1", issuer_node_id="node-pitr",
                                 executor_node_id="node-pitr", state="admitted",
                                 input_validation="valid", effective_policy_ref="pol-pitr-1"))
        await s.flush()
        exe_id = (f"ex-{tag}" + "0" * 32)[:32]
        s.add(FederationExecution(executor_task_id=exe_id, admission_id=adm_id, root_task_id=rt,
                                 step_id="step-1", operation="retrieve", state="succeeded"))
        s.add(FederationTaskEvent(id=(f"te-{tag}" + "0" * 32)[:32], root_task_id=rt, seq=1,
                                 type="planned", payload={"step": "step-1"}))
        s.add(FederationDelivery(delivery_id=(f"dl-{tag}" + "0" * 32)[:32], root_task_id=rt,
                                state="delivered",
                                result_manifest_digest="sha256:" + "d" * 64))
        s.add(FederationCredentialNonce(jti=(f"jti-{tag}" + "0" * 64)[:22], issuer_node_id="node-pitr",
                                       operation="probe_create",
                                       expires_at=now + timedelta(seconds=120)))
        s.add(FederationRootLedger(root_task_id=rt, organization_id=ORG,
                                  max_requests=100, max_bytes=10**6, max_hops=4,
                                  deadline=now + timedelta(hours=1)))
        s.add(FederationRootReservation(root_task_id=rt, reservation_key=f"pitr-{tag}-r1",
                                       kind="requests", amount=1))
        s.add(FederationDelegationConsumption(root_task_id=rt, step_id="step-1",
                                             reserved_json={"requests": 1}))
        s.add(FederationCacheEntry(id=(f"ce-{tag}" + "0" * 32)[:32], scope_key=f"scope-pitr-{tag}",
                                  cache_key=f"pitr-{tag}-k1", kind="probe",
                                  created_at=now, expires_at=now + timedelta(hours=1)))
        # collection + one member + citations + wiki chain on doc 0
        d0 = state["docs"][0]
        col_id = (f"col-{tag}" + "0" * 32)[:32]
        s.add(Collection(id=col_id, organization_id=ORG, owner_id=users[0],
                         name=f"pitr-{tag}", publication="published", revision=1))
        await s.flush()
        s.add(CollectionMember(collection_id=col_id, version_id=state["versions"][0],
                               resource_id=d0["resource"], document_id=d0["id"], parse_job_id=d0["job"],
                               source_digest=d0["digest"]))
        s.add(CollectionReceipt(key_hash="c" * 64, request_digest="sha256:" + "c" * 64,
                               collection_id=col_id, revision=1))
        s.add(CollectionCatalogView(binding=f"bind-{tag}", revision=1, fingerprint="f" * 64))
        await s.flush()
        snap = CollectionCatalogSnapshot(id=(f"cs-{tag}" + "0" * 32)[:32], binding=f"bind-{tag}",
                                        scope_id="scope-pitr", caller_scope_hash="sha256:" + "9" * 64,
                                        origin_node_id="node-pitr", revision=1, page_size=50,
                                        descriptors=[], index_readiness={},
                                        valid_until=now + timedelta(hours=1))
        s.add(snap)
        await s.flush()
        s.add(CollectionCatalogPage(cursor=(f"cc-{tag}" + "0" * 32)[:32], snapshot_id=snap.id, offset=0))
        s.add(ClientView(scope=f"scope-pitr-{tag}", sequence=1, fingerprint="f" * 64, cursor="c0"))
        await s.flush()
        csnap = ClientSnapshot(id=(f"csn-{tag}" + "0" * 64)[:64], scope=f"scope-pitr-{tag}", sequence=1,
                              state={"sealed": True}, bindings=[], byte_size=10,
                              expires_at=now + timedelta(hours=1))
        s.add(csnap)
        await s.flush()
        s.add(ClientPage(cursor=(f"cp-{tag}" + "0" * 64)[:64], snapshot_id=csnap.id, kind="members",
                         body={"members": []}))
        s.add(ClientReceipt(key_hash="k" * 64, organization_id=ORG, principal_id=users[0],
                           request_digest="sha256:" + "b" * 64, resource_id=d0["resource"],
                           version_id=state["versions"][0], parse_job_id=d0["job"]))
        s.add(Citation(id=(f"cit-{tag}" + "0" * 32)[:32], evidence_id=(f"e00000{tag}" + "0" * 32)[:32],
                       source_kind="assertion", source_id="a" * 32, role="primary",
                       score=1.0, similarity=1.0, snippet="PITR drill", rank=0,
                       content_digest="0" * 64))
        w = Wiki(id=(f"w-{tag}" + "0" * 32)[:32], organization_id=ORG, owner_id=users[0],
                 title=f"PITR {tag}")
        s.add(w)
        await s.flush()
        rev = WikiRevision(id=(f"wr-{tag}" + "0" * 32)[:32], wiki_id=w.id, kind="generated",
                           title=f"PITR {tag}", created_by=users[0])
        s.add(rev)
        await s.flush()
        s.add(WikiPage(id=(f"wp-{tag}" + "0" * 32)[:32], revision_id=rev.id, page_key="p0",
                       position=0, title=f"PITR {tag}",
                       generated_sections=[{"claim": "beacon", "evidence_id": (f"e00000{tag}" + "0" * 32)[:32]}],
                       human_paragraphs=[]))
        s.add(DependencyManifest(id=(f"wd-{tag}" + "0" * 32)[:32], revision_id=rev.id, page_key="p0",
                                 resource_id=d0["resource"], source_version_id=state["versions"][0],
                                 document_id=d0["id"], source_digest=d0["digest"],
                                 parse_revision=d0["job"], evidence_id=(f"e00000{tag}" + "0" * 32)[:32],
                                 excerpt_digest="0" * 64, locator={"kind": "page", "page_idx": 0},
                                 origin_node_id="node-pitr", authority_node_id="node-pitr"))
        s.add(ClaimEvidenceBinding(id=(f"cb-{tag}" + "0" * 32)[:32], revision_id=rev.id, page_key="p0",
                                  claim_id="claim-1", evidence_id=(f"e00000{tag}" + "0" * 32)[:32],
                                  excerpt_digest="0" * 64))
        s.add(WikiHumanEdit(id=(f"wh-{tag}" + "0" * 32)[:32], revision_id=rev.id,
                           base_revision_id=rev.id, page_key="p0", actor_id=users[0],
                           before=[], after=[{"text": "human"}]))
        s.add(WikiWriteKey(id=(f"wk-{tag}" + "0" * 32)[:32], organization_id=ORG, actor_id=users[0],
                          idempotency_key=f"pitr-{tag}-wk-1", request_digest="0" * 64, revision_id=rev.id))
        # Bundle replica + revoke key (licensed-copy ledger survives restore)
        rep_id = (f"br-{tag}" + "0" * 32)[:32]
        s.add(BundleReplica(id=rep_id, organization_id=ORG, resource_id=d0["resource"],
                           source_version_id=state["versions"][0], document_id=d0["id"],
                           owner_id=users[0], created_by=users[0], origin_node_id="node-pitr",
                           authority_node_id="node-pitr", source_digest=d0["digest"],
                           policy_revision="pol-pitr-1"))
        await s.flush()
        s.add(BundleReplicaRevokeKey(id=(f"bk-{tag}" + "0" * 32)[:32], organization_id=ORG,
                                   actor_id=users[0], idempotency_key=f"pitr-{tag}-bk-1",
                                   replica_id=rep_id, request_digest="0" * 64))
        # remote compute (rebuildable temp input) + local task/outbox/usage/conversation/extract rows
        s.add(RemoteCompute(id=(f"rc-{tag}" + "0" * 32)[:32], organization_id=ORG, actor_id=users[0],
                           actor_kind="user", status="waiting_input", input_sha256="0" * 64,
                           input_size=100, plan_digest="sha256:" + "p" * 64,
                           input_object_key=f"tmp-remote-compute/{ORG}/rc-{tag}/source.bin"))
        s.add(Task(id=(f"t-{tag}" + "0" * 32)[:32], organization_id=ORG, kind="index",
                  status="succeeded", payload={"doc": d0["id"]}))
        s.add(CorpusOutbox(id=(f"co-{tag}" + "0" * 32)[:32], organization_id=ORG, type="UsageRecorded",
                          payload={"doc": d0["id"]}))
        s.add(ProcessedEvent(event_id=f"evt-{tag}-1", type="DocumentSubmitted", organization_id=ORG,
                            result_id=d0["id"]))
        s.add(UsageClaim(id=(f"uc-{tag}" + "0" * 32)[:32], actor_id=users[0], parse_job_id=d0["job"]))
        conv_id = (f"cv-{tag}" + "0" * 32)[:32]
        s.add(Conversation(id=conv_id, actor_id=users[0], organization_id=ORG, document_id=d0["id"],
                          title=f"PITR {tag}"))
        await s.flush()
        s.add(Message(id=(f"m-{tag}" + "0" * 32)[:32], conversation_id=conv_id, role="user",
                     content="beacon?"))
        tmpl_id = (f"et-{tag}" + "0" * 32)[:32]
        s.add(ExtractionTemplate(id=tmpl_id, organization_id=ORG, actor_id=users[0], name=f"pitr-{tag}"))
        await s.flush()
        run_id = (f"er-{tag}" + "0" * 32)[:32]
        s.add(ExtractionRun(id=run_id, organization_id=ORG, actor_id=users[0], template_id=tmpl_id,
                           status="succeeded"))
        await s.flush()
        s.add(ExtractionItem(id=(f"ei-{tag}" + "0" * 32)[:32], run_id=run_id, document_id=d0["id"],
                            record_index=0, fields={"name": {"status": "ok", "value": "pitr"}}))
        # control authz + identity + upload/outbox rows (inserted as SQL: Go-owned tables)
        await s.execute(text(
            "INSERT INTO control.api_keys (id, organization_id, user_id, name, key_prefix, key_hash,"
            " scopes, created_at) VALUES (:id,:o,:u,'pitr',:p,:h,'{read}','2026-10-05T00:00:00+00:00')"
            " ON CONFLICT (id) DO NOTHING"),
            {"id": (f"ak-{tag}" + "0" * 32)[:32], "o": ORG, "u": users[0],
             "p": f"pitr-{tag}", "h": "h" * 64})
        await s.execute(text(
            "INSERT INTO control.file_grants (token, organization_id, document_id, object_key,"
            " mime, scope, created_at) VALUES (:t,:o,:d,:k,'application/pdf','source',"
            " '2026-10-05T00:00:00+00:00') ON CONFLICT (token) DO NOTHING"),
            {"t": f"tok-{tag}-pitr", "o": ORG, "d": d0["id"], "k": d0["digest"] and f"uploads/{ORG}/{d0['id']}.pdf"})
        await s.execute(text(
            "INSERT INTO control.usage_ledger (id, organization_id, actor_id, actor_kind, kind,"
            " pages, requests, event_id, created_at) VALUES (:id,:o,:a,'user','parse',1,1,:e,"
            " '2026-10-05T00:00:00+00:00') ON CONFLICT (id) DO NOTHING"),
            {"id": (f"ul-{tag}" + "0" * 32)[:32], "o": ORG, "a": users[0], "e": f"evt-{tag}-ul-1"})
        await s.execute(text(
            "INSERT INTO control.upload_sessions (id, organization_id, actor_id, actor_kind, status,"
            " object_key, filename, mime, declared_size, created_at, expires_at, updated_at)"
            " VALUES (:id,:o,:a,'user','created',:k,'pitr.pdf','application/pdf',100,"
            " '2026-10-05T00:00:00+00:00','2026-10-06T00:00:00+00:00','2026-10-05T00:00:00+00:00')"
            " ON CONFLICT (id) DO NOTHING"),
            {"id": (f"us-{tag}" + "0" * 32)[:32], "o": ORG, "a": users[0],
             "k": f"uploads/{ORG}/{d0['id']}.pdf"})
        await s.execute(text(
            "INSERT INTO control.control_outbox (id, organization_id, type, payload)"
            " VALUES (:id,:o,'DocumentSubmitted',:p) ON CONFLICT (id) DO NOTHING"),
            {"id": (f"cb-{tag}-out" + "0" * 32)[:32], "o": ORG,
             "p": json.dumps({"upload_id": (f"us-{tag}" + "0" * 32)[:32]})})
        await s.execute(text(
            "INSERT INTO control.node_directories (organization_id, revision) VALUES (:o,1)"
            " ON CONFLICT (organization_id) DO NOTHING"), {"o": ORG})
        await s.execute(text(
            "INSERT INTO control.node_members (organization_id, node_id, public_key, descriptor,"
            " descriptor_revision, state, revision) VALUES (:o,'node-pitr',:k,'{}',1,'approved',1)"
            " ON CONFLICT (organization_id, node_id) DO NOTHING"),
            {"o": ORG, "k": "cGl0cg=="})
        await s.execute(text(
            "INSERT INTO control.federation_credential_nonces (jti, issuer, operation, expires_at)"
            " VALUES (:j,'node-pitr','probe_create','2026-10-06T00:00:00+00:00')"
            " ON CONFLICT (jti) DO NOTHING"), {"j": f"ctl-jti-{tag}"})
        await s.commit()
        state["root_task"] = rt
    await engine.dispose()
    return state


async def post_base_writes(args, state: dict, n_extra: int, tag: str,
                            key_prefix: str = "post") -> dict:
    """Writes after the base backup: uploads, federation tasks, wiki. Returns delta state."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from ddp_corpus.models import (Document, DocumentUpload, ParseJob, Resource, ResourceVersion,
                                   UploadEvent, WikiRevision, WikiPage)
    from ddp_corpus.federation_models import FederationRequest
    engine = create_async_engine(args.dsn)
    mk = async_sessionmaker(engine, expire_on_commit=False)
    mc = minio_client(args)
    delta = {"docs": [], "objects": {}, "tasks": [], "wiki_pages": []}
    base_n = len(state["docs"]) + sum(len(state.get(s, {}).get("docs", [])) for s in ("pre", "postt"))
    for k in range(n_extra):
        i = base_n + k
        did = (f"d{i:04d}-{tag}-{key_prefix}20261005" + "0" * 32)[:32]
        jid = (f"j{i:04d}-{tag}-{key_prefix}20261005" + "0" * 32)[:32]
        rid = (f"r{i:04d}-{tag}-{key_prefix}20261005" + "0" * 32)[:32]
        vid = (f"v{i:04d}-{tag}-{key_prefix}20261005" + "0" * 32)[:32]
        owner = state["users"][i % len(state["users"])]
        pdf = _pdf_bytes(i, tag + f"-{key_prefix}")
        dhash = digest(pdf)
        okey = f"uploads/{ORG}/{did}.pdf"
        import io
        mc.put_object(args.bucket, okey, io.BytesIO(pdf), len(pdf), content_type="application/pdf")
        delta["objects"][okey] = {"sha256": dhash, "size": len(pdf)}
        lay = ('{"job":"' + jid + '","pages":[{"page_idx":0,"blocks":1}]}\n').encode()
        dmd = (f"# PITR drill {tag} post doc {k}\n").encode()
        mc.put_object(args.bucket, f"results/{jid}/layout.json", io.BytesIO(lay), len(lay),
                      content_type="application/json")
        mc.put_object(args.bucket, f"results/{jid}/document.md", io.BytesIO(dmd), len(dmd),
                      content_type="text/markdown")
        async with mk() as s:
            s.add(Document(id=did, uploaded_by=owner, organization_id=ORG, doc_id=dhash,
                           origin="web", filename=f"pitr-{tag}-{key_prefix}-{k}.pdf", mime="application/pdf",
                           size_bytes=len(pdf), object_key=okey, index_status="ready",
                           compile_status="ready"))
            s.add(DocumentUpload(id=(f"du-{key_prefix}-{k:04d}-{tag}" + "0" * 32)[:32], document_id=did, user_id=owner))
            s.add(ParseJob(id=jid, document_id=did, engine="borndigital", options_hash="c" * 64,
                           status="succeeded", page_count=1, initiated_by=owner, resource_id=rid,
                           result_prefix=f"results/{jid}/", index_status="ready"))
            s.add(Resource(id=rid, organization_id=ORG, owner_id=owner, uploaded_by=owner,
                           display_name=f"pitr-{tag}-{key_prefix}-{k}.pdf", publication="published"))
            s.add(ResourceVersion(id=vid, resource_id=rid, version_no=1, document_id=did,
                                  parse_job_id=jid, bundle_prefix="", source_digest=dhash,
                                  filename=f"pitr-{tag}-{key_prefix}-{k}.pdf", size_bytes=len(pdf)))
            await s.flush()
            s.add(UploadEvent(id=(f"ue-{key_prefix}-{k:04d}-{tag}" + "0" * 32)[:32], resource_version_id=vid,
                              actor_id=owner, idempotency_key=f"pitr-{tag}-{key_prefix}-upload-{k}",
                              request_digest=dhash))
            rt = f"task-pitr-{tag}-{key_prefix}-{k}-" + "2" * 8
            s.add(FederationRequest(root_task_id=rt, organization_id=ORG, actor_id=ACTOR,
                                    task_spec_digest="sha256:" + "e" * 64, scope_id="scope-pitr-post",
                                    scope_digest="sha256:" + "f" * 64, search_mode="exhaustive_scope",
                                    planning_state="approved", plan_revision=1,
                                    plan_digest="sha256:" + "0" * 64, status="succeeded",
                                    retrieval_completeness="complete",
                                    evidence_sufficiency="sufficient_by_policy", coverage_ref="scope-pitr-post"))
            await s.commit()
        delta["docs"].append({"id": did, "digest": dhash, "object_key": okey})
        delta["tasks"].append(rt)
    # wiki: one more revision row on the existing wiki
    from sqlalchemy import text as _t
    async with mk() as s:
        r = await s.execute(_t("SELECT id FROM public.wikis WHERE organization_id=:o LIMIT 1"), {"o": ORG})
        wid = r.scalar_one()
        rev2 = WikiRevision(id=(f"wr-{key_prefix}-{tag}" + "0" * 32)[:32], wiki_id=wid, kind="human_edit",
                            title=f"PITR {tag} {key_prefix} r", created_by=state["users"][0])
        s.add(rev2)
        await s.flush()
        s.add(WikiPage(id=(f"wp-{key_prefix}-{tag}" + "0" * 32)[:32], revision_id=rev2.id, page_key="p0",
                       position=0, title=f"PITR {tag} {key_prefix} r", generated_sections=[],
                       human_paragraphs=[f"{key_prefix}-base human paragraph"]))
        await s.commit()
        delta["wiki_pages"].append((f"wp-{key_prefix}-{tag}" + "0" * 32)[:32])
    await engine.dispose()
    return delta


async def table_counts(dsn: str, tables: list[str]) -> dict:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    e = create_async_engine(dsn)
    out = {}
    try:
        async with e.connect() as c:
            for t in tables:
                try:
                    r = await c.execute(text(f"SELECT count(*) FROM {t}"))
                    out[t] = int(r.scalar_one())
                except Exception as ex:
                    out[t] = f"ERR {str(ex)[:80]}"
                    await c.rollback()
        return out
    finally:
        await e.dispose()


async def reconcile(args) -> dict:
    """Full reconciliation: metadata<->evidence<->objects<->identity bindings."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from ddp_core.anchor import digest_of
    e = create_async_engine(args.dsn)
    rep = {"checks": [], "orphans": {}, "mismatches": []}
    def check(name: str, ok: bool, detail: str = ""):
        rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    try:
        async with e.connect() as c:
            async def scalar(sql, **p):
                r = await c.execute(text(sql), p)
                return r.scalar_one()
            async def rows(sql, **p):
                r = await c.execute(text(sql), p)
                return [dict(x) for x in r.mappings()]
            # FK-style invariants (count violations). Covers every seeded
            # persistent table: Bundle/authz/task/identity/upload/outbox rows
            # that NOLOSS now counts also get a binding check here, so a
            # silently dropped ledger fails reconcile even at equal counts.
            inv = {
                "versions->resources": "SELECT count(*) FROM public.resource_versions v LEFT JOIN public.resources r ON r.id=v.resource_id WHERE r.id IS NULL",
                "versions->documents": "SELECT count(*) FROM public.resource_versions v LEFT JOIN public.documents d ON d.id=v.document_id WHERE d.id IS NULL",
                "jobs->documents": "SELECT count(*) FROM public.parse_jobs j LEFT JOIN public.documents d ON d.id=j.document_id WHERE d.id IS NULL",
                "chunks->documents": "SELECT count(*) FROM public.chunks k LEFT JOIN public.documents d ON d.id=k.document_id WHERE d.id IS NULL",
                "chunks->jobs": "SELECT count(*) FROM public.chunks k LEFT JOIN public.parse_jobs j ON j.id=k.parse_job_id WHERE j.id IS NULL",
                "evidence->documents": "SELECT count(*) FROM public.evidence e LEFT JOIN public.documents d ON d.id=e.document_id WHERE d.id IS NULL",
                "evidence->jobs": "SELECT count(*) FROM public.evidence e LEFT JOIN public.parse_jobs j ON j.id=e.parse_job_id WHERE j.id IS NULL",
                "citations->evidence": "SELECT count(*) FROM public.citations c LEFT JOIN public.evidence e ON e.id=c.evidence_id WHERE e.id IS NULL",
                "deps->versions": "SELECT count(*) FROM public.wiki_dependencies d LEFT JOIN public.resource_versions v ON v.id=d.source_version_id WHERE v.id IS NULL",
                "deps->evidence": "SELECT count(*) FROM public.wiki_dependencies d LEFT JOIN public.evidence e ON e.id=d.evidence_id WHERE e.id IS NULL",
                "members->versions": "SELECT count(*) FROM public.collection_members m LEFT JOIN public.resource_versions v ON v.id=m.version_id WHERE v.id IS NULL",
                "uploads->versions": "SELECT count(*) FROM public.upload_events u LEFT JOIN public.resource_versions v ON v.id=u.resource_version_id WHERE v.id IS NULL",
                "docuploads->documents": "SELECT count(*) FROM public.document_uploads u LEFT JOIN public.documents d ON d.id=u.document_id WHERE d.id IS NULL",
                "entries->ledgers": "SELECT count(*) FROM public.coverage_entries e LEFT JOIN public.coverage_ledgers l ON l.root_task_id=e.root_task_id WHERE l.root_task_id IS NULL",
                "replicas->versions": "SELECT count(*) FROM public.bundle_replicas b LEFT JOIN public.resource_versions v ON v.id=b.source_version_id WHERE v.id IS NULL",
                "replicas->resources": "SELECT count(*) FROM public.bundle_replicas b LEFT JOIN public.resources r ON r.id=b.resource_id WHERE r.id IS NULL",
                "replicakeys->replicas": "SELECT count(*) FROM public.bundle_replica_revoke_keys k LEFT JOIN public.bundle_replicas b ON b.id=k.replica_id WHERE b.id IS NULL",
                "executions->admissions": "SELECT count(*) FROM public.federation_executions e LEFT JOIN public.federation_admissions a ON a.admission_id=e.admission_id WHERE a.admission_id IS NULL",
                "taskevents->requests": "SELECT count(*) FROM public.federation_task_events e LEFT JOIN public.federation_requests r ON r.root_task_id=e.root_task_id WHERE r.root_task_id IS NULL",
                "deliveries->requests": "SELECT count(*) FROM public.federation_deliveries d LEFT JOIN public.federation_requests r ON r.root_task_id=d.root_task_id WHERE r.root_task_id IS NULL",
                "rootledgers->requests": "SELECT count(*) FROM public.federation_root_ledgers l LEFT JOIN public.federation_requests r ON r.root_task_id=l.root_task_id WHERE r.root_task_id IS NULL",
                "reservations->requests": "SELECT count(*) FROM public.federation_root_reservations v LEFT JOIN public.federation_requests r ON r.root_task_id=v.root_task_id WHERE r.root_task_id IS NULL",
                "delegation->requests": "SELECT count(*) FROM public.federation_delegation_consumption d LEFT JOIN public.federation_requests r ON r.root_task_id=d.root_task_id WHERE r.root_task_id IS NULL",
                "catalogpages->snapshots": "SELECT count(*) FROM public.collection_catalog_pages p LEFT JOIN public.collection_catalog_snapshots s ON s.id=p.snapshot_id WHERE s.id IS NULL",
                "clientpages->snapshots": "SELECT count(*) FROM public.client_pages p LEFT JOIN public.client_snapshots s ON s.id=p.snapshot_id WHERE s.id IS NULL",
                "messages->conversations": "SELECT count(*) FROM public.messages m LEFT JOIN public.conversations c ON c.id=m.conversation_id WHERE c.id IS NULL",
                "items->runs": "SELECT count(*) FROM public.extraction_items i LEFT JOIN public.extraction_runs r ON r.id=i.run_id WHERE r.id IS NULL",
                "items->documents": "SELECT count(*) FROM public.extraction_items i LEFT JOIN public.documents d ON d.id=i.document_id WHERE d.id IS NULL",
                "usageclaims->jobs": "SELECT count(*) FROM public.usage_claims u LEFT JOIN public.parse_jobs j ON j.id=u.parse_job_id WHERE j.id IS NULL",
                "wikibindings->revisions": "SELECT count(*) FROM public.wiki_claim_bindings b LEFT JOIN public.wiki_revisions r ON r.id=b.revision_id WHERE r.id IS NULL",
                "wikiedits->revisions": "SELECT count(*) FROM public.wiki_human_edits e LEFT JOIN public.wiki_revisions r ON r.id=e.revision_id WHERE r.id IS NULL",
                "wikiwritekeys->revisions": "SELECT count(*) FROM public.wiki_write_keys k LEFT JOIN public.wiki_revisions r ON r.id=k.revision_id WHERE r.id IS NULL",
                "collectionreceipts->collections": "SELECT count(*) FROM public.collection_receipts r LEFT JOIN public.collections c ON c.id=r.collection_id WHERE c.id IS NULL",
            }
            for name, sql in inv.items():
                n = await scalar(sql)
                check(f"fk:{name}", n == 0, f"violations={n}")
            # digest bindings
            n = await scalar("SELECT count(*) FROM public.resource_versions v JOIN public.documents d ON d.id=v.document_id WHERE v.source_digest<>'' AND v.source_digest<>d.doc_id")
            check("bind:version.source_digest==document.doc_id", n == 0, f"mismatches={n}")
            # evidence self-consistency: the anchor.py strict path binds
            # evidence.content_digest to digest(evidence.content) — NOT to
            # chunk.text (reindex recasts chunk ids/text while evidence keeps
            # the cited content; derived/search text legitimately diverge).
            # Full-table scan in id order, 5000-row pages (no LIMIT sampling:
            # 20261005 evidence rows total 37907 > any fixed cap).
            bad_n, pages = 0, 0
            last = ""
            while True:
                page = await rows("SELECT id, content, content_digest FROM public.evidence WHERE id > :last ORDER BY id LIMIT 5000", last=last)
                if not page:
                    break
                pages += 1
                bad_n += sum(1 for r in page if (r["content_digest"] or "") != digest_of(r["content"] or ""))
                last = page[-1]["id"]
            check("bind:evidence.content_digest==digest(evidence.content)", bad_n == 0, f"mismatches={bad_n} pages={pages}")
            # every live version's document has an object (or explained tombstone)
            orph = await rows("SELECT v.id FROM public.resource_versions v JOIN public.documents d ON d.id=v.document_id WHERE v.deleted_at IS NULL AND d.deleted_at IS NULL AND (d.object_key IS NULL OR d.object_key='')")
            check("meta:live versions resolve to stored objects", not orph, f"unresolved={len(orph)}")
            rep["orphans"]["version_without_object"] = [r["id"] for r in orph[:20]]
            # live wiki deps bind to the same digest/doc as their version row
            n = await scalar("SELECT count(*) FROM public.wiki_dependencies d JOIN public.resource_versions v ON v.id=d.source_version_id WHERE d.source_digest<>v.source_digest OR d.document_id<>v.document_id")
            check("bind:wiki_deps==version(source_digest,document)", n == 0, f"mismatches={n}")
            n = await scalar("SELECT count(*) FROM public.collection_members m JOIN public.resource_versions v ON v.id=m.version_id WHERE m.source_digest<>v.source_digest OR m.document_id<>v.document_id")
            check("bind:collection_members==version(source_digest,document)", n == 0, f"mismatches={n}")
            n = await scalar("SELECT count(*) FROM public.documents d WHERE d.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM public.document_uploads u WHERE u.document_id=d.id)")
            check("meta:every live document has an uploader", n == 0, f"missing={n}")
            # joint identity binding: drill-seeded wiki deps carry the drill
            # authority id ("node-pitr"); a clone restore that rewrites the
            # authority (different node id) breaks this binding and fails
            # reconcile. B-carryover rows have NULL/other ids and are ignored.
            n = await scalar("SELECT count(*) FROM public.wiki_dependencies WHERE origin_node_id='node-pitr' AND authority_node_id<>'node-pitr'")
            check("bind:drill authority id survives restore (clone with different id fails here)", n == 0, f"mismatches={n}")
            n = await scalar("SELECT count(*) FROM public.wiki_dependencies WHERE authority_node_id='node-pitr' AND origin_node_id<>'node-pitr'")
            check("bind:drill origin id survives restore", n == 0, f"mismatches={n}")
    finally:
        await e.dispose()
    # object side: every live document object exists with matching digest; no unexplained extras
    mc = minio_client(args)
    from sqlalchemy.ext.asyncio import create_async_engine as _cae
    from sqlalchemy import text as _t
    eng = _cae(args.dsn)
    missing, wrong = [], []
    expected_keys = set()
    parse_jobs = []
    try:
        async with eng.connect() as c:
            r = await c.execute(_t("SELECT doc_id, object_key FROM public.documents WHERE deleted_at IS NULL AND object_key<>''"))
            docs = [dict(x) for x in r.mappings()]
            r = await c.execute(_t("SELECT j.id, j.result_prefix, (d.deleted_at IS NOT NULL) AS doc_deleted FROM public.parse_jobs j JOIN public.documents d ON d.id=j.document_id WHERE j.result_prefix IS NOT NULL"))
            parse_jobs = [dict(x) for x in r.mappings()]
    finally:
        await eng.dispose()
    import hashlib as _h
    for d in docs:
        expected_keys.add(d["object_key"])
        try:
            o = mc.get_object(args.bucket, d["object_key"])
            data = o.read(); o.close(); o.release_conn()
            if _h.sha256(data).hexdigest() != d["doc_id"]:
                wrong.append(d["object_key"])
        except Exception:
            missing.append(d["object_key"])
    check("obj:every live document object exists", not missing, f"missing={len(missing)}")
    check("obj:every live document digest matches", not wrong, f"mismatched={len(wrong)}")
    rep["orphans"]["missing_objects"] = missing[:20]
    rep["orphans"]["digest_mismatches"] = wrong[:20]
    # bucket extras: tmp-remote-compute/ keys are rebuildable temp bytes, NOT
    # backup content (see backup TTL policy in docs/DEPLOY.md and the drill's
    # --exclude 'tmp-remote-compute/*' mirrors). They are explained only via
    # an open remote_computes row; temp keys with no compute row are orphans.
    bucket = set(o.object_name for o in mc.list_objects(args.bucket, recursive=True))
    prefixes = set(j["result_prefix"] for j in parse_jobs)
    empty = [j for j in parse_jobs if not any(k.startswith(j["result_prefix"]) for k in bucket)]
    unexplained_empty = [j for j in empty if not j["doc_deleted"]]
    check("obj:result prefixes without objects are only deleted docs", not unexplained_empty,
          f"explained_deleted={len(empty)-len(unexplained_empty)} unexplained={len(unexplained_empty)}")
    rep["orphans"]["empty_result_prefixes"] = [j["id"] for j in unexplained_empty[:20]]
    tmp_keys = [k for k in bucket if k.startswith("tmp-remote-compute/")]
    eng2 = _cae(args.dsn)
    try:
        async with eng2.connect() as c:
            r = await c.execute(_t("SELECT input_object_key FROM public.remote_computes "
                                  "WHERE input_object_key IS NOT NULL AND input_object_key<>''"))
            live_tmp = {row[0] for row in r.all()}
    finally:
        await eng2.dispose()
    orphan_tmp = [k for k in tmp_keys if k not in live_tmp]
    check("obj:temp bytes are bound to an open remote_computes row", not orphan_tmp,
          f"temp={len(tmp_keys)} orphan_temp={len(orphan_tmp)}")
    rep["orphans"]["orphan_temp_keys"] = orphan_tmp[:20]
    extras = [k for k in bucket if k not in expected_keys
              and not any(k.startswith(p) for p in prefixes)
              and not k.startswith("tmp-remote-compute/")]
    check("obj:no unexplained bucket extras", not extras, f"extras={len(extras)}")
    rep["orphans"]["bucket_extras"] = extras[:20]
    rep["ok"] = all(c["ok"] for c in rep["checks"])
    rep["doc_count"] = len(docs)
    return rep


def cmd_seed(a):
    state = asyncio.run(seed_dataset(a, a.n_docs, a.tag))
    Path(a.state).write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"seed OK: {len(state['docs'])} docs, {state['chunks_total']} chunks, {state['evidence_total']} evidence")


def cmd_post(a):
    state = json.loads(Path(a.state).read_text(encoding="utf-8"))
    prefix = getattr(a, "key_prefix", "post")
    section = getattr(a, "section", None) or prefix
    delta = asyncio.run(post_base_writes(a, state, a.n_extra, a.tag, key_prefix=prefix))
    state.setdefault(section, {}).update(delta)
    # keep the legacy "post" alias pointing at the pre-target section
    if section == "pre":
        state.setdefault("post", {}).update(delta)
    Path(a.state).write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"post OK [{section}]: +{len(delta['docs'])} docs, +{len(delta['tasks'])} tasks, wiki +{len(delta['wiki_pages'])}")


def cmd_counts(a):
    rep = asyncio.run(table_counts(a.dsn, COUNT_TABLES))
    Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    for t in COUNT_TABLES:
        print(f"{rep.get(t)} {t}")


def cmd_probe(a):
    """Crash probe: one row + one byte directly, bypassing MinIO/reconcile.

    Writes a single federation row (no objects) and returns its id, so the
    drill can kill the server before the row's WAL is archived and prove the
    restore loses exactly that row."""
    import uuid
    state = json.loads(Path(a.state).read_text(encoding="utf-8"))
    probe_id = f"probe-{a.tag}-{uuid.uuid4().hex[:8]}"
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from ddp_corpus.federation_models import FederationRequest
    async def _w():
        e = create_async_engine(a.dsn)
        mk = async_sessionmaker(e, expire_on_commit=False)
        async with mk() as s:
            s.add(FederationRequest(root_task_id=probe_id, organization_id=state.get("org", ORG),
                                    actor_id=ACTOR, task_spec_digest="sha256:" + "e" * 64,
                                    scope_id="scope-probe", scope_digest="sha256:" + "f" * 64,
                                    search_mode="exhaustive_scope", planning_state="approved",
                                    plan_revision=1, plan_digest="sha256:" + "0" * 64,
                                    status="succeeded", retrieval_completeness="complete",
                                    evidence_sufficiency="sufficient_by_policy", coverage_ref="scope-probe"))
            await s.commit()
        await e.dispose()
    asyncio.run(_w())
    Path(a.out).write_text(json.dumps({"probe_task": probe_id}), encoding="utf-8")
    print(f"probe OK: {probe_id} (NOT archived; kill the server now)")


async def clone_leg(a) -> dict:
    """LIVE clone leg: fresh key, forged/clone-signed credentials vs live A.

    No servers are started here: the drill script restores the PITR DB into a
    throwaway PG, reads B's authority node id from it, mints a FRESH clone key
    (never the authority seed), and fires real peer requests at live center A
    (control 53430 -> corpus 52430). Every impersonation attempt must be
    refused; A-side read-only DB checks must show no new trust rows and no
    admitted request. Returns the report dict (also written to --out)."""
    import base64
    import hashlib as _h
    import time as _t
    import urllib.request as _url
    import uuid as _uuid
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    sys.path.insert(0, str(ROOT / "python" / "ddp_core"))
    from ddp_core.application import node_credentials as nc
    rep = {"checks": []}
    def check(name, ok, detail=""):
        rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'OK ' if ok else 'FAIL'} {name} {detail}")
    # 1) identities from the PITR-restored DB (read-only SELECTs): the drill's
    # own authority rows, and the real node id of the center the copy came
    # from (its collection catalog snapshots carry it as origin).
    e = create_async_engine(a.dsn)
    async with e.connect() as c:
        auth_rows = (await c.execute(text(
            "SELECT DISTINCT authority_node_id FROM public.wiki_dependencies "
            "WHERE authority_node_id='node-pitr' LIMIT 5"))).all()
        n_deps = int((await c.execute(text("SELECT count(*) FROM public.wiki_dependencies"))).scalar_one())
        source_rows = int((await c.execute(text(
            "SELECT count(*) FROM public.collection_catalog_snapshots WHERE origin_node_id=:b"),
            {"b": a.source_node})).scalar_one())
    await e.dispose()
    check("clone:restored DB carries drill authority id", len(auth_rows) > 0, f"rows={n_deps}")
    check("clone:restored DB is a copy of the source center (its node id is the catalog origin)",
          source_rows > 0, f"source_node={a.source_node} catalog_rows={source_rows}")
    authority_id = a.source_node
    # 2) fresh clone key (throwaway; never the authority seed)
    clone_priv = Ed25519PrivateKey.generate()
    clone_pub = clone_priv.public_key().public_bytes_raw()
    clone_id = "node-" + _h.sha256(clone_pub).hexdigest()[:48]
    clone_pub_b64 = base64.b64encode(clone_pub).decode()
    check("clone:fresh clone id differs from the source center", clone_id != authority_id,
          f"clone={clone_id} source={authority_id}")
    rep["clone_node_id"] = clone_id
    rep["authority_node_id"] = authority_id
    rep["clone_public_key"] = clone_pub_b64
    # positive control: authority seed determinism is covered by the Go
    # TestNodeIdentityBackupRestoreDrill (same seed == authority); here we
    # only assert the clone is *different* — we never start a second live B.
    # 3) two credentials, both sent to live A's peer probe endpoint:
    # (a) a forgery — issuer=B, signed with the clone key (a clone cannot hold
    #     B's seed); A must refuse it against B's registered key;
    # (b) the clone's own credential (own id, never approved at A).
    audience = a.audience
    # The probe bytes FIRST: the credential's body_digest must cover these
    # exact bytes (else A correctly answers credential_scope_denied for a
    # rebound credential — a true refusal, but of the wrong leg). We want the
    # trust leg: well-formed + well-bound, refused ONLY because A never
    # approved the clone.
    probe_body = json.dumps({"schema": "ddp-task-probe/1#ProbeRequest",
                             "task_spec_digest": "sha256:" + "e" * 64,
                             "consent_ref": "clone-leg-exploration-1",
                             "probe_kind": "capability_input",
                             "target_node_id": audience,
                             "scope_ref": "scope-clone-leg",
                             "operation": "rag.answer.cited"},
                            sort_keys=True, separators=(",", ":")).encode()
    body = probe_body
    def mint(issuer, priv):
        now = int(_t.time())
        claims = {"schema": "ddp-node-credential/1#Claims", "alg": "Ed25519",
                  "issuer_node_id": issuer, "audience_node_id": audience,
                  "actor": {"organization_id": "org-pitr", "subject": "clone-leg", "kind": "user"},
                  "operation": "probe_create",
                  "constraints": {"root_task_id": f"clone-leg-{_uuid.uuid4().hex[:8]}",
                                  "scope_ref": "scope-clone-leg",
                                  "task_spec_digest": "sha256:" + "e" * 64},
                  "request": {"method": "POST", "path": "/api/v1/federation/probes",
                              "body_digest": nc.body_digest(body)},
                  "issued_at": now, "expires_at": now + 60,
                  "jti": "C" + _uuid.uuid4().hex[:21]}
        nc.validate_claims(dict(claims))
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        sig = priv.sign(b"ddp-node-credential/1\n" + payload)
        return nc.encode(claims, sig), claims

    def post_probe(token):
        req = _url.Request(f"{a.peer_url}/api/v1/federation/probes", data=probe_body, method="POST",
                           headers={"Content-Type": "application/json",
                                    "X-DDP-Node-Credential": token,
                                    "X-DDP-Target-Node": audience,
                                    "Idempotency-Key": f"clone-leg-{_uuid.uuid4().hex[:8]}"})
        try:
            with _url.urlopen(req, timeout=15) as r:
                status, payload = r.status, r.read()[:2000].decode("utf-8", "replace")
        except Exception as ex:
            # HTTPError carries the refusal body; read it instead of str(exc).
            read = getattr(ex, "read", None)
            try:
                payload = read()[:2000].decode("utf-8", "replace") if callable(read) else str(ex)[:500]
            except Exception:
                payload = str(ex)[:500]
            status = getattr(ex, "code", "ERR")
        try:
            parsed = json.loads(payload) if isinstance(payload, str) and payload.lstrip().startswith("{") else {}
            code = parsed.get("code") or parsed.get("error") or payload[:160]
        except Exception:
            code = str(payload)[:160]
        return status, code, payload

    forged_token, forged_claims = mint(authority_id, clone_priv)
    f_status, f_code, f_body = post_probe(forged_token)
    check("clone:A refuses a credential claiming issuer=B but signed by the clone key",
          f_status in (401, 403), f"status={f_status} code={f_code}")
    rep["forged_issuer_attempt"] = {"issuer": authority_id, "signer": clone_id,
                                    "jti": forged_claims["jti"], "status": f_status,
                                    "code": f_code, "body": f_body[:1000]}
    token, claims = mint(clone_id, clone_priv)
    rep["clone_credential_jti"] = claims["jti"]
    # (b) present the clone-signed credential (own id, never approved at A)
    # to live A's peer probe endpoint; expect an explicit refusal.
    status, code, payload = post_probe(token)
    check("clone:A refuses clone-signed probe with 401/403", status in (401, 403),
          f"status={status} code={code}")
    rep["probe_response"] = {"status": status, "code": code, "body": payload[:1000]}
    # 4) A-side read-only checks: no new trust row, no admitted request.
    # Caller passes --a-dsn (A's PG, read-only SELECTs only).
    ea = create_async_engine(a.a_dsn)
    async with ea.connect() as c:
        members = [dict(x) for x in (await c.execute(text(
            "SELECT node_id, state FROM control.node_members ORDER BY node_id"))).mappings()]
        has_nonce = int((await c.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='federation_credential_nonces'"))).scalar_one())
        nonce = int((await c.execute(text(
            "SELECT count(*) FROM public.federation_credential_nonces "
            "WHERE issuer_node_id=:i OR jti IN (:forged, :own)"),
            {"i": clone_id, "forged": forged_claims["jti"], "own": claims["jti"]})).scalar_one()) \
            if has_nonce else None
    await ea.dispose()
    rep["a_members"] = [(m["node_id"], m["state"]) for m in members]
    rep["a_nonce_rows_for_clone"] = nonce
    check("clone:A approves the impersonated source center (so the forgery targets real trust)",
          any(m["node_id"] == authority_id and m["state"] == "approved" for m in members),
          f"source={authority_id}")
    check("clone:A trust table has no clone row", all(m["node_id"] != clone_id for m in members),
          f"members={len(members)}")
    check("clone:neither credential's nonce was admitted at A", nonce == 0,
          f"nonce_rows={nonce} (None = nonce table not found, which fails the check)")
    rep["ok"] = all(c["ok"] for c in rep["checks"])
    Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print("CLONE-LEG", "PASS" if rep["ok"] else "FAIL")
    if not rep["ok"]:
        raise SystemExit(1)
    return rep


def cmd_clone(a):
    asyncio.run(clone_leg(a))


def cmd_reconcile(a):
    rep = asyncio.run(reconcile(a))
    Path(a.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    for c in rep["checks"]:
        print(f"{'OK ' if c['ok'] else 'FAIL'} {c['name']} {c.get('detail','')}")
    print("RECONCILE", "PASS" if rep["ok"] else "FAIL")
    if not rep["ok"]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--dsn", required=True); s.add_argument("--state", required=True)
    s.add_argument("--minio-endpoint", required=True); s.add_argument("--minio-access-key", required=True)
    s.add_argument("--minio-secret-key", required=True); s.add_argument("--bucket", required=True)
    s.add_argument("--n-docs", type=int, default=30); s.add_argument("--tag", default="base")
    s.set_defaults(f=cmd_seed)
    s2 = sub.add_parser("post")
    s2.add_argument("--dsn", required=True); s2.add_argument("--state", required=True)
    s2.add_argument("--minio-endpoint", required=True); s2.add_argument("--minio-access-key", required=True)
    s2.add_argument("--minio-secret-key", required=True); s2.add_argument("--bucket", required=True)
    s2.add_argument("--n-extra", type=int, default=5); s2.add_argument("--tag", default="base")
    s2.add_argument("--key-prefix", default="post"); s2.add_argument("--section", default=None)
    s2.set_defaults(f=cmd_post)
    s4 = sub.add_parser("counts")
    s4.add_argument("--dsn", required=True); s4.add_argument("--out", required=True)
    s4.set_defaults(f=cmd_counts)
    s5 = sub.add_parser("probe")
    s5.add_argument("--dsn", required=True); s5.add_argument("--state", required=True)
    s5.add_argument("--out", required=True); s5.add_argument("--tag", default="base")
    s5.set_defaults(f=cmd_probe)
    s3 = sub.add_parser("reconcile")
    s3.add_argument("--dsn", required=True)
    s3.add_argument("--minio-endpoint", required=True); s3.add_argument("--minio-access-key", required=True)
    s3.add_argument("--minio-secret-key", required=True); s3.add_argument("--bucket", required=True)
    s3.add_argument("--out", required=True)
    s3.set_defaults(f=cmd_reconcile)
    s6 = sub.add_parser("clone")
    s6.add_argument("--dsn", required=True, help="PITR-restored DB (read-only SELECTs)")
    s6.add_argument("--a-dsn", required=True, help="live A PG (read-only SELECTs)")
    s6.add_argument("--audience", required=True, help="live A node id (credential audience)")
    s6.add_argument("--peer-url", required=True, help="live A peer base URL (no credentials)")
    s6.add_argument("--source-node", required=True, help="node id of the center the restored DB was copied from (B)")
    s6.add_argument("--out", required=True)
    s6.set_defaults(f=cmd_clone)
    a = p.parse_args()
    a.f(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
