"""联邦派发/对账：本地许可门、预算中止、未知结果不重放、交付校验。"""

import io
import json
import os

import httpx
import pytest

from ddp_core.application.plans import canonical_bytes, content_digest, task_plan_digest
from ddp_core.application.ports import ApplicationError
from ddp_local import federation_dispatch as module
from ddp_local.federation_client import CenterConfig, CenterFault, CenterOutcomeUnknown
from ddp_local.http import create_app
from ddp_local.runtime import LocalRuntime
from plan_samples import plan_scope, redigest

SECRET = "center-credential-never-persisted"
FILE_BYTES = b"approved file"
SESSION = "a" * 32


def config():
    return CenterConfig(endpoint="https://center.example", credential=SECRET, timeout_seconds=5.0)


class CenterStub:
    def __init__(self):
        self.plan = None
        self.probes = [{"probe_id": "probe-1", "probe_kind": "capability_input", "status": "succeeded"}]
        self.requests = []
        self.intents = {}
        self.submissions = {}
        self.submit_count = 0
        self.approve_count = 0
        self.ack_count = 0
        self.resume_count = 0
        self.next_resume_plan = None
        self.lose_next_resume = False
        #: ack 端点回执；默认确认。TTL 到期时中心回 200 + {"state": "expired"}。
        self.ack_response = {"state": "confirmed"}
        self.lose_next_submit = False
        self.lose_next_intent = False
        self.task_status = {"root_task_id": "root-1", "status": "running",
                            "planning_state": "approved"}
        self.coverage = {"schema": "ddp-scope-coverage/1#CoverageLedger", "root_task_id": "root-1",
                         "search_mode": "fast", "retrieval_completeness": "partial"}
        #: GET /api/v1/deliveries/{id} 的响应体；None => 404。
        self.delivery = None

    def handler(self, request):
        path, key = request.url.path, request.headers.get("Idempotency-Key")
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "key": key, "body": body,
                              "headers": dict(request.headers)})
        if path == "/api/v1/task-intents":
            # 与中心 `federation_tasks.validate_exploration_consent` 同一判据：
            # TaskSpec 必须引用随附的这份探索许可，否则 403 egress_denied。
            # 旧 stub 不验这一条，于是 App 从没绑过引用也一直是绿的。
            refs = (body.get("task_spec") or {}).get("consent_refs") or {}
            if refs.get("exploration") != (body.get("exploration_consent") or {}).get("consent_id"):
                return httpx.Response(403, json={"error": {
                    "code": "egress_denied",
                    "message": "task spec does not reference this exploration consent"}})
            if key in self.intents:
                stored_body, intent = self.intents[key]
                if stored_body != body:
                    return httpx.Response(409, json={"error": {
                        "code": "idempotency_conflict"}})
                return httpx.Response(200, json=intent)
            intent = {"root_task_id": "root-1", "task_spec_digest": "sha256:" + "a" * 64,
                      "planning_state": "exploring", "status": "queued",
                      "task_spec": body["task_spec"], "exploration_consent": body["exploration_consent"],
                      "created_at": "2026-09-13T00:00:00Z", "updated_at": None}
            self.intents[key] = (body, intent)
            if self.lose_next_intent:
                self.lose_next_intent = False
                raise httpx.ReadTimeout("the response was lost after persistence")
            return httpx.Response(201, json=intent)
        if path == "/api/v1/task-plans":
            return httpx.Response(200, json={**self.plan, "probes": self.probes})
        if path == "/api/v1/task-plans/root-1" and request.method == "GET":
            return httpx.Response(200, json=self.plan)
        if path == "/api/v1/tasks/root-1/resume" and request.method == "POST":
            self.resume_count += 1
            if self.next_resume_plan is not None:
                self.plan = self.next_resume_plan
                self.task_status = {**self.task_status, "status": "queued",
                                    "planning_state": "ready",
                                    "plan_revision": self.plan["revision"],
                                    "plan_digest": self.plan["plan_digest"]}
                self.next_resume_plan = None
            if self.lose_next_resume:
                self.lose_next_resume = False
                raise httpx.ReadTimeout("the resume reply was lost after persistence")
            return httpx.Response(202, json=self.task_status)
        if path == "/api/v1/task-plans/root-1/approve":
            self.approve_count += 1
            return httpx.Response(200, json={**self.plan, "planning_state": "approved",
                                             "execution_consent_ref": "consent-1"})
        if path == "/api/v1/tasks" and request.method == "POST":
            self.submit_count += 1
            replay = key in self.submissions
            self.submissions.setdefault(key, body)
            if self.lose_next_submit:
                self.lose_next_submit = False
                raise httpx.ReadTimeout("the response was lost after acceptance")
            return httpx.Response(200 if replay else 202, json=self.task_status)
        if path == "/api/v1/tasks/root-1":
            return httpx.Response(200, json=self.task_status)
        if path == "/api/v1/tasks/root-1/coverage":
            return httpx.Response(200, json=self.coverage)
        if path == "/api/v1/deliveries/delivery-1" and request.method == "GET":
            if self.delivery is None:
                return httpx.Response(404, json={"error": {"code": "delivery_not_found"}})
            if self.delivery == "expired":
                return httpx.Response(410, json={"error": {"code": "delivery_expired"}})
            return httpx.Response(200, json={
                "delivery_id": "delivery-1", "root_task_id": "root-1",
                "state": self.delivery.get("state", "pending"),
                "result_manifest_digest": self.delivery.get("result_manifest_digest"),
                "result": self.delivery.get("result"),
                "expires_at": "2030-01-01T00:00:00Z"})
        if path == "/api/v1/deliveries/delivery-1/ack":
            self.ack_count += 1
            return httpx.Response(200, json=self.ack_response)
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def runtime(tmp_path):
    instance = LocalRuntime(tmp_path / "workspace")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def wired(monkeypatch):
    stub = CenterStub()
    real = module.CenterFederationClient

    def factory(center, *, transport=None, actor_headers=None, before_send=None):
        return real(center, transport=httpx.MockTransport(stub.handler),
                    actor_headers=actor_headers, before_send=before_send)

    monkeypatch.setattr(module, "CenterFederationClient", factory)
    return stub


def prepare_plan(runtime, *, plan_id="plan-1", approve=("exploration", "execution"), probe_budget=32, request_budget=128):
    source = runtime.upload_stream(io.BytesIO(FILE_BYTES), filename="source.pdf",
                                   operation_key="upload-" + plan_id)
    scope = plan_scope(runtime.store.environment_id, runtime.store.workspace_id)
    scope["plan"]["plan_id"] = plan_id
    scope["task_spec"]["resource_scope"]["resource_refs"] = [source["version_id"]]
    scope["plan"]["steps"][0]["fixed_inputs"] = [source["version_id"]]
    scope["input_manifest"] = [{"ref": source["version_id"], "digest": content_digest(FILE_BYTES),
                                "size_bytes": len(FILE_BYTES)}]
    scope["exploration"]["budget"]["max_probe_requests"] = probe_budget
    scope["plan"]["budget"]["max_requests"] = request_budget
    scope["plan"]["budget"]["max_bytes"] = 4 * 1024 * 1024
    scope["exploration"]["budget"]["max_egress_bytes"] = 65536
    redigest(scope)
    identity = module.federation_identity(runtime)
    view = runtime.consents.prepare(identity, scope, operation_key="prepare-" + plan_id)
    for phase in approve:
        runtime.consents.approve(identity, plan_id, phase=phase,
                                 confirmed_scope_digest=view["scope_digest"],
                                 user_confirmed=True,
                                 operation_key="approve-%s-%s" % (plan_id, phase))
    return view


def status_for(plan_digest, *, status="running", delivery_state="not_requested", result=None,
               delivery_id=None):
    return {"root_task_id": "root-1", "status": status, "planning_state": "approved",
            "plan_revision": 1, "plan_digest": plan_digest, "task_spec_digest": "sha256:" + "a" * 64,
            "search_mode": "fast", "retrieval_completeness": "partial",
            "evidence_sufficiency": "sufficient_by_policy", "execution_consent_ref": "consent-1",
            "coverage_ref": "coverage-1", "delivery_id": delivery_id,
            "delivery_state": delivery_state, "result": result, "error": None, "scope_ref": None,
            "created_at": "2026-09-13T00:00:00Z", "updated_at": "2026-09-13T00:00:00Z"}


async def test_dispatch_without_approved_consent_sends_nothing(runtime, wired):
    prepare_plan(runtime, approve=())
    with pytest.raises(ApplicationError) as exc:
        await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    assert exc.value.code == "consent_required"
    assert wired.requests == []


async def test_execution_without_approval_sends_nothing(runtime, wired):
    prepare_plan(runtime, approve=("exploration",))
    with pytest.raises(ApplicationError) as exc:
        await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    assert exc.value.code == "consent_required"
    assert wired.requests == []


async def test_repeated_status_polls_exhaust_the_actual_http_budget(runtime, wired):
    view = prepare_plan(runtime)
    wired.plan = view["scope"]["plan"]
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    for _ in range(15):
        await module.reconcile(runtime, "plan-1", config())
    sent = len(wired.requests)
    assert sent == 32
    with pytest.raises(ApplicationError) as exc:
        await module.reconcile(runtime, "plan-1", config())
    assert exc.value.code == "budget_exceeded"
    assert len(wired.requests) == sent


async def test_resume_lost_reply_reads_new_revision_without_approving_or_resubmitting(runtime, wired):
    view = prepare_plan(runtime)
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(wired.plan["plan_digest"])
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    wired.task_status["status"] = "succeeded"
    await module.reconcile(runtime, "plan-1", config())
    revised = {**wired.plan, "revision": 2, "planning_state": "ready",
               "execution_consent_ref": None}
    revised["plan_digest"] = task_plan_digest(revised)
    wired.next_resume_plan = revised
    wired.lose_next_resume = True

    with pytest.raises(CenterOutcomeUnknown):
        await runtime.federation_resume("plan-1", config(), operation_key="continue-1")
    assert runtime.federation_state("plan-1")["state"] == "resume_unknown"
    sent = len(wired.requests)
    await runtime.federation_resume("plan-1", config(), operation_key="continue-1")
    assert len(wired.requests) == sent

    recovered = await runtime.federation_reconcile("plan-1", config())
    assert recovered["state"] == "planned"
    assert recovered["center_plan"]["plan_digest"] == revised["plan_digest"]
    assert recovered["center_plan"]["planning_state"] == "ready"
    assert wired.resume_count == wired.approve_count == wired.submit_count == 1
    assert all(request["method"] == "GET" for request in wired.requests[sent:])
    with pytest.raises(ApplicationError) as exc:
        await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    assert exc.value.code == "plan_changed"
    assert wired.approve_count == wired.submit_count == 1


async def test_exploration_execution_flow_delivery_ack(runtime, wired):
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest)

    explored = await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    assert explored["state"] == "planned"
    assert explored["center_plan_digest"] == plan_digest
    assert explored["probes"] == wired.probes
    assert explored["plan_revision"] == 1
    assert wired.requests[0]["body"]["task_spec"]["query"] == view["scope"]["task_spec"]["query"]
    # 发出的 TaskSpec 引用的就是用户批准的那份探索许可（中心据此放行），
    # 执行引用留空、由中心审批时写回；task_spec_digest 不因引用改变。
    granted = runtime.consents.get(module.federation_identity(runtime), "plan-1")["consents"]
    sent = wired.requests[0]["body"]
    assert sent["task_spec"]["consent_refs"] == {
        "exploration": granted["exploration"]["consent_id"], "execution": None}
    assert sent["exploration_consent"]["consent_id"] == granted["exploration"]["consent_id"]

    submitted = await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    assert submitted["state"] == "submitted"
    assert wired.approve_count == 1 and wired.submit_count == 1
    assert submitted["idempotency_key"].startswith("submit-")

    document = {"schema": "ddp-answer/1", "answer": "page 3"}
    manifest = content_digest(canonical_bytes(document))
    wired.task_status = status_for(plan_digest, status="succeeded", delivery_state="pending",
                                   delivery_id="delivery-1")
    wired.delivery = {"state": "pending", "result_manifest_digest": manifest,
                      "result": document}
    reconciled = await module.reconcile(runtime, "plan-1", config())
    assert reconciled["state"] == "succeeded"
    assert reconciled["coverage"]["search_mode"] == "fast"
    assert reconciled["delivery"]["state"] == "pending"

    # 交付字节必须经新端点下载：GET /api/v1/deliveries/delivery-1。
    fetched = await module.fetch_delivery(runtime, "plan-1", config())
    assert fetched["delivery"]["verified"] is True
    assert fetched["delivery"]["result_manifest_digest"] == manifest
    assert fetched["delivery"]["result"] == document, "校验通过的结果要持久到本地投影"
    assert any(request["path"] == "/api/v1/deliveries/delivery-1"
               and request["method"] == "GET" for request in wired.requests)

    with pytest.raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, "plan-1", "delivery-1", "sha256:" + "d" * 64, config())
    assert exc.value.code == "plan_changed"
    assert wired.ack_count == 0
    pending = module.load_federation_state(runtime, "plan-1")
    assert pending["delivery"]["state"] == "pending"

    confirmed = await module.confirm_delivery(runtime, "plan-1", "delivery-1", manifest, config())
    assert confirmed["delivery"]["state"] == "confirmed"
    assert wired.ack_count == 1
    assert SECRET not in json.dumps(confirmed)


async def test_reconcile_maps_center_cancelled_to_a_terminal_local_state(runtime, wired):
    """中心把任务显式取消：本地投影要落 cancelled，不能停在"执行中"。"""
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest)
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    submitted = await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    assert submitted["state"] == "submitted"

    wired.task_status = status_for(plan_digest, status="cancelled")
    reconciled = await module.reconcile(runtime, "plan-1", config())
    assert reconciled["state"] == "cancelled"
    assert module.load_federation_state(runtime, "plan-1")["state"] == "cancelled"


async def test_ack_expired_response_never_marks_local_confirmed(runtime, wired):
    """中心对过期件回 200 + state=expired：本地必须落 expired，绝不 confirmed。

    旧行为只看 HTTP 200，把中心明确说"已失效"的交付显示成"已保存本地"。
    """
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest, status="succeeded", delivery_state="pending",
                                   delivery_id="delivery-1")
    document = {"schema": "ddp-answer/1", "answer": "page 3"}
    manifest = content_digest(canonical_bytes(document))
    wired.delivery = {"state": "pending", "result_manifest_digest": manifest,
                      "result": document}
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    await module.reconcile(runtime, "plan-1", config())
    fetched = await module.fetch_delivery(runtime, "plan-1", config())
    assert fetched["delivery"]["verified"] is True

    wired.ack_response = {"state": "expired"}
    acked = await module.confirm_delivery(runtime, "plan-1", "delivery-1", manifest, config())
    assert acked["delivery"]["state"] == "expired", "200 + expired 不得落成 confirmed"
    assert acked["delivery"]["verified"] is False
    assert acked["delivery"]["reason"] == "delivery_expired"
    assert wired.ack_count == 1

    # 再确认一次：仍然不许写 confirmed（终态已经不是 pending）。
    with pytest.raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, "plan-1", "delivery-1", manifest, config())
    assert exc.value.code == "plan_changed"
    stored = module.load_federation_state(runtime, "plan-1")
    assert stored["delivery"]["state"] == "expired"
    # 正向路径（回执 state=confirmed）由
    # `test_exploration_execution_flow_delivery_ack` 钉着，这里不重复。


async def test_lost_intent_response_replays_without_duplicate_intent(runtime, wired):
    # Lost intent, replayed intent, and plan creation are three physical calls.
    view = prepare_plan(runtime, probe_budget=3)
    wired.plan = view["scope"]["plan"]
    wired.lose_next_intent = True
    with pytest.raises(CenterOutcomeUnknown):
        await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    unknown = module.load_federation_state(runtime, "plan-1")
    assert unknown["state"] == "explore_unknown" and unknown["root_task_id"] is None
    assert len(wired.intents) == 1

    retried = await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration",
                                         operation_key="retry-intent")
    assert retried["root_task_id"] == "root-1" and retried["state"] == "planned"
    assert len(wired.intents) == 1


async def test_unknown_submit_is_reconciled_without_replay(runtime, wired):
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest)
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")

    wired.lose_next_submit = True
    with pytest.raises(CenterOutcomeUnknown):
        await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    unknown = module.load_federation_state(runtime, "plan-1")
    assert unknown["state"] == "submit_unknown"
    assert unknown["last_error"]["code"] == "outcome_unknown"
    submits = wired.submit_count
    assert submits == 1

    reconciled = await module.reconcile(runtime, "plan-1", config())
    assert reconciled["state"] == "submitted"
    assert wired.submit_count == submits

    retried = await module.dispatch_plan(runtime, "plan-1", config(), phase="execution",
                                         operation_key="retry-after-reconcile")
    assert wired.submit_count == submits + 1
    assert len(wired.submissions) == 1
    assert retried["idempotency_key"] == unknown["idempotency_key"]


async def test_unverified_delivery_cannot_be_confirmed(runtime, wired):
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest, status="succeeded", delivery_state="pending",
                                   delivery_id="delivery-1", result={})
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    await module.reconcile(runtime, "plan-1", config())
    fetched = await module.fetch_delivery(runtime, "plan-1", config())
    assert fetched["delivery"]["verified"] is False
    with pytest.raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, "plan-1", "delivery-1", "sha256:" + "d" * 64, config())
    assert exc.value.code == "plan_changed"
    assert wired.ack_count == 0


async def test_tampered_delivery_result_is_refused_with_explicit_reason(runtime, wired):
    """中心结果被换掉（摘要对不上）-> 不持久、不确认，原因可见。"""
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest, status="succeeded", delivery_state="pending",
                                   delivery_id="delivery-1")
    document = {"schema": "ddp-answer/1", "answer": "trusted page 3"}
    manifest = content_digest(canonical_bytes(document))
    # 同一条交付，返回的字节已被替换：digest 仍声明原值。
    wired.delivery = {"state": "pending", "result_manifest_digest": manifest,
                      "result": {"schema": "ddp-answer/1", "answer": "tampered"}}
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    await module.reconcile(runtime, "plan-1", config())

    fetched = await module.fetch_delivery(runtime, "plan-1", config())
    delivery = fetched["delivery"]
    assert delivery["verified"] is False
    assert delivery["reason"] == "result_manifest_mismatch"
    assert "result" not in delivery, "被篡改的字节绝不落本地"
    assert delivery["received_digest"] != manifest

    with pytest.raises(ApplicationError) as exc:
        await module.confirm_delivery(runtime, "plan-1", "delivery-1", manifest, config())
    assert exc.value.code == "plan_changed"
    assert wired.ack_count == 0
    assert module.load_federation_state(runtime, "plan-1")["delivery"]["state"] == "pending"


async def test_expired_delivery_maps_to_local_expired_state(runtime, wired):
    """中心 410 -> 本地状态 expired + 原因；永远不准再说"已保存本地"。"""
    view = prepare_plan(runtime)
    plan_digest = view["scope"]["plan"]["plan_digest"]
    wired.plan = view["scope"]["plan"]
    wired.task_status = status_for(plan_digest, status="succeeded", delivery_state="pending",
                                   delivery_id="delivery-1")
    wired.delivery = "expired"
    await module.dispatch_plan(runtime, "plan-1", config(), phase="exploration")
    await module.dispatch_plan(runtime, "plan-1", config(), phase="execution")
    await module.reconcile(runtime, "plan-1", config())

    with pytest.raises(CenterFault) as exc:
        await module.fetch_delivery(runtime, "plan-1", config())
    assert (exc.value.code, exc.value.status) == ("delivery_expired", 410)
    state = module.load_federation_state(runtime, "plan-1")
    assert state["delivery"]["state"] == "expired"
    assert state["delivery"]["verified"] is False
    assert state["delivery"]["reason"] == "delivery_expired"

    with pytest.raises(ApplicationError) as refused:
        await module.confirm_delivery(runtime, "plan-1", "delivery-1",
                                      "sha256:" + "d" * 64, config())
    assert refused.value.code == "plan_changed"
    assert wired.ack_count == 0




async def test_center_ref_resolves_from_private_workspace_config(runtime, wired):
    view = prepare_plan(runtime)
    wired.plan = view["scope"]["plan"]
    path = module.workspace_directory(runtime) / "federation-centers.json"
    path.write_text(json.dumps({"file-center": {"endpoint": "https://center.example",
                                                "credential": SECRET}}))
    os.chmod(path, 0o600)
    resolved = module.resolve_center_ref(runtime, "file-center")
    assert resolved.endpoint == "https://center.example"
    state = await module.dispatch_plan(runtime, "plan-1", resolved, phase="exploration",
                                       center_ref="file-center")
    assert state["center_ref"] == "file-center"
    assert SECRET not in json.dumps(state)
    os.chmod(path, 0o644)
    with pytest.raises(ApplicationError) as exc:
        module.resolve_center_ref(runtime, "file-center")
    assert exc.value.code == "policy_denied"


async def test_center_ref_can_come_from_environment(runtime, wired, monkeypatch):
    monkeypatch.setenv(module.CENTER_REFS_ENV, json.dumps({
        "env-center": {"endpoint": "https://env.example", "credential": SECRET}}))
    resolved = module.resolve_center_ref(runtime, "env-center")
    assert resolved.endpoint == "https://env.example"


async def test_local_dispatch_http_endpoints_are_idempotent_and_hide_credentials(runtime, wired):
    view = prepare_plan(runtime)
    wired.plan = view["scope"]["plan"]
    app = create_app(runtime, session_token=SESSION, allowed_hosts={"127.0.0.1:8123"},
                     start_worker=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as client:
        assert (await client.get("/api/v1/plans/plan-1/federation")).status_code == 404
        body = {"center": {"endpoint": "https://center.example", "credential": SECRET},
                "phase": "exploration"}
        headers = {"Idempotency-Key": "dispatch-1", "X-DDP-Actor": "alice"}
        first = await client.post("/api/v1/plans/plan-1/dispatch", json=body, headers=headers)
        assert first.status_code == 200, first.text
        state = first.json()
        assert state["root_task_id"] == "root-1"
        assert SECRET not in first.text
        assert wired.requests[0]["headers"]["x-ddp-actor"] == "alice"
        sent = len(wired.requests)
        replay = await client.post("/api/v1/plans/plan-1/dispatch", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json()["root_task_id"] == "root-1"
        assert len(wired.requests) == sent

        changed = await client.post("/api/v1/plans/plan-1/dispatch",
                                    json={**body, "phase": "execution"}, headers=headers)
        assert changed.status_code == 409

        projection = await client.get("/api/v1/plans/plan-1/federation")
        assert projection.status_code == 200
        assert projection.json()["root_task_id"] == "root-1"
        assert SECRET not in projection.text

        # 内联凭据不落库：不带 center 的 reconcile 拒绝，带 center 才能对账
        refused = await client.post("/api/v1/plans/plan-1/reconcile", json={})
        assert refused.status_code == 400
        reconciled = await client.post("/api/v1/plans/plan-1/reconcile",
                                       json={"center": {"endpoint": "https://center.example",
                                                        "credential": SECRET}})
        assert reconciled.status_code == 200 and reconciled.json()["state"] == "submitted"

    prepare_plan(runtime, plan_id="plan-2", approve=())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123",
                                 headers={"Authorization": "Bearer " + SESSION}) as client:
        sent = len(wired.requests)
        denied = await client.post("/api/v1/plans/plan-2/dispatch", json=body,
                                   headers={"Idempotency-Key": "dispatch-2"})
        assert denied.status_code == 400
        assert len(wired.requests) == sent
