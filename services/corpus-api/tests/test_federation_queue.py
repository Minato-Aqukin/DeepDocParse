"""联邦执行队列切片（P5 队列 v3）的验收用例。

这一层要钉死的是"受理即持久、执行可恢复"：

1. `federation.admit` 的受理行、执行行、`federation_execute` 队列任务**同一个
   事务**落库 —— 响应返回时执行还没跑，进程重启后任务还在队列里；
2. `federation_execute` handler 从**持久 payload** 重建 actor 并重新判权，
   组织对不上就拒绝执行（worker 绝不信任网络输入）；
3. 取消是 `task_status=cancelled` 终态：执行行与队列任务一起停，迟到结果写不进去；
4. `POST /tasks` 新受理回 **202**，协调行在 worker 推进前是 running；
5. 回收清扫把过期租约的执行标 `lease_expired`，`resume` 只补做可重做的目标，
   不产生第二条受理；
6. `FEDERATION_EXECUTION_INLINE=true` 的旧行为仍然可用（逃生口，非生产路径）。

认证形态：联邦端点（admission / tasks 读写取消）只认节点凭证，调用方是
受信任的同组织远端节点（`PeerCaller` 现签）；协调者本地接口（`/api/v1/tasks`
等）仍是本地用户身份，不走凭证。
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from conftest import ORG, drain_tasks
from ddp_contracts import TASK_STATUS_VALUES, task_status_label
from ddp_corpus import federation, federation_tasks, reconcile
from ddp_corpus.config import settings
from ddp_corpus.deps import Actor
from ddp_corpus.errors import APIError
from ddp_corpus.federation_models import (
    CoverageEntry,
    CoverageLedger,
    FederationAdmission,
    FederationExecution,
    FederationRequest,
)
from ddp_corpus.main import app
from ddp_corpus.models import Task, new_id, utcnow
from ddp_corpus.queue import is_terminal
from node_credentials_fixture import caller, install
from test_federation_admissions import admission_body, exec_constraints, post_admission
from test_federation_probes import (
    NODE,
    configure_federation,
    indexed_source,
    publish_collection,
)
from test_federation_tasks import (
    approve_task,
    crash_before_ledger,
    create_intent,
    exploration,
    plan_task,
    task_spec,
)

#: 节点/执行者面的取消端点前缀。
BASE_CANCEL = "/api/v1/federation/tasks"


@pytest.fixture(autouse=True)
def _federation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)
    monkeypatch.setattr(settings, "federation_execution_inline", False)


@pytest.fixture
def _peer_auth(monkeypatch, app_state):
    """本节点 NODE；远端调用方是控制面批准的同组织成员，组织取自信任记录。"""
    return install(monkeypatch, app, node_id=NODE, organization_id=ORG)


def peer(client, **over):
    """一个同组织远端节点对本节点的调用方：每次调用现签一张凭证。"""
    return caller(client, audience_node_id=NODE, **over)


# ------------------------------------------------------------------ 队列往返

async def test_admit_persists_execution_and_queue_task_in_one_transaction(
        client, actor_client, session, app_state, _peer_auth):
    """受理返回时执行还在队列上；换一个会话（模拟重启）能看到同一行任务。"""
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    p = peer(client)
    body = admission_body(collection_id=collection["collection_id"])
    response = await post_admission(p, body)
    assert response.status_code == 201, response.text
    receipt = response.json()

    execution = await session.get(FederationExecution, receipt["executor_task_id"])
    assert execution is not None and execution.state == "queued", \
        "受理不等执行：执行行先落 queued"

    # 新会话 = 模拟进程重启后看到的库。任务行必须还在，payload 完整。
    from ddp_corpus.db import get_sessionmaker
    async with get_sessionmaker()() as fresh:
        task = await fresh.scalar(select(Task).where(
            Task.kind == "federation_execute",
            Task.dedupe_key == f"federation-execution:{receipt['executor_task_id']}"))
        assert task is not None, "受理与队列任务必须在同一个事务里提交"
        assert task.status == "queued"
        assert task.payload["executor_task_id"] == receipt["executor_task_id"]
        assert task.payload["actor"]["organization_id"] == "org-test"
        assert task.payload["actor"]["actor_id"] == p.actor_id

    assert await drain_tasks(app_state) >= 1
    execution = await session.get(FederationExecution, receipt["executor_task_id"],
                                  populate_existing=True)
    assert execution.state == "succeeded"
    assert execution.result_json["evidence"][0]["evidence_id"] == evidence_rows[0].id


async def test_worker_rebuilds_actor_from_payload_and_rechecks_organization(
        session, app_state):
    """payload 里的组织对不上受理行时，worker 拒绝执行（重新判权）。"""
    admission_id, task_id = new_id(), new_id()
    session.add_all([
        FederationAdmission(
            admission_id=admission_id, organization_id="org-test", actor_id="actor-alice",
            idempotency_key="worker-org-key", request_digest="sha256:" + "1" * 64,
            plan_digest="sha256:" + "2" * 64, root_task_id="task-org", step_id="retrieve-1",
            delegation_generation=0, issuer_node_id=NODE, executor_node_id=NODE,
            state="accepted", input_validation="content_verified", executor_task_id=task_id,
            verified_input_manifest_digest="sha256:" + "3" * 64,
            effective_policy_ref="policy-1", receipt_json={}, receipt_revision=1,
            created_at=utcnow(), updated_at=utcnow()),
        FederationExecution(
            executor_task_id=task_id, admission_id=admission_id, root_task_id="task-org",
            step_id="retrieve-1", operation="retrieve", state="queued", generation=1,
            result_json={"spec": {"query": "x"}, "result": None},
            created_at=utcnow(), updated_at=utcnow()),
    ])
    await session.commit()

    from ddp_corpus.queue import enqueue
    await enqueue(
        session, kind="federation_execute",
        payload={"executor_task_id": task_id,
                 "actor": {"organization_id": "org-other", "actor_id": "actor-alice",
                           "kind": "user", "role": "contributor",
                           "principal_id": "actor-alice"}},
        dedupe_key=f"federation-execution:{task_id}")
    await session.commit()
    await drain_tasks(app_state)

    row = await session.get(FederationExecution, task_id, populate_existing=True)
    assert row.state == "queued", "组织对不上就不许执行（require_execution 的 404）"
    task = await session.scalar(select(Task).where(Task.kind == "federation_execute"))
    assert task.status != "succeeded", "handler 必须把这次失败留成可见状态"


def test_actor_binding_round_trips_and_rejects_tampering():
    actor = Actor(id="key-1", kind="api_key", organization_id="org-1", role="viewer",
                  user_id="user-9")
    binding = federation.actor_binding(actor)
    assert set(binding) == {"organization_id", "actor_id", "kind", "role", "principal_id"}
    assert binding["principal_id"] == "user-9"
    rebuilt = federation.actor_from_binding(binding)
    assert rebuilt.organization_id == "org-1" and rebuilt.id == "key-1"
    assert rebuilt.principal_id == "user-9"
    with pytest.raises(ValueError):
        federation.actor_from_binding({"organization_id": "org-1"})


async def test_admission_replay_does_not_enqueue_a_second_task(
        client, session, app_state, monkeypatch, _peer_auth):
    calls = {"count": 0}
    real_execute = federation.execute

    async def _counting(*args, **kwargs):
        calls["count"] += 1
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(federation, "execute", _counting)
    p = peer(client)
    body = admission_body(key="queue-replay")
    first = await post_admission(p, body)
    second = await post_admission(p, body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json() == second.json()

    count = await session.scalar(select(func.count()).select_from(Task).where(
        Task.kind == "federation_execute"))
    assert count == 1, "同键重放不得排第二条任务"
    await drain_tasks(app_state)
    assert calls["count"] == 1, "同键重放不得执行第二次"


async def test_admission_is_atomic_with_the_queue_task(
        client, session, monkeypatch, _peer_auth):
    """入队失败 -> 受理整体回滚：绝不允许"受理行在、队列任务不在"的半截状态。

    这正是企业边界 7 要防的形态 —— 已受理却没排队 = 永远停在 queued。
    队列任务与受理/执行行同一个事务，所以这里让入队炸一次，三张表都必须干净。
    """
    from sqlalchemy.exc import IntegrityError

    async def _boom(*_args, **_kwargs):
        raise IntegrityError("INSERT tasks", {}, Exception("queue down"))

    monkeypatch.setattr(federation.queue, "enqueue", _boom)
    response = await post_admission(peer(client), admission_body(key="atomic-fail"))
    assert response.status_code == 409, response.text
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 0
    assert await session.scalar(select(func.count()).select_from(FederationExecution)) == 0
    assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_cancel_queued_execution_cancels_task_and_blocks_execution(
        client, actor_client, session, app_state, _peer_auth):
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    p = peer(client)
    body = admission_body(key="queue-cancel", collection_id=collection["collection_id"])
    created = await post_admission(p, body)
    task_id = created.json()["executor_task_id"]
    queue_task = await session.scalar(select(Task).where(
        Task.dedupe_key == f"federation-execution:{task_id}"))
    assert queue_task is not None and queue_task.status == "queued"
    queue_task_id = queue_task.id

    cancelled = await p.post(f"{BASE_CANCEL}/{task_id}/cancel",
                               constraints=exec_constraints(body))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["error"] == "cancelled"

    queue_task = await session.get(Task, queue_task_id, populate_existing=True)
    assert queue_task.status == "cancelled", "取消必须同时停掉队列任务"
    assert queue_task.dedupe_key is None, "取消腾出幂等键（与 succeed/fail 同口径）"

    await drain_tasks(app_state)
    row = await session.get(FederationExecution, task_id, populate_existing=True)
    assert row.state == "cancelled"
    assert row.result_json.get("result") is None, "被取消的执行绝不许留下结果"


async def test_execution_read_and_cancel_are_actor_scoped(
        client, actor_client, session, _peer_auth):
    """同组织另一个调用者读/取消别人的执行，与不存在同形 404.

    旧行为只按 organization_id 过滤：同组织的 bob 能读到 alice 的执行
    （含证据/答案）并把它取消。绑定取受理时的 actor（`admission.actor_id`），
    协调者用同一身份轮询不受影响。

    凭证形态下"另一个调用者"是同一个可信节点的另一个远端主体（subject 不同，
    组织仍取自信任记录）；越权与不存在同形 404，不给出存在性探测口。
    """
    _, version, *_ = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    owner = peer(client)
    intruder = peer(client, subject="user-intruder")
    body = admission_body(key="actor-scope", collection_id=collection["collection_id"])
    created = await post_admission(owner, body)
    assert created.status_code == 201, created.text
    task_id = created.json()["executor_task_id"]
    scope = exec_constraints(body)

    seen = await owner.get(f"{BASE_CANCEL}/{task_id}", constraints=scope)
    assert seen.status_code == 200

    denied_read = await intruder.get(f"{BASE_CANCEL}/{task_id}", constraints=scope)
    assert denied_read.status_code == 404
    assert denied_read.json()["error"]["code"] == "task_not_found"
    unknown = await intruder.get(f"{BASE_CANCEL}/no-such-execution")
    assert unknown.status_code == 404
    assert unknown.json() == denied_read.json(), "越权与不存在必须同形，不给出存在性探测口"

    denied = await intruder.post(f"{BASE_CANCEL}/{task_id}/cancel", constraints=scope)
    assert denied.status_code == 404
    assert (await session.get(FederationExecution, task_id,
                              populate_existing=True)).state == "queued"

    allowed = await owner.post(f"{BASE_CANCEL}/{task_id}/cancel", constraints=scope)
    assert allowed.status_code == 200 and allowed.json()["state"] == "cancelled"


# ------------------------------------------------------------------ 协调者异步

async def _local_flow(client, session, *, key, mode="fast"):
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(client, version)
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(client, spec=task_spec(scope="site_public", mode=mode),
                                 consent=consent)
    root = intent["root_task_id"]
    plan = await plan_task(client, root)
    await approve_task(client, root, plan)
    return {"root": root, "plan": plan, "collection": collection,
            "evidence_rows": evidence_rows}


async def test_submit_returns_202_and_worker_advances_the_task(
        actor_client, session, app_state):
    run = await _local_flow(actor_client, session, key="queue-async")
    response = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-async"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert response.status_code == 202, response.text
    assert response.json()["status"] in ("queued", "running")
    assert response.json()["result"] is None

    # 受理行落 running、执行还没发生；worker 领取后才推进。
    row = await session.get(FederationRequest, run["root"])
    assert row.status == "running"
    await drain_tasks(app_state)
    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "succeeded"
    assert status["result"]["counts"]["succeeded"] == 1

    replay = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-async"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert replay.status_code == 200, "同键重放是 200 + 权威状态，不是 202"
    assert replay.json() == status


async def test_cancel_before_worker_runs_marks_coordinator_cancelled(
        actor_client, session, app_state):
    """队列模式下取消一个还没被领取的协调任务：终态是 cancelled，不是 failed。"""
    run = await _local_flow(actor_client, session, key="queue-async-cancel")
    submitted = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-async-cancel"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert submitted.status_code == 202, submitted.text

    cancelled = await actor_client.post(f"/api/v1/tasks/{run['root']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    queue_task = await session.scalar(select(Task).where(
        Task.kind == "federation_plan", Task.dedupe_key == f"federation-request:{run['root']}"))
    assert queue_task is None or queue_task.status == "cancelled", \
        "取消必须同时停掉队列里的协调任务"

    await drain_tasks(app_state)
    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "cancelled", "被取消的协调任务不许被 worker 改写"
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["counts"]["total_targets"] == 1
    assert {entry["state"] for entry in coverage["entries"]} == {"not_attempted"}
    # 取消在"还没跑过"时现建覆盖账本行：counts 必须跟逐目标记录一致，
    # 不许留一份空的 `{}`。
    ledger = await session.get(CoverageLedger, run["root"], populate_existing=True)
    assert ledger.counts_json == coverage["counts"]
    assert ledger.retrieval_completeness == coverage["retrieval_completeness"]


async def test_resume_of_cancelled_task_is_409_and_performs_nothing(
        actor_client, session, app_state):
    """取消是显式终态：resume 必须 409 `task_cancelled`，不得复活、不得补做。

    旧行为：resume 只查"有计划/许可"，把 cancelled 行改回 running 再排一次
    `federation_plan`；dedupe 键已被取消腾出，于是 worker 真的把取消掉的
    任务整条跑完并写成 succeeded。
    """
    run = await _local_flow(actor_client, session, key="resume-cancelled")
    submitted = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "resume-cancelled"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert submitted.status_code == 202, submitted.text
    cancelled = await actor_client.post(f"/api/v1/tasks/{run['root']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

    resumed = await actor_client.post(f"/api/v1/tasks/{run['root']}/resume")
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["error"]["code"] == "task_cancelled"
    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "cancelled" and status["error"] == "cancelled"

    # 队列、执行、受理三张表都保持"什么都没发生"：resume 没有重新排队，
    # 更没有产生一次执行或受理。
    await drain_tasks(app_state)
    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "cancelled", "迟到的执行不许把取消翻成 succeeded"
    assert status["result"] is None
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 0
    assert await session.scalar(select(func.count()).select_from(FederationExecution)) == 0

    # 还没提交就取消的任务，也不许被一个新幂等键的 POST /tasks 复活。
    local_only = exploration(egress="local_only", recipients=(), payload=(),
                             budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    other = await create_intent(
        actor_client, spec=task_spec(scope="site_public", mode="fast"),
        consent=local_only, key="resume-cancelled-2")
    other_plan = await plan_task(actor_client, other["root_task_id"])
    await approve_task(actor_client, other["root_task_id"], other_plan)
    await actor_client.post(f"/api/v1/tasks/{other['root_task_id']}/cancel")
    late = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "late-submit"},
        json={"root_task_id": other["root_task_id"],
              "plan_digest": other_plan["plan_digest"]})
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "task_cancelled"
    assert (await actor_client.get(
        f"/api/v1/tasks/{other['root_task_id']}")).json()["status"] == "cancelled"


# ------------------------------------------------------------------ 回收清扫

def _admission_and_execution(*, state, lease_until, task_id=None, admission_id=None):
    task_id = task_id or new_id()
    admission_id = admission_id or new_id()
    admission = FederationAdmission(
        admission_id=admission_id, organization_id="org-test", actor_id="actor-alice",
        idempotency_key=f"key-{admission_id}", request_digest="sha256:" + "1" * 64,
        plan_digest="sha256:" + "2" * 64, root_task_id="task-sweep",
        step_id="retrieve-1", delegation_generation=0, issuer_node_id=NODE,
        executor_node_id=NODE, state="accepted", input_validation="content_verified",
        executor_task_id=task_id, verified_input_manifest_digest="sha256:" + "3" * 64,
        effective_policy_ref="policy-1", receipt_json={}, receipt_revision=1,
        created_at=utcnow(), updated_at=utcnow())
    execution = FederationExecution(
        executor_task_id=task_id, admission_id=admission_id, root_task_id="task-sweep",
        step_id="retrieve-1", operation="retrieve", state=state, generation=3,
        lease_until=lease_until, result_json={"spec": {}, "result": None},
        created_at=utcnow(), updated_at=utcnow())
    return admission, execution


class _RacingSession:
    """真实 session 的代理：在被测函数第一次落到终态写入原语之前，先在
    另一个会话里提交一次并发的 rival 结局（取消）。

    这是把"读与写之间被别人插了一刀"变成确定性的办法：先读后写的实现会在
    读到 running 之后被这刀插入，然后仍然按旧快照写 failed；条件 UPDATE 的
    实现要么在 UPDATE 前被插入（命中 0 行），要么根本不产生这个窗口。
    不对 rollback 下手：取消若发生在读之前，被测函数读到 cancelled 会正确地
    放弃 —— 那样量不到"读与写之间"的竞态。
    """

    def __init__(self, real, rival):
        self._real = real
        self._rival = rival
        self._fired = False

    async def _race(self):
        if not self._fired:
            self._fired = True
            await self._rival()

    async def get(self, *args, **kwargs):
        row = await self._real.get(*args, **kwargs)
        await self._race()
        return row

    async def execute(self, *args, **kwargs):
        await self._race()
        return await self._real.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        await self._race()
        return await self._real.scalar(*args, **kwargs)

    async def commit(self):
        return await self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _running_request(root_task_id: str) -> FederationRequest:
    """一条能被 `cancel` 走通的 running 协调行（无集合目标，取消只落账本）。"""
    old = utcnow() - timedelta(seconds=60)
    return FederationRequest(
        root_task_id=root_task_id, organization_id="org-test", actor_id="actor-alice",
        task_spec_digest="sha256:" + "1" * 64, scope_id="scope-1", search_mode="fast",
        planning_state="approved", status="running",
        task_spec_json={"query": "x", "resource_scope": {"kind": "site_public",
                                                         "resource_refs": []}},
        scope_digest="sha256:" + "2" * 64, created_at=old, updated_at=old)


async def _cancel_in_other_session(root_task_id: str) -> None:
    from ddp_corpus.db import get_sessionmaker

    actor = Actor(id="actor-alice", kind="user", organization_id="org-test",
                  role="contributor")
    async with get_sessionmaker()() as other:
        await federation_tasks.cancel(other, actor, root_task_id, now=utcnow())


async def test_mark_failed_cannot_overwrite_a_concurrent_cancel(session, engine):
    """业务错误抛出与用户取消竞态：终态写入必须让数据库仲裁。

    旧实现先 `session.get` 读 running、再无条件写 failed；取消只要在读与写
    之间提交，一次迟到的失败就会把用户显式取消改写成系统失败。
    """
    root = new_id()
    session.add(_running_request(root))
    await session.commit()

    racing = _RacingSession(session, lambda: _cancel_in_other_session(root))
    won = await federation_tasks._mark_failed(racing, root, now=utcnow(), error="boom")
    await session.commit()
    assert won is False, "取消已经拥有结局，失败写入不许声称赢"
    final = await session.get(FederationRequest, root, populate_existing=True)
    assert final.status == "cancelled" and final.error == "cancelled"


async def test_mark_stalled_cannot_overwrite_a_concurrent_cancel(session, engine):
    """清扫与取消竞态：条件 UPDATE 命中 0 行时 cancelled 原样保留。"""
    root = new_id()
    session.add(_running_request(root))
    await session.commit()

    racing = _RacingSession(session, lambda: _cancel_in_other_session(root))
    marked = await federation_tasks.mark_stalled(racing, root, now=utcnow())
    await session.commit()
    assert marked is False
    final = await session.get(FederationRequest, root, populate_existing=True)
    assert final.status == "cancelled" and final.error == "cancelled"


async def test_sweeper_fails_expired_lease_and_never_touches_terminal_rows(
        session, engine):
    """过期租约 -> failed/lease_expired；succeeded 行原样不动（终态守卫）。"""
    expired = utcnow() - timedelta(seconds=120)
    admission_a, running = _admission_and_execution(state="running", lease_until=expired)
    admission_b, succeeded = _admission_and_execution(state="succeeded", lease_until=expired)
    session.add_all([admission_a, running, admission_b, succeeded])
    await session.commit()

    from ddp_corpus.db import get_sessionmaker
    stats = await reconcile.sweep_federation_once(get_sessionmaker(), now=utcnow())
    assert stats["executions"] == 1, "只有过期租约的活跃行该被清扫"

    running = await session.get(FederationExecution, running.executor_task_id,
                                populate_existing=True)
    assert running.state == "failed" and running.error == "lease_expired"
    assert running.lease_until is None and running.generation == 4, \
        "清扫也要让代次前移：崩溃前那个 worker 的迟到写入不许覆盖这个事实"

    succeeded = await session.get(FederationExecution, succeeded.executor_task_id,
                                  populate_existing=True)
    assert succeeded.state == "succeeded" and succeeded.error is None

    # 再扫一遍是空转（failed 不是活跃状态）。
    assert (await reconcile.sweep_federation_once(get_sessionmaker(),
                                                  now=utcnow()))["executions"] == 0


async def test_sweeper_marks_stuck_coordinator_request_and_keeps_denominator(
        session, engine):
    stuck_id, done_id = new_id(), new_id()
    old = utcnow() - timedelta(seconds=settings.federation_request_stuck_seconds + 60)
    session.add_all([
        FederationRequest(root_task_id=stuck_id, organization_id="org-test",
                          actor_id="actor-alice", task_spec_digest="sha256:" + "1" * 64,
                          scope_id="scope-1", search_mode="fast", planning_state="approved",
                          status="running", created_at=old, updated_at=old),
        FederationRequest(root_task_id=done_id, organization_id="org-test",
                          actor_id="actor-alice", task_spec_digest="sha256:" + "2" * 64,
                          scope_id="scope-1", search_mode="fast", planning_state="approved",
                          status="succeeded", created_at=old, updated_at=old),
    ])
    session.add(CoverageLedger(
        root_task_id=stuck_id, scope_ref="scope-1", search_mode="fast",
        enumeration_state="sealed", retrieval_completeness="partial",
        evidence_sufficiency="insufficient", counts_json={}, manifest_digest="",
        created_at=old, updated_at=old))
    await session.flush()
    session.add(CoverageEntry(
        root_task_id=stuck_id, target_digest="d" * 64,
        target_key_json={"origin_node_id": NODE, "collection_id": "col-1",
                         "operation": "corpus.retrieve"},
        query_digest="sha256:" + "4" * 64, state="in_flight",
        probe_refs_json=[], evidence_refs_json=[], used_budget_json={},
        attempts=1, last_error=None))
    await session.commit()

    from ddp_corpus.db import get_sessionmaker
    stats = await reconcile.sweep_federation_once(get_sessionmaker(), now=utcnow())
    assert stats["requests"] == 1

    stuck = await session.get(FederationRequest, stuck_id, populate_existing=True)
    assert stuck.status == "failed" and stuck.error == "coordinator_stalled"
    done = await session.get(FederationRequest, done_id, populate_existing=True)
    assert done.status == "succeeded", "清扫不许改写终态"

    # **分母保留**：卡死只改任务轴，覆盖账本与逐目标记录原样留着 ——
    # 清扫掉分母等于把"没查完"洗成"没查过"。
    ledger = await session.get(CoverageLedger, stuck_id, populate_existing=True)
    assert ledger is not None
    entries = list(await session.scalars(select(CoverageEntry).where(
        CoverageEntry.root_task_id == stuck_id)))
    assert [entry.state for entry in entries] == ["in_flight"]


async def test_resume_redoes_lease_expired_execution_without_second_admission(
        actor_client, session, app_state):
    """清扫留下的 lease_expired 由 resume 补做：同一条受理、同一条执行行。"""
    run = await _local_flow(actor_client, session, key="queue-redo")
    response = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-redo"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert response.status_code == 202
    await drain_tasks(app_state)
    execution = (await session.scalars(select(FederationExecution))).one()
    admissions_before = await session.scalar(
        select(func.count()).select_from(FederationAdmission))
    assert admissions_before == 1

    # 模拟 worker 崩溃后被清扫：执行落 lease_expired，覆盖目标回 failed。
    await session.execute(update(FederationExecution).where(
        FederationExecution.executor_task_id == execution.executor_task_id).values(
        state="failed", error="lease_expired", lease_until=None,
        generation=FederationExecution.generation + 1))
    await session.execute(update(CoverageEntry).where(
        CoverageEntry.root_task_id == run["root"]).values(
        state="failed", last_error="lease_expired"))
    await session.commit()
    stale_generation = execution.generation

    resumed = await actor_client.post(f"/api/v1/tasks/{run['root']}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(app_state)

    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "succeeded"
    assert status["result"]["counts"]["succeeded"] == 1
    assert await session.scalar(select(func.count()).select_from(FederationAdmission)) == 1, \
        "补做不得新造受理"
    row = await session.get(FederationExecution, execution.executor_task_id,
                            populate_existing=True)
    assert row.state == "succeeded" and row.generation > stale_generation


async def test_dead_queue_task_fails_execution_and_resume_reruns_it(
        actor_client, session, app_state, monkeypatch):
    """队列任务重试耗尽死掉后，执行行不许永远停在 queued。

    旧行为：`federation_execute` handler 每次都炸（这里强制 max_attempts=1），
    任务落 failed 并清掉 dedupe_key，执行行却没人管 —— 没有租约，清扫扫不到，
    `retry_execution` 也只认 lease_expired。于是用户看到的是永远 queued。
    现在清扫按 payload 里的 executor_task_id 对账，把执行落成可见的
    `failed/queue_task_failed`，resume 用的补做路径就能重排新代次。
    """
    run = await _local_flow(actor_client, session, key="queue-dead")
    real_execute = federation.execute

    async def _boom(*args, **kwargs):
        raise APIError(500, "forced handler failure", "server_error", "forced_failure")

    monkeypatch.setattr(federation, "execute", _boom)
    # 只给一次机会：执行任务入队时就钉死 max_attempts=1，一轮 drain 落终态失败。
    real_enqueue = federation.queue.enqueue

    async def _enqueue_once(session_, **kwargs):
        task = await real_enqueue(session_, **kwargs)
        if task is not None and task.kind == "federation_execute":
            task.max_attempts = 1
        return task

    monkeypatch.setattr(federation.queue, "enqueue", _enqueue_once)
    submitted = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-dead"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert submitted.status_code == 202, submitted.text
    await drain_tasks(app_state)

    execution = (await session.scalars(select(FederationExecution))).one()
    execution_id = execution.executor_task_id
    stale_generation = execution.generation
    assert execution.state == "queued", "handler 失败本身不写执行行"
    queue_task = (await session.scalars(select(Task).where(
        Task.kind == "federation_execute"))).one()
    assert queue_task.status == "failed", "max_attempts=1 时任务必须落终态"
    assert (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()["status"] == "failed"

    from ddp_corpus.db import get_sessionmaker
    stats = await reconcile.sweep_federation_once(get_sessionmaker(), now=utcnow())
    assert stats["executions"] >= 1
    execution = await session.get(FederationExecution, execution_id,
                                  populate_existing=True)
    assert execution.state == "failed", "死掉的队列任务必须被对账成显式失败"
    assert execution.error == "queue_task_failed"
    assert execution.lease_until is None and execution.generation == stale_generation + 1

    monkeypatch.setattr(federation, "execute", real_execute)
    admissions = await session.scalar(select(func.count()).select_from(FederationAdmission))
    assert admissions == 1
    resumed = await actor_client.post(f"/api/v1/tasks/{run['root']}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(app_state)

    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "succeeded", status
    assert status["result"]["counts"]["succeeded"] == 1
    execution = await session.get(FederationExecution, execution_id,
                                  populate_existing=True)
    assert execution.state == "succeeded"
    assert execution.generation > stale_generation, "补做必须前进代次"
    assert await session.scalar(select(func.count()).select_from(
        FederationAdmission)) == 1, "补做复用同一条受理，不得新造"


async def test_resume_reconciles_target_admitted_before_the_crash(
        actor_client, session, app_state, monkeypatch):
    """上一轮受理了目标、落账前死掉：resume 必须对账到那条受理，而不是按新代次重发。

    旧行为：是否对账看"库里有没有这个目标的 coverage 行"。落账前崩溃时没有，
    于是 resume 用前移后的 delegation_generation 重新受理 —— 同一个业务键、
    请求摘要却变了，执行者只能 409，目标被记成 `failed/idempotency_conflict`，
    明明已经成功的执行拿不回来。
    """
    run = await _local_flow(actor_client, session, key="crash-local")
    crashed = await crash_before_ledger(actor_client, session, monkeypatch, root=run["root"],
                                        plan_digest=run["plan"]["plan_digest"],
                                        key="crash-local")
    assert crashed["status"] == "failed"
    executions = list(await session.scalars(select(FederationExecution).where(
        FederationExecution.root_task_id == run["root"])))
    assert [row.state for row in executions] == ["succeeded"], "崩溃前执行已经跑完"

    resumed = await actor_client.post(f"/api/v1/tasks/{run['root']}/resume")
    assert resumed.status_code == 202, resumed.text
    await drain_tasks(app_state)
    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "succeeded", status
    assert [item["evidence_id"] for item in status["result"]["evidence"]] \
        == [run["evidence_rows"][0].id]
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert [(entry["state"], entry["last_error"]) for entry in coverage["entries"]] \
        == [("succeeded", None)]
    assert await session.scalar(select(func.count()).select_from(FederationAdmission).where(
        FederationAdmission.root_task_id == run["root"])) == 1, "对账复用受理，不得新造"


async def test_sweep_does_not_rekill_an_execution_with_a_fresh_retry_task(
        actor_client, session, app_state, monkeypatch):
    """重排后"旧终态任务 + 新活跃任务"并存：活跃任务必须赢。

    复验反例：`retry_execution` 把执行排回队列并补了一条新任务，但旧 failed
    任务行还在（dedupe_key 已被 fail 清掉）。若清扫优先采用终态行，下一轮
    tick（尤其进程重启后的首轮）就会把刚重试的执行再判死 `queue_task_failed`，
    新任务随后领取只会空转 —— 恢复能力被自己的清扫取消。
    """
    run = await _local_flow(actor_client, session, key="queue-retry-sweep")
    real_execute = federation.execute

    async def _boom(*args, **kwargs):
        raise APIError(500, "forced handler failure", "server_error", "forced_failure")

    monkeypatch.setattr(federation, "execute", _boom)
    real_enqueue = federation.queue.enqueue

    async def _enqueue_once(session_, **kwargs):
        task = await real_enqueue(session_, **kwargs)
        if task is not None and task.kind == "federation_execute":
            task.max_attempts = 1
        return task

    monkeypatch.setattr(federation.queue, "enqueue", _enqueue_once)
    submitted = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-retry-sweep"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert submitted.status_code == 202, submitted.text
    await drain_tasks(app_state)
    execution = (await session.scalars(select(FederationExecution))).one()
    execution_id = execution.executor_task_id

    from ddp_corpus.db import get_sessionmaker
    await reconcile.sweep_federation_once(get_sessionmaker(), now=utcnow())
    execution = await session.get(FederationExecution, execution_id, populate_existing=True)
    assert execution.state == "failed" and execution.error == "queue_task_failed"

    monkeypatch.setattr(federation, "execute", real_execute)
    actor = Actor(id="actor-alice", kind="user", organization_id="org-test",
                  role="contributor")
    assert await federation.retry_execution(session, actor, execution_id, now=utcnow())

    before = (await session.execute(select(Task).where(
        Task.kind == "federation_execute"))).scalars().all()
    assert sorted(task.status for task in before) == ["failed", "queued"], \
        "重排后应看到一条旧终态与一条新活跃任务"

    stats = await reconcile.sweep_federation_once(get_sessionmaker(), now=utcnow())
    execution = await session.get(FederationExecution, execution_id, populate_existing=True)
    assert stats["executions"] == 0, "新活跃任务存在时清扫不得再把执行判死"
    assert execution.state == "queued" and execution.error is None, \
        "刚重试的执行必须保持可执行"

    await drain_tasks(app_state)
    execution = await session.get(FederationExecution, execution_id, populate_existing=True)
    assert execution.state == "succeeded", execution.error
    assert await session.scalar(select(func.count()).select_from(
        FederationAdmission)) == 1, "重排不得新造受理"


async def test_worker_reclaims_a_running_execution_with_expired_lease(
        client, actor_client, session, app_state, _peer_auth):
    """worker 崩溃留下的 running+过期租约：队列任务被重新领取时必须接管执行。

    这是 `federation.execute` 的过期租约接管分支；没有它，崩溃后执行行永远
    停在 running，而队列任务"成功"地空转。
    """
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    created = await post_admission(
        peer(client), admission_body(key="reclaim",
                                     collection_id=collection["collection_id"]))
    task_id = created.json()["executor_task_id"]
    await session.execute(update(FederationExecution).where(
        FederationExecution.executor_task_id == task_id).values(
        state="running", lease_until=utcnow() - timedelta(seconds=1)))
    await session.commit()

    assert await drain_tasks(app_state) >= 1
    row = await session.get(FederationExecution, task_id, populate_existing=True)
    assert row.state == "succeeded", "过期租约必须能被 worker 接管重跑"
    assert row.result_json["evidence"][0]["evidence_id"] == evidence_rows[0].id


async def test_execute_plan_never_overwrites_a_cancelled_row(
        actor_client, session, app_state, monkeypatch):
    """执行中途被取消：迟到的成功结果与账本重写都必须被挡在终态之外。"""
    run = await _local_flow(actor_client, session, key="queue-midway-cancel")

    real_answer = federation_tasks._answer_result

    async def _cancel_midway(*args, **kwargs):
        # 模拟用户在 worker 正在推进时点了取消（另一个会话走真实取消路径）。
        from ddp_corpus.db import get_sessionmaker
        from ddp_corpus.deps import Actor
        async with get_sessionmaker()() as other:
            actor = Actor(id="actor-alice", kind="user", organization_id="org-test",
                          role="contributor")
            await federation_tasks.cancel(other, actor, run["root"], now=utcnow())
        return await real_answer(*args, **kwargs)

    monkeypatch.setattr(federation_tasks, "_answer_result", _cancel_midway)
    submitted = await actor_client.post(
        "/api/v1/tasks", headers={"Idempotency-Key": "queue-midway-cancel"},
        json={"root_task_id": run["root"], "plan_digest": run["plan"]["plan_digest"]})
    assert submitted.status_code == 202, submitted.text
    await drain_tasks(app_state)

    status = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert status["status"] == "cancelled", "迟到的成功不许覆盖显式取消"
    assert status["error"] == "cancelled"
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert {entry["state"] for entry in coverage["entries"]} == {"not_attempted"}, \
        "执行期间的迟到账本重写必须被丢弃，cancel 的 not_attempted 保留"


# ------------------------------------------------------------------ 逃生口

async def test_inline_mode_keeps_request_inline_behaviour(
        client, actor_client, session, monkeypatch, _peer_auth):
    """FEDERATION_EXECUTION_INLINE=true：受理后立刻执行、不排队（旧行为）。"""
    monkeypatch.setattr(settings, "federation_execution_inline", True)
    _, version, _, _, evidence_rows = await indexed_source(session)
    collection = await publish_collection(actor_client, version)
    p = peer(client)
    body = admission_body(key="inline-mode", collection_id=collection["collection_id"])
    receipt = (await post_admission(p, body)).json()
    detail = (await p.get(
        f"/api/v1/federation/tasks/{receipt['executor_task_id']}",
        constraints=exec_constraints(body))).json()
    assert detail["state"] == "succeeded"
    assert await session.scalar(select(func.count()).select_from(Task).where(
        Task.kind == "federation_execute")) == 0, "内联模式不排队"


def test_task_status_contract_exposes_cancelled_with_copy():
    """契约的四件套：值、summary、label、severity 都从 enums.yaml 生成。"""
    assert "cancelled" in TASK_STATUS_VALUES
    assert task_status_label("cancelled") == "已取消"
    assert is_terminal("cancelled") is True
    # 旧值一个都不能少（老任务行仍要能解码）。
    assert {"queued", "claimed", "running", "succeeded", "failed"} <= set(TASK_STATUS_VALUES)
    assert task_status_label("failed") == "失败"
