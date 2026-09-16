"""P5 协调者（B1）：意图 → 规划 → 审批 → 执行 → 覆盖 → 交付 的端到端与负向。

本地路径用真实的发布集合与真实检索（与 probe/admission 测试同一套夹具）；
远端路径用注入 `MockTransport` 的 stub peer，断言的是**出站边界与来源身份**，
不是对端实现。所有"不许发请求"的断言都直接数 stub 收到的请求 —— 静默外发
在这类测试里最容易被放过。
"""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import ACTOR, ORG, actor_headers, drain_tasks
from ddp_corpus import federation, federation_tasks
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.main import app as corpus_app
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationAdmission,
    FederationDelivery,
    FederationExecution,
    FederationProbe,
    FederationRequest,
    FederationTaskEvent,
)
from ddp_corpus.federation_peers import PeerDirectory, parse_peers
from ddp_corpus.models import as_aware, new_id, utcnow
from ddp_core.application import plans
from test_federation_probes import (
    BASE,
    NODE,
    configure_federation,
    headers,
    indexed_source,
    publish_collection,
)

PEER_NODE = "node-" + "b" * 48
EXPIRY = "2030-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ 夹具构造

def exploration(*, egress="listed_nodes", recipients=(PEER_NODE,), payload=("query_text",),
                consent_id="explore-1", valid_until=EXPIRY, budget=None):
    return {
        "schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": consent_id,
        "granted_by": "user-1", "granted_at": "2026-01-01T00:00:00Z",
        "valid_until": valid_until, "egress_mode": egress,
        "allowed_payload": list(payload), "allowed_recipients": list(recipients),
        "budget": budget or {"max_probe_requests": 8, "max_egress_bytes": 1 << 20},
    }


def task_spec(*, scope="site_public", mode="fast", query="retrieval target",
              exploration_ref="explore-1", execution_ref=None, resource_refs=None,
              execution_mode="trusted_federation", scope_ref=None,
              operation="rag.answer.cited"):
    scope_body: dict = {"kind": scope}
    if scope_ref is not None:
        scope_body["scope_ref"] = scope_ref
    if resource_refs is not None:
        scope_body["resource_refs"] = list(resource_refs)
    return {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": operation, "workspace_ref": "workspace-a", "query": query,
        "resource_scope": scope_body,
        "search_policy": {"mode": mode, "ordering": "local_first"},
        "execution_policy": {"mode": execution_mode, "coordinator_ref": NODE},
        "consent_refs": {"exploration": exploration_ref, "execution": execution_ref},
        "budget_ref": "budget-1",
    }


def execution_consent(plan_digest, *, recipients=(NODE,), edges=(), retention="temporary",
                      consent_id="execute-1", valid_until=EXPIRY):
    return {
        "schema": "ddp-plan-admission/1#ExecutionConsent", "consent_id": consent_id,
        "plan_digest": plan_digest, "granted_by": "user-1",
        "granted_at": "2026-01-01T00:00:00Z", "valid_until": valid_until,
        "allowed_recipients": list(recipients), "allowed_edges": list(edges),
        "output_locations": ["local:workspace-a"], "retention": retention,
    }


def scope_manifest(members, *, enumeration="sealed", unexpanded=(), valid_until=EXPIRY,
                   revisions=None):
    vector = ([{"node_id": NODE, "registry_revision": 1,
                "fetched_at": "2026-01-01T00:00:00Z"}] if revisions is None else
              [{"node_id": node, "registry_revision": revision,
                "fetched_at": "2026-01-01T00:00:00Z"} for node, revision in revisions])
    body = {
        "schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": "scope-1",
        "caller_scope_hash": "sha256:" + "1" * 64,
        "created_at": "2026-01-01T00:00:00Z", "valid_until": valid_until,
        "registry_revision_vector": vector,
        "expanded_members": list(members), "unexpanded_subtrees": list(unexpanded),
        "enumeration_state": enumeration,
    }
    body["manifest_digest"] = plans.digest(
        {key: value for key, value in body.items() if key != "manifest_digest"})
    return body


def member(collection_id, node=NODE, operation="corpus.retrieve"):
    return {"origin_node_id": node, "collection_id": collection_id, "operation": operation}


# ------------------------------------------------------------------ stub peer

def peer_evidence(*, evidence_id="peer-evidence-1", resource_id="peer-resource-1"):
    return {
        "schema": "ddp-evidence/1#FederatedEvidence", "evidence_id": evidence_id,
        "origin_node_id": PEER_NODE, "authority_node_id": PEER_NODE,
        "resource_id": resource_id, "source_version_id": "peer-version-1",
        "parse_revision": "peer-parse-1", "source_digest": "sha256:" + "a" * 64,
        "excerpt_digest": "sha256:" + "b" * 64,
        "locator": {"kind": "page_block", "physical_page_index": 0, "seq": 1,
                    "bbox": [1, 2, 3, 4], "page_size": {"width": 100, "height": 200}},
        "source_type": "source", "derived_from": None, "uploader_ref": None,
        "retrieval_receipt_ref": "federation-probe:probe-remote-1",
        "policy_revision": "peer:1", "block_type": "text",
    }


def peer_probe(*, probe_id="probe-remote-1", collection="peer-collection-1",
               index_revision="peer-index-1"):
    return {
        "schema": "ddp-probe/1", "probe_id": probe_id, "target_node_id": PEER_NODE,
        "task_spec_digest": "sha256:" + "c" * 64, "consent_ref": "explore-1",
        "probe_kind": "evidence_retrieval",
        "capability_check": {"operation": "corpus.retrieve", "readiness": "ready",
                             "input_validation": "content_verified"},
        "retrieval": {"status": "succeeded", "collection_ref": collection,
                      "index_revision": index_revision, "candidate_limit": 8,
                      "continuation_ref": None,
                      "evidence_set_ref": f"federation-probe:{probe_id}",
                      "internal_limits": []},
        "can_generate": False, "missing_requirements": [], "offer": None,
        "observed_at": now_iso(),
    }


def peer_descriptor(collection_id, *, origin=PEER_NODE, index_revision="peer-index-1",
                    topics=(), languages=(), revision=1, time_range=None):
    """对等目录读的一页描述符（与 `catalog.descriptor` 同形状）。"""
    body = {
        "schema": "ddp-discovery/1#CollectionDescriptor", "origin_node_id": origin,
        "collection_id": collection_id, "index_revision": index_revision,
        "revision": revision, "valid_until": EXPIRY,
    }
    if topics:
        body["topics"] = list(topics)
    if languages:
        body["languages"] = list(languages)
    if time_range is not None:
        body["time_range"] = time_range
    return body


def peer_capability_probe(*, operation="corpus.retrieve", readiness="ready",
                          can_generate=False, probe_id="probe-cap-1"):
    """`probe_kind=capability_input` 的真实形状（retrieval 必须为 null）。"""
    return {
        "schema": "ddp-probe/1", "probe_id": probe_id, "target_node_id": PEER_NODE,
        "task_spec_digest": "sha256:" + "c" * 64, "consent_ref": "explore-1",
        "probe_kind": "capability_input",
        "capability_check": {"operation": operation, "readiness": readiness,
                             "input_validation": "metadata_only"},
        "retrieval": None, "can_generate": can_generate, "missing_requirements": [],
        "offer": None, "observed_at": now_iso(),
    }


class StubPeer:
    """一个可注入的远端：只实现协调者用到的端点，记录每一次出站请求。"""

    def __init__(self, *, fail="", fail_admit_step=None, items=None,
                 foreign_admit_field="", can_generate=False, answer_document=None,
                 fail_execution_status=False, collections=None, index_revision="peer-index-1"):
        # (method, path, idempotency-key)：键让测试能区分证据探测（plan:）与
        # 生成能力探测（answer-probe:），而不是只能数次数。
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail = fail
        self.fail_admit_step = fail_admit_step
        self.items = items if items is not None else [peer_evidence()]
        #: 让受理响应带一个"绑定对不上"的字段（外来 root/step/计划修订）。
        self.foreign_admit_field = foreign_admit_field
        #: 能力探测回执是否自称能生成（默认 false：诚实的大多数）。
        self.can_generate = can_generate
        #: answer 执行的返回文档（None = 对端没有答案执行结果）。
        self.answer_document = answer_document
        #: 轮询执行状态时是否回 500（故障对端）。
        self.fail_execution_status = fail_execution_status
        #: executor_task_id -> step_id：真实执行者每个受理一步，轮询时必须能
        #: 分辨取数执行与答案执行（否则 stub 会把 answer 文档回给取数轮询）。
        self.executions: dict[str, str] = {}
        #: 收到的受理请求体：证明数据边的载荷（evidence_excerpts）真的发到了。
        self.admissions: list[dict] = []
        #: 自发布集合描述符（None = 一个都不发布，终止页为空）。
        self.collections: list[dict] = list(collections or [])
        #: 探测回执回给哪一版索引修订（复用有效性测试会改它）。
        self.index_revision = index_revision
        #: 探测回执是否回 403（负面缓存 denied 分支）。
        self.deny_probes = False
        #: 答案步骤的受理回执状态（None = accepted）。非 accepted 时按契约去掉
        #: executor_task_id / 已校验摘要，形状与真实执行者的 waiting_input 回执一致。
        self.answer_receipt_state: str | None = None
        #: 答案执行轮询时覆盖的字段（例如 {"state": "running"} 模拟一直不结束）。
        self.answer_execution: dict | None = None
        #: 业务键 -> (请求体, 回执)。真实执行者（`federation.admit`）同键同体复用
        #: 回执、同键异体 409（delegation_generation 在请求摘要里），lookup 找得到
        #: 已受理的回执。旧 stub 每次都发新回执、lookup 永远 404，于是"resume 按新
        #: 代次重发同一个业务键"在远端路径上从来不会红（F-34 #11）。
        self.accepted: dict[str, tuple[dict, dict]] = {}

    def catalog_page(self, request: httpx.Request) -> dict:
        """对等目录读的一页：与 `catalog.snapshot_page` 同分页协议。

        生产端的每一页后面都跟一个**空的终止页**（`complete=true` 是完整性的
        证明），所以即使一页装得下也要第二次请求 —— stub 必须照这个形状来，
        否则"预算只够一页"这类用例量不到真实的请求次数。
        """
        params = request.url.params
        limit = int(params.get("limit") or 100)
        cursor = params.get("cursor") or ""
        start = int(cursor[1:]) if cursor.startswith("c") else 0
        if not self.collections or start >= len(self.collections):
            return {
                "snapshot_id": "peer-snapshot-1", "scope_id": "peer-directory",
                "caller_scope_hash": "sha256:" + "e" * 64, "origin_node_id": PEER_NODE,
                "registry_revision": 1, "first_cursor": "c0",
                "total": len(self.collections), "collections": [],
                "next_cursor": None, "complete": True,
                "content_snapshot_complete": False,
            }
        page = self.collections[start:start + limit]
        return {
            "snapshot_id": "peer-snapshot-1", "scope_id": "peer-directory",
            "caller_scope_hash": "sha256:" + "e" * 64, "origin_node_id": PEER_NODE,
            "registry_revision": 1, "first_cursor": "c0",
            "total": len(self.collections), "collections": page,
            "next_cursor": f"c{start + len(page)}", "complete": False,
            "content_snapshot_complete": False,
        }

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, request.url.path,
                               request.headers.get("Idempotency-Key")))
            if self.fail == "all":
                raise httpx.ConnectError("refused", request=request)
            path = request.url.path
            if path.endswith("/published-collections"):
                if self.fail == "catalog":
                    raise httpx.ConnectError("refused", request=request)
                return httpx.Response(200, json=self.catalog_page(request))
            if path.endswith("/probes"):
                if self.fail == "probe":
                    raise httpx.ConnectError("refused", request=request)
                body = json.loads(request.content or b"{}")
                if body.get("probe_kind") == "capability_input":
                    operation = str(body.get("operation") or "corpus.retrieve")
                    ready = self.can_generate and operation == "rag.answer.cited"
                    return httpx.Response(201, json=peer_capability_probe(
                        operation=operation, readiness="ready" if ready else "unknown",
                        can_generate=ready))
                if self.deny_probes:
                    return httpx.Response(403, json={"error": {
                        "code": "egress_denied", "message": "denied"}})
                return httpx.Response(201, json=peer_probe(
                    collection=str(body.get("collection_id") or "peer-collection-1"),
                    index_revision=self.index_revision))
            if path.endswith("/admissions"):
                body = json.loads(request.content or b"{}")
                self.admissions.append(body)
                if self.fail == "admit" or (
                        self.fail_admit_step and body.get("step_id") == self.fail_admit_step):
                    raise httpx.ConnectError("refused", request=request)
                previous = self.accepted.get(body["idempotency_key"])
                if previous is not None:
                    if previous[0] != body:
                        return httpx.Response(409, json={"error": {
                            "code": "idempotency_conflict",
                            "message": "same idempotency key with a different request"}})
                    return httpx.Response(200, json=previous[1])
                receipt = bound_peer_receipt(
                    len(self.calls), body=body, key=body["idempotency_key"])
                executor_task_id = f"remote-exec-{len(self.calls)}"
                receipt["executor_task_id"] = executor_task_id
                self.executions[executor_task_id] = str(body.get("step_id") or "")
                if self.answer_receipt_state and body.get("step_id") == "answer-1":
                    receipt["state"] = self.answer_receipt_state
                    receipt["input_validation"] = "metadata_only"
                    for field in ("executor_task_id", "verified_input_manifest_digest",
                                  "accepted_at"):
                        receipt.pop(field, None)
                if self.foreign_admit_field:
                    receipt[self.foreign_admit_field] = "foreign-value"
                self.accepted[body["idempotency_key"]] = (body, receipt)
                return httpx.Response(201, json=receipt)
            if "/evidence-sets/" in path:
                return httpx.Response(200, json={
                    "schema": "ddp-evidence/1#EvidenceSet",
                    "set_ref": path.rsplit("/", 1)[1], "items": self.items, "complete": True})
            if path.endswith("/admissions/lookup"):
                key = json.loads(request.content or b"{}").get("idempotency_key")
                if key in self.accepted:
                    return httpx.Response(200, json=self.accepted[key][1])
                return httpx.Response(404, json={"error": {"code": "admission_not_found"}})
            if "/tasks/" in path:
                executor_task_id = path.rsplit("/", 1)[1]
                step_id = self.executions.get(executor_task_id, "retrieve-1")
                is_answer = step_id == "answer-1"
                if self.fail_execution_status and is_answer:
                    # 故障对端：取数正常，答案执行轮询回 500。
                    return httpx.Response(500, json={"error": {"code": "peer_exploded"}})
                status = {
                    **peer_status(), "executor_task_id": executor_task_id,
                    "step_id": step_id,
                    "operation": "answer" if is_answer else "retrieve",
                    "evidence_set_ref": (None if is_answer
                                         else f"federation-execution:{executor_task_id}"),
                }
                if is_answer and self.answer_document is not None:
                    status["answer"] = self.answer_document
                if is_answer and self.answer_execution:
                    status.update(self.answer_execution)
                return httpx.Response(200, json=status)
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        return httpx.MockTransport(handler)


def peer_receipt(sequence: int) -> dict:
    digest = "sha256:" + "d" * 64
    return {
        "schema": "ddp-plan-admission/1#AdmissionReceipt",
        "admission_id": f"remote-adm-{sequence}",
        "executor_task_id": f"remote-exec-{sequence}", "issuer_node_id": NODE,
        "executor_node_id": PEER_NODE, "root_task_id": "root-1", "step_id": "retrieve-1",
        "delegation_generation": 0, "idempotency_key": f"remote-key-{sequence}",
        "request_digest": digest, "plan_digest": digest, "state": "accepted",
        "input_validation": "content_verified", "verified_input_manifest_digest": digest,
        "accepted_at": now_iso(), "receipt_revision": 1, "effective_policy_ref": "p-remote",
    }


def bound_peer_receipt(sequence: int, *, body: dict, key: str) -> dict:
    """A receipt bound to the admission request, like the real executor returns.

    The coordinator now refuses lookup/admit receipts whose root/step/key/plan
    digest/executor do not bind the current step (N2), so the stub must behave
    like `federation.admit` instead of answering with a fixed foreign shape.
    """
    receipt = peer_receipt(sequence)
    receipt.update({
        "idempotency_key": key,
        "root_task_id": body["root_task_id"],
        "step_id": body["step_id"],
        "plan_digest": body["plan"]["plan_digest"],
        "executor_node_id": PEER_NODE,
    })
    return receipt


def peer_status() -> dict:
    return {
        "executor_task_id": "remote-exec-1", "admission_id": "remote-adm-1",
        "root_task_id": "root-1", "step_id": "retrieve-1", "operation": "retrieve",
        "state": "succeeded", "generation": 1, "lease_until": None,
        "result_ref": "result:remote-exec-1",
        "evidence_set_ref": "federation-execution:remote-exec-1", "error": None,
        "updated_at": now_iso(),
    }


def install_peer(monkeypatch, peer: StubPeer) -> None:
    peers = parse_peers(json.dumps({PEER_NODE: {
        "endpoint": "https://peer.example", "service_token": "peer-service",
        "peer_token": "peer-trust"}}))

    def factory(actor: Actor) -> PeerDirectory:
        return PeerDirectory(peers, actor=actor, transport=peer.transport())

    monkeypatch.setattr(federation_tasks, "peer_directory", factory)


def calls_to(peer: StubPeer, suffix: str) -> int:
    return len([call for call in peer.calls if call[1].endswith(suffix)])


def probe_keys(peer: StubPeer) -> list[str]:
    """探测请求的幂等键：`plan:` = 证据探测，`answer-probe:` = 生成能力探测。"""
    return sorted(str(call[2]) for call in peer.calls if call[1].endswith("/probes"))


class ReconcilingStubPeer(StubPeer):
    """受理已落库但响应丢失的对端：lookup 必须能把同一份回执找回来。

    `foreign_lookup_field` 让 lookup 回一份"绑定对不上"的回执（对端键被复用
    或乱回），用于证明协调者不会采纳外来执行（N2）。
    """

    def __init__(self, *, foreign_lookup_field: str = "", **kwargs):
        super().__init__(**kwargs)
        self.admissions: list[dict] = []
        self.lookups = 0
        self.receipts: dict[str, dict] = {}
        self.drop_next_admission_response = False
        self.foreign_lookup_field = foreign_lookup_field

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.calls.append((request.method, path,
                               request.headers.get("Idempotency-Key")))
            if path.endswith("/probes"):
                return httpx.Response(201, json=peer_probe())
            if path.endswith("/admissions/lookup"):
                self.lookups += 1
                key = json.loads(request.content or b"{}").get("idempotency_key")
                receipt = self.receipts.get(key)
                if receipt is None:
                    return httpx.Response(404, json={"error": {
                        "code": "admission_not_found"}})
                if self.foreign_lookup_field:
                    receipt = {**receipt,
                               self.foreign_lookup_field: "foreign-value"}
                return httpx.Response(200, json=receipt)
            if path.endswith("/admissions"):
                body = json.loads(request.content or b"{}")
                self.admissions.append(body)
                key = body["idempotency_key"]
                existing = self.receipts.get(key)
                if existing is not None:
                    return httpx.Response(200, json=existing)
                receipt = bound_peer_receipt(len(self.receipts) + 1, body=body, key=key)
                self.receipts[key] = receipt
                if self.drop_next_admission_response:
                    self.drop_next_admission_response = False
                    raise httpx.ConnectError("response lost", request=request)
                return httpx.Response(201, json=receipt)
            if "/evidence-sets/" in path:
                return httpx.Response(200, json={
                    "schema": "ddp-evidence/1#EvidenceSet",
                    "set_ref": path.rsplit("/", 1)[1], "items": self.items, "complete": True})
            if "/tasks/" in path:
                return httpx.Response(200, json=peer_status())
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        return httpx.MockTransport(handler)


# ------------------------------------------------------------------ HTTP 流程

async def create_intent(client, *, spec, consent, manifest=None, key=None):
    body = {"task_spec": spec, "exploration_consent": consent}
    if manifest is not None:
        body["scope_manifest"] = manifest
    # 稳定幂等键：契约要求 POST /task-intents 必带，同键同体重放返回同一 intent。
    stable_key = key or plans.digest(body)
    response = await client.post("/api/v1/task-intents", json=body,
                                 headers={"Idempotency-Key": stable_key})
    assert response.status_code == 201, response.text
    return response.json()


async def plan_task(client, root_task_id):
    response = await client.post("/api/v1/task-plans", json={"root_task_id": root_task_id})
    assert response.status_code == 200, response.text
    return response.json()


async def approve_task(client, root_task_id, plan_body, *, recipients=(NODE,)):
    consent = execution_consent(plan_body["plan_digest"], recipients=recipients,
                                edges=[edge["edge_id"] for edge in plan_body["data_edges"]])
    response = await client.post(f"/api/v1/task-plans/{root_task_id}/approve",
                                 json={"plan_digest": plan_body["plan_digest"],
                                       "execution_consent": consent})
    assert response.status_code == 200, response.text
    return response.json()


async def submit_task(client, root_task_id, plan_digest, key):
    """POST /tasks：新受理是 202，跑一轮**真实 worker 队列**后返回权威状态。

    返回 `GET /tasks/{id}` 的响应（200 + 终态），既有的 `.status_code == 200`
    与 `.json()["status"]` 断言一字不用改 —— 但请求确实走了"202 受理 →
    `federation_plan` 被 worker 领取执行"这条生产路径，而不是请求内执行。
    同键重放（200）直接返回，不需要 drain。
    """
    response = await client.post("/api/v1/tasks", headers={"Idempotency-Key": key},
                                 json={"root_task_id": root_task_id,
                                       "plan_digest": plan_digest})
    if response.status_code == 202:
        await drain_tasks(corpus_app.state)
        response = await client.get(f"/api/v1/tasks/{root_task_id}")
    return response


async def ack_delivery(client, delivery_id, digest, key):
    return await client.post(f"/api/v1/deliveries/{delivery_id}/ack",
                             headers={"Idempotency-Key": key},
                             json={"result_manifest_digest": digest})


async def run_local_task(client, session, *, key, mode="fast"):
    """本地发布集合上的完整闭环：intent → plan → approve → execute。"""
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(client, version)
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(client, spec=task_spec(scope="site_public", mode=mode),
                                 consent=consent)
    root = intent["root_task_id"]
    plan_body = await plan_task(client, root)
    await approve_task(client, root, plan_body)
    executed = await submit_task(client, root, plan_body["plan_digest"], key)
    assert executed.status_code == 200, executed.text
    return {"root": root, "plan": plan_body, "status": executed.json(),
            "collection": collection, "evidence_rows": evidence_rows}


def test_go_produced_manifest_digest_is_accepted():
    """跨语言摘要：控制面 `FinalizeScope`（Go）算出的 digest 必须能验证通过。

    期望值是 Go 侧对同一字段、同一时间戳字节序列计算出的冻结值 —— 时间戳
    不重新格式化，所以两种编码才能真正对上。
    """
    manifest = {
        "schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": "go-scope-1",
        "caller_scope_hash": "sha256:" + "1" * 64,
        "created_at": "2026-01-01T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z",
        "registry_revision_vector": [{
            "node_id": "node-" + "a" * 48, "registry_revision": 3,
            "fetched_at": "2026-01-01T00:00:00Z", "directory_ref": "collections",
            "snapshot_ref": "snap-1"}],
        "expanded_members": [{"origin_node_id": "node-" + "a" * 48,
                              "collection_id": "col-1", "operation": "corpus.retrieve"}],
        "unexpanded_subtrees": [], "enumeration_state": "sealed",
        "manifest_digest": "sha256:cc39d96f5fd1b73bf194f71a2a9427e2a6c8679ce45aa6c0032492be5c4721c6",
    }
    assert federation_tasks._manifest_digest_matches(manifest) is True
    tampered = dict(manifest, scope_id="go-scope-2")
    assert federation_tasks._manifest_digest_matches(tampered) is False


def test_go_produced_manifest_with_children_digest_is_accepted():
    """远端展开产出的 manifest 带 `child_manifests`，Go 把它编进摘要原像
    （位于 registry_revision_vector 与 expanded_members 之间，omitempty）。

    期望值与 control-api 的 `TestScopeDigestCrossLanguageFixture` 冻结的是同
    一个 —— 任何一侧编码漂移，两边之一就会红。`<`/`>`/`&` 验 Go 的 HTML 转义。
    """
    node = "node-" + "a" * 48
    manifest = {
        "schema": "ddp-scope-coverage/1#ScopeManifest", "scope_id": "go-scope-children",
        "caller_scope_hash": "sha256:" + "1" * 64,
        "created_at": "2026-01-01T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z",
        "registry_revision_vector": [
            {"node_id": node, "registry_revision": 3, "fetched_at": "2026-01-01T00:00:00Z",
             "directory_ref": "members", "snapshot_ref": "snap-m"},
            {"node_id": "node-p", "registry_revision": 4, "fetched_at": "2026-01-01T00:00:00Z",
             "directory_ref": "collections", "snapshot_ref": "snap-c"}],
        "child_manifests": [{"node_id": "node-p", "scope_ref": "snap-m",
                             "enumeration_state": "sealed"}],
        "expanded_members": [{"origin_node_id": "node-p", "collection_id": "col<1>&",
                              "operation": "corpus.retrieve"}],
        "unexpanded_subtrees": [], "enumeration_state": "sealed",
        "manifest_digest": "sha256:2477d7718bc66f6ce793a05bc6020b9d8ef72e84eba88a0130b6091948704caf",
    }
    assert federation_tasks._manifest_digest_matches(manifest) is True
    # child manifest 的状态必须在摘要里：改它就对不上。
    tampered = json.loads(json.dumps(manifest))
    tampered["child_manifests"][0]["enumeration_state"] = "partial"
    assert federation_tasks._manifest_digest_matches(tampered) is False
    # 空列表与缺省等价（Go omitempty），不能因为多了一个 `[]` 就改变摘要。
    bare = {key: value for key, value in manifest.items() if key != "child_manifests"}
    assert (federation_tasks._go_manifest_digest(dict(bare, child_manifests=[]))
            == federation_tasks._go_manifest_digest(bare))


async def test_coordination_tables_and_tampered_manifest(actor_client, engine):
    from sqlalchemy import text as sql_text

    async with engine.connect() as conn:
        rows = await conn.execute(sql_text("SELECT name FROM sqlite_master WHERE type='table'"))
    assert "federation_task_events" in {row[0] for row in rows}

    manifest = scope_manifest([member("some-collection")])
    manifest["manifest_digest"] = "sha256:" + "0" * 64
    body = {"task_spec": task_spec(scope="federation_public", mode="exhaustive_scope",
                                   scope_ref="scope-1"),
            "exploration_consent": exploration(), "scope_manifest": manifest}
    response = await actor_client.post("/api/v1/task-intents", json=body,
                                       headers={"Idempotency-Key": plans.digest(body)})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_changed"


# ------------------------------------------------------------------ 正向闭环

async def test_local_intent_plan_approve_execute_coverage_events_and_ack(
        actor_client, session):
    run = await run_local_task(actor_client, session, key="local-key")
    status = run["status"]
    assert status["status"] == "succeeded"
    # fast 的结局必须显式是 partial，即使选中的候选全部成功（§7.2）。
    assert status["retrieval_completeness"] == "partial"
    assert status["evidence_sufficiency"] == "sufficient_by_policy"
    result = status["result"]
    assert result["answer"] is None and result["answer_reason"] == "local_model_missing"
    assert [item["evidence_id"] for item in result["evidence"]] == [run["evidence_rows"][0].id]
    assert result["evidence"][0]["locator"]["bbox"] == [10, 20, 300, 60]
    assert "_excerpt" not in result["evidence"][0], "HTTP 结果不得带内部审计字段"
    assert result["counts"] == {"total_targets": 1, "applicable_targets": 1,
                                "succeeded": 1, "excluded": 0, "incomplete": 0}

    probe = await session.scalar(select(FederationProbe).where(
        FederationProbe.organization_id == ORG).execution_options(populate_existing=True))
    retrieve = next(step for step in run["plan"]["steps"] if step["operation"] == "retrieve")
    assert retrieve["fixed_inputs"] == ["query", f"collection:{run['collection']['collection_id']}"]
    assert retrieve["probe_refs"] == [probe.probe_id]

    # 规划与审批的幂等重放：同一修订不重探测、不重签名。
    replay_plan = await actor_client.post("/api/v1/task-plans",
                                          json={"root_task_id": run["root"]})
    assert replay_plan.status_code == 200
    assert replay_plan.json() == run["plan"] | {
        "planning_state": "approved", "execution_consent_ref": "execute-1"}
    replay_approve = await approve_task(actor_client, run["root"], run["plan"])
    assert replay_approve["planning_state"] == "approved"
    assert replay_approve["execution_consent_ref"] == "execute-1"

    fetched = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert fetched == status
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["retrieval_completeness"] == "partial"
    assert coverage["counts"] == result["counts"]
    events = (await actor_client.get(f"/api/v1/tasks/{run['root']}/events")).json()
    assert [event["type"] for event in events["events"]] == [
        "intent_created", "plan_ready", "plan_approved", "execution_started",
        "task_completed", "delivery_pending"]
    assert [event["seq"] for event in events["events"]] == [1, 2, 3, 4, 5, 6]
    assert events["complete"] is True
    after = (await actor_client.get(f"/api/v1/tasks/{run['root']}/events",
                                    params={"after": events["next_seq"]})).json()
    assert after["events"] == [] and after["next_seq"] == events["next_seq"]

    assert status["delivery_state"] == "pending"
    ack = await ack_delivery(actor_client, status["delivery_id"],
                             result["result_manifest_digest"], "ack-key")
    assert ack.status_code == 200, ack.text
    receipt = ack.json()
    assert receipt["state"] == "confirmed" and receipt["verified_at"]
    assert receipt["retention"] == "temporary"
    replay = await ack_delivery(actor_client, status["delivery_id"],
                                result["result_manifest_digest"], "ack-key")
    assert replay.json() == receipt
    row = await session.get(FederationDelivery, status["delivery_id"])
    assert row.state == "confirmed"
    assert (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()["delivery_state"] \
        == "confirmed"


async def test_exhaustive_sealed_scope_reaches_complete(actor_client, session):
    _, version, _, _, _ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    # 控制面枚举出的 operation 可以是 "search" 这类名字；本切片一律按集合检索执行，
    # 不拿 operation 字符串当执行类型的判据。
    manifest = scope_manifest([member(collection["collection_id"], operation="search")],
                              enumeration="sealed")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    retrieve = next(step for step in plan_body["steps"] if step["operation"] == "retrieve")
    assert retrieve["fixed_inputs"] == ["query", f"collection:{collection['collection_id']}"]
    await approve_task(actor_client, root, plan_body)
    executed = await submit_task(actor_client, root, plan_body["plan_digest"], "exhaustive")
    status = executed.json()
    assert status["status"] == "succeeded"
    assert status["retrieval_completeness"] == "complete"
    assert status["result"]["counts"]["incomplete"] == 0
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["retrieval_completeness"] == "complete"


# ---------------------------------------------------- 内部限制的诚实传播（T85）

TEXTS_12 = tuple(f"retrieval target text number {index}" for index in range(12))


async def test_truncated_retrieve_stays_partial_and_never_completes(
        actor_client, session):
    """F1/T85：probe 与 execution 都自报 truncated_by_limit 时不许洗成 succeeded。

    旧行为：probe 阶段记 partial，执行阶段用 `state="succeeded"` 覆盖成成功，
    counts.incomplete=0、retrieval_completeness=complete —— 一次被截断的检索
    被账本洗成了"全范围查完"。
    """
    from test_federation_probes import BASE, headers as peer_headers

    _, version, _, _, _ = await indexed_source(session, texts=TEXTS_12)
    collection = await publish_collection(actor_client, version, key="truncated")
    manifest = scope_manifest([member(collection["collection_id"])], enumeration="sealed")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1", query="retrieval target"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    probe = await session.scalar(select(FederationProbe).where(
        FederationProbe.organization_id == ORG).execution_options(populate_existing=True))
    assert "truncated_by_limit" in probe.result_json["result"]["retrieval"]["internal_limits"], \
        "夹具必须真的把 probe 截断，否则这条用例量不到东西"

    await approve_task(actor_client, root, plan_body)
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "truncated-key")
              ).json()
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["counts"]["incomplete"] == 1
    assert status["result"]["counts"]["succeeded"] == 0

    execution = await session.scalar(select(FederationExecution).where(
        FederationExecution.root_task_id == root).execution_options(populate_existing=True))
    # 执行回执自己也要对协调者可见地报告限制（契约 ExecutionStatus 新增字段）。
    assert "truncated_by_limit" in (execution.result_json or {}).get("internal_limits", [])
    detail = await actor_client.get(f"{BASE}/tasks/{execution.executor_task_id}",
                                    headers=peer_headers())
    assert detail.status_code == 200, detail.text
    assert "truncated_by_limit" in detail.json()["internal_limits"]
    assert "degraded" in detail.json()

    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    entry = coverage["entries"][0]
    assert entry["state"] == "partial", "执行自报内部限制时条目不许是 succeeded"
    assert coverage["retrieval_completeness"] == "partial"
    assert coverage["counts"]["incomplete"] == 1


async def test_all_partial_targets_with_evidence_is_succeeded_not_failed(
        actor_client, session):
    """N1：目标全 partial 但真的产出了证据时，状态轴必须与证据轴一致。

    旧行为：`counts.succeeded == 0` 就把任务标 failed 并写
    `error=no_retrievable_target`，尽管结果里有真实证据、账本如实标着 partial。
    失败只留给"一个目标都没产出证据"。
    """
    _, version, _, _, _ = await indexed_source(session, texts=TEXTS_12)
    collection = await publish_collection(actor_client, version, key="n1-partial")
    manifest = scope_manifest([member(collection["collection_id"])], enumeration="sealed")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1", query="retrieval target"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body)
    status = (await submit_task(actor_client, root, plan_body["plan_digest"],
                                "n1-partial")).json()

    assert status["status"] == "succeeded", status
    assert status["error"] is None, "有证据就不许再带失败原因"
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["counts"]["succeeded"] == 0
    assert status["result"]["counts"]["incomplete"] == 1
    assert status["result"]["evidence"], "证据是状态为 succeeded 的依据"
    assert status["delivery_state"] == "pending", "有结果的 partial 也要交付"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "partial"
    assert coverage["retrieval_completeness"] == "partial"


async def test_no_evidence_at_all_stays_failed_with_truthful_reason(
        actor_client, session, monkeypatch):
    """N1 的另一半：没有任何目标产出证据时仍是 failed，原因要如实。"""
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"],
                                "n1-denied")).json()

    assert status["status"] == "failed"
    assert status["error"] == "egress_mode:local_only", "原因必须描述真实结局"
    assert status["result"]["evidence"] == []
    assert status["result"]["evidence_sufficiency"] == "insufficient"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "denied"


async def test_coverage_entry_exclusion_basis_column_admits_long_basis(session):
    """F6：排除依据列已放宽到 Text；内核上限之下不许再撞列宽。

    这只是结构断言（迁移在 PostgreSQL 上跑，本机单测是 SQLite create_all）；
    行为上限由 ddp_core 的 test_exclusion_basis_is_explicitly_bounded_never_truncated
    钉住。
    """
    from sqlalchemy import Text

    from ddp_corpus.federation_models import CoverageEntry

    assert isinstance(CoverageEntry.__table__.c.exclusion_basis.type, Text)


def test_evidence_set_producer_drops_whitespace_only_excerpt():
    """N5 的生产侧：空白文本不该被当成"有正文"随证据集出口。"""
    public = federation._public_evidence_with_excerpt(
        {"_excerpt": "   ", "evidence_id": "e"})
    assert "excerpt" not in public


def test_evidence_set_producer_still_bounds_long_excerpt():
    """N6 的生产侧契约仍在：有界截断到 2000，绝不外推截断事实。"""
    public = federation._public_evidence_with_excerpt(
        {"_excerpt": "x" * 5000, "evidence_id": "e"})
    assert len(public["excerpt"]) == federation.EVIDENCE_EXCERPT_CHARS


# ------------------------------------------------------------------ 探索许可门

async def test_local_only_never_contacts_peer_and_stays_partial(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    _, version, _, _, _ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    manifest = scope_manifest([member(collection["collection_id"]),
                               member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    assert peer.calls == [], "local_only 探索在规划阶段就一个远端请求都不许发"
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    executed = await submit_task(actor_client, root, plan_body["plan_digest"], "local-only")
    assert peer.calls == [], "执行阶段也不得联系被探索许可挡住的远端"
    status = executed.json()
    assert status["retrieval_completeness"] != "complete"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["counts"]["total_targets"] == 2
    remote = next(entry for entry in coverage["entries"]
                  if entry["target_key"]["origin_node_id"] == PEER_NODE)
    assert remote["state"] == "denied"
    assert coverage["retrieval_completeness"] == "partial"


async def test_listed_nodes_without_query_payload_denies_remote(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          payload=("collection_filters",))
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    assert peer.calls == []
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    executed = await submit_task(actor_client, root, plan_body["plan_digest"], "no-payload")
    assert peer.calls == []
    assert executed.json()["status"] == "failed"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "denied"
    assert coverage["entries"][0]["last_error"] == "payload_not_allowed"


async def test_missing_or_expired_consent_is_egress_denied_without_probes(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    spec = task_spec(scope="site_public", mode="fast")
    for consent in (
            exploration(consent_id="other-consent"),
            {},
            exploration(valid_until="2020-01-01T00:00:00Z"),
            exploration(egress="trust_domain", recipients=(), payload=())):
        body = {"task_spec": spec, "exploration_consent": consent}
        response = await actor_client.post(
            "/api/v1/task-intents", json=body,
            headers={"Idempotency-Key": plans.digest(body)})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "egress_denied"
    count = await session.scalar(select(func.count()).select_from(FederationProbe))
    assert count == 0 and peer.calls == []

    _, version, _, _, _ = await indexed_source(session)
    await publish_collection(actor_client, version)
    intent = await create_intent(actor_client, spec=spec, consent=exploration())
    row = await session.get(FederationRequest, intent["root_task_id"])
    row.exploration_consent_json = dict(row.exploration_consent_json,
                                        valid_until="2020-01-01T00:00:00Z")
    await session.commit()
    planned = await actor_client.post("/api/v1/task-plans",
                                      json={"root_task_id": intent["root_task_id"]})
    assert planned.status_code == 403, planned.text
    assert planned.json()["error"]["code"] == "egress_denied"
    count = await session.scalar(select(func.count()).select_from(FederationProbe))
    assert count == 0 and peer.calls == []


# ------------------------------------------------------------------ 远端

async def test_remote_peer_success_preserves_origin_and_completes(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    # 两条探测：证据探测 + 生成能力探测（本地生成未就绪时的 answer 委托规划）。
    assert calls_to(peer, "/probes") == 2
    keys = probe_keys(peer)
    assert sum(1 for key in keys if key.startswith("plan:")) == 1
    assert sum(1 for key in keys if key.startswith("answer-probe:")) == 1
    stored = await session.scalar(select(FederationProbe).where(
        FederationProbe.organization_id == ORG,
        FederationProbe.target_node_id == PEER_NODE))
    assert stored is not None and stored.probe_kind == "evidence_retrieval"
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "remote-ok")
              ).json()
    assert status["status"] == "succeeded"
    assert status["retrieval_completeness"] == "complete"
    evidence = status["result"]["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["origin_node_id"] == PEER_NODE
    assert evidence[0]["authority_node_id"] == PEER_NODE
    assert evidence[0]["evidence_id"] == "peer-evidence-1"
    assert calls_to(peer, "/admissions") == 1
    assert [call for call in peer.calls if "/tasks/" in call[1]]


async def test_unknown_admission_outcome_reconciles_without_second_execution(
        actor_client, session, monkeypatch):
    """F2/T81：受理成功但响应丢失 -> lookup 复用回执，对端只有一条执行。

    旧行为：协调者把丢失响应记成 unreachable，resume 再带**新代次的新键**
    重新受理，对端出现第二条执行行 —— 一次已受理的检索被重做。
    """
    peer = ReconcilingStubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    step = next(item for item in plan_body["steps"] if item["operation"] == "retrieve")
    peer.drop_next_admission_response = True

    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "lost-response")
              ).json()
    assert status["status"] == "succeeded", status
    assert status["retrieval_completeness"] == "complete"
    # 对端只收到一次受理，一次执行；协调者用同一个业务键对账找回了回执。
    assert len(peer.admissions) == 1, peer.admissions
    assert peer.admissions[0]["idempotency_key"] == f"{root}:{step['step_id']}"
    assert peer.admissions[0]["delegation_generation"] == 1
    assert len(peer.receipts) == 1
    assert peer.lookups == 1, "未知结果必须走一次对账 lookup"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "succeeded"

    # 补做时已成功的目标不再动手：既不重建执行，也不新增受理。
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    assert len(peer.admissions) == 1
    assert len(peer.receipts) == 1


@pytest.mark.parametrize("field", ["root_task_id", "step_id", "idempotency_key",
                                   "plan_digest", "executor_node_id"])
async def test_remote_lookup_receipt_that_does_not_bind_is_not_adopted(
        actor_client, session, monkeypatch, field):
    """N2：lookup 回执必须逐字段绑定本次步骤，否则按"没有有效回执"处理。

    不绑定就采用等于轮询并融合一次外来执行（foreign root/step/plan）。旧代码
    只看 `state`，对端回什么就信什么。
    """
    peer = ReconcilingStubPeer(foreign_lookup_field=field)
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    peer.drop_next_admission_response = True

    status = (await submit_task(actor_client, root, plan_body["plan_digest"],
                                f"n2-{field}")).json()
    assert status["status"] == "failed", status
    assert status["error"] == f"receipt_binding_mismatch:{field}"
    assert status["result"]["evidence"] == [], "外来回执的证据绝不能进结果"
    assert calls_to(peer, "/tasks/") == 0, "binding 对不上就不许轮询外来执行"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "failed"
    assert coverage["entries"][0]["last_error"] == f"receipt_binding_mismatch:{field}"


async def test_remote_admit_receipt_that_does_not_bind_is_not_adopted(
        actor_client, session, monkeypatch):
    """N2：直接受理响应也必须绑定；外来回执不许触发轮询/证据融合。"""
    peer = StubPeer(foreign_admit_field="root_task_id")
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"],
                                "n2-admit")).json()
    assert status["status"] == "failed", status
    assert status["error"] == "receipt_binding_mismatch:root_task_id"
    assert status["result"]["evidence"] == []
    assert calls_to(peer, "/tasks/") == 0, "binding 对不上就不许轮询外来执行"


async def test_local_unknown_admission_outcome_reconciles_without_second_execution(
        actor_client, session, monkeypatch):
    """F2(c)：本地 admission 结果未知（已提交但返回路径炸了）也必须先对账。"""
    real_admit = federation.admit
    calls = {"count": 0}

    async def _admit_then_lose_reply(*args, **kwargs):
        receipt, created = await real_admit(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("response lost after persistence")
        return receipt, created

    monkeypatch.setattr(federation, "admit", _admit_then_lose_reply)
    run = await run_local_task(actor_client, session, key="local-unknown")
    assert run["status"]["status"] == "succeeded", run["status"]
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1
    assert await session.scalar(select(func.count()).select_from(FederationExecution)) == 1
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["entries"][0]["state"] == "succeeded"


async def test_local_admit_failure_before_persistence_marks_task_failed_not_stuck(
        actor_client, session, monkeypatch):
    """N3：admit 在落库前抛非 APIError，对账 404 必须把任务标 failed。

    旧行为：`_lookup_local_receipt` 读 `exc.status`（APIError 上不存在），对账
    一拿到真实 404 就 AttributeError -> 500，任务永远停在 running。
    """

    async def _explode(*args, **kwargs):
        raise RuntimeError("admission exploded before persistence")

    monkeypatch.setattr(federation, "admit", _explode)
    run = await run_local_task(actor_client, session, key="n3-early-crash")
    status = run["status"]
    assert status["status"] == "failed", status
    assert status["error"] == "admission_unknown"
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["entries"][0]["state"] == "unreachable"
    assert coverage["entries"][0]["last_error"] == "admission_unknown"
    stored = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert stored["status"] == "failed", "行不许停在 running"
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 0


async def test_local_lookup_receipt_that_does_not_bind_this_step_is_rejected(
        actor_client, session, monkeypatch):
    """N2 本地镜像：lookup 回执的 step/plan 与本次步骤对不上就不许采纳。"""
    run = await run_local_task(actor_client, session, key="n2-local")
    entry = await session.scalar(select(CoverageEntry).where(
        CoverageEntry.root_task_id == run["root"]))
    entry.state = "unreachable"
    entry.last_error = "simulated"
    await session.commit()

    real_lookup = federation.lookup_admission

    async def _foreign_lookup(session_, actor_, key):
        receipt = await real_lookup(session_, actor_, key)
        return {**receipt, "step_id": "retrieve-99",
                "plan_digest": "sha256:" + "9" * 64}

    monkeypatch.setattr(federation, "lookup_admission", _foreign_lookup)
    resumed = await actor_client.post(f"/api/v1/tasks/{run['root']}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    # 本任务的结果里还保留首轮的真实证据（状态轴因此仍是 succeeded），
    # 量的是**这次补做**：lookup 回执对不上绑定就不许被执行行复用。
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["entries"][0]["state"] == "failed"
    assert coverage["entries"][0]["last_error"] == "receipt_binding_mismatch:step_id"
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1, \
        "拒绝外来回执后不许再产生受理"


async def test_concurrent_intent_same_key_insert_race_replays_not_500(
        actor_client, session, monkeypatch):
    """N4：并发同键 intent 的唯一约束必须在 try 内映射成重放/409，而不是 500。

    旧行为：`_append_event` 在 try 外面，它的 `SELECT max(seq)` autoflush 先撞
    唯一约束，裸 IntegrityError 直接冒出去。
    """
    spec = task_spec(scope="site_public", mode="fast")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    body = {"task_spec": spec, "exploration_consent": consent}
    key = "race-intent-key"
    first = await actor_client.post("/api/v1/task-intents", json=body,
                                    headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text

    real_find = federation_tasks._find_intent_by_key
    misses = {"remaining": 1}

    async def _flaky_find(session_, organization_id, idempotency_key):
        if misses["remaining"]:
            misses["remaining"] -= 1
            return None
        return await real_find(session_, organization_id, idempotency_key)

    monkeypatch.setattr(federation_tasks, "_find_intent_by_key", _flaky_find)
    replay = await actor_client.post("/api/v1/task-intents", json=body,
                                     headers={"Idempotency-Key": key})
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    count = await session.scalar(select(func.count()).select_from(FederationRequest).where(
        FederationRequest.intent_idempotency_key == key))
    assert count == 1, "同键重放不得造出第二个 root task"

    misses["remaining"] = 1
    changed = {**body, "task_spec": {**spec, "query": "a different query"}}
    conflict = await actor_client.post("/api/v1/task-intents", json=changed,
                                       headers={"Idempotency-Key": key})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_remote_probe_persist_race_reuses_existing_row(session, monkeypatch):
    """N4：远端探测的确定性 id 撞唯一约束时，SAVEPOINT 只回滚这一条并复用已有行。"""
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    probe = peer_probe()
    kwargs = {"key": "remote-race-key", "task_spec_digest": probe["task_spec_digest"],
              "consent_ref": "explore-1", "query_digest": "sha256:" + "e" * 64,
              "request_digest": "sha256:" + "f" * 64, "now": utcnow()}
    real_get = AsyncSession.get
    misses = {"remaining": 1}

    async def _flaky_get(self, entity, ident, *args, **kwargs):
        if entity is FederationProbe and misses["remaining"]:
            misses["remaining"] -= 1
            return None
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", _flaky_get)
    first = await federation_tasks._persist_remote_probe(
        session, actor, probe, [peer_evidence()], **kwargs)
    misses["remaining"] = 1
    second = await federation_tasks._persist_remote_probe(
        session, actor, probe, [peer_evidence()], **kwargs)
    assert second == first
    assert await session.scalar(select(func.count()).select_from(FederationProbe)) == 1


async def test_resume_reuses_an_admitted_local_waiting_input_receipt(
        actor_client, session, monkeypatch):
    """F2：本地补做先对账，已受理的 waiting_input 不会因新代次重发 key 撞 409。"""
    from ddp_corpus import capabilities

    async def _no_gateway(_http):
        return None

    monkeypatch.setattr(capabilities, "_fetch_gateway", _no_gateway)
    resource, _, _, _, _ = await indexed_source(session)
    spec = task_spec(scope="fixed_resources", mode="fast", resource_refs=[resource.id])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(actor_client, spec=spec, consent=consent)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body)
    first = (await submit_task(actor_client, root, plan_body["plan_digest"],
                               "waiting-1")).json()
    assert first["status"] == "failed"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["last_error"] == "waiting_input"

    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "not_attempted"
    assert coverage["entries"][0]["last_error"] == "waiting_input"
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1, \
        "同一个 (root, step) 不许有第二条受理"


async def test_fusion_dedupes_identical_evidence_but_keeps_distinct_resources(
        actor_client, session, monkeypatch):
    items = [peer_evidence(evidence_id="peer-e1", resource_id="peer-r1"),
             peer_evidence(evidence_id="peer-e1", resource_id="peer-r1"),
             peer_evidence(evidence_id="peer-e1", resource_id="peer-r2")]
    peer = StubPeer(items=items)
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "fusion")
              ).json()
    # 同 (origin, resource, version, evidence_id) 只留一份；不同资源归属都保留 ——
    # 重复上传不得被当成独立共识。
    assert [item["resource_id"] for item in status["result"]["evidence"]] \
        == ["peer-r1", "peer-r2"]
    assert all(item["origin_node_id"] == PEER_NODE
               for item in status["result"]["evidence"])


async def test_remote_peer_unreachable_is_visible_and_never_retried(
        actor_client, session, monkeypatch):
    peer = StubPeer(fail="all")
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration()
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    # 证据探测 + 生成能力探测各一次（对端不可达，两次都失败）。
    assert calls_to(peer, "/probes") == 2
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "remote-down")
              ).json()
    # 两次探测 + 受理一次 + 对账一次：受理结果未知时先按业务键 lookup（T82），
    # 对账也失败才如实记 unreachable —— 没有重试风暴，也没有伪造证据。
    assert len(peer.calls) == 4, peer.calls
    assert peer.calls[-1][1].endswith("/admissions/lookup")
    assert status["status"] == "failed" and status["error"] == "peer_unavailable"
    assert status["retrieval_completeness"] == "partial"
    assert status["result"]["evidence"] == []
    assert status["result"]["evidence_sufficiency"] == "insufficient"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["entries"][0]["state"] == "unreachable"


# ------------------------------------------------------------------ 幂等与恢复

async def test_execute_replay_and_conflicts(actor_client, session):
    run = await run_local_task(actor_client, session, key="replay-key")
    admissions = await session.scalar(select(func.count()).select_from(FederationAdmission))
    probes = await session.scalar(select(func.count()).select_from(FederationProbe))
    replay = await submit_task(actor_client, run["root"], run["plan"]["plan_digest"],
                               "replay-key")
    assert replay.status_code == 200 and replay.json() == run["status"]
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) \
        == admissions
    assert await session.scalar(select(func.count()).select_from(FederationProbe)) == probes

    changed = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "replay-key"},
        json={"root_task_id": run["root"], "plan_digest": "sha256:" + "0" * 64})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "plan_changed"
    other_key = await submit_task(actor_client, run["root"], run["plan"]["plan_digest"],
                                  "another-key")
    assert other_key.status_code == 409
    assert other_key.json()["error"]["code"] == "idempotency_conflict"


async def test_task_intent_idempotency_replays_and_conflicts(actor_client, session):
    """F3/T80：POST /task-intents 必须真正按 Idempotency-Key 幂等。

    旧行为：路由不读这个头，App 客户端按内容算的稳定键被忽略；丢响应后的
    显式重试会造出第二个 root task，随后在对端出现第二份探测量。
    """
    spec = task_spec(scope="site_public", mode="fast")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    body = {"task_spec": spec, "exploration_consent": consent}
    key = "stable-intent-key"

    missing = await actor_client.post("/api/v1/task-intents", json=body)
    assert missing.status_code == 422, "契约要求所有写请求带 Idempotency-Key"

    first = await actor_client.post("/api/v1/task-intents", json=body,
                                    headers={"Idempotency-Key": key})
    replay = await actor_client.post("/api/v1/task-intents", json=body,
                                     headers={"Idempotency-Key": key})
    assert first.status_code == 201 and replay.status_code == 201
    assert first.json() == replay.json()
    assert first.json()["root_task_id"] == replay.json()["root_task_id"]
    count = await session.scalar(select(func.count()).select_from(FederationRequest)
                                 .where(FederationRequest.intent_idempotency_key == key))
    assert count == 1, "同键重放不得造出第二个 root task"

    changed = {**body, "task_spec": {**spec, "query": "a different query"}}
    conflict = await actor_client.post("/api/v1/task-intents", json=changed,
                                       headers={"Idempotency-Key": key})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    # 异实体被拒之后原 intent 原样可用。
    still = await actor_client.post("/api/v1/task-intents", json=body,
                                    headers={"Idempotency-Key": key})
    assert still.json()["root_task_id"] == first.json()["root_task_id"]


async def test_commit_maps_unique_violation_to_conflict(session):
    """F8：唯一约束冲突不许变成裸 500（并发同任务写的事件序号）。"""
    for _ in range(2):
        session.add(FederationTaskEvent(id=new_id(), root_task_id="dup-root", seq=1,
                                        type="x", payload={}, created_at=utcnow()))
    with pytest.raises(APIError) as exc:
        await federation_tasks._commit(session)
    assert exc.value.status_code == 409
    assert exc.value.code == "idempotency_conflict"


async def test_resume_reruns_only_incomplete_targets(actor_client, session, monkeypatch):
    peer = StubPeer(fail_admit_step="retrieve-2")
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, plan_body["plan_digest"], "resume-1")
             ).json()
    assert first["status"] == "succeeded", "一个目标成功也算执行完成"
    assert first["retrieval_completeness"] == "partial"
    admits_before = calls_to(peer, "/admissions")
    assert admits_before == 2

    peer.fail_admit_step = None
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    assert calls_to(peer, "/admissions") - admits_before == 1, \
        "resume 只补做未完成目标，不得重发已完成目标"
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert status["retrieval_completeness"] == "complete"
    assert status["result"]["counts"]["succeeded"] == 2


# ------------------------------------------------------------------ 取消

async def test_cancel_keeps_denominator_and_never_rewrites_success(
        actor_client, session, monkeypatch):
    """取消只落在**还没结局**的任务上；已落定的结局（含 failed）一律不改写。

    旧行为：failed 被改写成 cancelled，逐目标的 unreachable/denied 原因全被
    抹成 `not_attempted/cancelled`，覆盖账本行的 counts 还停在旧值，resume
    也因此被永久封死 —— "系统做砸了"被改写成了"用户不要了"。
    """
    other = "node-" + "c" * 48
    peer = StubPeer(fail="admit")
    install_peer(monkeypatch, peer)
    # 第三个目标在探索许可的接收方之外：执行期落 denied，它是需要新授权的
    # 结论，不是"没试过"，取消不许把它洗掉。
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE),
                               member("other-collection", other)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE, other))
    failed = (await submit_task(actor_client, root, plan_body["plan_digest"], "cancel-1")
              ).json()
    assert failed["status"] == "failed"
    before = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert before["counts"]["total_targets"] == 3 and before["counts"]["incomplete"] == 3
    before_states = {entry["target_key"]["collection_id"]: (entry["state"], entry["last_error"])
                     for entry in before["entries"]}
    assert before_states["other-collection"] == ("denied", "recipient_not_allowed")

    # failed 是已落定的结局：取消原样返回，覆盖记录一条不动。
    kept = await actor_client.post(f"/api/v1/tasks/{root}/cancel")
    assert kept.status_code == 200
    assert kept.json() == failed, "取消不得把 failed 改写成 cancelled"
    untouched = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert untouched == before

    # resume 让任务回到 running（排队、worker 还没领）：这时取消才落 cancelled。
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    cancelled = await actor_client.post(f"/api/v1/tasks/{root}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error"] == "cancelled"
    after = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert after["counts"]["total_targets"] == 3, "取消不得把未完成目标从分母删掉"
    assert after["counts"]["incomplete"] == 3
    after_states = {entry["target_key"]["collection_id"]: (entry["state"], entry["last_error"])
                    for entry in after["entries"]}
    assert after_states == {
        "peer-collection-1": ("not_attempted", "cancelled"),
        "peer-collection-2": ("not_attempted", "cancelled"),
        "other-collection": ("denied", "recipient_not_allowed"),
    }
    ledger = await session.get(CoverageLedger, root, populate_existing=True)
    assert ledger.counts_json == after["counts"], "覆盖账本行必须跟着逐目标记录重算"
    again = await actor_client.post(f"/api/v1/tasks/{root}/cancel")
    assert again.json() == cancelled.json()
    await drain_tasks(corpus_app.state)
    assert (await actor_client.get(f"/api/v1/tasks/{root}")).json()["status"] == "cancelled"

    # 已完成的任务不得被取消改写。
    run = await run_local_task(actor_client, session, key="cancel-success")
    protected = await actor_client.post(f"/api/v1/tasks/{run['root']}/cancel")
    assert protected.status_code == 200
    assert protected.json() == run["status"]
    assert protected.json()["status"] == "succeeded"


# ------------------------------------------------------------------ 证据矛盾轴

def validate_ledger_contract(ledger: dict) -> None:
    """GET coverage 的形状必须过冻结契约（含"有矛盾记录 ⇔ conflicting"双向 allOf）。"""
    from jsonschema import Draft202012Validator
    from test_federation_admissions import SCHEMAS
    schema = SCHEMAS["schemas"]["ddp-scope-coverage/v1.json"]
    Draft202012Validator({"$ref": "#/$defs/CoverageLedger", "$defs": schema["$defs"]}).validate(ledger)


async def test_version_divergence_of_one_source_marks_the_ledger_conflicting(
        actor_client, monkeypatch):
    """同一来源的两个固定版本在同一定位上取回不同正文：账本必须报 conflicting。

    旧行为：协调者组账本时从不传冲突轴，两版互相矛盾的证据被记成
    sufficient_by_policy（路由评测的发现 1）。这里没有生成模型，冲突只能来自规则。
    """
    old = {**peer_evidence(evidence_id="peer-old"), "source_version_id": "peer-version-1",
           "excerpt_digest": "sha256:" + "1" * 64}
    new = {**peer_evidence(evidence_id="peer-new"), "source_version_id": "peer-version-2",
           "excerpt_digest": "sha256:" + "2" * 64}
    peer = StubPeer(items=[old, new])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "divergence")).json()

    assert status["status"] == "succeeded"
    assert status["evidence_sufficiency"] == "conflicting"
    expected = [{"basis": "version_divergence", "evidence_refs": ["peer-new", "peer-old"],
                 "semantic_review": "needs_review"}]
    assert status["result"]["conflicts"] == expected
    assert status["result"]["evidence_sufficiency"] == "conflicting"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["evidence_sufficiency"] == "conflicting", \
        "覆盖读取从逐目标记录重算，必须带回持久化的矛盾记录"
    assert coverage["conflicts"] == expected
    validate_ledger_contract(coverage)


@pytest.mark.parametrize("legacy_result", [False, True])
async def test_resume_keeps_the_divergence_found_by_an_earlier_round(
        actor_client, session, monkeypatch, legacy_result):
    """第一轮成功的目标在 resume 时被跳过：它那一轮算出的规则矛盾不能在补做后消失。

    少了这一步，补做完剩下的目标后账本会从 conflicting 被重算回 sufficient_by_policy
    —— 矛盾证据还在结果里，冲突轴却静默消失。`legacy_result` 模拟归属字段上线前
    完成的结果（没有 `_attributed_evidence`）：只能靠原样复原已记下的矛盾。
    """
    old = {**peer_evidence(evidence_id="peer-old"), "source_version_id": "peer-version-1",
           "excerpt_digest": "sha256:" + "1" * 64}
    new = {**peer_evidence(evidence_id="peer-new"), "source_version_id": "peer-version-2",
           "excerpt_digest": "sha256:" + "2" * 64}
    peer = StubPeer(items=[old, new], fail_admit_step="retrieve-2")
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, plan_body["plan_digest"], "diverge-1")).json()
    assert first["evidence_sufficiency"] == "conflicting"
    expected = first["result"]["conflicts"]
    assert [item["basis"] for item in expected] == ["version_divergence"]

    assert "_attributed_evidence" not in first["result"], "内部簿记不许出 HTTP"
    if legacy_result:
        row = await session.get(FederationRequest, root)
        stripped = dict(row.result_json)
        stripped.pop("_attributed_evidence")
        row.result_json = stripped
        await session.commit()
    # 第二个目标补做时只返回一条无关证据：矛盾只可能来自上一轮。
    peer.fail_admit_step = None
    peer.items = [peer_evidence(evidence_id="peer-other", resource_id="peer-r9")]
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert status["retrieval_completeness"] == "complete"
    assert status["evidence_sufficiency"] == "conflicting"
    assert status["result"]["conflicts"] == expected
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["conflicts"] == expected
    validate_ledger_contract(coverage)


async def test_versions_split_across_resume_rounds_are_still_compared(actor_client, monkeypatch):
    """同一定位的两个版本分两轮到达（第一轮 v1，resume 补做的目标带回 v2）也必须比较。

    第六次验收复现的形状：复原的条目不进规则一路，两版矛盾的证据都在结果里，
    账本却照报 sufficient_by_policy —— 与"同一轮到达就判 conflicting"不一致。
    """
    old = {**peer_evidence(evidence_id="peer-old"), "source_version_id": "peer-version-1",
           "excerpt_digest": "sha256:" + "1" * 64}
    new = {**peer_evidence(evidence_id="peer-new"), "source_version_id": "peer-version-2",
           "excerpt_digest": "sha256:" + "2" * 64}
    peer = StubPeer(items=[old], fail_admit_step="retrieve-2")
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, plan_body["plan_digest"], "split-1")).json()
    assert first["evidence_sufficiency"] == "sufficient_by_policy"
    assert first["result"]["conflicts"] == []

    peer.fail_admit_step = None
    peer.items = [new]
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert {item["evidence_id"] for item in status["result"]["evidence"]} >= {"peer-old", "peer-new"}
    assert status["evidence_sufficiency"] == "conflicting"
    assert status["result"]["conflicts"] == [{
        "basis": "version_divergence", "evidence_refs": ["peer-new", "peer-old"],
        "semantic_review": "needs_review"}]
    assert "_attributed_evidence" not in status["result"]
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["evidence_sufficiency"] == "conflicting"
    validate_ledger_contract(coverage)


async def test_same_text_across_versions_is_not_a_conflict(actor_client, monkeypatch):
    same = "sha256:" + "3" * 64
    peer = StubPeer(items=[
        {**peer_evidence(evidence_id="peer-a"), "source_version_id": "v1", "excerpt_digest": same},
        {**peer_evidence(evidence_id="peer-b"), "source_version_id": "v2", "excerpt_digest": same}])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "same-text")).json()
    assert status["evidence_sufficiency"] == "sufficient_by_policy"
    assert status["result"]["conflicts"] == []
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert "conflicts" not in coverage
    validate_ledger_contract(coverage)


# ------------------------------------------------------------------ 执行期边界

async def test_execution_after_planning_probe_ttl_keeps_evidence(actor_client, session):
    """审批慢于探测 TTL：计划仍在有效期内，成功执行不许被降成 partial。

    旧行为：执行时把规划期探测喂给内核的 `record`，内核按探测派生 succeeded
    并拒绝"过期探测记成新成功"，except 分支把 entry 重置成空行，于是真实成功
    的执行落 `partial/probe_receipt_missing`、证据被丢、任务 failed。
    """
    _, version, _, _, evidence_rows = await indexed_source(session)
    await publish_collection(actor_client, version, key="stale-probe-collection")
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(actor_client, spec=task_spec(scope="site_public"),
                                 consent=consent)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body)
    probe_ids = [ref for step in plan_body["steps"] for ref in step.get("probe_refs") or []]
    assert probe_ids, "本地目标规划时必须留下探测回执"
    # 用户过了 PROBE_TTL_SECONDS 才提交（计划有效期是 SCOPE_TTL_SECONDS=900）。
    assert federation.PROBE_TTL_SECONDS < federation_tasks.SCOPE_TTL_SECONDS
    for probe_id in probe_ids:
        row = await session.get(FederationProbe, probe_id)
        stored = json.loads(json.dumps(row.result_json))
        observed = datetime.fromisoformat(stored["result"]["observed_at"])
        stored["result"]["observed_at"] = (
            observed - timedelta(seconds=federation.PROBE_TTL_SECONDS + 60)).isoformat()
        # 规划期看到的索引修订与执行期实际检索的不同（期间重建过索引）。
        stored["result"]["retrieval"]["index_revision"] = "planning-time-revision"
        row.result_json = stored
    await session.commit()

    executed = await submit_task(actor_client, root, plan_body["plan_digest"], "stale-probe")
    body = executed.json()
    assert body["status"] == "succeeded", body
    assert [item["evidence_id"] for item in body["result"]["evidence"]] \
        == [evidence_rows[0].id], "成功执行的证据不许因探测回执过期被丢掉"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert [(entry["state"], entry["last_error"]) for entry in coverage["entries"]] \
        == [("succeeded", None)]
    assert coverage["entries"][0]["probe_receipts"] == probe_ids
    # 覆盖账本记"实际检索的是哪一版"：执行报了修订就以执行为准，不沿用规划期的。
    execution = await session.scalar(select(FederationExecution).where(
        FederationExecution.root_task_id == root))
    executed_revision = (execution.result_json.get("result") or {}).get("index_revision")
    assert executed_revision and executed_revision != "planning-time-revision"
    assert coverage["entries"][0]["actual_index_revision"] == executed_revision


async def test_stored_excerpts_trust_the_plan_executor_not_the_probe_row(session):
    """resume 路径从探测行取正文：本地来源按计划的执行者判，不按行里的 target。

    远端探测行的 `target_node_id` 取自对端回执。坏对端把它报成协调者自己、
    再塞一个超长 `_excerpt`：按行判来源就会把它当本地正文静默截断进 prompt，
    绕过对端越界必须显式拒绝的规则（N6）。本地行的长块照常截到契约上限。
    """
    actor = Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")
    now = utcnow()

    def probe_row(probe_id, evidence):
        return FederationProbe(
            probe_id=probe_id, organization_id=ORG, actor_id=ACTOR,
            target_node_id=NODE,               # 远端那一行也"自称"是本节点
            task_spec_digest="sha256:" + "1" * 64, consent_ref="explore-1",
            probe_kind="evidence_retrieval", collection_id="c", query_digest="",
            state="succeeded", result_json={"result": {}, "evidence": evidence},
            expires_at=now + timedelta(minutes=5), created_at=now)

    session.add_all([
        probe_row("probe-local-long", [{"evidence_id": "local-1", "_excerpt": "L" * 5000}]),
        probe_row("probe-peer-spoof", [{"evidence_id": "peer-1", "_excerpt": "P" * 5000,
                                        "excerpt": "p" * 3000}]),
    ])
    await session.commit()
    plan = {"steps": [
        {"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
         "probe_refs": ["probe-local-long"]},
        {"step_id": "retrieve-2", "operation": "retrieve", "executor_node_id": PEER_NODE,
         "probe_refs": ["probe-peer-spoof"]},
    ]}
    excerpts = await federation_tasks._load_excerpts(session, actor, plan)
    assert excerpts["local-1"] == "L" * federation.EVIDENCE_EXCERPT_CHARS
    assert excerpts["peer-1"] == "p" * 3000, "对端正文原样交给越界检查，不许被当成本地截断"
    assert federation.excerpt_reason(excerpts["peer-1"]) == "excerpt_over_contract_bound"


def _single_attempt_plans(monkeypatch):
    """协调任务只给一次机会：模拟崩溃时一轮 drain 就落终态，dedupe 键随之释放。"""
    from ddp_corpus import queue as queue_module

    real_enqueue = queue_module.enqueue

    async def _enqueue_once(session_, **kwargs):
        task = await real_enqueue(session_, **kwargs)
        if task is not None and task.kind == "federation_plan":
            task.max_attempts = 1
        return task

    monkeypatch.setattr(queue_module, "enqueue", _enqueue_once)


async def crash_before_ledger(actor_client, session, monkeypatch, *, root, plan_digest, key):
    """受理并跑到落账之前崩溃；清扫把卡住的协调任务落 failed。返回崩溃后状态。

    崩溃点选在 `_answer_result`：取数目标已经真实受理（执行者侧有回执），
    覆盖账本却还一行都没写 —— 这正是 resume 最难对账的形状。
    """
    _single_attempt_plans(monkeypatch)
    real_answer = federation_tasks._answer_result

    async def _die(*_args, **_kwargs):
        raise RuntimeError("worker died before the coverage ledger was written")

    monkeypatch.setattr(federation_tasks, "_answer_result", _die)
    submitted = await actor_client.post("/api/v1/tasks", headers={"Idempotency-Key": key},
                                        json={"root_task_id": root, "plan_digest": plan_digest})
    assert submitted.status_code == 202, submitted.text
    await drain_tasks(corpus_app.state)
    monkeypatch.setattr(federation_tasks, "_answer_result", real_answer)
    assert await session.scalar(select(func.count()).select_from(CoverageEntry).where(
        CoverageEntry.root_task_id == root)) == 0, "崩溃点必须在落账之前"
    assert await federation_tasks.mark_stalled(session, root, now=utcnow())
    await session.commit()
    return (await actor_client.get(f"/api/v1/tasks/{root}")).json()


async def test_resume_after_crash_before_ledger_fills_fast_denominator(
        actor_client, monkeypatch, session):
    """fast 模式里没被选中的目标在"落账前崩溃 -> resume"时也要进分母。

    旧行为：`retry_only=True` 跳过分母补齐，按全量目标取 entries 直接
    KeyError —— 不是 APIError/ApplicationError，队列里重试，行一直 running。
    """
    monkeypatch.setattr(federation_tasks, "FAST_CANDIDATE_LIMIT", 1)
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE),
                               member("peer-collection-2", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="fast",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    assert len([step for step in plan_body["steps"] if step["operation"] == "retrieve"]) == 1
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    crashed = await crash_before_ledger(actor_client, session, monkeypatch, root=root,
                                        plan_digest=plan_body["plan_digest"],
                                        key="crash-fast")
    assert crashed["status"] == "failed" and crashed["error"] == "coordinator_stalled"

    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert status["status"] == "succeeded", status
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["counts"]["total_targets"] == 2
    states = sorted((entry["state"], entry["last_error"]) for entry in coverage["entries"])
    assert states == [("not_attempted", "search_mode_fast"), ("succeeded", None)]


class SlowExecutorPeer(ReconcilingStubPeer):
    """执行者跑得比协调者的轮询耐心慢；`/cancel` 与真实执行者一样落终态。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hold = True
        self.cancelled: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        inner = super().transport()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/tasks/" not in path:
                return inner.handle_request(request)
            self.calls.append((request.method, path, request.headers.get("Idempotency-Key")))
            executor = path.split("/tasks/", 1)[1].split("/", 1)[0]
            if path.endswith("/cancel"):
                self.cancelled.add(executor)
            state = ("cancelled" if executor in self.cancelled
                     else "running" if self.hold else "succeeded")
            return httpx.Response(200, json={
                **peer_status(), "executor_task_id": executor, "state": state,
                "evidence_set_ref": (f"federation-execution:{executor}"
                                     if state == "succeeded" else None)})

        return httpx.MockTransport(handler)


async def test_remote_poll_timeout_stays_retryable_on_resume(actor_client, monkeypatch):
    """远端执行超过轮询耐心：记 unreachable，resume 对账到同一条执行接着等。

    旧行为：超时先 `cancel` 对端执行。resume 的对账拿回同一张回执，轮询到
    cancelled -> `not_attempted`；换代次重新受理又是同键异体 409 —— 这个
    标着"可重做"的目标永远重做不了。
    """
    monkeypatch.setattr(federation_tasks, "PEER_POLL_DEADLINE_SECONDS", 0.0)
    peer = SlowExecutorPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    first = (await submit_task(actor_client, root, plan_body["plan_digest"], "slow-peer")
             ).json()
    assert first["status"] == "failed"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert [(entry["state"], entry["last_error"]) for entry in coverage["entries"]] \
        == [("unreachable", "peer_execution_timeout")]
    assert not peer.cancelled, "轮询超时不得取消对端执行"

    peer.hold = False
    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    status = (await actor_client.get(f"/api/v1/tasks/{root}")).json()
    assert status["status"] == "succeeded", status
    assert status["result"]["counts"]["succeeded"] == 1
    assert len(peer.receipts) == 1, "resume 必须对账到同一条受理，不得新造"
    assert len(peer.admissions) == 1


# ------------------------------------------------------------------ 交付

async def test_ack_wrong_digest_and_expired_delivery_cannot_confirm(
        actor_client, session):
    run = await run_local_task(actor_client, session, key="ack-task")
    status, result = run["status"], run["status"]["result"]
    wrong = await ack_delivery(actor_client, status["delivery_id"], "sha256:" + "0" * 64,
                               "ack-wrong")
    assert wrong.status_code == 409
    assert wrong.json()["error"]["code"] == "input_not_verified"

    await session.execute(update(FederationDelivery).where(
        FederationDelivery.delivery_id == status["delivery_id"]).values(
        expires_at=utcnow() - timedelta(seconds=1)))
    await session.commit()
    expired = await ack_delivery(actor_client, status["delivery_id"],
                                 result["result_manifest_digest"], "ack-late")
    assert expired.status_code == 200
    assert expired.json()["state"] == "expired" and expired.json()["verified_at"] is None
    replay = await ack_delivery(actor_client, status["delivery_id"],
                                result["result_manifest_digest"], "ack-late")
    assert replay.json() == expired.json()
    row = await session.get(FederationDelivery, status["delivery_id"])
    assert row.state == "expired"


async def test_ack_is_owner_scoped_like_delivery_read(actor_client, session):
    """同组织的另一个成员拿到 delivery_id + 摘要，也不许替别人确认交付。

    旧行为：`ack_delivery` 只核组织，不核发起人 —— 读取端点对他 404，
    确认端点却把别人的交付钉成 confirmed（顺带绕过 TTL 过期）。
    """
    run = await run_local_task(actor_client, session, key="ack-owner")
    status, result = run["status"], run["status"]["result"]
    intruder = actor_headers("actor-mallory")
    read = await actor_client.get(f"/api/v1/deliveries/{status['delivery_id']}",
                                  headers=intruder)
    assert read.status_code == 404
    stolen = await actor_client.post(
        f"/api/v1/deliveries/{status['delivery_id']}/ack",
        headers={**intruder, "Idempotency-Key": "ack-stolen"},
        json={"result_manifest_digest": result["result_manifest_digest"]})
    assert stolen.status_code == 404, stolen.text
    assert stolen.json()["error"]["code"] == read.json()["error"]["code"] \
        == "delivery_not_found", "越权与不存在同形，不给出存在性探测口"
    row = await session.get(FederationDelivery, status["delivery_id"], populate_existing=True)
    assert row.state == "pending" and row.verified_at is None, "越权确认不得落任何写入"

    # 管理员仍可代管；发起人自己当然可以确认。
    owner = await ack_delivery(actor_client, status["delivery_id"],
                               result["result_manifest_digest"], "ack-owner-ok")
    assert owner.status_code == 200 and owner.json()["state"] == "confirmed"


async def test_delivery_read_returns_verifiable_bounded_result(actor_client, session):
    """GET /deliveries/{id}：客户端重算 content_digest(result) 能对上声明的摘要。"""
    run = await run_local_task(actor_client, session, key="delivery-read")
    status, result = run["status"], run["status"]["result"]
    response = await actor_client.get(f"/api/v1/deliveries/{status['delivery_id']}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store", "交付字节不得被缓存"
    body = response.json()
    assert {"delivery_id", "root_task_id", "state", "result_manifest_digest",
            "result", "expires_at"} <= set(body)
    assert body["delivery_id"] == status["delivery_id"]
    assert body["root_task_id"] == run["root"]
    assert body["state"] == "pending"
    assert body["result_manifest_digest"] == result["result_manifest_digest"]
    assert plans.digest(body["result"]) == body["result_manifest_digest"], \
        "客户端重算 content_digest(canonical result) 必须能对上声明的摘要"
    assert "result_manifest_digest" not in body["result"], \
        "摘要是响应顶层字段，不在文档里自我引用"
    for item in body["result"]["evidence"]:
        assert "excerpt" not in item and "_excerpt" not in item, \
            "交付文档有界且不含正文/源文件字节"
    assert body["expires_at"]


async def test_delivery_read_expires_at_ttl_and_never_claims_saved(actor_client, session):
    run = await run_local_task(actor_client, session, key="delivery-ttl")
    delivery_id = run["status"]["delivery_id"]
    await session.execute(update(FederationDelivery).where(
        FederationDelivery.delivery_id == delivery_id).values(
        expires_at=utcnow() - timedelta(seconds=1)))
    await session.commit()

    response = await actor_client.get(f"/api/v1/deliveries/{delivery_id}")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "delivery_expired"
    row = await session.get(FederationDelivery, delivery_id, populate_existing=True)
    assert row.state == "expired"
    task = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert task["delivery_state"] == "expired"
    # 幂等：再过一次还是 410，不会"复活"成可确认。
    again = await actor_client.get(f"/api/v1/deliveries/{delivery_id}")
    assert again.status_code == 410


async def test_delivery_read_is_invisible_to_other_actors(actor_client, session):
    run = await run_local_task(actor_client, session, key="delivery-visibility")
    visible = await actor_client.get(f"/api/v1/deliveries/{run['status']['delivery_id']}")
    assert visible.status_code == 200
    other = await actor_client.get(f"/api/v1/deliveries/{run['status']['delivery_id']}",
                                   headers=headers("bob"))
    assert other.status_code == 404
    unknown = await actor_client.get("/api/v1/deliveries/no-such-delivery")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "delivery_not_found"


async def test_oversized_delivery_document_is_not_persisted_or_truncated(
        actor_client, session, monkeypatch):
    monkeypatch.setattr(federation_tasks, "DELIVERY_RESULT_MAX_BYTES", 64)
    run = await run_local_task(actor_client, session, key="delivery-oversize")
    status, result = run["status"], run["status"]["result"]
    body = (await actor_client.get(f"/api/v1/deliveries/{status['delivery_id']}")).json()
    assert body["result"] is None, "超界不截断、也不存半截"
    assert body["result_manifest_digest"] == result["result_manifest_digest"]
    ack = await ack_delivery(actor_client, status["delivery_id"],
                             result["result_manifest_digest"], "ack-oversize")
    assert ack.status_code == 409, "没有可校验字节就不许确认"
    assert ack.json()["error"]["code"] == "input_not_verified"


# ------------------------------------------------------------------ 边界与拒绝

async def test_fixed_resources_wait_for_input_and_stay_in_denominator(
        actor_client, session, monkeypatch):
    from ddp_corpus import capabilities

    async def _no_gateway(_http):
        return None

    monkeypatch.setattr(capabilities, "_fetch_gateway", _no_gateway)
    resource, _, _, _, _ = await indexed_source(session)
    spec = task_spec(scope="fixed_resources", mode="fast", resource_refs=[resource.id])
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(actor_client, spec=spec, consent=consent)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body)
    response = await submit_task(actor_client, root, plan_body["plan_digest"], "fixed")
    assert response.status_code == 200, response.text
    body = response.json()
    # 本切片执行者无法本地重算资源内容摘要 -> waiting_input，而不是假装验过。
    assert body["status"] == "failed"
    assert body["result"]["evidence"] == []
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["counts"]["total_targets"] == 1
    assert coverage["entries"][0]["state"] == "not_attempted"
    assert coverage["entries"][0]["last_error"] == "waiting_input"


async def test_expired_scope_is_410_and_probe_budget_exhaustion_is_visible(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                            budget={"max_probe_requests": 0, "max_egress_bytes": 0}),
        manifest=manifest)
    root = intent["root_task_id"]
    # 探测预算为 0：远端目标不探测（也未联系对端）。
    await plan_task(actor_client, root)
    assert peer.calls == []
    # manifest 过期 -> 410 scope_expired，不拿旧授权继续。
    row = await session.get(FederationRequest, root)
    stale = dict(row.scope_manifest_json, valid_until="2020-01-01T00:00:00Z")
    stale["manifest_digest"] = plans.digest(
        {key: value for key, value in stale.items() if key != "manifest_digest"})
    row.scope_manifest_json = stale
    row.plan_json = None
    row.planning_state = "draft"
    await session.commit()
    expired = await actor_client.post("/api/v1/task-plans", json={"root_task_id": root})
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "scope_expired"


async def test_execution_consent_must_cover_every_edge_and_executor(
        actor_client, session, monkeypatch):
    peer = StubPeer()
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public",
                                     mode="exhaustive_scope", scope_ref="scope-1"),
        consent=exploration(), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    # 接收方少了远端执行者。
    weak = execution_consent(plan_body["plan_digest"], recipients=(NODE,),
                             edges=[edge["edge_id"] for edge in plan_body["data_edges"]])
    response = await actor_client.post(f"/api/v1/task-plans/{root}/approve",
                                       json={"plan_digest": plan_body["plan_digest"],
                                             "execution_consent": weak})
    assert response.status_code == 403 and response.json()["error"]["code"] == "egress_denied"
    # 数据边没被批准。
    no_edges = execution_consent(plan_body["plan_digest"], recipients=(NODE, PEER_NODE))
    response = await actor_client.post(f"/api/v1/task-plans/{root}/approve",
                                       json={"plan_digest": plan_body["plan_digest"],
                                             "execution_consent": no_edges})
    assert response.status_code == 403 and response.json()["error"]["code"] == "egress_denied"
    # 未批准计划不得受理。
    denied = await submit_task(actor_client, root, plan_body["plan_digest"], "not-approved")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "egress_denied"


# ------------------------------------------------------------------ 证据集端点

async def test_evidence_set_endpoint_reauthorizes_and_bounds(actor_client, session):
    run = await run_local_task(actor_client, session, key="evidence-set")
    probe = await session.scalar(select(FederationProbe).where(
        FederationProbe.organization_id == ORG).execution_options(populate_existing=True))
    set_ref = probe.result_json["result"]["retrieval"]["evidence_set_ref"]
    assert set_ref
    response = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "ddp-evidence/1#EvidenceSet" and body["complete"] is True
    assert [item["evidence_id"] for item in body["items"]] == [run["evidence_rows"][0].id]

    execution = await session.scalar(select(FederationExecution).where(
        FederationExecution.root_task_id == run["root"]))
    execution_set = await actor_client.get(
        f"{BASE}/evidence-sets/{execution.evidence_set_ref}", headers=headers())
    assert execution_set.status_code == 200, execution_set.text
    assert [item["evidence_id"] for item in execution_set.json()["items"]] \
        == [run["evidence_rows"][0].id]

    forged = await actor_client.get(f"{BASE}/evidence-sets/federation-probe:forged",
                                    headers=headers())
    assert forged.status_code == 404
    other_actor = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}",
                                         headers=headers("bob"))
    assert other_actor.status_code == 404
    other_org = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}",
                                       headers=headers(org="other-org"))
    assert other_org.status_code == 404

    await session.execute(update(FederationProbe).where(
        FederationProbe.probe_id == probe.probe_id).values(
        expires_at=utcnow() - timedelta(seconds=1)))
    await session.commit()
    expired = await actor_client.get(f"{BASE}/evidence-sets/{set_ref}", headers=headers())
    assert expired.status_code == 410


# ------------------------------------------------------------------ 本人任务列表

LOCAL_EXPLORATION = dict(egress="local_only", recipients=(), payload=(),
                         budget={"max_probe_requests": 0, "max_egress_bytes": 0})


async def _intent_as(client, headers, query, *, key):
    body = {"task_spec": task_spec(scope="site_public", mode="fast", query=query),
            "exploration_consent": exploration(**LOCAL_EXPLORATION)}
    response = await client.post("/api/v1/task-intents", json=body,
                                 headers={**headers, "Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()["root_task_id"]


async def _set_created(session, root_task_id, at):
    await session.execute(update(FederationRequest).where(
        FederationRequest.root_task_id == root_task_id).values(created_at=at))
    await session.commit()


def validate_task_list_contract(page: dict) -> None:
    from jsonschema import Draft202012Validator
    from test_federation_admissions import FEDERATION_TASKS_SPEC
    Draft202012Validator({"$ref": "#/components/schemas/TaskListPage",
                          "components": FEDERATION_TASKS_SPEC["components"]}).validate(page)


async def test_task_list_shows_only_my_tasks_newest_first_with_state_axes(actor_client, session):
    """列表只给本人：同组织别人、别的组织、管理员自己的列表都互不串。"""
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    mine = []
    for index, query in enumerate(["第一问", "第二问", "第三问"]):
        root = await _intent_as(actor_client, actor_headers(), query, key=f"list-mine-{index}")
        await _set_created(session, root, base + timedelta(minutes=index))
        mine.append(root)
    other_member = await _intent_as(actor_client, actor_headers("actor-mallory"), "别人的问题",
                                    key="list-mallory")
    other_org = await _intent_as(actor_client, actor_headers(organization_id="org-other"),
                                 "别的组织", key="list-other-org")
    admin_root = await _intent_as(actor_client, actor_headers("actor-admin", role="admin"),
                                  "管理员自己的", key="list-admin")

    response = await actor_client.get("/api/v1/tasks")
    assert response.status_code == 200, response.text
    page = response.json()
    validate_task_list_contract(page)
    assert [item["root_task_id"] for item in page["items"]] == list(reversed(mine))
    assert page["next_cursor"] is None
    newest = page["items"][0]
    assert newest["query"] == "第三问" and newest["scope_kind"] == "site_public"
    assert newest["operation"] == "rag.answer.cited" and newest["search_mode"] == "fast"
    assert (newest["status"], newest["planning_state"], newest["retrieval_completeness"],
            newest["evidence_sufficiency"], newest["delivery_state"]) == (
        "queued", "draft", "not_started", "unknown", "not_requested")
    assert not {"result", "exploration_consent", "task_spec"} & set(newest), \
        "列表不带结果与许可原文"

    listed = set()
    for someone in (actor_headers("actor-mallory"), actor_headers(organization_id="org-other"),
                    actor_headers("actor-admin", role="admin")):
        other = (await actor_client.get("/api/v1/tasks", headers=someone)).json()
        listed |= {item["root_task_id"] for item in other["items"]}
        assert not set(mine) & {item["root_task_id"] for item in other["items"]}
    assert listed == {other_member, other_org, admin_root}, "每个人只看得到自己的那一条"


async def test_task_list_pages_by_cursor_without_repeats_when_new_tasks_arrive(
        actor_client, session):
    base = datetime(2026, 9, 2, 8, 0, 0, 123457, tzinfo=timezone.utc)
    roots = []
    for index in range(5):
        root = await _intent_as(actor_client, actor_headers(), f"问题 {index}", key=f"page-{index}")
        # 第 2、3 条同一时刻，而且正好跨第一页边界（limit=2 时第一页最后一条是其中之一）：
        # 键集翻页必须靠 root_task_id 决出先后，否则另一条会被 `created_at <` 跳过。
        # 间隔只有一微秒：游标的时刻精度差一微秒就会漏行或重复。
        await _set_created(session, root, base + timedelta(microseconds=(0, 1, 2, 2, 3)[index]))
        roots.append(root)
    first = (await actor_client.get("/api/v1/tasks", params={"limit": 2})).json()
    validate_task_list_contract(first)
    assert len(first["items"]) == 2 and first["next_cursor"]

    # 两页之间来了一条更新的任务：下一页不许因此重复上一页的内容。
    await _intent_as(actor_client, actor_headers(), "翻页途中的新问题", key="page-late")
    seen = [item["root_task_id"] for item in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = (await actor_client.get("/api/v1/tasks", params={"limit": 2, "cursor": cursor})).json()
        validate_task_list_contract(page)
        seen += [item["root_task_id"] for item in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 5, "翻页不重复、不遗漏"
    assert set(seen) == set(roots)
    stamps = [(await session.get(FederationRequest, root, populate_existing=True)) for root in seen]
    keys = [(as_aware(row.created_at), row.root_task_id) for row in stamps]
    assert keys == sorted(keys, reverse=True), "创建时间倒序，同一时刻按 id 倒序"


@pytest.mark.parametrize("cursor", [
    "not-base64-%%%",
    "W10",                                   # base64url("[]")
    "WyJ4IiwieSJd",                          # ["x","y"]：时刻不是整数
    "WzEsIiJd",                              # [1,""]：空 id
    "WzEsIlx1ZDgwMCJd",                      # [1,"\ud800"]：孤立代理字符（SQLite 上曾是 500）
    "WzEsIlx1MDAwMCJd",                      # [1,"\u0000"]：NUL（PostgreSQL 上曾是 500）
    "WzEsImEiXQ%%%",                         # 合法游标后面跟垃圾：不许被静默丢掉后接受
    "WzEsImEiXQ+/",                          # [1,"a"] 后面多出的字节：JSON 解析失败
])
async def test_task_list_rejects_a_tampered_cursor_instead_of_restarting(actor_client, cursor):
    response = await actor_client.get("/api/v1/tasks", params={"cursor": cursor})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", [0, 101])
async def test_task_list_limit_is_bounded(actor_client, limit):
    assert (await actor_client.get("/api/v1/tasks", params={"limit": limit})).status_code == 422


async def test_task_list_last_full_page_has_no_dangling_cursor(actor_client, session):
    """条数恰好是 limit 的整数倍：最后一页的 next_cursor 必须是 null，不多给一个空页。"""
    base = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    for index in range(4):
        root = await _intent_as(actor_client, actor_headers(), f"整页 {index}", key=f"full-{index}")
        await _set_created(session, root, base + timedelta(seconds=index))
    first = (await actor_client.get("/api/v1/tasks", params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (await actor_client.get("/api/v1/tasks", params={
        "limit": 2, "cursor": first["next_cursor"]})).json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None


async def test_task_list_follows_the_person_not_the_credential(actor_client):
    """同一个人用 Web 会话与 API key 建的任务出现在同一张列表里（按 principal 认人）。"""
    web_root = await _intent_as(actor_client, actor_headers(), "网页上问的", key="principal-web")
    api_key = {**actor_headers(), "X-DDP-Actor": "apikey-42", "X-DDP-Actor-Kind": "api_key",
               "X-DDP-User": ACTOR, "X-DDP-Api-Key": "apikey-42"}
    key_root = await _intent_as(actor_client, api_key, "脚本里问的", key="principal-key")
    for credential in (actor_headers(), api_key):
        page = (await actor_client.get("/api/v1/tasks", headers=credential)).json()
        assert {web_root, key_root} <= {item["root_task_id"] for item in page["items"]}


def _cursor(micros, root_task_id):
    raw = json.dumps([micros, root_task_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def test_task_list_cursor_id_is_bounded_by_the_column_width(actor_client):
    """游标里的 id 不超过 root_task_id 的列宽（64）：65 位是 400，不是一次注定查空的查询。"""
    assert (await actor_client.get("/api/v1/tasks", params={
        "cursor": _cursor(1, "a" * 64)})).status_code == 200
    too_long = await actor_client.get("/api/v1/tasks", params={"cursor": _cursor(1, "a" * 65)})
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "invalid_cursor"


def test_task_event_types_match_the_contract_both_ways():
    """`_EVENT_*` 常量与契约 `task_event_type` 一一对应：多一个界面没有文案，少一个契约里是空话。"""
    from ddp_contracts.enums import TASK_EVENT_TYPE_VALUES

    produced = {value for name, value in vars(federation_tasks).items()
                if name.startswith("_EVENT_") and isinstance(value, str)}
    assert produced == set(TASK_EVENT_TYPE_VALUES)


async def test_undeclared_event_type_is_refused_before_it_is_stored(session):
    with pytest.raises(ValueError):
        await federation_tasks._append_event(session, "root-x", "task_exploded", {}, now=utcnow())


# ------------------------------------------------ 协调者 operation 是闭集（改进项 ② 的 G1）

@pytest.mark.parametrize("operation", ["wiki.pages", "rag.answer", "corpus.Retrieve", "", "回答"])
async def test_unknown_coordinator_operation_is_refused_before_anything_is_stored(
        actor_client, session, operation):
    """认不出来的 operation 当场拒绝：放进去不会报错，只会被顺手配一个 answer 步。"""
    body = {"task_spec": task_spec(scope="site_public", mode="fast",
                                   operation=operation or "rag.answer.cited"),
            "exploration_consent": exploration(**LOCAL_EXPLORATION)}
    body["task_spec"]["operation"] = operation
    key = "bad-op-" + hashlib.sha256(operation.encode("utf-8")).hexdigest()[:16]
    response = await actor_client.post("/api/v1/task-intents", json=body,
                                       headers={"Idempotency-Key": key})
    # 空串先被内核的形状校验挡掉（409 invalid_plan）；其余在闭集这一关 400。
    assert response.status_code in (400, 409, 422), response.text
    if response.status_code == 400:
        assert response.json()["error"]["code"] == "capability_unsupported"
    assert await session.scalar(select(func.count()).select_from(FederationRequest)) == 0


async def test_retrieve_only_task_never_gets_an_answer_step_or_a_generation_budget(
        actor_client, session, monkeypatch):
    """`corpus.retrieve` 是"只取证据"：即使本地生成就绪，也不配 answer 步、不留生成预算。

    旧行为：规划不看 operation，本地模型就绪就追加 answer 步 —— 用户要的是证据，
    系统白跑一次生成，还把生成结果当成这次任务的答案。
    """
    async def _ready(_http, *, now):
        return True

    monkeypatch.setattr(federation_tasks, "_generation_available", _ready)
    resource, _, _, _, _ = await indexed_source(session)
    spec = task_spec(scope="fixed_resources", mode="fast", operation="corpus.retrieve",
                     resource_refs=[resource.id])
    intent = await create_intent(actor_client, spec=spec, consent=exploration(**LOCAL_EXPLORATION),
                                 key="retrieve-only")
    plan_body = await plan_task(actor_client, intent["root_task_id"])
    assert "answer" not in [step["operation"] for step in plan_body["steps"]]
    assert plan_body["budget"]["max_generation_tokens"] == 0

    await approve_task(actor_client, intent["root_task_id"], plan_body)
    status = (await submit_task(actor_client, intent["root_task_id"],
                                plan_body["plan_digest"], "retrieve-only-exec")).json()
    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] is None, "没要答案就不是'没有模型'，不该编一个原因"
    assert status["evidence_sufficiency"] in ("sufficient_by_policy", "insufficient"), status
    assert result["counts"]["total_targets"] >= 1, "目标照常进覆盖账本"

