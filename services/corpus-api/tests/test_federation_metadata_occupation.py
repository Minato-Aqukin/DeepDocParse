"""Metadata-only admission never enters the execution or model resource planes."""
import httpx
import pytest
from sqlalchemy import func, select

from conftest import ORG, drain_tasks
from ddp_core.application.plans import task_plan_digest
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationAdmission, FederationExecution
from ddp_corpus.main import app
from ddp_corpus.models import Task
from node_credentials_fixture import caller, install
from test_federation_admissions import admission_body, post_admission
from test_federation_probes import NODE, configure_federation


@pytest.mark.parametrize("operation", ["retrieve", "answer", "wiki_pages"])
@pytest.mark.parametrize("inline", [False, True])
async def test_metadata_only_admission_has_no_gateway_calls_tasks_or_leases(
        client, session, app_state, monkeypatch, operation, inline):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_execution_inline", inline)
    install(monkeypatch, app, node_id=NODE, organization_id=ORG)
    gateway_calls = []

    def gateway(request):
        gateway_calls.append((request.method, request.url.path))
        return httpx.Response(503, json={"error": "model plane must not be contacted"})

    steps = [{"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
              "depends_on": [], "fixed_inputs": ["pending-file"]}]
    step_id = "retrieve-1"
    if operation != "retrieve":
        step_id = "generate-1"
        predecessor = "retrieve-1"
        if operation == "wiki_pages":
            predecessor = "manifest-1"
            steps.append({"step_id": predecessor, "operation": "source_manifest",
                          "executor_node_id": NODE, "depends_on": ["retrieve-1"]})
        steps.append({"step_id": step_id, "operation": operation, "executor_node_id": NODE,
                      "depends_on": [predecessor], "fixed_inputs": ["pending-file"]})
    body = admission_body(
        key=f"metadata-only-{operation}-{inline}", operation=operation, step_id=step_id,
        inputs=[{"ref": "pending-file", "digest": "sha256:" + "a" * 64,
                 "size_bytes": 1024}], steps=steps)
    body["plan"]["budget"]["max_generation_tokens"] = 256
    body["plan"]["plan_digest"] = task_plan_digest(body["plan"])
    body["execution_consent"]["plan_digest"] = body["plan"]["plan_digest"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http:
        monkeypatch.setattr(app_state, "http", http)
        p = caller(client, audience_node_id=NODE)
        first = await post_admission(p, body)
        assert gateway_calls == [], "metadata inspection cannot query any gateway channel"
        assert first.status_code == 201, first.text
        receipt = first.json()
        assert receipt["state"] == "waiting_input"
        assert receipt["input_validation"] == "metadata_only"
        assert receipt.get("executor_task_id") is None
        assert receipt["verified_input_manifest_digest"] is None
        assert receipt["accepted_at"] is None
        replay = await post_admission(p, body)
        assert replay.status_code == 200 and replay.json() == receipt
        assert await drain_tasks(app_state) == 0
        assert gateway_calls == []

    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1
    # No execution or queue record exists, so no generation fence, lease or slot
    # can be claimed by the normal worker or the inline executor.
    assert await session.scalar(select(func.count()).select_from(FederationExecution)) == 0
    assert await session.scalar(select(func.count()).select_from(Task)) == 0
