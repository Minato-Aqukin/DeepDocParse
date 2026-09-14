"""CenterFederationClient 的协议/安全边界：全部走 httpx.MockTransport，无网络。"""

import json
import traceback

import httpx
import pytest

from ddp_core.application.ports import ApplicationError
from ddp_local.federation_client import (
    RESPONSE_BYTES_LIMIT,
    CenterConfig,
    CenterFault,
    CenterFederationClient,
    CenterOutcomeUnknown,
)

SECRET = "center-service-token-never-leaks"
ENDPOINT = "https://center.example"
PLAN_DIGEST = "sha256:" + "b" * 64
TASK_DIGEST = "sha256:" + "a" * 64


def config(**overrides):
    values = {"endpoint": ENDPOINT, "credential": SECRET, "timeout_seconds": 5.0}
    values.update(overrides)
    return CenterConfig(**values)


class StubCenter:
    """最小协调者桩：记录请求、按幂等键去重、可注入一次丢失的响应。"""

    def __init__(self):
        self.requests = []
        self.intents = {}
        self.submissions = {}
        self.submit_count = 0
        self.approve_count = 0
        self.ack_count = 0
        self.lose_next_submit = False
        self.forced_response = None
        self.plan = {
            "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": "plan-1", "revision": 1,
            "plan_digest": PLAN_DIGEST, "task_spec_digest": TASK_DIGEST,
            "root_coordinator_node_id": "center-a", "planning_state": "ready",
            "steps": [], "data_edges": [], "budget": {}, "final_result_writer": "local-x",
            "valid_until": "2030-01-01T00:00:00Z", "execution_consent_ref": None,
        }
        self.task_status = {
            "root_task_id": "root-1", "status": "running", "planning_state": "approved",
            "plan_revision": 1, "plan_digest": PLAN_DIGEST, "task_spec_digest": TASK_DIGEST,
        }

    def handler(self, request):
        path, key = request.url.path, request.headers.get("Idempotency-Key")
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "key": key, "body": body,
                              "headers": dict(request.headers), "params": dict(request.url.params)})
        if self.forced_response is not None:
            response, self.forced_response = self.forced_response, None
            return response
        if path == "/api/v1/task-intents":
            if key in self.intents:
                stored_body, intent = self.intents[key]
                if stored_body != body:
                    return httpx.Response(409, json={"error": {
                        "code": "idempotency_conflict"}})
                return httpx.Response(200, json=intent)
            intent = {"root_task_id": "root-1", "task_spec_digest": TASK_DIGEST,
                      "planning_state": "exploring", "status": "queued",
                      "task_spec": body["task_spec"], "exploration_consent": body["exploration_consent"],
                      "created_at": "2026-09-13T00:00:00Z", "updated_at": None}
            self.intents[key] = (body, intent)
            return httpx.Response(201, json=intent)
        if path == "/api/v1/task-plans":
            return httpx.Response(200, json=self.plan)
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
            return httpx.Response(200, json={"schema": "ddp-scope-coverage/1#CoverageLedger",
                                             "root_task_id": "root-1"})
        if path == "/api/v1/tasks/root-1/events":
            return httpx.Response(200, json={"root_task_id": "root-1", "events": [],
                                             "next_seq": 0, "complete": True})
        if path == "/api/v1/tasks/root-1/resume":
            return httpx.Response(202, json=self.task_status)
        if path == "/api/v1/tasks/root-1/cancel":
            return httpx.Response(200, json={**self.task_status, "status": "failed",
                                             "error": "cancelled"})
        if path == "/api/v1/deliveries/delivery-1" and request.method == "GET":
            return httpx.Response(200, json={
                "delivery_id": "delivery-1", "root_task_id": "root-1", "state": "pending",
                "result_manifest_digest": "sha256:" + "c" * 64,
                "result": {"answer": "page 3"}, "expires_at": None})
        if path == "/api/v1/deliveries/delivery-1/ack":
            self.ack_count += 1
            return httpx.Response(200, json={"state": "confirmed"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


def client_for(stub, **kwargs):
    return CenterFederationClient(config(), transport=httpx.MockTransport(stub.handler), **kwargs)


async def test_exploration_execution_shapes_headers_and_stable_keys():
    stub = StubCenter()
    client = client_for(stub, actor_headers={
        "X-DDP-Actor": "alice", "X-DDP-Organization": "org-1",
        "Authorization": "Bearer forged", "Host": "forged",
    })
    try:
        consent = {"schema": "ddp-task-probe/1#ExplorationConsent", "consent_id": "consent-1"}
        spec = {"schema": "ddp-task-probe/1#TaskSpec", "query": "which page?"}
        first = await client.create_intent(spec, consent, {"scope_ref": "scope-1"})
        replay = await client.create_intent(spec, consent, {"scope_ref": "scope-1"})
        assert first["root_task_id"] == replay["root_task_id"] == "root-1"
        assert len(stub.intents) == 1
        assert stub.requests[1]["key"] == stub.requests[0]["key"]
        assert stub.requests[0]["headers"]["authorization"] == "Bearer " + SECRET
        assert stub.requests[0]["headers"]["x-ddp-actor"] == "alice"
        assert stub.requests[0]["body"]["scope_manifest"] == {"scope_ref": "scope-1"}

        # 同键异实体：真实中心 409 idempotency_conflict，客户端如实上报、不重放。
        with pytest.raises(CenterFault) as conflict:
            await client.create_intent(
                {"schema": "ddp-task-probe/1#TaskSpec", "query": "another question"},
                consent, {"scope_ref": "scope-1"},
                idempotency_key=stub.requests[0]["key"])
        assert conflict.value.status == 409
        assert conflict.value.code == "idempotency_conflict"

        plan = await client.create_plan("root-1")
        assert plan["planning_state"] == "ready"
        await client.approve("root-1", plan["plan_digest"],
                             {"schema": "ddp-plan-admission/1#ExecutionConsent",
                              "plan_digest": plan["plan_digest"]})
        status = await client.submit_task("root-1", plan["plan_digest"], "submit-stable")
        assert status["root_task_id"] == "root-1"
        assert (await client.task("root-1"))["status"] == "running"
        assert (await client.coverage("root-1"))["root_task_id"] == "root-1"
        page = await client.events("root-1", after=3)
        assert page["complete"] is True
        assert stub.requests[-1]["params"] == {"after": "3"}
        assert (await client.resume("root-1"))["status"] == "running"
        assert (await client.cancel("root-1"))["error"] == "cancelled"
        fetched = await client.delivery("delivery-1")
        assert fetched["delivery_id"] == "delivery-1"
        assert fetched["state"] == "pending"
        assert fetched["result_manifest_digest"] == "sha256:" + "c" * 64
        assert fetched["result"] == {"answer": "page 3"}
        assert stub.requests[-1]["method"] == "GET"
        assert stub.requests[-1]["path"] == "/api/v1/deliveries/delivery-1"
        await client.ack_delivery("delivery-1", "sha256:" + "c" * 64)
        assert stub.requests[-1]["path"] == "/api/v1/deliveries/delivery-1/ack"
        assert stub.requests[-1]["body"] == {"result_manifest_digest": "sha256:" + "c" * 64}

        again = await client.submit_task("root-1", plan["plan_digest"], "submit-stable")
        assert again["root_task_id"] == "root-1"
        assert stub.submit_count == 2 and len(stub.submissions) == 1
    finally:
        await client.aclose()


async def test_lost_write_response_is_typed_unknown_and_never_replayed():
    stub = StubCenter()
    stub.lose_next_submit = True
    client = client_for(stub)
    try:
        with pytest.raises(CenterOutcomeUnknown) as exc:
            await client.submit_task("root-1", PLAN_DIGEST, "submit-stable")
        assert exc.value.code == "outcome_unknown" and exc.value.status == 0
        assert exc.value.retryable is False
        assert stub.submit_count == 1
        found = await client.task("root-1")
        assert found["root_task_id"] == "root-1"
        replay = await client.submit_task("root-1", PLAN_DIGEST, "submit-stable")
        assert replay["root_task_id"] == "root-1"
        assert stub.submit_count == 2 and len(stub.submissions) == 1
    finally:
        await client.aclose()


async def test_unreachable_transport_errors_are_separate_classifications():
    def broken(request):
        raise httpx.ConnectError("no route")

    client = CenterFederationClient(config(), transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(CenterFault) as exc:
            await client.task("root-1")
        assert exc.value.code == "unreachable" and exc.value.retryable is True
        with pytest.raises(CenterFault) as exc:
            await client.submit_task("root-1", PLAN_DIGEST, "submit-stable")
        assert exc.value.code == "unreachable" and not isinstance(exc.value, CenterOutcomeUnknown)
    finally:
        await client.aclose()

    def slow(request):
        raise httpx.ReadTimeout("too slow")

    client = CenterFederationClient(config(), transport=httpx.MockTransport(slow))
    try:
        with pytest.raises(CenterFault) as exc:
            await client.task("root-1")
        assert exc.value.code == "transport_error" and exc.value.retryable is True
    finally:
        await client.aclose()


async def test_credential_never_leaks_into_errors_or_repr():
    stub = StubCenter()
    stub.forced_response = httpx.Response(401, json={"error": {"code": "unauthorized"}})
    client = client_for(stub)
    try:
        with pytest.raises(CenterFault) as exc:
            await client.task("root-1")
        blob = str(exc.value) + repr(exc.value) + "".join(traceback.format_exception(exc.value))
        assert SECRET not in blob
    finally:
        await client.aclose()

    def broken(request):
        raise httpx.ReadTimeout("the response was lost")

    client = CenterFederationClient(config(), transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(CenterFault) as exc:
            await client.submit_task("root-1", PLAN_DIGEST, "submit-stable")
        blob = str(exc.value) + repr(exc.value) + "".join(traceback.format_exception(exc.value))
        assert SECRET not in blob
    finally:
        await client.aclose()
    assert SECRET not in repr(config())


@pytest.mark.parametrize("endpoint", [
    "http://center.example",
    "http://localhost:8000",
    "http://127.0.0.2:8000",
    "https://user:pass@center.example",
    "https://center.example/path?x=1",
    "https://center.example/path#frag",
    "https://center.example/",
    "ftp://center.example",
])
def test_endpoint_rejects_unapproved_forms(endpoint):
    with pytest.raises(ApplicationError) as exc:
        CenterConfig(endpoint=endpoint, credential=SECRET)
    assert exc.value.code == "policy_denied"
    with pytest.raises(ApplicationError):
        CenterConfig(endpoint=endpoint, credential=SECRET, allow_loopback=True)


@pytest.mark.parametrize("endpoint,allow_loopback", [
    ("https://center.example", False),
    ("https://center.example:8443/api", False),
    ("http://127.0.0.1:8090", True),
    ("http://[::1]:8090/path", True),
])
def test_endpoint_accepts_https_and_explicit_loopback(endpoint, allow_loopback):
    assert config(endpoint=endpoint, allow_loopback=allow_loopback).endpoint == endpoint


async def test_response_bytes_are_bounded():
    def huge(request):
        return httpx.Response(200, content=b"x" * (RESPONSE_BYTES_LIMIT + 1))

    client = CenterFederationClient(config(), transport=httpx.MockTransport(huge))
    try:
        with pytest.raises(CenterFault) as exc:
            await client.task("root-1")
        assert exc.value.code == "response_too_large"
    finally:
        await client.aclose()


async def test_expired_delivery_is_a_typed_410_fault():
    """未确认且过 TTL：中心回 410 delivery_expired，客户端拿到机器码与状态。"""
    stub = StubCenter()
    client = client_for(stub)
    try:
        stub.forced_response = httpx.Response(
            410, json={"error": {"code": "delivery_expired"}})
        with pytest.raises(CenterFault) as exc:
            await client.delivery("delivery-1")
        assert (exc.value.code, exc.value.status) == ("delivery_expired", 410)
        assert exc.value.retryable is False
        assert stub.requests[-1]["path"] == "/api/v1/deliveries/delivery-1"
    finally:
        await client.aclose()


async def test_center_error_codes_are_preserved():
    stub = StubCenter()
    client = client_for(stub)
    try:
        stub.forced_response = httpx.Response(409, json={"error": {"code": "egress_denied"}})
        with pytest.raises(CenterFault) as exc:
            await client.create_intent({}, {})
        assert (exc.value.code, exc.value.status, exc.value.retryable) == ("egress_denied", 409, False)
        stub.forced_response = httpx.Response(503, content=b"no body")
        with pytest.raises(CenterFault) as exc:
            await client.task("root-1")
        assert exc.value.code == "http_503" and exc.value.retryable is True
    finally:
        await client.aclose()
