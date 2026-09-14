import copy
import io
import json
import stat

import httpx
import pytest

from ddp_core.application.plans import task_plan_digest, utc_instant
from ddp_core.application.ports import ApplicationError
from ddp_local.consents import ConsentStore
from ddp_local.http import create_app
from ddp_local.runtime import LocalRuntime
from plan_samples import NOW, IDENTITY, plan_scope


@pytest.fixture
def ledger(tmp_path):
    store = ConsentStore(tmp_path, local_node_id="local-env", clock=lambda: NOW, input_resolver=lambda ref: b"approved file")
    yield store
    store.close()


def approve(ledger, phase="execution", scope=None):
    prepared = ledger.prepare(IDENTITY, scope or plan_scope(), operation_key="prepare")
    return ledger.approve(IDENTITY, prepared["plan_id"], phase=phase, confirmed_scope_digest=prepared["scope_digest"], user_confirmed=True, operation_key="approve-" + phase)


def dispatch_args(prepared, phase="execution"):
    scope = prepared["scope"]
    return dict(phase=phase, payload_id=phase + "-query", recipient_node_id="center-a", payload=scope["task_spec"]["query"].encode(),
                operation_key="dispatch-1", confirmed_scope_digest=prepared["scope_digest"], current_spec=scope["task_spec"], current_plan=scope["plan"],
                input_bytes={"input-1": b"approved file"}, output_location="local:workspace-a", retention="temporary")


def test_prepare_is_not_consent_and_model_cannot_sign(ledger):
    prepared = ledger.prepare(IDENTITY, plan_scope(), operation_key="prepare")
    assert prepared["consents"] == {} and prepared["admission_state"] == "not_submitted"
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **dispatch_args(prepared))
    assert exc.value.code == "consent_required"
    with pytest.raises(ApplicationError):
        ledger.approve(IDENTITY, "plan-1", phase="execution", confirmed_scope_digest=prepared["scope_digest"], user_confirmed=False, operation_key="model-suggestion")
    scope = plan_scope()
    scope["plan"]["planning_state"] = "approved"
    scope["plan"]["execution_consent_ref"] = "model-created"
    with pytest.raises(ApplicationError):
        ledger.prepare(IDENTITY, scope, operation_key="model-plan")


def test_persistent_identity_isolation_and_idempotency(tmp_path):
    ledger = ConsentStore(tmp_path, local_node_id="local-env", clock=lambda: NOW, input_resolver=lambda ref: b"approved file")
    prepared = approve(ledger)
    assert stat.S_IMODE((tmp_path / "consents.sqlite3").stat().st_mode) == 0o600
    ledger.close()
    ledger = ConsentStore(tmp_path, local_node_id="local-env", clock=lambda: NOW, input_resolver=lambda ref: b"approved file")
    try:
        assert ledger.get(IDENTITY, "plan-1")["consents"] == prepared["consents"]
        for field in IDENTITY:
            other = {**IDENTITY, field: "different-identity"}
            with pytest.raises(ApplicationError) as exc:
                ledger.get(other, "plan-1")
            assert exc.value.code == "not_found"
        assert ledger.prepare(IDENTITY, plan_scope(), operation_key="prepare")["plan_id"] == "plan-1"
        changed = plan_scope()
        changed["task_spec"]["query"] = "private new question"
        with pytest.raises(ApplicationError) as exc:
            ledger.prepare(IDENTITY, changed, operation_key="prepare")
        assert exc.value.code == "idempotency_conflict"
        other = {**IDENTITY, "subject": "bob"}
        assert ledger.prepare(other, changed, operation_key="prepare")["scope_digest"] != prepared["scope_digest"]
    finally:
        ledger.close()


@pytest.mark.parametrize("change,code", [("receiver", "policy_denied"), ("payload", "input_changed"), ("input", "input_changed"), ("plan", "plan_changed"), ("query", "plan_changed"), ("output", "policy_denied"), ("retention", "policy_denied"), ("local_only", "local_only"), ("invalidated", "plan_changed")])
def test_dispatch_rechecks_exact_approved_boundary(ledger, change, code):
    prepared = approve(ledger)
    args = copy.deepcopy(dispatch_args(prepared))
    if change == "receiver":
        args["recipient_node_id"] = "node-new"
    elif change == "payload":
        args["payload"] += b" private attachment"
    elif change == "input":
        args["input_bytes"]["input-1"] = b"modified file"
    elif change == "plan":
        args["current_plan"]["budget"]["max_bytes"] += 1
        args["current_plan"]["plan_digest"] = task_plan_digest(args["current_plan"])
    elif change == "query":
        args["current_spec"]["query"] = "new query"
    elif change == "output":
        args["output_location"] = "remote:public"
    elif change == "retention":
        args["retention"] = "persistent"
    elif change == "invalidated":
        args["current_plan"]["planning_state"] = "invalidated"
    else:
        args["local_only"] = True
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == code
    assert ledger.db.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0


def test_expiry_revocation_and_retry_never_reuse_stale_grant(ledger):
    prepared = approve(ledger)
    args = dispatch_args(prepared)
    assert ledger.authorize_dispatch(IDENTITY, "plan-1", **args) == args["payload"]
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "dispatch_already_reserved"
    args["discovery"] = True
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "idempotency_conflict"
    ledger.revoke(IDENTITY, "plan-1", operation_key="revoke")
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "consent_revoked"
    with pytest.raises(ApplicationError):
        ledger.approve(IDENTITY, "plan-1", phase="execution", confirmed_scope_digest=prepared["scope_digest"], user_confirmed=True, operation_key="approve-execution")


def test_expiry_and_root_probe_budget_are_atomic(ledger):
    scope = plan_scope()
    scope["plan"]["valid_until"] = utc_instant(NOW + 30)
    prepared = approve(ledger, "exploration", scope)
    args = dispatch_args(prepared, "exploration")
    ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    args["operation_key"] = "second-probe"
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "budget_exceeded"
    ledger.clock = lambda: NOW + 31
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "consent_expired"
    assert ledger.get(IDENTITY, "plan-1")["planning_state"] == "invalidated"


@pytest.mark.asyncio
async def test_http_fixed_prepare_approve_get_revoke(tmp_path):
    runtime = LocalRuntime(tmp_path)
    app = create_app(runtime, session_token="a" * 32, allowed_hosts={"127.0.0.1:8123"}, start_worker=False)
    identity = {"environment_id": runtime.store.environment_id, "workspace_id": runtime.store.workspace_id, "subject": "workspace:" + runtime.store.workspace_id}
    scope = plan_scope(runtime.store.environment_id, runtime.store.workspace_id)
    scope["input_manifest"] = []
    scope["plan"]["steps"][0]["fixed_inputs"] = []
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8123", headers={"Authorization": "Bearer " + "a" * 32}) as client:
            prepared = await client.post("/api/v1/plans/prepare", json=scope, headers={"Idempotency-Key": "prepare"})
            assert prepared.status_code == 201, prepared.text
            body = prepared.json()
            assert body["consents"] == {}
            repeated = await client.post("/api/v1/plans/prepare", json=scope, headers={"Idempotency-Key": "prepare"})
            assert repeated.status_code == 201 and repeated.json()["scope_digest"] == body["scope_digest"]
            grant = {"phase": "execution", "confirmed_scope_digest": body["scope_digest"], "user_confirmed": True}
            wrong = await client.post("/api/v1/plans/plan-1/approve", json={**grant, "granted_by": "model"}, headers={"Idempotency-Key": "model"})
            assert wrong.status_code == 422
            approved = await client.post("/api/v1/plans/plan-1/approve", json=grant, headers={"Idempotency-Key": "approve"})
            assert approved.status_code == 200, approved.text
            assert approved.json()["consents"]["execution"]["granted_by"] == identity["subject"]
            assert (await client.get("/api/v1/plans/plan-1")).json()["planning_state"] == "approved"
            revoked = await client.post("/api/v1/plans/plan-1/revoke", headers={"Idempotency-Key": "revoke"})
            assert revoked.json()["planning_state"] == "invalidated"
            assert (await client.post("/api/v1/plans/plan-1/approve", json=grant, headers={"Idempotency-Key": "approve"})).status_code == 400
    finally:
        runtime.close()


def test_workspace_local_only_policy_cannot_be_overridden_by_dispatch_flag(ledger):
    prepared = approve(ledger)
    ledger.set_local_only(True)
    args = dispatch_args(prepared)
    args["local_only"] = False
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "consent_revoked"
    ledger.set_local_only(False)
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "consent_revoked"


def test_input_manifest_cannot_self_assert_content_existence(tmp_path):
    store = ConsentStore(tmp_path, local_node_id="local-env", clock=lambda: NOW)
    try:
        with pytest.raises(ApplicationError) as exc:
            store.prepare(IDENTITY, plan_scope(), operation_key="prepare")
        assert exc.value.code == "input_changed"
    finally:
        store.close()


def test_source_policy_revocation_is_rechecked_on_dispatch(ledger):
    prepared = approve(ledger)
    ledger.source_policy_resolver = lambda scope: {"local:local-env": {"source_node_id": "local-env", "allowed_recipients": [], "allowed_payload": [], "allowed_retention": [], "valid_until": scope["plan"]["valid_until"]}}
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **dispatch_args(prepared))
    assert exc.value.code == "policy_denied"


def test_runtime_cannot_relabel_imported_remote_authority_as_local(tmp_path):
    runtime = LocalRuntime(tmp_path)
    try:
        task = runtime.upload_stream(io.BytesIO(b"approved file"), filename="source.pdf", operation_key="import")
        with runtime.store.tx():
            runtime.store.db.execute("UPDATE versions SET source_json=? WHERE id=?", (json.dumps({"authority_node_id": "remote-origin"}), task["version_id"]))
        scope = plan_scope(runtime.store.environment_id, runtime.store.workspace_id)
        scope["input_manifest"][0]["ref"] = task["version_id"]
        scope["plan"]["steps"][0]["fixed_inputs"] = [task["version_id"]]
        identity = {"environment_id": runtime.store.environment_id, "workspace_id": runtime.store.workspace_id, "subject": "workspace:" + runtime.store.workspace_id}
        with pytest.raises(ApplicationError) as exc:
            runtime.consents.prepare(identity, scope, operation_key="prepare")
        assert exc.value.code == "policy_denied"
        assert runtime.consents.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    finally:
        runtime.close()


def test_generation_reservation_is_derived_from_approved_binding(ledger):
    scope = plan_scope()
    scope["plan"]["steps"].append({"step_id": "answer-1", "operation": "answer", "executor_node_id": "center-a", "depends_on": ["retrieve-1"]})
    scope["payload_bindings"][1]["generation_tokens"] = 100
    prepared = approve(ledger, scope=scope)
    args = dispatch_args(prepared)
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args, generation_tokens=0)
    assert exc.value.code == "budget_exceeded"
    ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert ledger.db.execute("SELECT generation_tokens FROM dispatches").fetchone()[0] == 100
    args["operation_key"] = "another-generation"
    with pytest.raises(ApplicationError) as exc:
        ledger.authorize_dispatch(IDENTITY, "plan-1", **args)
    assert exc.value.code == "budget_exceeded"
