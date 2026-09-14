#!/usr/bin/env python
"""`scripts/backup_restore_drill.sh` 的数据库/对象存储部分。

职责边界：**容器生命周期、两套迁移、pg_dump/pg_restore 都在 shell 里** ——
那几步要人看得见命令。这里只做两件在 shell 里写会很啰嗦的事：

    drift seed --dsn ... --state ... --minio-endpoint ...   # 灌最小数据集
    drift verify --source-dsn ... --restored-dsn ... --state ... --minio-endpoint ...

`seed` 写入的最小数据集（§19.8 演练口径）：
`control.organizations/users/memberships` + `documents/parse_jobs/resources/
resource_versions` + `federation_requests/coverage_ledgers/coverage_entries`，
外加两个对象存储对象（document.object_key 与 version.bundle_prefix 下）。

`verify` 对**恢复库**做行数对拍、跨表不变量与对象存储对账；任何一项不成立
就非零退出。它不"抽样看一眼"，每一项都是全量 SQL。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "corpus-api"))
sys.path.insert(0, str(ROOT / "python" / "ddp_core"))
sys.path.insert(0, str(ROOT / "python" / "ddp_contracts"))

ORG = "org-drill"
USER = "user-drill"
ACTOR = "actor-drill"
DOCUMENT = "d" * 32
PARSE_JOB = "j" * 32
RESOURCE = "r" * 32
VERSION = "v" * 32
ROOT_TASK = "task-drill-" + "1" * 16
PDF_BYTES = b"%PDF-1.4\nrecovery drill source\n%%EOF\n"
BUNDLE_BYTES = b'{"schema":"ddp-bundle/1","drill":true}\n'

COUNT_QUERIES = [
    ("control.organizations", "SELECT count(*) FROM control.organizations"),
    ("control.users", "SELECT count(*) FROM control.users"),
    ("control.memberships", "SELECT count(*) FROM control.memberships"),
    ("public.documents", "SELECT count(*) FROM public.documents"),
    ("public.parse_jobs", "SELECT count(*) FROM public.parse_jobs"),
    ("public.resources", "SELECT count(*) FROM public.resources"),
    ("public.resource_versions", "SELECT count(*) FROM public.resource_versions"),
    ("public.federation_requests", "SELECT count(*) FROM public.federation_requests"),
    ("public.coverage_ledgers", "SELECT count(*) FROM public.coverage_ledgers"),
    ("public.coverage_entries", "SELECT count(*) FROM public.coverage_entries"),
]

INVARIANT_QUERIES = [
    ("federation_requests 的组织都有 control.organizations 行",
     "SELECT count(*) FROM public.federation_requests r LEFT JOIN control.organizations o "
     "ON o.id = r.organization_id WHERE o.id IS NULL"),
    ("coverage_entries 都有 coverage_ledgers",
     "SELECT count(*) FROM public.coverage_entries e LEFT JOIN public.coverage_ledgers l "
     "ON l.root_task_id = e.root_task_id WHERE l.root_task_id IS NULL"),
    ("resource_versions 的资源都在",
     "SELECT count(*) FROM public.resource_versions v LEFT JOIN public.resources r "
     "ON r.id = v.resource_id WHERE r.id IS NULL"),
    ("resource_versions 的文档都在",
     "SELECT count(*) FROM public.resource_versions v LEFT JOIN public.documents d "
     "ON d.id = v.document_id WHERE d.id IS NULL"),
    ("parse_jobs 的文档都在",
     "SELECT count(*) FROM public.parse_jobs p LEFT JOIN public.documents d "
     "ON d.id = p.document_id WHERE d.id IS NULL"),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def minio_client(args):
    from minio import Minio

    return Minio(args.minio_endpoint, access_key=args.minio_access_key,
                 secret_key=args.minio_secret_key, secure=False)


async def _seed_control(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO control.organizations (id, name, slug) "
            "VALUES (:id, :name, :slug) ON CONFLICT (id) DO NOTHING"),
            {"id": ORG, "name": "Recovery drill", "slug": "recovery-drill"})
        await conn.execute(text(
            "INSERT INTO control.users (id, username, password_hash) "
            "VALUES (:id, :username, :password_hash) ON CONFLICT (id) DO NOTHING"),
            {"id": USER, "username": "drill-user", "password_hash": "not-a-real-hash"})
        await conn.execute(text(
            "INSERT INTO control.memberships (organization_id, user_id, role) "
            "VALUES (:org, :user, 'admin') ON CONFLICT DO NOTHING"),
            {"org": ORG, "user": USER})


async def _seed_corpus(engine, document_key: str, bundle_key: str) -> None:
    from ddp_corpus.federation_models import CoverageEntry, CoverageLedger, FederationRequest
    from ddp_corpus.models import Document, ParseJob, Resource, ResourceVersion

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(Document(
            id=DOCUMENT, uploaded_by=USER, organization_id=ORG, doc_id="c" * 64,
            origin="web", filename="drill.pdf", mime="application/pdf",
            size_bytes=len(PDF_BYTES), object_key=document_key))
        session.add(ParseJob(
            id=PARSE_JOB, document_id=DOCUMENT, engine="borndigital",
            options_hash="a" * 64, status="succeeded", page_count=1,
            initiated_by=USER, resource_id=RESOURCE))
        session.add(Resource(
            id=RESOURCE, organization_id=ORG, owner_id=USER, uploaded_by=USER,
            display_name="drill.pdf", publication="published"))
        session.add(ResourceVersion(
            id=VERSION, resource_id=RESOURCE, version_no=1, document_id=DOCUMENT,
            parse_job_id=PARSE_JOB, bundle_prefix=bundle_key,
            source_digest=digest(PDF_BYTES), filename="drill.pdf", size_bytes=len(PDF_BYTES)))
        # 显式 flush：coverage_entries 的外键指向 coverage_ledgers，而这里没有
        # ORM relationship 替 UoW 排依赖；不 flush 时它会先插子行。
        await session.flush()
        session.add(FederationRequest(
            root_task_id=ROOT_TASK, organization_id=ORG, actor_id=ACTOR,
            task_spec_digest="sha256:" + "e" * 64, scope_id="scope-drill",
            scope_digest="sha256:" + "f" * 64, search_mode="exhaustive_scope",
            planning_state="approved", plan_revision=1, plan_digest="sha256:" + "0" * 64,
            status="succeeded", retrieval_completeness="complete",
            evidence_sufficiency="sufficient_by_policy", coverage_ref="scope-drill"))
        await session.flush()
        session.add(CoverageLedger(
            root_task_id=ROOT_TASK, scope_ref="scope-drill", search_mode="exhaustive_scope",
            enumeration_state="sealed", retrieval_completeness="complete",
            evidence_sufficiency="sufficient_by_policy", counts_json={"succeeded": 1},
            manifest_digest="sha256:" + "1" * 64))
        await session.flush()
        session.add(CoverageEntry(
            root_task_id=ROOT_TASK, target_digest="2" * 64,
            target_key_json={"origin_node_id": "node-drill", "collection_id": "col-drill",
                             "operation": "corpus.retrieve"},
            query_digest="sha256:" + "3" * 64, state="succeeded", attempts=1,
            actual_index_revision="index-1", evidence_refs_json=[],
            used_budget_json={"requests": 1, "bytes": 0}))
        await session.commit()


async def _table_counts(dsn: str) -> dict[str, int]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            counts = {}
            for name, query in COUNT_QUERIES:
                counts[name] = int((await conn.execute(text(query))).scalar_one())
            return counts
    finally:
        await engine.dispose()


async def _invariants(dsn: str) -> dict[str, int]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            return {name: int((await conn.execute(text(query))).scalar_one())
                    for name, query in INVARIANT_QUERIES}
    finally:
        await engine.dispose()


async def seed(args) -> int:
    engine = create_async_engine(args.dsn)
    document_key = f"uploads/{ORG}/{DOCUMENT}.pdf"
    bundle_key = f"bundles/{VERSION}/"
    try:
        await _seed_control(engine)
        await _seed_corpus(engine, document_key, bundle_key)
    finally:
        await engine.dispose()

    state = {
        "organization": ORG, "user": USER, "document_id": DOCUMENT,
        "parse_job_id": PARSE_JOB, "resource_id": RESOURCE, "version_id": VERSION,
        "root_task_id": ROOT_TASK,
        "document_object_key": document_key,
        "document_sha256": digest(PDF_BYTES),
        "bundle_prefix": bundle_key,
        "bundle_manifest_key": bundle_key + "manifest.json",
        "bundle_sha256": digest(BUNDLE_BYTES),
    }
    if args.minio_endpoint:
        client = minio_client(args)
        if not client.bucket_exists(args.bucket):
            client.make_bucket(args.bucket)
        client.put_object(args.bucket, document_key,
                          io.BytesIO(PDF_BYTES), len(PDF_BYTES),
                          content_type="application/pdf")
        client.put_object(args.bucket, bundle_key + "manifest.json",
                          io.BytesIO(BUNDLE_BYTES), len(BUNDLE_BYTES),
                          content_type="application/json")
        state["minio_bucket"] = args.bucket
    Path(args.state).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"seed OK: 组织 {ORG} / 用户 {USER} / 资源 {RESOURCE} / 任务 {ROOT_TASK} "
          f"（对象 {document_key} 与 {bundle_key}manifest.json）")
    return 0


async def verify(args) -> int:
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    problems: list[str] = []

    source = await _table_counts(args.source_dsn)
    restored = await _table_counts(args.restored_dsn)
    for name, _query in COUNT_QUERIES:
        mark = "OK " if source[name] == restored[name] else "DIFF"
        print(f"  {mark} {name:32s} source={source[name]:4d} restored={restored[name]:4d}")
        if source[name] != restored[name]:
            problems.append(f"{name}: source={source[name]} restored={restored[name]}")
        if name in ("control.organizations", "public.resource_versions",
                    "public.federation_requests", "public.coverage_entries") \
                and restored[name] < 1:
            problems.append(f"{name}: 恢复库里没有演练种下的数据")

    invariants = await _invariants(args.restored_dsn)
    for name, count in invariants.items():
        print(f"  {'OK ' if count == 0 else 'BAD'} 不变量：{name}（违反行数 {count}）")
        if count != 0:
            problems.append(f"不变量失败：{name} -> {count}")

    engine = create_async_engine(args.restored_dsn)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT source_digest FROM public.resource_versions WHERE id = :id"),
                {"id": state["version_id"]})).scalar_one_or_none()
            if row != state["document_sha256"]:
                problems.append(f"恢复后的版本摘要变了：{row!r}")
            else:
                print(f"  OK  固定版本摘要保持：sha256:{row}")
    finally:
        await engine.dispose()

    if args.minio_endpoint:
        client = minio_client(args)
        bucket = state["minio_bucket"]
        for key, expected in ((state["document_object_key"], state["document_sha256"]),
                              (state["bundle_manifest_key"], state["bundle_sha256"])):
            response = None
            try:
                response = client.get_object(bucket, key)
                data = response.read()
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
            actual = digest(data)
            print(f"  {'OK ' if actual == expected else 'BAD'} 对象对账：{key}")
            if actual != expected:
                problems.append(f"对象 {key} 摘要 {actual} != {expected}")
        listed = {obj.object_name for obj in client.list_objects(bucket, recursive=True)}
        referenced = {state["document_object_key"], state["bundle_manifest_key"]}
        extra = sorted(listed - referenced)
        print(f"  {'OK ' if not extra else 'BAD'} 桶内没有未对账对象（共 {len(listed)} 个）")
        if extra:
            problems.append(f"桶内出现未被任何种子行引用的对象：{extra}")

    if problems:
        print("::error::recovery drill 对账失败：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("recovery drill 对账 PASS：行数、不变量与对象存储三方一致")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    seed_parser = sub.add_parser("seed")
    seed_parser.add_argument("--dsn", required=True)
    seed_parser.add_argument("--state", required=True)
    seed_parser.add_argument("--minio-endpoint", default="")
    seed_parser.add_argument("--minio-access-key", default="drill")
    seed_parser.add_argument("--minio-secret-key", default="drill-secret")
    seed_parser.add_argument("--bucket", default="deepdocparse")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--source-dsn", required=True)
    verify_parser.add_argument("--restored-dsn", required=True)
    verify_parser.add_argument("--state", required=True)
    verify_parser.add_argument("--minio-endpoint", default="")
    verify_parser.add_argument("--minio-access-key", default="drill")
    verify_parser.add_argument("--minio-secret-key", default="drill-secret")
    verify_parser.add_argument("--bucket", default="deepdocparse")
    args = parser.parse_args()
    if args.command == "seed":
        return asyncio.run(seed(args))
    return asyncio.run(verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
