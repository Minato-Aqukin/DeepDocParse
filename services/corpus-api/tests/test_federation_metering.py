"""Federated metering: one usage event per business key (T81 / T58).

Executors record `federated_execution` once per execution that reaches succeeded;
coordinators record `federated_delivery` once per root task that produces a
deliverable result. Both are staged in the transaction that wins the terminal
fence, with an event id derived from the business key, so replays, lost receipts,
resume, generation bumps and cancellation never add a second record. The root
budget ledger keeps counting every physical attempt — the two are separate.
"""
import pytest
from sqlalchemy import func, select

from conftest import drain_tasks
from ddp_corpus import federation
from ddp_corpus.config import settings
from ddp_corpus.federation_models import FederationExecution
from ddp_corpus.models import CorpusOutbox, utcnow
from ddp_corpus.usage import business_event_id
from test_federation_admissions import admission_body, peer, post_admission
from test_federation_probes import NODE, configure_federation
from test_federation_tasks import (
    PEER_NODE,
    ReconcilingStubPeer,
    approve_task,
    create_intent,
    exploration,
    install_peer,
    member,
    plan_task,
    run_local_task,
    scope_manifest,
    submit_task,
    task_spec,
)
from node_credentials_fixture import install
from conftest import ORG
from ddp_corpus.main import app as corpus_app


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    return install(monkeypatch, corpus_app, node_id=NODE, organization_id=ORG)


async def usage_rows(session, kind: str) -> list[CorpusOutbox]:
    rows = (await session.scalars(select(CorpusOutbox).where(
        CorpusOutbox.type == "UsageRecorded").execution_options(populate_existing=True))).all()
    return [row for row in rows if (row.payload or {}).get("kind") == kind]


async def test_lost_admission_reply_meters_one_execution_and_one_delivery(
        actor_client, session, monkeypatch):
    """T81: the reconciled admission is one execution and one delivery, each metered once."""
    real_admit = federation.admit
    calls = {"count": 0}

    async def _admit_then_lose_reply(*args, **kwargs):
        receipt, created = await real_admit(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("response lost after persistence")
        return receipt, created

    monkeypatch.setattr(federation, "admit", _admit_then_lose_reply)
    run = await run_local_task(actor_client, session, key="metering-lost-reply")
    assert run["status"]["status"] == "succeeded", run["status"]
    execution = await session.scalar(select(FederationExecution))
    executions = await usage_rows(session, "federated_execution")
    deliveries = await usage_rows(session, "federated_delivery")
    assert [row.id for row in executions] == [
        business_event_id(f"federation-execution:{execution.executor_task_id}")]
    assert [row.id for row in deliveries] == [
        business_event_id(f"federation-delivery:{run['root']}")]
    assert executions[0].payload["requests"] == 1 and executions[0].payload["pages"] == 0
    assert deliveries[0].payload["actor_id"] == "actor-alice"


async def test_physical_attempts_are_counted_but_metering_stays_once(actor_client, session, monkeypatch):
    """T58: every physical request is prepaid in the root ledger (including the reconcile
    lookup after a lost admission reply); metering records the business outcome once."""
    peer = ReconcilingStubPeer()
    install_peer(monkeypatch, peer)
    consent = exploration(egress="listed_nodes", recipients=(PEER_NODE,),
                          budget={"max_probe_requests": 4, "max_egress_bytes": 1 << 20})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                                     scope_ref="scope-1"),
        consent=consent, manifest=scope_manifest([member("peer-collection-1", PEER_NODE)]))
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    peer.drop_next_admission_response = True
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "metering-t58")).json()
    assert status["status"] == "succeeded", status
    assert len(peer.admissions) == 1 and peer.lookups == 1
    assert status["used_budget"]["requests"] >= len(peer.calls), \
        f"{len(peer.calls)} physical requests reached the peer, ledger shows {status['used_budget']}"
    deliveries = await usage_rows(session, "federated_delivery")
    assert [row.id for row in deliveries] == [business_event_id(f"federation-delivery:{root}")]

    resumed = await actor_client.post(f"/api/v1/tasks/{root}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(corpus_app.state)
    assert len(await usage_rows(session, "federated_delivery")) == 1, \
        "resuming an already delivered root is not a second delivery"


async def test_admission_replay_and_refusals_add_no_usage(client, session, app_state, _peer_auth):
    p = peer(client)
    body = admission_body(key="metering-replay")
    assert (await post_admission(p, body)).status_code == 201
    assert (await post_admission(p, body)).status_code == 200
    await drain_tasks(app_state)
    assert len(await usage_rows(session, "federated_execution")) == 1

    changed = admission_body(key="metering-replay", query="a different query")
    assert (await post_admission(p, changed)).status_code == 409
    waiting = admission_body(
        key="metering-waiting",
        inputs=[{"ref": "input-1", "digest": "sha256:" + "a" * 64, "size_bytes": 10}],
        steps=[{"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
                "depends_on": [], "fixed_inputs": ["input-1"]}])
    response = await post_admission(p, waiting)
    assert response.json()["state"] == "waiting_input"
    await drain_tasks(app_state)
    assert len(await usage_rows(session, "federated_execution")) == 1, \
        "conflicting and waiting_input admissions did no work and are not metered"


async def test_fenced_out_terminal_writers_meter_nothing(client, session, app_state, _peer_auth):
    p = peer(client)
    receipt = (await post_admission(p, admission_body(key="metering-fence"))).json()
    executor_task_id = receipt["executor_task_id"]
    execution = await session.get(FederationExecution, executor_task_id)
    usage = federation._execution_usage(
        federation.Actor(id="peer-x", kind="user", organization_id=ORG, role="contributor"),
        executor_task_id)
    stale = await federation._finish_execution(
        session, executor_task_id, execution.generation + 7, state="succeeded",
        now=utcnow(), usage=usage)
    assert stale is False
    assert await usage_rows(session, "federated_execution") == [], \
        "a writer that lost the generation fence must not meter"

    cancelled = await p.post(f"/api/v1/federation/tasks/{executor_task_id}/cancel",
                             constraints={"root_task_id": "task-1"})
    assert cancelled.status_code == 200, cancelled.text
    execution = await session.get(FederationExecution, executor_task_id,
                                  populate_existing=True)
    late = await federation._finish_execution(
        session, executor_task_id, execution.generation, state="succeeded",
        now=utcnow(), usage=usage)
    assert late is False
    await drain_tasks(app_state)
    assert await usage_rows(session, "federated_execution") == [], \
        "a cancelled execution is never metered, even by a late success"


async def test_failed_root_meters_no_delivery(actor_client, session, monkeypatch):
    async def _explode(*args, **kwargs):
        raise RuntimeError("admission exploded before persistence")

    monkeypatch.setattr(federation, "admit", _explode)
    run = await run_local_task(actor_client, session, key="metering-failed-root")
    assert run["status"]["status"] == "failed"
    assert await usage_rows(session, "federated_delivery") == []
    assert await session.scalar(select(func.count()).select_from(FederationExecution)) == 0
