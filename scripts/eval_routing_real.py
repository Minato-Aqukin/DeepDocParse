#!/usr/bin/env python
"""真实联邦评测驱动：对独立 A/B/C/P/R 节点跑 one real driver。

合成夹具（`eval/routing/run.py`）与本驱动彻底分开：
- 合成：冻结 JSON + `FixtureExecutor` 预言机，不碰网络；
- 真实：本脚本，对每个节点走真实 HTTP。

节点配置 schema（`--nodes` JSON，`schema="ddp-real-eval-nodes/1"`）::

    {"schema": "ddp-real-eval-nodes/1",
     "entry": "a",
     "auth": {"username": "...", "password": "..."},
     "nodes": {
       "a": {"control": "http://127.0.0.1:30080",
             "node_id": "node-<a 的权威 id，运行期从该 control 权威描述比对>",
             "role": "ingest"},
       "b": {"control": "http://127.0.0.1:30180", "node_id": "...", "role": "ingest"},
       "c": {"control": "http://127.0.0.1:30280", "node_id": "...", "role": "generate"},
       "p": {"control": "http://127.0.0.1:30380", "node_id": "...", "role": "expand"},
       "r": {"control": "http://127.0.0.1:30480", "node_id": "...", "role": "expand"}},
     "placement": {"pico": "a", "esp32": "b", "attention": "b"},
     "generation": {"node": "c"}}

规则（写死在 `eval/routing/real.py::validate_node_config`，违反直接 exit 2）：
- `entry` 是发起联邦任务的入口节点（A/B 其中之一）；
- `placement` 的每个 source 只落 exactly 一个 ingest 节点；A/B 分布入库；
- `role=generate` 的 C 与 `role=expand` 的 P/R 永不出现在 placement，
  驱动永不向它们上传 PDF 冒充来源；
- 每个节点的 `role` 与 `node_id` 在运行期从各 control 权威描述
  （`GET /api/v1/federation/node` 的 `authority_node_id`）逐一比对，
  对不上就显式失败，不猜、不 fallback。

每 case 真实链路（全部经各 control 转发的语料面；未知接口不猜）：
`POST /api/uploads` -> 分片 PUT 预签名 -> finalize（幂等键=会话 id，
形状见 control `upload_handlers.go`） -> 轮询到 ready 且
`verified_sha256` 与本地一致 -> 按 doc_id 找文档 -> 解析 `succeeded`
-> 查 jobs 取 `resource_id/version_id` -> 建集合（`version_ids=[version]`）
-> publish（需 `expected_revision`；索引未 ready 会 409
`collection_not_publishable` 并显式失败） -> 入口建联邦 scope
（`POST /api/v1/federation/scopes`，`operation="corpus.retrieve"`）
-> 冻结 manifest 覆盖 placement 期望 -> task-intent（fast 与 exhaustive 均带
本次已验证的 federation_public manifest；exhaustive 另要求 sealed） ->
plan（TaskPlan 直接返回，`plan_digest` 顶层） -> approve（执行许可覆盖
全部 data_edges） -> submit（202 受理/200 重放） -> 轮询任务终态 ->
读覆盖账本（直接返回，entries 覆盖 scope 全分母） -> 归属校验失败记
`attribution_mismatch`（不吞）。

C 模型就绪证明（禁止把 endpoint 配置当就绪）：
`GET /api/v1/capabilities`（经入口 control）必须看到
`operation="rag.answer.cited"` 且 `readiness="ready"`；否则
`rag.answer.cited` 直接显式失败 `local_model_missing`。
`corpus.retrieve` 不要求生成就绪。
产物里 token 只有 `plan.budget.max_generation_tokens`（上限口径）；
result 无 usage 字段，`generation_tokens_actual` 恒为 null 并如实标注。

产物：每 case 一份原始可复核 JSON（`real-routing-<run>.json` +
每 case `<case>.json`），含问题、待复核提示（永为 pending）、全部
生成 claim 原文、证据信封（含 locator/origin/index_revision）、
requests/bytes/tokens-cap/latency、脱敏后的请求日志。
数字出现不等于 claim-support precision：不计算、不输出任何
人工标签口径的准确率/召回分母。

PDF 与 source-cases 均由 CLI 传入，仓库内不硬编码任何绝对路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from routing.real import (  # noqa: E402
    CostLedger,
    assert_not_synthetic_report,
    check_coverage_entries_cover_scope,
    check_evidence_attribution,
    check_scope_targets_cover_placement,
    explicit_failure,
    generation_capability_envelope,
    intent_keys,
    load_source_cases,
    now_seconds,
    real_report_path,
    redact_for_log,
    reference_hints,
    validate_node_config,
)

REAL_SCHEMA = "ddp-routing-real-eval/1#Report"
REQUEST_TIMEOUT = 60.0


def _redacted_exchange(*, method: str, url: str, status: int | None,
                       request_bytes: int = 0, response_bytes: int = 0,
                       latency_s: float = 0.0) -> dict:
    return redact_for_log({
        "method": method, "url": url, "status": status,
        "request_bytes": request_bytes, "response_bytes": response_bytes,
        "latency_s": round(latency_s, 3),
        "headers": {"Authorization": "Bearer [WITHHELD]"},
    })


def _fail(reason: str, detail: str, *, case_id: str = "") -> dict:
    body = explicit_failure(reason, detail)
    body["case_id"] = case_id
    return body


class _ExplicitFail(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="real federated routing eval driver")
    parser.add_argument("--nodes", type=Path, required=True)
    parser.add_argument("--source-cases", type=Path, required=True)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operation", default="rag.answer.cited",
                        choices=["corpus.retrieve", "rag.answer.cited"])
    parser.add_argument("--mode", default="exhaustive_scope",
                        choices=["fast", "exhaustive_scope"])
    parser.add_argument("--max-generation-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(argv)

    try:
        import httpx
    except ImportError:
        print("eval driver needs httpx", file=sys.stderr)
        return 2

    try:
        node_cfg = validate_node_config(
            json.loads(args.nodes.read_text(encoding="utf-8")))
    except ValueError as exc:
        print(f"bad node config: {exc}", file=sys.stderr)
        return 2
    try:
        cases_doc = load_source_cases(args.source_cases)
    except ValueError as exc:
        print(f"bad source-cases: {exc}", file=sys.stderr)
        return 2
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or uuid.uuid4().hex[:16]
    sources = {s["id"]: s for s in cases_doc["sources"]}

    session = _Session(args, run_id, node_cfg, sources)
    case_ids: list[str] = []
    failures: list[dict] = []
    for case in cases_doc["cases"]:
        started = now_seconds()
        artifact: dict = {
            "schema": "ddp-routing-real-eval/1#CaseArtifact",
            "run_id": run_id,
            "case_id": case["id"],
            "question": case["question"],
            "operation": args.operation,
            "search_mode": args.mode,
            "sources": [
                {k: sources[sid][k] for k in
                 ("id", "title", "publisher", "url", "version", "sha256", "domain")
                 if k in sources[sid]}
                for sid in case.get("sources", [])],
            "reference_hints": reference_hints(case),
            "executing_nodes": {},
            "human_review_status": "pending",
            "generated_claims": [],
            "evidence": [],
            "coverage": None,
            "cost": None,
            "exchanges": [],
            "failure": None,
        }
        ledger = CostLedger()
        try:
            session.run_case(case, sources, artifact, ledger)
        except _ExplicitFail as exc:
            artifact["failure"] = _fail(exc.reason, exc.detail,
                                        case_id=case["id"])
            failures.append({"case_id": case["id"], **artifact["failure"]})
        except Exception as exc:  # noqa: BLE001 —— 未知错误同样显式化
            artifact["failure"] = _fail("upstream_error",
                                        f"{type(exc).__name__}: {exc}"[:200],
                                        case_id=case["id"])
            failures.append({"case_id": case["id"], **artifact["failure"]})
        artifact["cost"] = ledger.as_dict()
        artifact["wall_seconds"] = round(now_seconds() - started, 3)
        (out_dir / f"{case['id']}.json").write_text(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        case_ids.append(case["id"])

    report = {
        "schema": REAL_SCHEMA,
        "run_id": run_id,
        "operation": args.operation,
        "search_mode": args.mode,
        "nodes": sorted(node_cfg.keys()),
        "cases": case_ids,
        "failures": failures,
        "failure_count": len(failures),
        "human_review_status": "pending",
        "note": ("Raw reviewable artifacts only. No claim-support precision, "
                 "no CI/cost/recall denominators are computed here."),
        "cost": None,
    }
    assert_not_synthetic_report(report)
    path = real_report_path(out_dir, run_id=run_id)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    print(json.dumps({"run_id": run_id, "cases": len(case_ids),
                      "failures": len(failures), "report": str(path)},
                     ensure_ascii=False))
    return 2 if failures else 0


class _Node:
    """一个节点的已认证会话：control 基址 + 权威 node_id + 角色 + token。"""

    def __init__(self, httpx, alias: str, spec: dict, token: str, subject: str):
        self.alias = alias
        self.control = spec["control"]
        self.node_id = spec["node_id"]
        self.role = spec["role"]
        self.subject = subject
        self.http = httpx.Client(base_url=self.control, timeout=REQUEST_TIMEOUT,
                                 trust_env=False)
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.http.headers["X-DDP-Client-Scope"] = (
            "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        )


class _Session:
    """一次评测运行的跨节点会话：各节点分别注册/登录，入口发起联邦任务。"""

    def __init__(self, args, run_id: str, node_cfg: dict, sources: dict):
        import httpx as _httpx

        self.args = args
        self.run_id = run_id
        self.node_cfg = node_cfg
        self.sources = sources
        self._httpx = _httpx
        raw = json.loads(args.nodes.read_text(encoding="utf-8"))
        auth = raw.get("auth") or {}
        username = auth.get("username") or f"eval-{run_id[:8]}"
        password = auth.get("password") or ""
        if not password:
            raise _ExplicitFail("peer_unavailable", "node config auth.password missing")
        entry_alias = raw.get("entry") or sorted(node_cfg)[0]
        if entry_alias not in node_cfg:
            raise _ExplicitFail("peer_unavailable",
                                f"entry node {entry_alias!r} not in nodes")
        self.entry_alias = entry_alias
        # 各节点分别注册/登录：同一用户名在各自治中心是不同主体，不合并。
        self.nodes: dict[str, _Node] = {}
        for alias, spec in node_cfg.items():
            token, subject = self._register_or_login(alias, spec["control"],
                                                    username, password)
            self.nodes[alias] = _Node(_httpx, alias, spec, token, subject)
        # 权威 node_id 比对：每个 control 的 federation/node 说了算。
        for alias, node in self.nodes.items():
            observed = self._authority_node_id(node)
            declared = node.node_id
            if declared and observed != declared:
                raise _ExplicitFail(
                    "peer_unavailable",
                    f"node {alias!r} declares {declared!r} "
                    f"but control reports {observed!r}"[:200])
            node.node_id = observed
        if self.nodes[entry_alias].role != "ingest":
            raise _ExplicitFail("peer_unavailable",
                                "entry node must have role ingest")
        # source -> {node_alias, document, version, collection...}（本轮内存态）。
        self.ingested: dict[str, dict] = {}

    # ---------------------------------------------------------- 认证与身份

    def _anon(self, control: str):
        return self._httpx.Client(base_url=control, timeout=REQUEST_TIMEOUT,
                                  trust_env=False)

    def _register_or_login(self, alias: str, control: str,
                           username: str, password: str) -> tuple[str, str]:
        with self._anon(control) as http:
            response = http.post("/api/auth/register",
                                 json={"username": username, "password": password})
            if response.status_code != 201:
                response = http.post("/api/auth/login",
                                     json={"username": username, "password": password})
            if response.status_code not in (200, 201):
                raise _ExplicitFail(
                    "peer_unavailable",
                    f"auth failed on node {alias!r}: {response.status_code}"[:200])
            body = response.json()
            if (body.get("user") or {}).get("role") == "admin":
                raise _ExplicitFail(
                    "peer_unavailable",
                    f"node {alias!r} evaluation requires a non-admin account",
                )
            subject = (body.get("user") or {}).get("id")
            if not isinstance(subject, str) or not subject:
                raise _ExplicitFail("peer_unavailable",
                                    f"node {alias!r} omitted its authenticated subject")
            return body["access_token"], subject

    def _authority_node_id(self, node: _Node) -> str:
        response = node.http.get("/api/v1/federation/node")
        if response.status_code != 200:
            raise _ExplicitFail("peer_unavailable",
                                f"node {node.alias!r} has no authority identity: "
                                f"{response.status_code}"[:200])
        body = response.json()
        authority = body.get("authority_node_id") or (body.get("descriptor") or {}).get(
            "node_id", "")
        if not authority:
            raise _ExplicitFail("peer_unavailable",
                                f"node {node.alias!r} identity response has no id"[:200])
        return authority

    # ---------------------------------------------------------- 每题执行

    def run_case(self, case: dict, sources: dict, artifact: dict,
                 ledger: CostLedger) -> None:
        needed = list(case.get("sources", []) or [])
        placement = self._placement()
        for sid in needed:
            if sid not in placement:
                raise _ExplicitFail("peer_unavailable",
                                    f"source {sid!r} has no placement in node config")
            alias = placement[sid]
            if self.node_cfg[alias]["role"] != "ingest":
                raise _ExplicitFail("peer_unavailable",
                                    f"source {sid!r} placed on non-ingest node {alias!r}")
            if sid not in self.ingested:
                self.ingested[sid] = self._ingest(alias, sources[sid], artifact, ledger)
        artifact["executing_nodes"] = {
            sid: {"alias": self._placement()[sid],
                  "node_id": self.ingested[sid]["origin_node_id"],
                  "collection_id": self.ingested[sid]["collection_id"],
                  "index_revision": self.ingested[sid].get("index_revision")}
            for sid in needed}
        # 冻结 scope：入口建，operation=corpus.retrieve，覆盖 placement 期望。
        scope = self._create_scope(artifact, ledger)
        expected = []
        for sid in needed:
            record = self.ingested[sid]
            expected.append({"origin_node_id": record["origin_node_id"],
                             "collection_id": record["collection_id"],
                             "operation": "corpus.retrieve"})
        try:
            check_scope_targets_cover_placement(scope, expected=expected)
        except ValueError as exc:
            raise _ExplicitFail("peer_unavailable", str(exc)[:200])
        artifact["scope"] = scope
        # C 生成就绪证明（仅 rag.answer.cited 需要）。
        generation_node = self._generation_node()
        if self.args.operation == "rag.answer.cited":
            self._require_generation_ready(artifact, ledger, generation_node)
        artifact["generation_node"] = generation_node
        self._federated_task(case, artifact, ledger, scope)

    def _placement(self) -> dict:
        raw = json.loads(self.args.nodes.read_text(encoding="utf-8"))
        return dict(raw.get("placement") or {})

    def _generation_node(self) -> str | None:
        raw = json.loads(self.args.nodes.read_text(encoding="utf-8"))
        generation = raw.get("generation") or {}
        node = generation.get("node")
        if node is not None and node not in self.nodes:
            raise _ExplicitFail("peer_unavailable",
                                f"generation node {node!r} not in nodes")
        return node

    # ---------------------------------------------------------- 真实入库

    def _timed(self, node: _Node, method: str, url: str, *, artifact, ledger,
               **kwargs):
        start = now_seconds()
        try:
            response = node.http.request(method, url, **kwargs)
            status = response.status_code
            req_n = len(response.request.content or b"")
            resp_n = len(response.content or b"")
        except Exception:
            artifact["exchanges"].append(_redacted_exchange(
                method=method, url=f"{node.alias}{url}", status=None,
                latency_s=now_seconds() - start))
            raise
        elapsed = now_seconds() - start
        ledger.add_exchange(request_bytes=req_n, response_bytes=resp_n)
        ledger.phase(f"{node.alias} {method} {url.split('?')[0]}", elapsed)
        artifact["exchanges"].append(_redacted_exchange(
            method=method, url=f"{node.alias}{url}", status=status,
            request_bytes=req_n, response_bytes=resp_n, latency_s=elapsed))
        return response

    def _blob_put(self, alias: str, url: str, content: bytes, *, artifact,
                  ledger: CostLedger, part_no: int):
        """分片字节直传对象存储：独立无凭据 client。

        预签名 URL 自带查询串凭证；复用带 `Authorization: Bearer <用户 JWT>`
        默认头的 control client 会把用户 JWT 送给 MinIO（凭据跨域外泄）。
        这里每次新建无任何默认 Auth 头的 client，禁止跟随重定向，
        用完即关。URL 本体永不进产物（只记别名+分片号+字节）。"""
        import httpx as _httpx

        start = now_seconds()
        client = _httpx.Client(trust_env=False, follow_redirects=False,
                               timeout=REQUEST_TIMEOUT)
        try:
            response = client.put(url, content=content)
        finally:
            client.close()
        elapsed = now_seconds() - start
        ledger.add_exchange(request_bytes=len(content),
                            response_bytes=len(response.content or b""))
        ledger.phase(f"{alias} PUT blob-part", elapsed)
        artifact["exchanges"].append(_redacted_exchange(
            method="PUT", url=f"{alias} blob-part-{part_no}",
            status=response.status_code,
            request_bytes=len(content),
            response_bytes=len(response.content or b""),
            latency_s=elapsed))
        return response

    def _ingest(self, alias: str, source: dict, artifact: dict,
                ledger: CostLedger) -> dict:
        node = self.nodes[alias]
        pdf_dir = Path(self.args.pdf_dir)
        path = pdf_dir / source["path"]
        if not path.exists():
            raise _ExplicitFail("peer_unavailable",
                                f"source pdf not staged at pdf-dir: {source['path']}")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != source["sha256"].lower():
            raise _ExplicitFail("peer_unavailable",
                                f"source sha256 mismatch for {source['id']}")
        created = self._timed(node, "POST", "/api/uploads", artifact=artifact,
                              ledger=ledger,
                              json={"filename": path.name, "size": len(content),
                                    "mime": "application/pdf", "sha256": digest})
        if created.status_code != 201:
            raise _ExplicitFail("peer_unavailable",
                                f"create upload on {alias}: {created.status_code}")
        session = created.json()
        parts = session.get("parts") or []
        if not parts:
            raise _ExplicitFail("peer_unavailable",
                                f"upload on {alias} returned no parts")
        part_size = session["part_size"]
        uploaded = []
        for part in parts:
            start = (part["part_number"] - 1) * part_size
            chunk = content[start:start + part_size]
            put = self._blob_put(alias, part["url"], chunk, artifact=artifact,
                                 ledger=ledger, part_no=part["part_number"])
            if put.status_code not in (200, 204):
                raise _ExplicitFail("peer_unavailable",
                                    f"part upload on {alias}: {put.status_code}")
            etag = put.headers.get("ETag")
            if etag:
                uploaded.append({"part_number": part["part_number"], "etag": etag})
        finalized = self._timed(
            node, "POST", f"/api/uploads/{session['id']}/finalize",
            artifact=artifact, ledger=ledger,
            headers={"Idempotency-Key": session["id"]},
            json={"parts": uploaded or None})
        if finalized.status_code != 202:
            raise _ExplicitFail("peer_unavailable",
                                f"finalize on {alias}: {finalized.status_code}")
        state = self._poll_upload(node, session["id"], artifact, ledger)
        if state.get("status") != "ready" or state.get("verified_sha256") != digest:
            raise _ExplicitFail("peer_unavailable",
                                f"server digest not verified on {alias}: "
                                f"{state.get('status')}")
        receipt = self._poll_upload_receipt(node, session["id"], artifact, ledger)
        version = self._require_version(node, receipt, digest, artifact, ledger)
        document = self._poll_document(node, version, artifact, ledger)
        collection = self._publish_collection(node, version, artifact, ledger,
                                              source=source)
        record = {
            "source": source["id"], "node_alias": alias,
            "origin_node_id": node.node_id,
            "document_id": document["id"],
            "resource_id": version["resource_id"],
            "source_version_id": version["id"],
            "parse_revision": version["parse_job_id"],
            "collection_id": collection["collection_id"],
            "index_revision": collection.get("index_revision"),
            "licence": self._collection_licence(source),
            "verified_sha256": digest,
            "engine": document.get("engine") or version.get("engine"),
        }
        artifact.setdefault("ingested", []).append(record)
        return record

    def _poll_upload(self, node: _Node, upload_id: str, artifact, ledger) -> dict:
        deadline = time.time() + 180.0
        while time.time() < deadline:
            settled = self._timed(node, "GET", f"/api/uploads/{upload_id}",
                                  artifact=artifact, ledger=ledger)
            if settled.status_code != 200:
                raise _ExplicitFail("peer_unavailable",
                                    f"upload status on {node.alias}: {settled.status_code}")
            state = settled.json()
            if state.get("status") == "ready":
                return state
            if state.get("status") not in ("created", "uploading", "verifying"):
                raise _ExplicitFail("resource_index_unavailable",
                                    f"upload on {node.alias} ended as {state.get('status')}")
            time.sleep(2.0)
        raise _ExplicitFail("peer_unavailable",
                            f"digest verification timed out on {node.alias}")

    def _poll_upload_receipt(self, node: _Node, upload_id: str, artifact, ledger) -> dict:
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            response = self._timed(
                node, "GET", f"/api/v1/client/receipts/{upload_id}",
                artifact=artifact, ledger=ledger,
            )
            if response.status_code == 200:
                receipt = response.json()
                if (receipt.get("operation_key") != upload_id
                        or receipt.get("operation") != "document.upload"
                        or receipt.get("accepted") is not True):
                    raise _ExplicitFail("attribution_mismatch", "upload receipt does not match")
                return receipt
            if response.status_code != 404:
                raise _ExplicitFail("peer_unavailable",
                                    f"upload receipt on {node.alias}: {response.status_code}")
            time.sleep(2.0)
        raise _ExplicitFail("resource_index_unavailable",
                            f"upload receipt did not arrive on {node.alias}")

    def _require_version(self, node: _Node, receipt: dict, digest: str,
                         artifact, ledger) -> dict:
        response = self._timed(
            node, "GET", f"/api/resources/{receipt['resource_id']}/versions",
            artifact=artifact, ledger=ledger,
        )
        if response.status_code != 200:
            raise _ExplicitFail("peer_unavailable",
                                f"uploaded versions on {node.alias}: {response.status_code}")
        version = next((item for item in response.json()
                        if item["id"] == receipt["version_id"]), None)
        if (version is None or version.get("resource_id") != receipt["resource_id"]
                or version.get("parse_job_id") != receipt["parse_revision"]
                or version.get("source_digest_verified") is not True
                or version.get("source_digest") != digest):
            raise _ExplicitFail("attribution_mismatch",
                                f"uploaded version binding changed on {node.alias}")
        return version

    def _poll_document(self, node: _Node, version: dict, artifact, ledger) -> dict:
        deadline = time.monotonic() + 300.0
        context = {"resource_id": version["resource_id"], "version_id": version["id"]}
        path = f"/api/documents/{version['document_id']}"
        while time.monotonic() < deadline:
            response = self._timed(node, "GET", path, artifact=artifact,
                                   ledger=ledger, params=context)
            if response.status_code != 200:
                raise _ExplicitFail("peer_unavailable",
                                    f"parse status on {node.alias}: {response.status_code}")
            document = response.json()
            if (document.get("id") != version["document_id"]
                    or document.get("resource_id") != version["resource_id"]
                    or document.get("source_version_id") != version["id"]
                    or document.get("current_job_id") != version["parse_job_id"]):
                raise _ExplicitFail("attribution_mismatch",
                                    f"parse binding changed on {node.alias}")
            if document.get("status") == "failed" or document.get("index_status") == "failed":
                raise _ExplicitFail(
                    "resource_index_unavailable",
                    f"parse/index failed on {node.alias}: "
                    f"{document.get('error') or document.get('index_error')}"[:200],
                )
            if document.get("status") == "succeeded" and document.get("index_status") == "ready":
                jobs = self._timed(node, "GET", path + "/jobs", artifact=artifact,
                                   ledger=ledger, params=context)
                if jobs.status_code != 200:
                    raise _ExplicitFail("peer_unavailable",
                                        f"parse history on {node.alias}: {jobs.status_code}")
                job = next((item for item in jobs.json()
                            if item["id"] == version["parse_job_id"]), None)
                if job is None or job.get("status") != "succeeded":
                    raise _ExplicitFail("attribution_mismatch",
                                        f"accepted parse revision missing on {node.alias}")
                document["engine"] = job["engine"]
                return document
            time.sleep(3.0)
        raise _ExplicitFail("resource_index_unavailable",
                            f"parse/index timed out on {node.alias}")

    @staticmethod
    def _collection_licence(source: dict) -> str:
        """集合 licence 只取 source 输入的真实声明，不伪造分发许可。

        source-cases 的 `license_notice`（如 pico 的 CC BY-ND 4.0）是唯一
        可写入集合的文本；没有该字段（esp32/attention）时填
        `private-eval-only; no redistribution`，明确仅私有评测、不授外部
        再分发。驱动永不对下载的厂商手册编造开放许可。"""
        notice = (source or {}).get("license_notice")
        if isinstance(notice, str) and notice.strip():
            return notice.strip()[:512]
        return "private-eval-only; no redistribution"

    def _publish_collection(self, node: _Node, version: dict,
                            artifact, ledger, *, source: dict) -> dict:
        resource = self._timed(
            node, "PATCH", f"/api/resources/{version['resource_id']}",
            artifact=artifact, ledger=ledger, json={"publication": "published"},
        )
        if resource.status_code != 200:
            raise _ExplicitFail("resource_index_unavailable",
                                f"resource publication on {node.alias}: {resource.status_code}")
        key = f"real-{self.run_id}-{version['id']}"[:120]
        licence = self._collection_licence(source)
        created = self._timed(
            node, "POST", "/api/v1/collections", artifact=artifact, ledger=ledger,
            headers={"Idempotency-Key": key},
            json={"name": f"real-eval {version['id'][:8]}", "licence": licence,
                  "languages": ["en", "zh"], "topics": ["evaluation", "manual"],
                  "version_ids": [version["id"]]})
        if created.status_code != 201:
            raise _ExplicitFail("peer_unavailable",
                                f"create collection on {node.alias}: "
                                f"{created.status_code}")
        body = created.json()
        published = self._timed(
            node, "POST",
            f"/api/v1/collections/{body['collection_id']}/publish",
            artifact=artifact, ledger=ledger,
            headers={"Idempotency-Key": key + "-publish"},
            json={"expected_revision": body["revision"]})
        if published.status_code == 409:
            raise _ExplicitFail("resource_index_unavailable",
                                f"collection not publishable on {node.alias} "
                                f"(index not ready?): {published.text[:160]}")
        if published.status_code != 200:
            raise _ExplicitFail("peer_unavailable",
                                f"publish collection on {node.alias}: "
                                f"{published.status_code}")
        return published.json()

    # ---------------------------------------------------------- scope 与能力

    def _create_scope(self, artifact, ledger) -> dict:
        entry = self.nodes[self.entry_alias]
        response = self._timed(
            entry, "POST", "/api/v1/federation/scopes", artifact=artifact,
            ledger=ledger, json={"operation": "corpus.retrieve",
                                 "allowed_node_ids": sorted(node.node_id for node in self.nodes.values())})
        if response.status_code != 201:
            raise _ExplicitFail("peer_unavailable",
                                f"create scope on {entry.alias}: "
                                f"{response.status_code} {response.text[:160]}")
        return response.json()

    def _require_generation_ready(self, artifact, ledger,
                                  generation_node: str | None) -> None:
        """向生成节点 C 自身的 control 观测真实能力。

        真实形状（`discovery_handlers.go:116`）：每个 control 只报告**本节点**
        的能力，外层 `identity.authority_node_id` 即被观测节点的权威 id，
        `profiles` 里的单条 CapabilityProfile 本来就没有 node_id。
        因此向 C 自身 `GET /api/v1/capabilities`，校验外层 authority 等于已核实
        的 C 身份，再按 `capability_status/readiness/valid_until` 判定；
        永不要求 C 出现在 entry 的全域档案里。"""
        if generation_node is None:
            raise _ExplicitFail("local_model_missing",
                                "node config generation.node is required for "
                                "rag.answer.cited")
        node = self.nodes[generation_node]
        response = self._timed(node, "GET", "/api/v1/capabilities",
                               artifact=artifact, ledger=ledger)
        if response.status_code != 200:
            raise _ExplicitFail("local_model_missing",
                                f"capabilities unreadable on {generation_node}: "
                                f"{response.status_code}")
        try:
            ready = generation_capability_envelope(
                response.json(), node.node_id)
        except ValueError as exc:
            raise _ExplicitFail("local_model_missing", str(exc)[:200])
        if not ready:
            raise _ExplicitFail("local_model_missing",
                                f"no ready rag.answer.cited profile on "
                                f"{generation_node}")
        artifact["generation_profiles"] = [
            {"node": generation_node, "node_id": node.node_id,
             "operation": p.get("operation"), "readiness": p.get("readiness"),
             "accepting_admissions": p.get("accepting_admissions")}
            for p in ready]

    # ---------------------------------------------------------- 联邦任务

    def _federated_task(self, case: dict, artifact: dict, ledger: CostLedger,
                        scope: dict) -> None:
        entry = self.nodes[self.entry_alias]
        manifest = scope.get("manifest") or scope
        scope_id = manifest.get("scope_id", "")
        # 探索许可的接收方 = 配置批准的全部真实 node_id（含生成 C 与目录 P/R），
        # 不从“有没有来源集合”推断：C/P/R 本来就没有来源集合，但它们的
        # probe/answer 探测与目录展开同样要经过许可门。缺一不可。
        recipients = sorted({node.node_id for node in self.nodes.values()})
        keys = intent_keys(self.run_id, case["id"],
                           self.args.operation, self.args.mode)
        consent_id = f"consent-{keys['intent']}"
        granted_at = datetime.now(timezone.utc)
        # fast 与 exhaustive 均使用本次已验证的 federation_public scope +
        # manifest：fast 只查 A 会把全部 B 样本判成“本地无证据”，是驱动在
        # 撒谎而不是路由在漏。覆盖差异只来自 search_policy.mode（fast 的
        # 候选上限 vs 穷查全量）；穷查另要求 manifest sealed（见下）。
        if manifest.get("enumeration_state") != "sealed" \
                and self.args.mode == "exhaustive_scope":
            raise _ExplicitFail("peer_unavailable",
                                "exhaustive_scope requires a sealed scope manifest")
        task_spec: dict = {
            "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
            "operation": self.args.operation,
            "workspace_ref": "workspace-real-eval",
            "query": case["question"],
            "resource_scope": {"kind": "federation_public",
                               "scope_ref": scope_id},
            "search_policy": {"mode": self.args.mode, "ordering": "local_first"},
            "execution_policy": {"mode": "trusted_federation",
                                 "coordinator_ref": entry.node_id},
            "consent_refs": {"exploration": consent_id, "execution": None},
            "budget_ref": "budget-real-eval",
        }
        consent = {
            "schema": "ddp-task-probe/1#ExplorationConsent",
            "consent_id": consent_id,
            "granted_by": entry.subject, "granted_at": granted_at.isoformat(),
            "valid_until": (granted_at + timedelta(minutes=30)).isoformat(),
            "egress_mode": "listed_nodes",
            "allowed_payload": ["query_text", "collection_filters"],
            "allowed_recipients": recipients,
            "budget": {"max_probe_requests": 32, "max_egress_bytes": 1 << 20},
        }
        body: dict = {"task_spec": task_spec, "exploration_consent": consent,
                      "scope_manifest": manifest}
        intent = self._timed(entry, "POST", "/api/v1/task-intents",
                             artifact=artifact, ledger=ledger,
                             headers={"Idempotency-Key": keys["intent"]}, json=body)
        if intent.status_code == 403:
            raise _ExplicitFail("peer_unavailable",
                                f"exploration denied: {intent.text[:160]}")
        if intent.status_code != 201:
            raise _ExplicitFail("peer_unavailable",
                                f"create intent: {intent.status_code} "
                                f"{intent.text[:160]}")
        root = intent.json()["root_task_id"]
        plan_resp = self._timed(entry, "POST", "/api/v1/task-plans",
                                artifact=artifact, ledger=ledger,
                                json={"root_task_id": root})
        if plan_resp.status_code not in (200, 201, 202):
            raise _ExplicitFail("peer_unavailable",
                                f"plan: {plan_resp.status_code}")
        plan = plan_resp.json()
        plan_digest = plan.get("plan_digest")
        if not plan_digest:
            raise _ExplicitFail("peer_unavailable", "plan has no digest")
        generation_cap = (plan.get("budget") or {}).get("max_generation_tokens")
        if type(generation_cap) is not int or generation_cap < 0:
            raise _ExplicitFail("peer_unavailable", "plan has no valid generation budget")
        ledger.generation_tokens_cap = generation_cap
        if generation_cap > self.args.max_generation_tokens:
            raise _ExplicitFail("budget_exceeded", "plan exceeds the configured generation-token ceiling")
        artifact["plan"] = {
            "plan_digest": plan_digest,
            "budget": plan.get("budget"),
            "answer_executor": next(
                (s.get("executor_node_id") for s in plan.get("steps", [])
                 if s.get("operation") == "answer"), None),
            "steps": [{"step_id": s.get("step_id"),
                       "operation": s.get("operation"),
                       "executor_node_id": s.get("executor_node_id")}
                      for s in plan.get("steps", [])],
        }
        edges = [e.get("edge_id") for e in (plan.get("data_edges") or [])
                 if e.get("edge_id")]
        execution_consent = {
            "schema": "ddp-plan-admission/1#ExecutionConsent",
            "consent_id": keys["exec"][:64],
            "plan_digest": plan_digest, "granted_by": entry.subject,
            "granted_at": datetime.now(timezone.utc).isoformat(),
            "valid_until": plan["valid_until"],
            "allowed_recipients": recipients, "allowed_edges": edges,
            "output_locations": ["local:workspace-real-eval"],
            "retention": "temporary",
        }
        approved = self._timed(
            entry, "POST", f"/api/v1/task-plans/{root}/approve",
            artifact=artifact, ledger=ledger,
            json={"plan_digest": plan_digest,
                  "execution_consent": execution_consent})
        if approved.status_code not in (200, 201):
            raise _ExplicitFail("peer_unavailable",
                                f"approve: {approved.status_code} "
                                f"{approved.text[:160]}")
        submitted = self._timed(
            entry, "POST", "/api/v1/tasks", artifact=artifact, ledger=ledger,
            headers={"Idempotency-Key": keys["exec"]},
            json={"root_task_id": root, "plan_digest": plan_digest})
        if submitted.status_code not in (200, 202):
            raise _ExplicitFail("peer_unavailable",
                                f"submit: {submitted.status_code} "
                                f"{submitted.text[:160]}")
        status = self._poll_task(entry, root, artifact, ledger)
        result = status.get("result") or {}
        evidence = result.get("evidence") or []
        try:
            check_evidence_attribution(evidence, scope=manifest)
        except ValueError as exc:
            raise _ExplicitFail("attribution_mismatch", str(exc)[:200])
        artifact["evidence"] = evidence
        coverage = self._timed(entry, "GET", f"/api/v1/tasks/{root}/coverage",
                               artifact=artifact, ledger=ledger)
        if coverage.status_code != 200:
            raise _ExplicitFail("peer_unavailable",
                                f"coverage unreadable on {entry.alias}: "
                                f"{coverage.status_code}")
        coverage_body = coverage.json()
        try:
            check_coverage_entries_cover_scope(coverage_body, manifest)
        except ValueError as exc:
            raise _ExplicitFail("peer_unavailable", str(exc)[:200])
        artifact["coverage"] = coverage_body
        answer = result.get("answer")
        bindings = result.get("claim_evidence_bindings") or []
        if self.args.operation == "rag.answer.cited":
            if answer:
                artifact["generated_claims"] = [
                    {"claim_id": b.get("claim_id"),
                     "claim_text": b.get("claim_text"),
                     "evidence_refs": b.get("evidence_refs"),
                     "structural_validation": b.get("structural_validation"),
                     "semantic_review": "needs_review"}
                    for b in bindings] or [
                    {"claim_id": "claim-raw", "claim_text": answer,
                     "evidence_refs": [], "structural_validation": "unparsed",
                     "semantic_review": "needs_review"}]
                artifact["answer"] = answer
                artifact["answer_reason"] = result.get("answer_reason")
                artifact["provider"] = result.get("provider")
                artifact["disclosure"] = result.get("disclosure")
                artifact["token_cap"] = (plan.get("budget") or {}).get(
                    "max_generation_tokens")
                artifact["tokens_actual"] = None
            else:
                reason = result.get("answer_reason") or "insufficient_evidence"
                raise _ExplicitFail(
                    reason if isinstance(reason, str) and reason in
                    ("local_model_missing", "insufficient_evidence",
                     "upstream_error", "no_model_output", "budget_exceeded",
                     "unsupported_generation",
                     "evidence_excerpt_unavailable") else "insufficient_evidence",
                    f"no answer: {reason}"[:200])

    def _poll_task(self, entry: _Node, root: str, artifact, ledger,
                   *, timeout_s: float = 300.0) -> dict:
        """只返回成功终态；失败、取消、HTTP 错误与未知状态均显式失败。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            response = self._timed(entry, "GET", f"/api/v1/tasks/{root}",
                                   artifact=artifact, ledger=ledger)
            if response.status_code == 404:
                raise _ExplicitFail("peer_unavailable",
                                    f"task {root[:8]}… not found on {entry.alias}")
            if response.status_code != 200:
                raise _ExplicitFail("peer_unavailable",
                                    f"task poll on {entry.alias}: "
                                    f"{response.status_code}")
            status = response.json()
            state = status.get("status")
            if state in ("succeeded", "failed", "cancelled"):
                artifact["task_status"] = status
                if state != "succeeded":
                    raise _ExplicitFail(
                        f"task_{state}", f"task {root} ended as {state}"
                    )
                return status
            if state not in ("running", "queued"):
                raise _ExplicitFail("peer_unavailable",
                                    f"task on {entry.alias} in unknown state "
                                    f"{state!r}"[:200])
            time.sleep(3.0)
        raise _ExplicitFail("peer_unavailable", "task poll timed out")


if __name__ == "__main__":
    raise SystemExit(main())
