"""远端答案委托与执行者 answer 受理：绑定子集、边界、故障对端与许可门。

协调者侧用 `StubPeer`（httpx.MockTransport）只测**出站边界与采纳判据**；执行者
侧用真实的 corpus-api 测试 app + respx mock 的模型通道，测证据摘要重算、越界
拒绝、生成后的结构验收与 `execution_status.answer` 出口。

负向用例覆盖：
- 对端伪造/越界引用绑定 -> 整份答案作废、证据保留；
- 空引用绑定 -> 作废；
- 对端执行 500 -> 显式原因、证据保留；
- 执行许可不覆盖 answer 边 -> egress_denied，一个字节都不发；
- `rag.answer.cited` 未就绪的节点绝不会收到 answer 步骤（能力探测诚实）；
- 证据缺失/摘要不符/空白/越界/超条数 -> waiting_input / input_not_verified，不截断。
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from conftest import SERVICE, drain_tasks
from ddp_core.application.plans import canonical_bytes, content_digest, task_plan_digest
from ddp_corpus import federation_tasks
from ddp_corpus.config import settings
from test_federation_admissions import admission_body
from test_federation_answer import chat_answer, gateway_channel, mock_chat, mock_gateway
from test_federation_probes import (
    BASE,
    NODE,
    configure_federation,
    headers,
)
from test_federation_tasks import (
    PEER_NODE,
    StubPeer,
    approve_task,
    calls_to,
    create_intent,
    execution_consent,
    exploration,
    install_peer,
    member,
    plan_task,
    probe_keys,
    scope_manifest,
    submit_task,
    task_spec,
)

GATEWAY_CAP = f"{SERVICE}/v1/capabilities"


@pytest.fixture(autouse=True)
def _delegation_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)
    monkeypatch.setattr(settings, "chat_url", "")
    monkeypatch.setattr(settings, "chat_model", "")


def mock_gateway_not_ready():
    """协调者本地生成未知：规划必须走远端能力探测。"""
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={
        "capability_status": "unknown", "profiles": [], "model_channels": []}))


def ready_document(*, refs=("peer-evidence-1",), answer="peer cited answer [1]",
                   bindings=True):
    document = {
        "answer": answer, "answer_reason": None,
        "claim_evidence_bindings": [{
            "claim_id": "claim-1", "claim_text": "peer cited answer",
            "evidence_refs": list(refs), "structural_validation": "passed",
            "semantic_review": "needs_review"}] if bindings else [],
        "provider": {"model": "peer-instruct", "endpoint": "https://peer.example",
                     "location": "local"},
        "disclosure": {"remote": False, "payload": ["question", "selected_evidence"]},
        "validation_state": "passed",
    }
    return document


def peer_with_excerpt():
    return StubPeer(items=[{**{
        "schema": "ddp-evidence/1#FederatedEvidence", "evidence_id": "peer-evidence-1",
        "origin_node_id": PEER_NODE, "authority_node_id": PEER_NODE,
        "resource_id": "peer-resource-1", "source_version_id": "peer-version-1",
        "parse_revision": "peer-parse-1", "source_digest": "sha256:" + "a" * 64,
        "excerpt_digest": "sha256:" + "b" * 64,
        "locator": {"kind": "page_block", "physical_page_index": 0, "seq": 1},
        "source_type": "source", "derived_from": None, "uploader_ref": None,
        "retrieval_receipt_ref": "federation-probe:probe-remote-1",
        "policy_revision": "peer:1", "block_type": "text",
    }, "excerpt": "retrieval target text"}])


async def start_delegated(actor_client, *, peer, mode="exhaustive_scope",
                          query="retrieval target"):
    """规划一条指向远端执行者的任务；返回 (root, plan)。"""
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    consent = exploration(recipients=(PEER_NODE,))
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode=mode, scope_ref="scope-1",
                       query=query),
        consent=consent, manifest=manifest)
    root = intent["root_task_id"]
    plan = await plan_task(actor_client, root)
    return root, plan


def answer_step(plan):
    return next((step for step in plan["steps"] if step["operation"] == "answer"), None)


def answer_edge(plan):
    return next((edge for edge in plan["data_edges"] if edge["edge_id"] == "edge-answer-1"),
                None)


# ------------------------------------------------------------- 协调者：正向委托

@respx.mock
async def test_delegated_answer_keeps_real_evidence_bindings(actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    peer.answer_document = ready_document()
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)

    step = answer_step(plan)
    assert step is not None and step["executor_node_id"] == PEER_NODE
    assert step["depends_on"] == ["fuse-1"] and step["fixed_inputs"] == ["query"]
    edge = answer_edge(plan)
    assert edge is not None, "委托计划必须先在批准之前落下 evidence_excerpts 数据边"
    assert edge["from_node_id"] == NODE and edge["to_node_id"] == PEER_NODE
    assert edge["payload_kind"] == "evidence_excerpts"
    assert plan["budget"]["max_generation_tokens"] > 0
    assert plan["budget"]["max_hops"] >= 3
    assert sum(1 for key in probe_keys(peer) if key.startswith("answer-probe:")) == 1

    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "delegated")).json()

    assert status["status"] == "succeeded"
    result = status["result"]
    assert result["answer"] == "peer cited answer [1]"
    assert result["answer_reason"] is None
    assert result["validation_state"] == "passed"
    assert result["disclosure"] == {"remote": True,
                                    "payload": ["question", "selected_evidence"]}
    assert result["provider"]["location"] == "remote"
    bindings = result["claim_evidence_bindings"]
    assert bindings and bindings[0]["evidence_refs"] == ["peer-evidence-1"]
    assert bindings[0]["semantic_review"] == "needs_review", "远端自报的人审不可信"
    sent_ids = {item["evidence_id"] for item in result["evidence"]}
    assert set(bindings[0]["evidence_refs"]) <= sent_ids

    # 证据摘录真的经数据边到达对端：admission 体里带着逐条校验过摘要的摘录。
    assert [body["step_id"] for body in peer.admissions] == ["retrieve-1", "answer-1"]
    sent = peer.admissions[1]["evidence"]
    assert [item["evidence_id"] for item in sent] == ["peer-evidence-1"]
    assert sent[0]["excerpt"] == "retrieval target text"
    assert sent[0]["digest"] == content_digest(b"retrieval target text")


@respx.mock
async def test_fabricated_binding_ids_reject_the_answer_but_keep_evidence(
        actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    # 对端报了一个我们从未发送过的 evidence id：伪造引用。
    peer.answer_document = ready_document(refs=("fabricated-evidence",))
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "fabricated")).json()

    assert status["status"] == "succeeded", "生成被拒不许把检索任务标失败"
    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "delegated_binding_out_of_scope"
    assert result["validation_state"] == "failed"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"], "证据必须原样保留"
    assert result["evidence"][0]["evidence_id"] == "peer-evidence-1"


@respx.mock
async def test_remote_conflicts_outside_the_sent_evidence_reject_the_answer(
        actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    peer.answer_document = {**ready_document(), "conflicts": [{
        "basis": "generation_reported", "evidence_refs": ["peer-evidence-1", "never-sent"],
        "semantic_review": "needs_review"}]}
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "conflict-forged")).json()
    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "delegated_conflict_out_of_scope"
    assert result["conflicts"] == [] and result["evidence"], "证据原样保留"
    assert status["evidence_sufficiency"] != "conflicting"


@respx.mock
async def test_remote_conflicts_are_accepted_as_generation_reported_only(
        actor_client, monkeypatch):
    """对端自报的依据/人审不可信：依据重写成 generation_reported，复核恒为人工态。"""
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    second = {**peer.items[0], "evidence_id": "peer-evidence-2",
              "resource_id": "peer-resource-2", "excerpt": "retrieval target other text"}
    peer.items.append(second)
    peer.can_generate = True
    peer.answer_document = {**ready_document(), "conflicts": [{
        "basis": "version_divergence", "evidence_refs": ["peer-evidence-2", "peer-evidence-1"],
        "semantic_review": "passed"}]}
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "conflict-remote")).json()
    result = status["result"]
    assert result["answer"] == "peer cited answer [1]"
    assert result["conflicts"] == [{"basis": "generation_reported",
                                    "evidence_refs": ["peer-evidence-1", "peer-evidence-2"],
                                    "semantic_review": "needs_review"}]
    assert status["evidence_sufficiency"] == "conflicting"


@respx.mock
async def test_empty_citations_are_rejected(actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    peer.answer_document = ready_document(bindings=False)
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    result = (await submit_task(actor_client, root, plan["plan_digest"], "empty-bindings")
              ).json()["result"]

    assert result["answer"] is None
    assert result["answer_reason"] == "delegated_bindings_missing"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"]


@respx.mock
async def test_peer_execution_failure_is_visible_and_keeps_evidence(
        actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    peer.fail_execution_status = True
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    result = (await submit_task(actor_client, root, plan["plan_digest"], "peer-500")
              ).json()["result"]

    assert result["answer"] is None
    assert result["answer_reason"], "故障必须写可见原因，不能沉默"
    assert result["validation_state"] == "failed"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"]


# --------------------------------------------------------- 协调者：许可与能力门

@respx.mock
async def test_consent_without_answer_edge_is_egress_denied_without_bytes(
        actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    peer.can_generate = True
    peer.answer_document = ready_document()
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    assert answer_edge(plan) is not None
    admissions_before = calls_to(peer, "/admissions")
    # 用户许可没有覆盖 answer 边：审批必须当场拒绝。
    consent = execution_consent(
        plan["plan_digest"], recipients=(NODE, PEER_NODE),
        edges=[edge["edge_id"] for edge in plan["data_edges"]
               if edge["edge_id"] != "edge-answer-1"])
    denied = await actor_client.post(
        f"/api/v1/task-plans/{root}/approve",
        json={"plan_digest": plan["plan_digest"], "execution_consent": consent})
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "egress_denied"
    assert calls_to(peer, "/admissions") == admissions_before == 0, \
        "许可不覆盖时一个字节都不许发"


@respx.mock
async def test_not_ready_node_never_gets_an_answer_step(actor_client, monkeypatch):
    mock_gateway_not_ready()
    peer = peer_with_excerpt()          # can_generate=False（默认）
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)

    assert answer_step(plan) is None, "未就绪节点不许进 answer 步"
    assert plan["budget"]["max_generation_tokens"] == 0
    # 探测本身发生了（诚实问了一次），但没有 answer 边。
    assert sum(1 for key in probe_keys(peer) if key.startswith("answer-probe:")) == 1
    assert answer_edge(plan) is None
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    result = (await submit_task(actor_client, root, plan["plan_digest"], "not-ready")
              ).json()["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "local_model_missing"
    assert result["evidence"], "证据仍在，只是没有生成"
    assert calls_to(peer, "/admissions") == 1, "只有取数受理，没有 answer 受理"


# ------------------------------------------------------- 执行者：证据校验与生成

def executor_answer_body(*, key, evidence=None, budget=64, step_inputs=None):
    # 注册工作流里 answer 必须有类型化前驱（retrieve/fuse/rerank），不能凭空出现。
    steps = [
        {"step_id": "retrieve-1", "operation": "retrieve", "executor_node_id": NODE,
         "depends_on": [], "fixed_inputs": []},
        {"step_id": "answer-1", "operation": "answer", "executor_node_id": NODE,
         "depends_on": ["retrieve-1"], "fixed_inputs": ["query"]},
    ]
    body = admission_body(key=key, operation="answer", step_id="answer-1", steps=steps)
    body["plan"]["budget"]["max_generation_tokens"] = budget
    body["plan"]["plan_digest"] = task_plan_digest(body["plan"])
    body["execution_consent"]["plan_digest"] = body["plan"]["plan_digest"]
    if evidence is not None:
        body["evidence"] = evidence
    if step_inputs is not None:
        body["inputs"] = step_inputs
    return body


async def post_executor_answer(client, body, *, key=None):
    return await client.post(f"{BASE}/admissions",
                             headers={**headers(),
                                      "Idempotency-Key": key or body["idempotency_key"]},
                             json=body)


def evidence_item(evidence_id="ev-1", excerpt="retrieval target text"):
    return {"evidence_id": evidence_id, "excerpt": excerpt,
            "digest": content_digest(excerpt.encode("utf-8"))}


@respx.mock
async def test_executor_verifies_evidence_before_accepting_answer(
        actor_client, session, app_state):
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("verified answer [1]"))
    body = executor_answer_body(key="exec-answer", evidence=[evidence_item()])
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["state"] == "accepted"
    assert receipt["input_validation"] == "content_verified"
    # 受理摘要覆盖排序后的 (evidence_id, digest) 列表。
    expected = content_digest(canonical_bytes(sorted(
        [[item["evidence_id"], item["digest"]] for item in body["evidence"]])))
    assert receipt["verified_input_manifest_digest"] == expected

    await drain_tasks(app_state)
    detail = (await actor_client.get(f"{BASE}/tasks/{receipt['executor_task_id']}",
                                     headers=headers())).json()
    assert detail["state"] == "succeeded"
    assert detail["operation"] == "answer"
    document = detail["answer"]
    assert document["answer"] == "verified answer [1]"
    assert document["validation_state"] == "passed"
    assert document["claim_evidence_bindings"][0]["evidence_refs"] == ["ev-1"]
    assert document["claim_evidence_bindings"][0]["semantic_review"] == "needs_review"
    assert "retrieval target text" in chat.calls[0].request.content.decode()


@respx.mock
async def test_executor_missing_evidence_waits_without_occupying_work(
        actor_client, session):
    mock_gateway(channels=[gateway_channel()])
    body = executor_answer_body(key="exec-no-evidence", evidence=[])
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["state"] == "waiting_input"
    assert receipt["input_validation"] == "metadata_only"
    assert receipt.get("verified_input_manifest_digest") is None
    assert "executor_task_id" not in receipt
    from ddp_corpus.federation_models import FederationExecution
    from sqlalchemy import func, select
    count = await session.scalar(select(func.count()).select_from(FederationExecution))
    assert count == 0, "等待输入不得占算力、不得留执行行"


@respx.mock
@pytest.mark.parametrize("mutate,label", [
    (lambda item: {**item, "digest": "sha256:" + "0" * 64}, "digest-mismatch"),
    (lambda item: {**item, "excerpt": "   "}, "blank-excerpt"),
    (lambda item: {**item, "excerpt": "x" * 2001,
                   "digest": content_digest(("x" * 2001).encode())}, "over-2000"),
])
async def test_executor_unverifiable_evidence_is_rejected_not_truncated(
        actor_client, mutate, label):
    mock_gateway(channels=[gateway_channel()])
    body = executor_answer_body(key="exec-" + label, evidence=[mutate(evidence_item())])
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "input_not_verified"


@respx.mock
async def test_executor_duplicate_evidence_ids_and_count_bounds_are_rejected(
        actor_client):
    mock_gateway(channels=[gateway_channel()])
    duplicate = [evidence_item("ev-1"), evidence_item("ev-1", excerpt="other text")]
    response = await post_executor_answer(
        actor_client, executor_answer_body(key="exec-dupe", evidence=duplicate))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "input_not_verified"

    too_many = [evidence_item(f"ev-{index}", excerpt=f"text {index}")
                for index in range(51)]
    response = await post_executor_answer(
        actor_client, executor_answer_body(key="exec-many", evidence=too_many))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "input_not_verified"


@respx.mock
async def test_executor_without_generation_budget_rejects_answer(actor_client):
    mock_gateway(channels=[gateway_channel()])
    body = executor_answer_body(key="exec-no-budget", evidence=[evidence_item()],
                                budget=0)
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "budget_exceeded"


@respx.mock
async def test_executor_generation_without_citations_is_a_visible_empty_answer(
        actor_client, app_state):
    mock_gateway(channels=[gateway_channel()])
    mock_chat(chat_answer("这段答案没有任何引用。"))
    body = executor_answer_body(key="exec-uncited", evidence=[evidence_item()])
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    await drain_tasks(app_state)
    detail = (await actor_client.get(f"{BASE}/tasks/{receipt['executor_task_id']}",
                                     headers=headers())).json()
    document = detail["answer"]
    assert document["answer"] is None
    assert document["answer_reason"] == "unsupported_generation"
    assert document["validation_state"] == "failed"
    assert document["claim_evidence_bindings"] == []


@respx.mock
async def test_executor_readiness_is_rechecked_at_admission(actor_client):
    """能力清单说未知时，即使请求带齐证据也只能 capability_unsupported。"""
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={
        "capability_status": "unknown", "profiles": [], "model_channels": []}))
    body = executor_answer_body(key="exec-not-ready", evidence=[evidence_item()])
    response = await post_executor_answer(actor_client, body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capability_unsupported"


# ------------------------------------------------ 矛盾不能盖掉"证据不足"（第五次验收）

def _zero_hit_probes(monkeypatch):
    """规划期探测照常成功，但没有证据集（零命中）：覆盖记录里没有绑定。"""
    import test_federation_tasks as tasks_module
    original = tasks_module.peer_probe

    def zero_hit(**kwargs):
        body = original(**kwargs)
        body["retrieval"]["evidence_set_ref"] = None
        return body

    monkeypatch.setattr(tasks_module, "peer_probe", zero_hit)


def _two_versions(peer, *, origin=PEER_NODE):
    base = {**peer.items[0], "origin_node_id": origin}
    peer.items[:] = [
        {**base, "evidence_id": "peer-old", "source_version_id": "peer-version-1",
         "excerpt_digest": "sha256:" + "1" * 64, "excerpt": "retrieval target says 240 V"},
        {**base, "evidence_id": "peer-new", "source_version_id": "peer-version-2",
         "excerpt_digest": "sha256:" + "2" * 64, "excerpt": "retrieval target says 120 V"},
    ]


@respx.mock
async def test_version_divergence_never_hides_insufficient_or_opens_the_generation_gate(
        actor_client, monkeypatch):
    """复现形状：探测零命中（没有绑定），执行期对端返回同一定位的两版不同正文。

    旧行为：矛盾优先于"没有绑定"，账本从 insufficient 被改写成 conflicting，
    `_answer_result` 只拦 insufficient —— 生成闸被绕过，远端借规则一路藏掉了
    "证据不足"。现在：充分性保持 insufficient、矛盾记录照样保留，一个 answer
    受理都不发。
    """
    mock_gateway_not_ready()
    _zero_hit_probes(monkeypatch)
    peer = peer_with_excerpt()
    _two_versions(peer)
    peer.can_generate = True
    peer.answer_document = ready_document(refs=("peer-old",))
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    assert answer_step(plan) is not None, "前提：计划里有委托生成这一步，闸才有意义"
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "thin-divergent")).json()

    assert status["status"] == "succeeded"
    assert status["evidence_sufficiency"] == "insufficient"
    result = status["result"]
    assert result["answer"] is None and result["answer_reason"] == "insufficient_evidence"
    assert [item["basis"] for item in result["conflicts"]] == ["version_divergence"]
    assert not [body for body in peer.admissions if body.get("step_id") == "answer-1"], \
        "证据不足时一个 answer 受理都不许发"
    coverage = (await actor_client.get(f"/api/v1/tasks/{root}/coverage")).json()
    assert coverage["evidence_sufficiency"] == "insufficient"
    assert coverage["conflicts"] == result["conflicts"]
    from test_federation_tasks import validate_ledger_contract
    validate_ledger_contract(coverage)


@respx.mock
async def test_items_a_peer_reports_for_another_origin_cannot_raise_a_divergence(
        actor_client, monkeypatch):
    """对端自报"这两条来自本节点"：不可归属的条目不进规则一路，不能把证据标成矛盾。"""
    mock_gateway_not_ready()
    peer = peer_with_excerpt()
    _two_versions(peer, origin=NODE)
    install_peer(monkeypatch, peer)
    root, plan = await start_delegated(actor_client, peer=peer)
    await approve_task(actor_client, root, plan, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan["plan_digest"], "forged-origin")).json()

    assert status["result"]["conflicts"] == []
    assert status["evidence_sufficiency"] != "conflicting"
