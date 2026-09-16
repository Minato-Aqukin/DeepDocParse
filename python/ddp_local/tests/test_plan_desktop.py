"""桌面宿主走的固定计划路径：模板计划、恢复读取、回执、端点绑定与执行前计划一致性。

中心用 httpx.MockTransport 替身，拒绝规则按中心真实端点写（探索许可引用、
ack 回执状态），本地运行时与许可账本都是真的。
"""

import hashlib
import io
import json

import httpx
import pytest

from ddp_core.application.plans import canonical_bytes, content_digest
from ddp_core.application.ports import ApplicationError
from ddp_local import federation_dispatch as module
from ddp_local.federation_client import CenterConfig
from ddp_local.http import create_app
from ddp_local.runtime import LocalRuntime

SECRET = "synthetic-center-credential-for-test"
SESSION = "s" * 32
ENDPOINT = "https://center.example/team"
CENTER = "node-" + "c" * 48
FILE = b"%PDF-1.4 pinned local input"


class Center:
    def __init__(self):
        self.requests = []
        self.plan = None
        self.plan_override = None
        self.ack_state = "confirmed"
        self.lose_next_ack = False
        self.status = {"root_task_id": "root-1", "status": "running", "planning_state": "approved"}
        self.delivery = None

    def handler(self, request):
        path = request.url.path.removeprefix("/team")
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "body": body,
                              "authorization": request.headers.get("authorization"),
                              "url": str(request.url)})
        if path == "/api/v1/task-intents":
            refs = (body["task_spec"].get("consent_refs") or {})
            if refs.get("exploration") != body["exploration_consent"]["consent_id"]:
                return httpx.Response(403, json={"error": {"code": "egress_denied"}})
            return httpx.Response(201, json={"root_task_id": "root-1", "task_spec_digest": "sha256:" + "a" * 64})
        if path == "/api/v1/task-plans":
            return httpx.Response(200, json=self.plan_override or {**self.plan, "probes": []})
        if path == "/api/v1/task-plans/root-1/approve":
            if body["execution_consent"]["plan_digest"] != body["plan_digest"]:
                return httpx.Response(409, json={"error": {"code": "plan_changed"}})
            return httpx.Response(200, json={**self.plan, "planning_state": "approved"})
        if path == "/api/v1/tasks" and request.method == "POST":
            return httpx.Response(202, json=self.status)
        if path == "/api/v1/tasks/root-1":
            return httpx.Response(200, json=self.status)
        if path == "/api/v1/tasks/root-1/coverage":
            return httpx.Response(200, json={"root_task_id": "root-1", "retrieval_completeness": "partial"})
        if path == "/api/v1/deliveries/delivery-1":
            return httpx.Response(200, json={"delivery_id": "delivery-1", "state": "pending", **self.delivery})
        if path == "/api/v1/deliveries/delivery-1/ack":
            if self.lose_next_ack:
                self.lose_next_ack = False
                raise httpx.ReadTimeout("ack reply lost after the center confirmed")
            return httpx.Response(200, json={"state": self.ack_state})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def runtime(tmp_path):
    instance = LocalRuntime(tmp_path / "workspace")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def center(monkeypatch):
    stub = Center()
    real = module.CenterFederationClient

    def factory(config, *, transport=None, actor_headers=None):
        return real(config, transport=httpx.MockTransport(stub.handler), actor_headers=actor_headers)

    monkeypatch.setattr(module, "CenterFederationClient", factory)
    return stub


@pytest.fixture
async def client(runtime):
    app = create_app(runtime, session_token=SESSION, allowed_hosts={"127.0.0.1:8123"}, start_worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as http:
        yield http


def transport(**changes):
    return {"recipient_node_id": CENTER, "environment_id": CENTER, "workspace_id": "org-1",
            "profile_id": "profile-alice", "issuer": CENTER, "subject": "user-alice",
            "endpoint": ENDPOINT, **changes}


def proposal(runtime, **changes):
    source = runtime.upload_stream(io.BytesIO(FILE), filename="manual.pdf", operation_key="upload-manual")
    version = runtime.store.version(source["version_id"])
    body = {"center": transport(), "query": "控制器工作温度是多少？",
            "inputs": [{"ref": version["id"], "digest": "sha256:" + version["source_digest"],
                        "size_bytes": version["size_bytes"]}],
            "retention": "temporary", "valid_seconds": 3600}
    body.update(changes)
    return body


def inline():
    return {"endpoint": ENDPOINT, "credential": SECRET}


async def propose(client, body, key="propose-1"):
    response = await client.post("/api/v1/plans/propose", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()


async def approve(client, view, *phases):
    for phase in phases:
        response = await client.post(f"/api/v1/plans/{view['plan_id']}/approve", headers={"Idempotency-Key": "approve-" + phase},
                                     json={"phase": phase, "confirmed_scope_digest": view["scope_digest"], "user_confirmed": True})
        assert response.status_code == 200, response.text


async def test_proposal_is_a_reviewable_template_that_only_releases_the_query(runtime, client):
    body = proposal(runtime)
    view = await propose(client, body)
    scope = view["scope"]
    assert view["consents"] == {} and view["planning_state"] == "ready"
    payload = body["query"].encode()
    # 实际外发的只有问题文本：两个阶段各一份，接收方就是配对中心，走审阅过的传输绑定。
    assert [(item["phase"], item["payload_kind"], item["recipient_node_id"], item["size_bytes"], item["digest"])
            for item in scope["payload_bindings"]] == [
        ("exploration", "query_text", CENTER, len(payload), content_digest(payload)),
        ("execution", "query_text", CENTER, len(payload), content_digest(payload))]
    assert all(item["transport_ref"] == "center" for item in scope["payload_bindings"])
    assert scope["transport_bindings"] == [{"transport_ref": "center", **transport()}]
    assert SECRET not in json.dumps(view)
    # 本地输入被摘要钉住，但模板里没有任何 source_files 载荷。
    assert scope["input_manifest"] == body["inputs"]
    assert "source_files" not in {edge["payload_kind"] for edge in scope["plan"]["data_edges"]}
    assert scope["output_locations"] == ["local:" + runtime.store.workspace_id]
    assert scope["exploration"]["allowed_recipients"] == [CENTER]

    replay = await propose(client, body)
    assert replay["plan_id"] == view["plan_id"] and replay["scope_digest"] == view["scope_digest"]
    conflict = await client.post("/api/v1/plans/propose", json={**body, "query": "另一个问题"},
                                 headers={"Idempotency-Key": "propose-1"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize("change", [
    {"center": {**transport(), "credential": SECRET}},
    {"center": transport(endpoint="https://center.example/team?next=other")},
    {"center": transport(issuer="node-" + "d" * 48)},
    {"retention": "persistent"},
    {"valid_seconds": 30},
    {"path": "/etc/passwd"},
])
async def test_proposal_rejects_credentials_urls_and_unreviewable_policy(runtime, client, change):
    response = await client.post("/api/v1/plans/propose", json=proposal(runtime, **change),
                                 headers={"Idempotency-Key": "bad-proposal"})
    assert response.status_code in (400, 422), response.text
    assert runtime.consents.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


async def test_proposal_rechecks_the_imported_snapshot(runtime, client):
    body = proposal(runtime)
    body["inputs"][0]["digest"] = "sha256:" + "0" * 64
    response = await client.post("/api/v1/plans/propose", json=body, headers={"Idempotency-Key": "tampered"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "input_changed"


async def test_desktop_path_recovers_from_mirror_and_resolves_every_write_key(runtime, client, center):
    view = await propose(client, proposal(runtime))
    plan_id = view["plan_id"]
    center.plan = view["scope"]["plan"]
    listed = (await client.get("/api/v1/plans")).json()
    assert listed["visible_total"] == 1 and listed["items"][0]["federation"] is None
    assert listed["items"][0]["approved_phases"] == []

    await approve(client, view, "exploration", "execution")
    explored = await client.post(f"/api/v1/plans/{plan_id}/dispatch", headers={"Idempotency-Key": "dispatch-explore"},
                                 json={"center": inline(), "phase": "exploration"})
    assert explored.status_code == 200 and explored.json()["state"] == "planned"
    assert center.requests[0]["authorization"] == "Bearer " + SECRET
    assert center.requests[0]["url"].startswith(ENDPOINT + "/")
    submitted = await client.post(f"/api/v1/plans/{plan_id}/dispatch", headers={"Idempotency-Key": "dispatch-execute"},
                                  json={"center": inline(), "phase": "execution"})
    assert submitted.json()["state"] == "submitted"

    document = {"schema": "ddp-answer/1", "answer": "40 °C", "score": 1.0}
    manifest = content_digest(canonical_bytes(document))
    center.status = {**center.status, "status": "succeeded", "delivery_id": "delivery-1", "delivery_state": "pending"}
    center.delivery = {"result_manifest_digest": manifest, "result": document}
    assert (await client.post(f"/api/v1/plans/{plan_id}/delivery/result")).status_code == 405
    assert (await client.get(f"/api/v1/plans/{plan_id}/delivery/result")).status_code == 404
    reconciled = await client.post(f"/api/v1/plans/{plan_id}/reconcile", json={"center": inline()})
    assert reconciled.json()["state"] == "succeeded"
    fetched = await client.post(f"/api/v1/plans/{plan_id}/delivery/fetch", json={"center": inline()})
    assert fetched.json()["delivery"]["verified"] is True
    raw = await client.get(f"/api/v1/plans/{plan_id}/delivery/result")
    assert raw.status_code == 200
    # 调用方重算的就是这份字节；浮点 1.0 的写法也必须与中心声明摘要时一致。
    assert "sha256:" + hashlib.sha256(raw.content).hexdigest() == manifest

    summary = (await client.get("/api/v1/plans")).json()["items"][0]["federation"]
    assert summary["state"] == "succeeded" and summary["delivery_state"] == "pending"
    assert summary["delivery_verified"] is True and summary["root_task_id"] == "root-1"

    center.lose_next_ack = True
    ack = {"delivery_id": "delivery-1", "result_manifest_digest": manifest, "center": inline()}
    lost = await client.post(f"/api/v1/plans/{plan_id}/delivery/ack", json=ack, headers={"Idempotency-Key": "ack-1"})
    assert lost.status_code == 200 and lost.json()["error"]["code"] == "outcome_unknown"
    assert lost.json()["delivery"]["state"] == "pending"
    assert lost.json()["delivery"]["result"] == document, "确认前本地已校验结果不得被清理"
    acks = sum(item["path"].endswith("/ack") for item in center.requests)
    replay = await client.post(f"/api/v1/plans/{plan_id}/delivery/ack", json=ack, headers={"Idempotency-Key": "ack-1"})
    assert replay.json()["delivery"]["state"] == "pending"
    assert sum(item["path"].endswith("/ack") for item in center.requests) == acks, "同键重放不再发 ack"
    confirmed = await client.post(f"/api/v1/plans/{plan_id}/delivery/ack", json=ack, headers={"Idempotency-Key": "ack-2"})
    assert confirmed.json()["delivery"]["state"] == "confirmed"
    again = await client.post(f"/api/v1/plans/{plan_id}/delivery/ack", json=ack, headers={"Idempotency-Key": "ack-3"})
    assert again.json()["delivery"]["state"] == "confirmed"
    assert sum(item["path"].endswith("/ack") for item in center.requests) == acks + 1

    for key, expected in (("propose-1", "scope_digest"), ("approve-execution", "consents"),
                          ("dispatch-explore", "root_task_id"), ("ack-1", "delivery")):
        receipt = await client.get("/api/v1/client/receipts/" + key)
        assert receipt.status_code == 200 and expected in receipt.json(), key
        assert SECRET not in receipt.text
    assert (await client.get("/api/v1/client/receipts/never-admitted")).status_code == 404
    assert SECRET not in (await client.get("/api/v1/plans")).text


async def test_center_endpoint_must_equal_the_reviewed_transport(runtime, client, center):
    view = await propose(client, proposal(runtime))
    center.plan = view["scope"]["plan"]
    await approve(client, view, "exploration")
    moved = {"endpoint": "https://other.example/team", "credential": SECRET}
    denied = await client.post(f"/api/v1/plans/{view['plan_id']}/dispatch", headers={"Idempotency-Key": "moved"},
                               json={"center": moved, "phase": "exploration"})
    assert denied.status_code == 400 and denied.json()["error"]["code"] == "policy_denied"
    assert center.requests == []
    assert runtime.consents.db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
    ok = await client.post(f"/api/v1/plans/{view['plan_id']}/dispatch", headers={"Idempotency-Key": "reviewed"},
                           json={"center": inline(), "phase": "exploration"})
    assert ok.status_code == 200
    sent = len(center.requests)
    for route in ("reconcile", "delivery/fetch"):
        refused = await client.post(f"/api/v1/plans/{view['plan_id']}/{route}", json={"center": moved})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "policy_denied", route
    assert len(center.requests) == sent


async def test_ledger_compares_the_actual_endpoint_not_the_reviewed_copy(runtime, client):
    """`_current_transport` 以前把 scope 自己的绑定交回账本比较，恒真。"""
    view = await propose(client, proposal(runtime))
    await approve(client, view, "exploration")
    identity = module.federation_identity(runtime)
    view = runtime.consents.get(identity, view["plan_id"])
    moved = CenterConfig(endpoint="https://other.example/team", credential=SECRET)
    with pytest.raises(ApplicationError) as exc:
        module._authorize_bindings(runtime, identity, view["plan_id"], view, view["scope"],
                                   "exploration", "seed", moved)
    assert exc.value.code == "policy_denied"
    reviewed = CenterConfig(endpoint=ENDPOINT, credential=SECRET)
    assert module._authorize_bindings(runtime, identity, view["plan_id"], view, view["scope"],
                                      "exploration", "seed", reviewed)


async def test_execution_refuses_a_center_revision_the_user_never_reviewed(runtime, client, center):
    view = await propose(client, proposal(runtime))
    plan_id = view["plan_id"]
    center.plan = view["scope"]["plan"]
    center.plan_override = {**view["scope"]["plan"], "plan_id": "plan-root-1",
                            "plan_digest": "sha256:" + "e" * 64, "planning_state": "ready"}
    await approve(client, view, "exploration", "execution")
    explored = await client.post(f"/api/v1/plans/{plan_id}/dispatch", headers={"Idempotency-Key": "explore"},
                                 json={"center": inline(), "phase": "exploration"})
    assert explored.json()["center_plan_digest"] == "sha256:" + "e" * 64
    sent = len(center.requests)
    refused = await client.post(f"/api/v1/plans/{plan_id}/dispatch", headers={"Idempotency-Key": "execute"},
                                json={"center": inline(), "phase": "execution"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "plan_changed"
    assert len(center.requests) == sent, "不得把执行许可发给未审阅的中心修订"
    phases = [row[0] for row in runtime.consents.db.execute("SELECT phase FROM dispatches")]
    assert phases == ["exploration"], "执行阶段不得预占预算"
    state = (await client.get(f"/api/v1/plans/{plan_id}/federation")).json()
    assert state["attempts"][-1] == {**state["attempts"][-1], "action": "authorize:execution",
                                     "outcome": "rejected", "code": "plan_changed"}
