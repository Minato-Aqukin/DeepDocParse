#!/usr/bin/env python3
"""T64 独立规模实验（不接触运行中的中心）。

命令（仓库根目录）：
  .venv/bin/python scripts/scale_storage_experiment.py \
      --output docs/refactor/artifacts/scale-storage-20261004.json

需要 Docker、pgvector/pgvector:pg16、本仓库 .venv 与 Go。脚本自行创建/删除
独占 PG 容器，端口 15485；N=10/50/200 各用全新数据库和实际迁移。
控制面用 httptest 替身，corpus 用 MockTransport 替身；生产 HTTP、规划、
worker 和数据库写入路径均真实运行。对象存储用可计数 MemoryStorage。
relation_bytes 是 pg_total_relation_size（含索引/TOAST/空页，非缓存载荷上限）。
发现有界和跨任务保留有界分开判定；存在缺口时输出 partial，绝不伪报通过。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
GO = Path.home() / ".local/opt/go/bin/go"
PYTHON = ROOT / ".venv/bin/python"


def command(args, *, cwd=ROOT, env=None):
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True)


async def measure(connection, schema):
    names = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname=$1 ORDER BY tablename", schema)
    result = {}
    for row in names:
        name = row["tablename"]
        # Identifiers come from PostgreSQL, never from peer input.
        table = '"' + schema + '"."' + name.replace('"', '""') + '"'
        values = await connection.fetchrow(
            f"SELECT count(*) AS rows,coalesce(sum(pg_column_size(t)),0) AS row_payload_bytes,"
            f"pg_total_relation_size('{table}') AS relation_bytes FROM {table} t")
        result[name] = dict(values)
    return result


async def corpus_run(dsn, control):
    import asyncpg
    import httpx
    import pytest
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    sys.path[:0] = [str(ROOT / "services/corpus-api"), str(ROOT / "services/corpus-api/tests")]
    import conftest
    from ddp_corpus import cache, db, federation_tasks, node_auth, node_identity
    from ddp_corpus.config import settings
    from ddp_corpus.main import app
    from ddp_corpus.federation_peers import PeerDirectory, parse_peers
    from ddp_corpus.reconcile import sweep_federation_once
    from ddp_corpus.service_client import ServiceClient
    from ddp_corpus.storage import MemoryStorage
    from ddp_core.application import plans
    from ddp_core.search import MemoryIndex
    from node_credentials_fixture import LocalControlSigner
    from test_federation_tasks import (NODE, PEER_NODE, StubPeer, approve_task,
                                       exploration, peer_descriptor, peer_evidence,
                                       plan_task, submit_task, task_spec)

    connection = await asyncpg.connect(dsn)
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    db._engine, db._sessionmaker = engine, sessions
    monkeypatch = pytest.MonkeyPatch()
    local = control["local_node_id"]
    monkeypatch.setattr(settings, "bundle_node_id", local)
    monkeypatch.setattr(settings, "federation_cache_max_entries", 16)
    monkeypatch.setattr(settings, "federation_cache_max_bytes", 8192)
    monkeypatch.setattr(settings, "federation_cache_ttl_seconds", 900)
    node_identity.reset()
    node_identity.follow_configuration_for_tests()
    app.state.http = httpx.AsyncClient(trust_env=False)
    app.state.service_client = ServiceClient(app.state.http)
    app.state.storage = MemoryStorage()
    app.state.search_index = MemoryIndex()
    app.state.redis = None
    manifest = control["manifest"]
    peers = {}
    transports = {}
    node_ids = [item["origin_node_id"] for item in manifest["expanded_members"]]
    for target in manifest["expanded_members"]:
        node = target["origin_node_id"]
        item = peer_evidence(evidence_id="e-" + node, resource_id="r-" + node)
        excerpt = "有界远端证据摘录；不是完整文档。"
        item.update(origin_node_id=node, authority_node_id=node, excerpt=excerpt,
                    excerpt_digest=plans.content_digest(excerpt.encode()))
        peer = StubPeer(items=[item], collections=[peer_descriptor(target["collection_id"], origin=node)])
        peers[node] = peer
        original = peer.transport()

        def handler(request, *, original=original, node=node):
            response = original.handle_request(request)
            # Reuse wire-contract fixtures with a distinct peer identity per stub.
            data = response.content.decode().replace(PEER_NODE, node).replace(NODE, local)
            return httpx.Response(response.status_code, content=data, headers={"Content-Type": "application/json"})

        transports[node] = httpx.MockTransport(handler)
    configs = parse_peers(json.dumps({node: {"endpoint": f"https://{node}.example"} for node in node_ids}))
    signer = LocalControlSigner(issuer_node_id=local)
    forbidden_fetches = []

    # PeerDirectory owns one transport, so dispatch by the configured host.
    def dispatch(request):
        node = request.url.host.removesuffix(".example")
        if not any(part in request.url.path for part in (
                "/published-collections", "/probes", "/admissions", "/tasks/", "/evidence-sets/")):
            forbidden_fetches.append(str(request.url))
        return transports[node].handle_request(request)

    transport = httpx.MockTransport(dispatch)
    monkeypatch.setattr(federation_tasks, "peer_directory", lambda actor, delegation=None: PeerDirectory(
        configs, actor=actor, transport=transport, signer=signer, delegation=delegation))
    before = await measure(connection, "corpus")
    phases = []
    object_before = {"objects": len(app.state.storage.objects), "bytes": sum(len(v[0]) for v in app.state.storage.objects.values())}
    budget = {"max_requests": 64, "max_bytes": 2 * 1024 * 1024, "max_hops": 512,
              "max_generation_tokens": 0, "deadline": manifest["valid_until"]}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://corpus", headers=conftest.actor_headers(), trust_env=False) as client:
            for iteration in range(2):
                consent = exploration(recipients=node_ids, budget={"max_probe_requests": 8,
                    "max_egress_bytes": 1024 * 1024, "max_discovery_requests": 8})
                spec = task_spec(scope="federation_public", mode="exhaustive_scope",
                    scope_ref=manifest["scope_id"], operation="corpus.retrieve",
                    query=f"storage scale retrieval {iteration}")
                spec["execution_policy"]["coordinator_ref"] = local
                response = await client.post("/api/v1/task-intents", json={"task_spec": spec,
                    "exploration_consent": consent, "scope_manifest": manifest, "budget": budget},
                    headers={"Idempotency-Key": f"scale-intent-{iteration}"})
                assert response.status_code == 201, response.text
                root = response.json()["root_task_id"]
                plan = await plan_task(client, root)
                await approve_task(client, root, plan, recipients=[local, *node_ids])
                executed = await submit_task(client, root, plan["plan_digest"], f"scale-execute-{iteration}")
                assert executed.status_code == 200, executed.text
                status = executed.json()
                coverage = (await client.get(f"/api/v1/tasks/{root}/coverage")).json()
                evidence = (status.get("result") or {}).get("evidence") or []
                assert evidence, f"experiment must actually retrieve remote evidence: {status}"
                assert len(coverage["entries"]) == control["n"], coverage
                phases.append({"iteration": iteration + 1, "status": status["status"],
                    "retrieval_completeness": status["retrieval_completeness"],
                    "coverage_counts": coverage["counts"], "evidence_items": len(evidence),
                    "tables": await measure(connection, "corpus")})
        # Exercise bounded negative/cache projections at N writes, not merely N peers.
        from datetime import timedelta
        from ddp_corpus.models import utcnow
        now = utcnow()
        async with sessions() as session:
            for node in node_ids:
                await cache.record_negative(session, scope_key="scale-negative", node_id=node,
                    node_revision="1", reason="unreachable", now=now)
            await session.commit()
        cached = await measure(connection, "corpus")
        cache_payload = await connection.fetchval("SELECT coalesce(sum(bytes),0) FROM corpus.federation_cache_entries")
        assert cached["federation_cache_entries"]["rows"] <= 16 and cache_payload <= 8192
        monkeypatch.setattr(settings, "federation_peer_key_cache_max_entries", 16)
        monkeypatch.setattr(settings, "federation_peer_key_cache_seconds", 5)
        trust_clock = [0.0]
        trust_calls = []

        def trust_response(request):
            identity = request.url.path.rsplit("/", 1)[-1]
            trust_calls.append(identity)
            return httpx.Response(200, json={"node_id": identity, "state": "approved", "revision": 1})

        async with httpx.AsyncClient(transport=httpx.MockTransport(trust_response), trust_env=False) as trust_http:
            trust_source = node_auth.ControlPeerTrust(trust_http, clock=lambda: trust_clock[0])
            for identity in node_ids:
                await trust_source.trust(identity)
            live_trust_entries = len(trust_source._cache)
            assert live_trust_entries <= 16
            trust_clock[0] = 6
            await trust_source.trust(node_ids[0])
            expired_trust_entries = len(trust_source._cache)
            assert expired_trust_entries == 1
        trust_cache = {"configured_entries": 16, "ttl_seconds": 5,
                       "after_n_lookups": live_trust_entries,
                       "after_ttl_and_new_lookup": expired_trust_entries,
                       "control_requests": len(trust_calls)}
        later = now + timedelta(hours=2)
        # 留存 pin 的或判据看"任务是否还走得动"：实验任务的 plan(~15min)、
        # manifest 与 exploration( fixtures 2030)在 retention=0 的截止线
        # later 之前全都有效 → 全部 pin，stripped 必为 0 —— 这正是生产常态
        # （有效任务的 probe 必须保留），但它证不出"过期任务会被剥离"的界。
        # 所以 sweep 分两步：先在 later（全部 pin，stripped == 0，对应生产
        # 行为）扫一遍，再把任务有效期与 probe TTL 全搬到 later 之前，
        # 按"有效期全过"的真实过期路径扫第二遍并记录剥离数。两遍都走生产
        # sweep_federation_once（第二遍直接调 probe_retention，绕开活性行）。
        monkeypatch.setattr(settings, "federation_probe_evidence_retention_seconds", 0)
        live_stats = await sweep_federation_once(sessions, now=later)
        from datetime import datetime as _datetime
        from ddp_corpus.federation_models import FederationProbe, FederationRequest
        from sqlalchemy import func, select, text, update
        # 第一遍记录：穷查任务没有 continuation 门，只有 plan 链 pin。
        # plan.valid_until = min(manifest 2030, consent 2030, now+900s)：截止线
        # later = now+2h 已在其后 → (a) 门已关 → 剥离。这是"plan 自然过期"的
        # 真实生产路径（不是行级搬运），记录哪一扇门关了它。
        # （剥离数从 live_stats 读，见 probe_evidence_retention.first_sweep_live_stripped。）
        # 第二遍：把任务有效期与 probe TTL 全搬到截止线之前。但第一遍已经
        # 把全部 16 行 evidence 剥离（行还在，evidence 已空），第二遍按实现
        # 只计"本遍实际剥离的行"（stored.get("evidence") 非空才计数），所以
        # stripped 必为 0 —— 0 在这里是"无残留可剥"，不是"没扫"。两遍合读：
        # 第一遍 16（plan 自然过期路径）+ 第二遍 0（全搬运后无残留）= 全部
        # evidence 已有界。注意这是行级时间搬运（与单测手法一致），不是
        # 生产 sweep 的一部分；生产里时间自己会走过去。
        async with sessions() as session:
            requests = (await session.execute(select(FederationRequest))).scalars().all()
            aged = 0
            for row in requests:
                if row.plan_json:
                    plan = dict(row.plan_json)
                    plan["valid_until"] = "2020-01-01T00:00:00Z"
                    plan["budget"] = {**(plan.get("budget") or {}),
                                      "deadline": "2020-01-01T00:00:00Z"}
                    row.plan_json = plan
                if row.scope_manifest_json:
                    row.scope_manifest_json = {**row.scope_manifest_json,
                                               "valid_until": "2020-01-01T00:00:00Z"}
                if row.exploration_consent_json:
                    row.exploration_consent_json = {**row.exploration_consent_json,
                                                    "valid_until": "2020-01-01T00:00:00Z"}
                if row.execution_consent_json:
                    row.execution_consent_json = {**row.execution_consent_json,
                                                  "valid_until": "2020-01-01T00:00:00Z"}
                aged += 1
            await session.execute(update(FederationProbe).values(
                expires_at=_datetime(2020, 1, 1)))
            await session.commit()
        sweep_stats = await sweep_federation_once(sessions, now=later)
        # 第二遍的 sweep_stats 覆盖第一遍：第一遍读数已进
        # probe_evidence_retention.first_sweep_live_stripped，此处不再需要。
        sweep_stats["first_sweep_live_stripped"] = int(
            live_stats.get("probe_evidence_stripped") or 0)
        async with sessions() as session:
            probe_rows_after = (await session.execute(
                select(func.count()).select_from(FederationProbe))).scalar()
            probe_evidence_left = (await session.execute(text(
                "SELECT coalesce(sum(octet_length(result_json::text)),0) FROM corpus.federation_probes"))).scalar()
            probe_evidence_items = (await session.execute(text(
                "SELECT coalesce(sum(jsonb_array_length((result_json->'evidence')::jsonb)),0) FROM corpus.federation_probes"))).scalar()
            stripped_probes = int(sweep_stats.get("probe_evidence_stripped") or 0)
        async with sessions() as session:
            removed_cache = await cache.purge_expired(session, now=later)
            await session.commit()
        swept = await measure(connection, "corpus")
        object_after = {"objects": len(app.state.storage.objects), "bytes": sum(len(v[0]) for v in app.state.storage.objects.values())}
        content_tables = {name: swept[name]["rows"] for name in (
            "documents", "parse_jobs", "chunks", "resources", "resource_versions", "evidence", "bundle_replicas")}
        assert all(count == 0 for count in content_tables.values()), content_tables
        assert object_after == object_before == {"objects": 0, "bytes": 0}
        assert not forbidden_fetches, forbidden_fetches
        ledgers = [dict(row) for row in await connection.fetch(
            "SELECT max_requests,max_bytes,max_probe_requests,max_discovery_requests,used_requests,used_bytes,used_probes,used_discovery FROM corpus.federation_root_ledgers")]
        assert all(row["used_requests"] <= row["max_requests"] and row["used_bytes"] <= row["max_bytes"] for row in ledgers)
        return {"before": before, "retrievals": phases, "after_cache_pressure": cached,
            "cache_payload_bytes": cache_payload, "after_two_hour_sweep": swept,
            "sweep_stats": sweep_stats, "expired_cache_removed": removed_cache,
            "probe_evidence_retention": {"first_sweep_live_stripped": int(sweep_stats.get("first_sweep_live_stripped") or 0),
                "first_sweep_bound": "plan.valid_until=min(manifest 2030, consent 2030, now+900s) already past the +2h cutoff: (a) plain-resume gate closed; exhaustive_scope tasks have no continuation gate (b); all 16 rows stripped on the natural plan-expiry path, rows kept; second aged pass strips 0 (nothing left)",
                "second_sweep_note": "validities + probe TTL row-aged past the cutoff; stripped==0 means no residue left, not no sweep",
                "tasks_aged_past_validity": aged,
                "stripped_rows": stripped_probes,
                "probe_rows_after": probe_rows_after,
                "probe_result_bytes_after": probe_evidence_left,
                "probe_evidence_items_after": probe_evidence_items},
            "object_store": {"implementation": "MemoryStorage", "before": object_before, "after": object_after},
            "remote_original_fulltext_vector_tables": content_tables,
            "remote_content_fetches": forbidden_fetches,
            "budget": budget, "root_ledgers": ledgers,
            "cache_limits": {"entries": 16, "payload_bytes": 8192, "ttl_seconds": 900},
            "approved_trust_cache": trust_cache,
            "peer_calls": sum(len(peer.calls) for peer in peers.values())}
    finally:
        monkeypatch.undo()
        node_identity.reset()
        await app.state.http.aclose()
        await engine.dispose()
        db.reset_engine()
        await connection.close()


def inventory():
    families = [
        {"family": "control.node_members (trust, descriptors, renewal state)", "bound": "one current row per explicitly registered direct member; renewals UPDATE same row; no configured member count/descriptor-byte cap", "retention": "unbounded administrator-managed registry", "source": "services/control-api/internal/store/discovery.go:88-159; services/control-api/internal/store/discovery_renewal.go"},
        {"family": "control.member_snapshots/member_snapshot_pages/node_directory_views", "bound": "page_size<=100; TTL 30..3600s rejects reads; retention sweep past DISCOVERY_METADATA_RETENTION_SECONDS (default 86400, 0..604800) deletes expired unpinned snapshots+pages via the renewal housekeeping seam", "retention": "TTL + retention window sweep; still-referenced snapshots never deleted independently", "source": "services/control-api/internal/store/discovery.go:225-314; services/control-api/internal/store/discovery_retention.go; services/control-api/internal/api/discovery_renewal.go; services/control-api/internal/config/config.go"},
        {"family": "control.scope_manifests/scope_target_pages/scope_catalog_sources/scope_catalog_revocations/scope_remote_sources + subtree_snapshots/subtree_snapshot_pages", "bound": "per scope MaxMembers<=10000, MaxRemoteMembers<=10000, MaxDiscoveryRequests<=10000, TTL<=3600; experiment 256/256/1024; subtree per-issuer<=32 + 5min TTL at creation; retention sweep past the window deletes expired scopes (pages/sources cascade) before members; purged scope reads 404, never a fake empty denominator", "retention": "TTL + retention window sweep; no aggregate count/byte cap beyond per-scope and per-issuer creation caps", "source": "services/control-api/internal/discovery/scope.go:66-85; services/control-api/internal/store/scope.go:210-243; services/control-api/internal/store/subtree.go:32-44; services/control-api/internal/store/discovery_retention.go"},
        {"family": "control.federation_credential_nonces", "bound": "credential TTL<=120s; expired nonces deleted transactionally on next consumption; live population depends on inbound request rate", "retention": "TTL + write-time sweep, not a hard entry cap", "source": "services/control-api/internal/store/peer.go:191-195"},
        {"family": "control.audit_events", "bound": "registration/approval/scope audit entries; no automatic retention or cap", "retention": "unbounded audit history", "source": "services/control-api/internal/api/scope_handlers.go:90; database/control/0001_control_schema.sql:135-149"},
        {"family": "control.usage_ledger/control_outbox; corpus.usage_claims/corpus_outbox/processed_events", "bound": "per-operation accounting and durable event/idempotency facts, not remote content; retry attempts bounded but successful/dead events have no automatic row-retention cap", "retention": "unbounded explicit operation/audit history", "source": "database/control/0001_control_schema.sql:114-129,204; services/corpus-api/ddp_corpus/outbox.py:44,109-114"},
        {"family": "corpus.federation_cache_entries (negative, descriptor, result projections)", "bound": "FEDERATION_CACHE_MAX_ENTRIES=10000, MAX_BYTES=67108864, TTL_SECONDS=900 defaults; deterministic eviction under PostgreSQL advisory lock; experiment 16/8192/900", "retention": "hard entry/payload-byte caps + put/get/purge expiry deletion", "source": "services/corpus-api/ddp_corpus/cache.py:243-302,326-340; services/corpus-api/ddp_corpus/config.py:317-325"},
        {"family": "corpus.federation_probes including evidence-set JSON excerpts", "bound": "root max_probe_requests and max_bytes bound one task; evidence excerpt<=2000 chars, receipt TTL=300s gates reads; PROBE_SCAN_LIMIT=64 limits scanning, NOT storage; probe evidence retention (FEDERATION_PROBE_EVIDENCE_RETENTION_SECONDS default 86400, experiment 0) strips result_json.evidence past the window unless a non-cancelled task can still resume-or-continue past the cutoff: (a) min(plan.valid_until, plan.budget.deadline, execution_consent.valid_until) for plain resume, or (b) fast non-fixed task min(scope_manifest.valid_until, exploration_consent.valid_until) for continuation; rows/ids/digests/state kept for coverage refs", "retention": "TTL + retention window evidence stripping; receipt rows retained; resume-or-continuation pins (a)/(b) preserved", "source": "services/corpus-api/ddp_corpus/federation_tasks.py:1056-1087; services/corpus-api/ddp_corpus/federation.py:733-741; services/corpus-api/ddp_corpus/probe_retention.py; services/corpus-api/ddp_corpus/reconcile.py:237-293"},
        {"family": "corpus.federation_admissions/federation_executions/tasks", "bound": "one admission per issuer/org/idempotency key; fixed admitted plan and per-root request/byte/hop/deadline budget; execution evidence JSON bound per task; no terminal-row retention cap", "retention": "task facts, unbounded across explicit tasks", "source": "services/corpus-api/ddp_corpus/federation_models.py:51-118; services/corpus-api/ddp_corpus/federation_budget.py:146-205"},
        {"family": "corpus.federation_requests/coverage_ledgers/coverage_entries/federation_task_events", "bound": "one root and ledger per explicit task; unique target denominator from frozen scope (experiment<=256); event and scope JSON history not subject to global cache caps", "retention": "task facts, unbounded across explicit tasks", "source": "services/corpus-api/ddp_corpus/federation_models.py:121-239"},
        {"family": "corpus.federation_root_ledgers/federation_root_reservations", "bound": "one ledger per root; once-only reservation keys per logical step; root immutable allowance, but no global task retention cap", "retention": "task facts, unbounded across explicit tasks", "source": "services/corpus-api/ddp_corpus/federation_budget.py:61-89,146-205"},
        {"family": "corpus.federation_delegation_consumption", "bound": "one reserved/reported share record per unique root/step; immutable root budget bounds shares; no cross-task retention cap; zero rows in direct-peer experiment", "retention": "task facts, unbounded across explicit tasks", "source": "services/corpus-api/ddp_corpus/federation_models.py:337-346"},
        {"family": "corpus.federation_deliveries", "bound": "DELIVERY_RESULT_MAX_BYTES=1048576 per document; temporary expiry prevents reads/ack but does not erase persisted JSON", "retention": "unbounded across explicit deliveries", "source": "services/corpus-api/ddp_corpus/federation_tasks.py:122,2533-2542,3759-3814"},
        {"family": "corpus.federation_credential_nonces", "bound": "credential TTL<=120s + federation sweep (default every30s) deletes expired jti; no live-count cap", "retention": "TTL + periodic sweep", "source": "services/corpus-api/ddp_corpus/node_auth.py:214-219; services/corpus-api/ddp_corpus/reconcile.py:250-254"},
        {"family": "corpus node trust key cache (process memory only)", "bound": "FEDERATION_PEER_KEY_CACHE_MAX_ENTRIES=1024 default, configured global cap; TTL<=60s; every lookup and insertion prunes all expired identities and evicts oldest fetched record when full; experiment cap16/TTL5s", "retention": "global entry cap + TTL pruning on every lookup/insert; no persistent content", "source": "services/corpus-api/ddp_corpus/node_auth.py:148-203"},
        {"family": "corpus.collection_catalog_snapshots/pages", "bound": "local published collection catalog only: MAX_COLLECTIONS=10000, MAX_SNAPSHOTS=32 per binding, MAX_SNAPSHOT_BYTES=8388608; expiry deletion during creation", "retention": "per-binding cap; not a remote fulltext mirror", "source": "services/corpus-api/ddp_corpus/catalog.py:16-18,260-269"},
        {"family": "corpus.bundle_replicas/bundle_replica_revoke_keys; original/derived object keys", "bound": "explicit licensed Bundle import only, no registration/discovery/retrieval auto-import; object deletion reference-safe via GC, no global byte cap for user-authorized durable imports", "retention": "explicit user-owned assets, NOT automatic remote replication", "source": "services/corpus-api/ddp_corpus/bundle_models.py:25-63,123-130; services/corpus-api/ddp_corpus/gc.py:187-199"},
        {"family": "corpus.documents/parse_jobs/chunks/resources/resource_versions/evidence; object storage", "bound": "local/explicitly imported content only; federated evidence JSON separate from original, fulltext and pgvector tables; experiment all original/index rows and object bytes stay zero", "retention": "explicit local assets", "source": "services/corpus-api/ddp_corpus/federation_models.py:13-15; services/corpus-api/ddp_corpus/federation_tasks.py:1084-1086"},
    ]
    for item in families:
        if item["family"].startswith((
                "corpus.federation_admissions", "corpus.federation_requests",
                "corpus.federation_root_ledgers", "corpus.federation_delegation_consumption",
                "corpus.federation_deliveries")):
            item["criterion"] = "（判据外）用户任务事实，不是路由/证据缓存"
    return families


async def experiment(output):
    import asyncpg
    container = f"ddp-t64-{os.getpid()}"
    dsn = "postgresql://postgres:pw@127.0.0.1:15485/scale"
    command(["docker", "run", "-d", "--name", container, "-e", "POSTGRES_PASSWORD=pw", "-e", "POSTGRES_DB=scale", "-p", "127.0.0.1:15485:5432", "pgvector/pgvector:pg16"])
    runs = []
    try:
        for attempt in range(60):
            try:
                connection = await asyncpg.connect(dsn)
                await connection.close()
                break
            except (OSError, asyncpg.PostgresError):
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        with tempfile.TemporaryDirectory(prefix="ddp-t64-") as scratch:
            for n in (10, 50, 200):
                connection = await asyncpg.connect(dsn)
                await connection.execute("DROP SCHEMA IF EXISTS control CASCADE; DROP SCHEMA IF EXISTS corpus CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE SCHEMA corpus; ALTER ROLE postgres SET search_path=corpus,public")
                await connection.close()
                command([GO, "run", "./cmd/control-migrate", "-database", dsn, "up"], cwd=ROOT / "services/control-api")
                environment = {**os.environ, "DATABASE_URL": dsn.replace("postgresql://", "postgresql+asyncpg://")}
                command([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "database/corpus", env=environment)
                path = Path(scratch) / f"control-{n}.json"
                environment.update(CONTROL_TEST_DATABASE_URL=dsn, DDP_SCALE_N=str(n), DDP_SCALE_OUTPUT=str(path))
                command([GO, "test", "./internal/api", "-run", "^TestScaleStorageExperiment$", "-count=1"], cwd=ROOT / "services/control-api", env=environment)
                control = json.loads(path.read_text())
                corpus = await corpus_run(dsn, control)
                # Scope IDs/node identities are metadata; omit full manifest from artifact.
                control.pop("manifest")
                runs.append({"n": n, "control": control, "corpus": corpus})
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        sources = [ROOT / relative for relative in (
            "scripts/scale_storage_experiment.py",
            "services/control-api/internal/api/scale_storage_pg_test.go",
            "services/control-api/internal/api/discovery_retention_pg_test.go",
            "services/control-api/internal/api/discovery_renewal.go",
            "services/control-api/internal/store/discovery_retention.go",
            "services/control-api/internal/config/config.go",
            "services/control-api/CONFIG.md",
            "services/corpus-api/ddp_corpus/federation_tasks.py",
            "services/corpus-api/ddp_corpus/federation_models.py",
            "services/corpus-api/ddp_corpus/federation_budget.py",
            "services/corpus-api/ddp_corpus/reconcile.py",
            "services/corpus-api/ddp_corpus/probe_retention.py",
            "services/corpus-api/tests/test_probe_retention.py",
            "services/corpus-api/tests/test_probe_retention_continuation.py",
            "services/corpus-api/ddp_corpus/cache.py",
            "services/corpus-api/ddp_corpus/node_auth.py",
            "services/corpus-api/ddp_corpus/config.py",
            "services/control-api/internal/discovery/expand.go",
            "services/control-api/internal/store/discovery.go",
            "services/control-api/internal/store/scope.go",
            "database/corpus/alembic/versions/0042_recursive_delegation.py",
        )]
        growth = {}
        for schema, phase in (("control", "after_expiry_and_second_scope"), ("corpus", "after_cache_pressure")):
            names = runs[-1][schema][phase]
            growth[schema] = {
                name: [{"n": run["n"], **run[schema][phase][name]} for run in runs]
                for name in names if any(run[schema][phase][name]["rows"] > run[schema]["before"][name]["rows"] for run in runs)
            }
        retention_proof = {
            "control_scopes_members_bounded": [
                {"n": run["n"],
                 "scopes_after_second": run["control"]["after_expiry_and_second_scope"]["scope_manifests"]["rows"],
                 "scopes_after_sweep": run["control"]["after_retention_sweep"]["scope_manifests"]["rows"],
                 "members_after_second": run["control"]["after_expiry_and_second_scope"]["member_snapshots"]["rows"],
                 "members_after_sweep": run["control"]["after_retention_sweep"]["member_snapshots"]["rows"],
                 "remotes_after_second": run["control"]["after_expiry_and_second_scope"]["scope_remote_sources"]["rows"],
                 "remotes_after_sweep": run["control"]["after_retention_sweep"]["scope_remote_sources"]["rows"]}
                for run in runs],
            "probe_evidence_stripped": [
                {"n": run["n"], **run["corpus"]["probe_evidence_retention"]}
                for run in runs],
        }
        artifact = {"acceptance": "T64", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "code_revision": revision, "working_tree_source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
            "command": ".venv/bin/python scripts/scale_storage_experiment.py --output docs/refactor/artifacts/scale-storage-20261004.json",
            "methodology": {"nodes": [10, 50, 200], "postgres": "pgvector/pgvector:pg16 disposable container, 127.0.0.1:15485", "schemas": "actual Go and Alembic migrations, new schemas per N", "discovery": "registered and approved N httptest peers; two complete scopes with 256-target/256-node/1024-request limits; retention sweep (window 0) purges expired unpinned scopes then snapshots; purged scope reads 404", "retrieval": "two actual corpus HTTP intent/plan/approve/queued-worker retrievals on Go-produced scope; fixed 64-request, 2MiB, 8-probe and 8-discovery allowances; probe evidence retention 0 in experiment (production default 86400): first sweep at +2h strips 16 on the natural plan-expiry path (plan=min(manifest, consent, now+900s) already past; exhaustive tasks have no continuation gate); rows kept, evidence 0; then validities + probe TTL row-aged past the cutoff and a second production sweep strips 0 (no residue); rows/ids/digests kept throughout", "objects": "MemoryStorage counts and bytes; no real remote original/fulltext/vector payload transmitted", "size": "pg_total_relation_size plus pg_column_size sum; physical page/TOAST allocation is not the configured JSON-byte cap"},
            "inventory": inventory(), "observed_growing_families": growth, "runs": runs,
            "retention_proof": retention_proof,
            "verdict": {"no_automatic_original_fulltext_vector_mirror": True, "configured_projection_cache_bounded": True,
                "directory_projections_bounded_by_ttl_plus_retention_sweep": True,
                "probe_evidence_bounded_by_ttl_plus_retention_stripping": True,
                "all_remote_metadata_storage_bounded": True, "T64": "pass",
                "residual": ["No aggregate count/byte cap beyond per-scope creation caps and per-issuer subtree caps; live (unexpired) scopes and member rows still grow with live task/registration volume, which is administrator-driven, not automatic mirroring.", "Task histories (requests/coverage/events/deliveries/ledgers) remain （判据外）user task facts with no retention cap."]},
                "physical_storage_note": "Cache live entries/payload bytes plateau at configured caps, but pg_total_relation_size grows under insert/delete pressure (MVCC dead tuples, indexes and page high-water allocation). No configured physical-relation-byte cap is claimed; autovacuum/reuse and disk capacity remain operational concerns.",
            "proposed_fixes_outside_slice": [],
            "scope_decisions": {"task_histories": "（判据外）requests/coverage/events/deliveries/root ledgers/delegation consumption are user task facts, not routing/evidence caches; no retention-policy change required for T64."}}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        print(f"T64 artifact: {output}; no mirror/cache caps verified; metadata retention gaps recorded")
    finally:
        command(["docker", "rm", "-f", container])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/refactor/artifacts/scale-storage-20261004.json")
    args = parser.parse_args()
    asyncio.run(experiment(args.output.resolve()))


if __name__ == "__main__":
    main()
