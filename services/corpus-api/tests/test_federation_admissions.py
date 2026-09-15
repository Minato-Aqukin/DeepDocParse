"""P5 admission endpoints: fail-closed validation, idempotent receipts, inline execution.

Covers the ordered contract checks (plan/consent binding, executor identity,
input verification, idempotency), the waiting-input path that must not take a
GPU, the no-fake-generation discipline (an `answer` step is rejected with
`capability_unsupported` unless this node's generation is actually ready;
positive answer paths live in `test_federation_answer_delegation.py`), and
generation-fenced cancellation.
"""
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import func, select

from conftest import ACTOR, ORG, actor_headers, drain_tasks
from ddp_corpus import federation
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationAdmission, FederationExecution
from ddp_corpus.models import new_id, utcnow
from ddp_core.application.plans import content_digest, task_plan_digest, task_spec_digest
from test_federation_probes import (
    BASE, NODE, configure_federation, headers, indexed_source, publish_collection,
)


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)

EXPIRY = "2030-01-01T00:00:00Z"
SCHEMAS = json.loads((Path(__file__).resolve().parents[3]
                      / "packages/contracts/generated/schemas-resolved.json").read_text())
FEDERATION_TASKS_SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[3]
     / "packages/contracts/openapi/federation-tasks-v1.yaml").read_text(encoding="utf-8"))


def validate_receipt_contract(receipt: dict) -> None:
    schema = SCHEMAS["schemas"]["ddp-plan-admission/v1.json"]
    Draft202012Validator({"$ref": "#/$defs/AdmissionReceipt",
                          "$defs": schema["$defs"]}).validate(receipt)


def validate_execution_status_contract(status: dict) -> None:
    """ExecutionStatus 是 OpenAPI 里的冻结形状；响应必须逐字段对得上。"""
    schema = FEDERATION_TASKS_SPEC["components"]["schemas"]["ExecutionStatus"]
    Draft202012Validator(schema).validate(status)


def admission_body(*, key="admission-key", query="retrieval target", collection_id=None,
                   inputs=None, operation="retrieve", step_id="retrieve-1", steps=None,
                   root_task_id="task-1", consent_id="consent-1", target_node=NODE,
                   execution_mode="center_only"):
    task_spec = {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "rag.answer.cited", "workspace_ref": "workspace-a", "query": query,
        "resource_scope": {"kind": "fixed_resources", "resource_refs": ["resource-1"]},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": execution_mode, "coordinator_ref": NODE},
        "consent_refs": {"exploration": None, "execution": consent_id},
        "budget_ref": "budget-1",
    }
    if steps is None:
        fixed_inputs = ["query"]
        if collection_id:
            fixed_inputs.append(f"collection:{collection_id}")
        steps = [{"step_id": step_id, "operation": operation, "executor_node_id": target_node,
                  "depends_on": [], "fixed_inputs": fixed_inputs}]
    plan = {
        "schema": "ddp-plan-admission/1#TaskPlan", "plan_id": "plan-1", "revision": 1,
        "task_spec_digest": task_spec_digest(task_spec), "root_coordinator_node_id": NODE,
        "planning_state": "approved", "steps": steps, "data_edges": [],
        "execution_consent_ref": consent_id,
        "budget": {"max_requests": 4, "max_bytes": 4096, "max_generation_tokens": 0,
                   "max_hops": 2, "deadline": EXPIRY},
        "final_result_writer": NODE, "valid_until": EXPIRY,
    }
    plan["plan_digest"] = task_plan_digest(plan)
    consent = {
        "schema": "ddp-plan-admission/1#ExecutionConsent", "consent_id": consent_id,
        "plan_digest": plan["plan_digest"], "granted_by": "user-1",
        "granted_at": "2026-01-01T00:00:00Z", "valid_until": EXPIRY,
        "allowed_recipients": [NODE], "allowed_edges": [], "retention": "temporary",
    }
    if inputs is None:
        inputs = [{"ref": "query", "digest": content_digest(query.encode("utf-8")),
                   "size_bytes": len(query.encode("utf-8"))}]
    return {"schema": "ddp-plan-admission/1#AdmissionRequest", "idempotency_key": key,
            "root_task_id": root_task_id, "step_id": step_id, "delegation_generation": 0,
            "task_spec": task_spec, "plan": plan, "execution_consent": consent,
            "inputs": inputs}


async def post_admission(client, body, *, who=ACTOR, key=None, **header_over):
    return await client.post(f"{BASE}/admissions",
        headers={**headers(who, **header_over),
                 "Idempotency-Key": key or body["idempotency_key"]}, json=body)


async def test_admission_accepted_only_with_verified_inputs_and_runs_retrieve(
        actor_client, session, app_state):
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    body = admission_body(collection_id=collection["collection_id"])
    response = await post_admission(actor_client, body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    validate_receipt_contract(receipt)
    assert receipt["state"] == "accepted"
    assert receipt["input_validation"] == "content_verified"
    assert receipt["executor_task_id"] and receipt["verified_input_manifest_digest"]
    assert receipt["accepted_at"] and receipt["issuer_node_id"] == NODE
    assert receipt["executor_node_id"] == NODE

    # 受理返回时执行还排在 `federation_execute` 队列上（受理 201 不等执行），
    # 跑一轮队列后状态才落 succeeded。
    queued = await actor_client.get(f"{BASE}/tasks/{receipt['executor_task_id']}",
                                    headers=headers())
    assert queued.json()["state"] == "queued"
    assert await drain_tasks(app_state) >= 1
    status = await actor_client.get(f"{BASE}/tasks/{receipt['executor_task_id']}",
                                   headers=headers())
    assert status.status_code == 200, status.text
    detail = status.json()
    validate_execution_status_contract(detail)
    assert detail["state"] == "succeeded" and detail["generation"] >= 1
    assert detail["evidence_set_ref"]
    assert detail["internal_limits"] == [] and detail["degraded"] is None
    row = await session.get(FederationExecution, receipt["executor_task_id"])
    assert row.result_json["result"]["collection_id"] == collection["collection_id"]
    assert row.result_json["evidence"][0]["evidence_id"] == evidence_rows[0].id
    assert row.result_json["evidence"][0]["_excerpt"] == "retrieval target text"


async def test_admission_waiting_input_when_content_cannot_be_verified(actor_client, session):
    body = admission_body(
        key="waiting-key",
        inputs=[{"ref": "input-1", "digest": "sha256:" + "a" * 64, "size_bytes": 10}],
        steps=[{"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
                "depends_on": [], "fixed_inputs": ["input-1"]}])
    response = await post_admission(actor_client, body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    validate_receipt_contract(receipt)
    assert receipt["state"] == "waiting_input"
    assert receipt["input_validation"] == "metadata_only"
    assert "executor_task_id" not in receipt
    assert receipt["verified_input_manifest_digest"] is None
    assert receipt["accepted_at"] is None
    # 等待输入不占算力：没有执行行，也没有生成任务。
    count = await session.scalar(select(func.count()).select_from(FederationExecution))
    assert count == 0


async def test_admission_input_digest_mismatch_is_rejected(actor_client):
    body = admission_body(key="mismatch-key")
    body["inputs"] = [{"ref": "query", "digest": "sha256:" + "b" * 64,
                       "size_bytes": len("retrieval target")}]
    response = await post_admission(actor_client, body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "input_changed"


async def test_admission_replay_returns_same_receipt_without_second_execution(
        actor_client, session, app_state, monkeypatch):
    calls = {"count": 0}
    real_execute = federation.execute

    async def _counting(*args, **kwargs):
        calls["count"] += 1
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(federation, "execute", _counting)
    body = admission_body(key="replay-admission")
    first = await post_admission(actor_client, body)
    second = await post_admission(actor_client, body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json() == second.json()
    await drain_tasks(app_state)
    assert calls["count"] == 1, "同键同摘要的重放不得再跑一次执行"


async def test_admission_same_key_different_digest_is_conflict(actor_client):
    body = admission_body(key="conflict-admission")
    assert (await post_admission(actor_client, body)).status_code == 201
    changed = admission_body(key="conflict-admission", query="a different query")
    response = await post_admission(actor_client, changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(("mutate", "code"), [
    # 许可在规划/批准时有效、到受理时已过期：执行者必须当场再查一遍，不信协调者。
    (lambda body: body["execution_consent"].update(valid_until="2026-01-01T00:00:01Z"),
     "consent_expired"),
    # 计划变了（新修订）而许可仍绑旧摘要：不是同一个批准范围。
    (lambda body: body["execution_consent"].update(plan_digest="sha256:" + "d" * 64),
     "plan_changed"),
    # 接收方变了：本节点不在被批准的接收方里。
    (lambda body: body["execution_consent"].update(allowed_recipients=["node-other"]),
     "egress_denied"),
])
async def test_admission_rechecks_stale_or_widened_consent_and_writes_nothing(
        actor_client, session, mutate, code):
    """T79：admission 自己重新检查许可；过期、换计划修订、换接收方都拒绝，不留受理行。"""
    body = admission_body(key=f"stale-consent-{code}-{id(mutate)}")
    mutate(body)
    response = await post_admission(actor_client, body)
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["error"]["code"] == code
    count = await session.scalar(select(func.count()).select_from(FederationAdmission))
    assert count == 0, "被拒的许可不得留下受理行"


async def test_admission_unsupported_operation_is_rejected_not_faked(actor_client, session):
    """生成未就绪（无模型通道）时 `answer` 当场 capability_unsupported，不收单不伪造。"""
    steps = [
        {"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
         "depends_on": [], "fixed_inputs": []},
        {"step_id": "answer-1", "operation": "answer", "executor_node_id": NODE,
         "depends_on": ["retrieve-1"]},
    ]
    body = admission_body(key="answer-key", operation="answer", step_id="answer-1",
                          steps=steps)
    response = await post_admission(actor_client, body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capability_unsupported"
    count = await session.scalar(select(func.count()).select_from(FederationAdmission))
    assert count == 0, "被拒的操作不得留下受理行"


async def test_admission_targeting_another_node_is_wrong_target(actor_client):
    # trusted_federation 允许计划里出现远端执行者；此时本节点必须拒绝别人的 step。
    body = admission_body(key="wrong-node", target_node="node-other",
                          execution_mode="trusted_federation")
    response = await post_admission(actor_client, body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "wrong_target"


async def test_admission_requires_peer_credentials(actor_client):
    body = admission_body(key="no-peer-admission")
    response = await actor_client.post(f"{BASE}/admissions",
        headers={**actor_headers(), "Idempotency-Key": body["idempotency_key"]}, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "peer_unauthenticated"


async def test_disabled_admissions_endpoint_fails_closed(actor_client, monkeypatch):
    """开关关着时端点必须拒绝，不能"能力清单说不接单、端点照样收单"。"""
    monkeypatch.setattr(settings, "federation_admissions_enabled", False)
    response = await post_admission(actor_client, admission_body(key="disabled-key"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "admissions_disabled"


async def test_lookup_returns_receipt_and_unknown_key_is_404(actor_client):
    body = admission_body(key="lookup-key")
    created = await post_admission(actor_client, body)
    assert created.status_code == 201
    found = await actor_client.post(f"{BASE}/admissions/lookup", headers=headers(),
                                    json={"idempotency_key": "lookup-key"})
    assert found.status_code == 200 and found.json() == created.json()
    missing = await actor_client.post(f"{BASE}/admissions/lookup", headers=headers(),
                                      json={"idempotency_key": "nope"})
    assert missing.status_code == 404


def queued_execution(*, task_id, admission_id):
    admission = FederationAdmission(
        admission_id=admission_id, organization_id=ORG, actor_id=ACTOR,
        idempotency_key=f"key-{admission_id}", request_digest="sha256:" + "1" * 64,
        plan_digest="sha256:" + "2" * 64, root_task_id="task-cancel", step_id="retrieve-1",
        delegation_generation=0, issuer_node_id=NODE, executor_node_id=NODE,
        state="accepted", input_validation="content_verified",
        executor_task_id=task_id, verified_input_manifest_digest="sha256:" + "3" * 64,
        effective_policy_ref="policy-1", receipt_json={}, receipt_revision=1,
        created_at=utcnow(), updated_at=utcnow())
    execution = FederationExecution(
        executor_task_id=task_id, admission_id=admission_id, root_task_id="task-cancel",
        step_id="retrieve-1", operation="retrieve", state="queued", generation=1,
        result_json={"spec": {"query": "retrieval target"}, "result": None},
        created_at=utcnow(), updated_at=utcnow())
    return admission, execution


async def test_cancel_is_idempotent_and_generation_fenced(actor_client, session):
    task_id, admission_id = new_id(), new_id()
    admission, execution = queued_execution(task_id=task_id, admission_id=admission_id)
    session.add_all([admission, execution])
    await session.commit()

    first = await actor_client.post(f"{BASE}/tasks/{task_id}/cancel", headers=headers())
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "cancelled" and first.json()["error"] == "cancelled"
    assert first.json()["generation"] == 2

    second = await actor_client.post(f"{BASE}/tasks/{task_id}/cancel", headers=headers())
    assert second.status_code == 200 and second.json() == first.json()

    # 旧代次的完成写不进来：取消是终态，迟到结果不得覆盖。
    stale = await federation._finish_execution(
        session, task_id, 1, state="succeeded", now=utcnow(), result_json={"result": "late"})
    assert stale is False
    # populate_existing：ORM 的同步可能把内存对象改成 SET 值，但库里的行没动。
    row = await session.scalar(select(FederationExecution).where(
        FederationExecution.executor_task_id == task_id).execution_options(populate_existing=True))
    assert row.state == "cancelled" and row.error == "cancelled" and row.generation == 2
    assert row.result_json.get("result") is None


async def test_cancel_does_not_rewrite_a_finished_execution(actor_client, session, app_state):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    created = await post_admission(
        actor_client, admission_body(key="finished-key", collection_id=collection["collection_id"]))
    assert created.status_code == 201
    await drain_tasks(app_state)
    task_id = created.json()["executor_task_id"]
    before = await actor_client.get(f"{BASE}/tasks/{task_id}", headers=headers())
    assert before.json()["state"] == "succeeded"
    after = await actor_client.post(f"{BASE}/tasks/{task_id}/cancel", headers=headers())
    assert after.status_code == 200
    assert after.json()["state"] == "succeeded"
    assert after.json() == before.json()


async def test_late_timeout_never_overwrites_a_committed_success(actor_client, session, app_state):
    """F9：同代次的迟到超时写入不得翻掉已提交的 succeeded。

    旧行为：`_finish_execution` 只按 generation 围栏；execute 提交成功之后
    到达的 TimeoutError 分支用同一个代次写 failed，把刚提交的成功改掉。
    """
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    created = await post_admission(
        actor_client,
        admission_body(key="late-timeout", collection_id=collection["collection_id"]))
    assert created.status_code == 201, created.text
    await drain_tasks(app_state)
    task_id = created.json()["executor_task_id"]
    row = await session.scalar(select(FederationExecution).where(
        FederationExecution.executor_task_id == task_id).execution_options(populate_existing=True))
    assert row.state == "succeeded"
    generation = row.generation

    late = await federation._finish_execution(
        session, task_id, generation, state="failed", now=utcnow(),
        error="execution_timeout")
    assert late is False, "已成功的执行不能被同代次的迟到失败覆盖"
    row = await session.scalar(select(FederationExecution).where(
        FederationExecution.executor_task_id == task_id).execution_options(populate_existing=True))
    assert row.state == "succeeded" and row.error is None
