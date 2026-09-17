"""协调者本地生成带引用答案：计划形状、结构验收与可见降级。

生成就绪判据走**能力清单生产者**（respx mock 网关的 `/v1/capabilities`），
chat 上游同样用 respx mock —— 这里不接任何真实模型。负向用例钉死四件事：

1. 伪造引用 / 无引用必须被拒，且**不许修补引用**；
2. 生成失败（上游 500 / 超时 / 空输出）不许打挂检索任务，证据原样保留；
3. 未就绪时保持旧行为（计划无 answer 步、`answer=null`、`local_model_missing`）；
4. 生成预算超限必须可见，不截断冒充完整答案。
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from conftest import SERVICE
from ddp_corpus import federation, federation_tasks, upstream
from ddp_corpus.config import settings
from federation_two_node import TwoNodeFixture
from test_federation_probes import NODE, configure_federation, indexed_source, publish_collection
from test_federation_tasks import (
    PEER_NODE,
    StubPeer,
    approve_task,
    create_intent,
    exploration,
    install_peer,
    member,
    peer_evidence,
    plan_task,
    scope_manifest,
    submit_task,
    task_spec,
)
from test_federation_two_node import (
    NODE_B,
    approve_task as two_node_approve,
    create_intent as two_node_create,
    exploration as two_node_exploration,
    member as two_node_member,
    plan_task as two_node_plan,
    scope_manifest as two_node_scope_manifest,
    submit_task as two_node_submit,
    task_spec as two_node_task_spec,
)

GATEWAY_CAP = f"{SERVICE}/v1/capabilities"
CHAT = f"{SERVICE}/v1/chat/completions"


@pytest.fixture(autouse=True)
def _answer_config(monkeypatch):
    configure_federation(monkeypatch)
    monkeypatch.setattr(settings, "chat_url", "")
    monkeypatch.setattr(settings, "chat_model", "")
    monkeypatch.setattr(settings, "federation_peers", "")
    monkeypatch.setattr(settings, "federation_allow_loopback", False)


# ------------------------------------------------------------------ mock 工具

def gateway_channel(*, model="qwen3-4b-instruct", readiness="ready", instruct=True,
                    default=True):
    now = datetime.now(timezone.utc)
    return {"channel": "chat", "model": model, "profile": model, "default": default,
            "readiness": readiness, "supports": {"instruct": instruct},
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=60)).isoformat()}


def mock_gateway(*, status="observed", channels=()):
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={
        "capability_status": status, "profiles": [], "model_channels": list(channels)}))


def mock_chat(route_response) -> respx.Route:
    if isinstance(route_response, httpx.Response):
        return respx.post(CHAT).mock(return_value=route_response)
    return respx.post(CHAT).mock(side_effect=route_response)


def chat_answer(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": "qwen3-4b-instruct"})


async def run_answer_task(actor_client, session, *, texts=("retrieval target text",),
                          query="retrieval target", key="answer-key", mode="fast"):
    """本地发布集合上的完整闭环；返回值里带规划、执行结果与原始证据行。"""
    _, version, _, _, evidence_rows = await indexed_source(session, texts=texts)
    await publish_collection(actor_client, version, key=key)
    consent = exploration(egress="local_only", recipients=(), payload=(),
                          budget={"max_probe_requests": 0, "max_egress_bytes": 0})
    intent = await create_intent(
        actor_client, spec=task_spec(scope="site_public", mode=mode, query=query),
        consent=consent)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body)
    executed = await submit_task(actor_client, root, plan_body["plan_digest"], key)
    assert executed.status_code == 200, executed.text
    return {"root": root, "plan": plan_body, "status": executed.json(),
            "evidence_rows": evidence_rows}


def answer_step(plan):
    return next((step for step in plan["steps"] if step["operation"] == "answer"), None)


# ------------------------------------------------------------ (a) 正向：带引用答案

@respx.mock
async def test_local_generation_persists_cited_answer_and_bindings(actor_client, session):
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("第一句依据。[1] 第二句依据。[2]"))
    run = await run_answer_task(
        actor_client, session, key="cited-answer",
        texts=("retrieval target text", "another retrieval target detail"))

    plan = run["plan"]
    step = answer_step(plan)
    assert step is not None, "生成就绪时计划里必须有协调者本地的 answer 步"
    retrieve_ids = [item["step_id"] for item in plan["steps"]
                    if item["operation"] == "retrieve"]
    assert step["executor_node_id"] == NODE
    assert step["depends_on"] == retrieve_ids
    assert step["fixed_inputs"] == ["query"]
    assert plan["budget"]["max_generation_tokens"] == federation_tasks.GENERATION_TOKEN_BUDGET

    status = run["status"]
    assert status["status"] == "succeeded"
    result = status["result"]
    assert result["answer"] == "第一句依据。[1] 第二句依据。[2]"
    assert result["answer_reason"] is None
    assert result["validation_state"] == "passed"
    assert result["evidence_sufficiency"] == "sufficient_by_policy"

    bindings = result["claim_evidence_bindings"]
    assert [binding["claim_text"] for binding in bindings] == ["第一句依据。", "第二句依据。"]
    fused_ids = {item["evidence_id"] for item in result["evidence"]}
    assert len(fused_ids) == 2, result["evidence"]
    cited = [ref for binding in bindings for ref in binding["evidence_refs"]]
    assert set(cited) == fused_ids, "绑定必须指向真实融合证据 id，不许对不上编号"
    for binding in bindings:
        assert binding["structural_validation"] == "passed"
        assert binding["semantic_review"] == "needs_review", "语义支持只能人看"
        assert binding["evidence_refs"]
    for item in result["evidence"]:
        assert item["excerpt_digest"].startswith("sha256:")
        assert "_excerpt" not in item, "HTTP 结果不得带内部审计字段"
    assert result["provider"] == {"model": "qwen3-4b-instruct",
                                  "endpoint": settings.chat_endpoint, "location": "local"}
    assert result["disclosure"] == {"remote": False,
                                    "payload": ["question", "selected_evidence"]}
    assert chat.call_count == 1

    fetched = (await actor_client.get(f"/api/v1/tasks/{run['root']}")).json()
    assert fetched["result"] == result, "read_task 必须与存储行一致"


@respx.mock
async def test_remote_evidence_has_typed_edge_and_feeds_local_generation(
        actor_client, session, monkeypatch):
    """远端取数 + 本地生成：回传边必须类型化，正文真的进 prompt。

    证据集出口（`get_federation_evidence_set`）带的公开字段是 `excerpt` ——
    不再往 stub 里塞内部字段 `_excerpt`，否则这条用例绕过了真实的序列化边界。
    """
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("peer fact answer [1]"))
    peer = StubPeer(items=[{**peer_evidence(),
                            "excerpt": "beta federation keyword fact"}])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                       scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)

    step = answer_step(plan_body)
    assert step is not None and step["executor_node_id"] == NODE
    retrieve = next(item for item in plan_body["steps"] if item["operation"] == "retrieve")
    assert retrieve["executor_node_id"] == PEER_NODE
    assert step["depends_on"] == [retrieve["step_id"]]
    edges = [edge for edge in plan_body["data_edges"]
             if edge["payload_kind"] == "evidence_excerpts"
             and edge["from_node_id"] == PEER_NODE and edge["to_node_id"] == NODE]
    assert edges, "远端执行者的证据必须有一条到协调者的 evidence_excerpts 回传边"

    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    executed = await submit_task(actor_client, root, plan_body["plan_digest"], "remote-gen")
    assert executed.status_code == 200, executed.text
    result = executed.json()["result"]
    assert result["answer"] == "peer fact answer [1]"
    binding = result["claim_evidence_bindings"][0]
    assert binding["evidence_refs"] == ["peer-evidence-1"]
    assert result["evidence"][0]["origin_node_id"] == PEER_NODE
    prompt = chat.calls[0].request.content.decode()
    assert "beta federation keyword fact" in prompt, "远端证据正文必须真的进生成上下文"
    # 正文只进 prompt：任务结果里只留公开信封，不扩散正文。
    assert "excerpt" not in result["evidence"][0]


@respx.mock
async def test_missing_remote_excerpt_refuses_generation_without_placeholder(
        actor_client, session, monkeypatch):
    """F4：证据集没有正文时不许拿占位符生成，必须显式拒绝。

    旧行为：`_grounded_answer` 用 `"(excerpt unavailable)"` 填上下文，
    模型给出 [1] 之后结构校验照样通过 —— 一条无法复核的引用被当成成功答案。
    """
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("peer fact answer [1]"))
    # 只有身份、没有正文：真实对端在缺正文时就是这个形状。
    peer = StubPeer(items=[peer_evidence()])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                       scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "no-excerpt")
              ).json()

    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "evidence_excerpt_unavailable"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"], "证据身份仍要保留，只是不生成"
    assert chat.call_count == 0, "正文缺失时一个生成请求都不许发"


@respx.mock
async def test_whitespace_only_remote_excerpt_is_not_evidence(
        actor_client, session, monkeypatch):
    """N5：`"   "` 不是正文，不许拿它生成带 [n] 的答案。"""
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("peer fact answer [1]"))
    peer = StubPeer(items=[{**peer_evidence(), "excerpt": "   "}])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                       scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "ws-excerpt")
              ).json()

    result = status["result"]
    assert result["answer"] is None, "空白正文被当成可用证据生成了答案"
    assert result["answer_reason"] == "evidence_excerpt_unavailable"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"], "证据身份仍要保留，只是不生成"
    assert chat.call_count == 0, "空白正文一个生成请求都不许发"


@respx.mock
async def test_overlong_remote_excerpt_is_refused_with_machine_reason(
        actor_client, session, monkeypatch):
    """N6：对端越过 `excerpt.maxLength=2000` 时，协调者绝不原样喂进 prompt。

    选择"显式拒绝 + 机器原因"而不是静默截断：对端已经违约，静默改写对端给
    的证据正文正是"降级必须可见"要防的事。
    """
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("peer fact answer [1]"))
    peer = StubPeer(items=[{**peer_evidence(), "excerpt": "x" * 5000}])
    install_peer(monkeypatch, peer)
    manifest = scope_manifest([member("peer-collection-1", PEER_NODE)])
    intent = await create_intent(
        actor_client,
        spec=task_spec(scope="federation_public", mode="exhaustive_scope",
                       scope_ref="scope-1"),
        consent=exploration(recipients=(PEER_NODE,)), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await plan_task(actor_client, root)
    await approve_task(actor_client, root, plan_body, recipients=(NODE, PEER_NODE))
    status = (await submit_task(actor_client, root, plan_body["plan_digest"], "long-excerpt")
              ).json()

    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "excerpt_over_contract_bound"
    assert result["claim_evidence_bindings"] == []
    assert chat.call_count == 0, "越界正文一个生成请求都不许发"
    assert "excerpt" not in result["evidence"][0], "正文只进 prompt，不许进结果"


# ------------------------------------------------- (b) 伪造/缺失引用：结构拒绝

@pytest.mark.parametrize("output", ["这段答案没有任何引用。", "这段答案引用了不存在的证据。[9]"])
@respx.mock
async def test_fabricated_or_missing_citations_reject_the_answer(actor_client, session,
                                                                 output):
    mock_gateway(channels=[gateway_channel()])
    mock_chat(chat_answer(output))
    run = await run_answer_task(actor_client, session, key="unsupported")

    status = run["status"]
    assert status["status"] == "succeeded", "生成被拒不许把检索任务标失败"
    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "unsupported_generation"
    assert result["validation_state"] == "failed"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"], "证据结果必须原样保留"


# ------------------------------------------------ (c) 生成失败：可见且不打挂任务

@pytest.mark.parametrize("failure,reason", [
    ("status", "upstream_error"),
    ("timeout", "upstream_error"),
    ("empty", "no_model_output"),
])
@respx.mock
async def test_generation_failure_keeps_evidence_and_is_visible(actor_client, session,
                                                                 failure, reason):
    mock_gateway(channels=[gateway_channel()])
    if failure == "status":
        mock_chat(httpx.Response(503, json={"error": {"message": "model down"}}))
    elif failure == "timeout":
        mock_chat(httpx.ReadTimeout("model timed out"))
    else:
        mock_chat(chat_answer(""))

    run = await run_answer_task(actor_client, session, key=f"failure-{failure}")
    status = run["status"]
    assert status["status"] == "succeeded", "生成失败不得改写检索结局"
    result = status["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == reason
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"] and result["evidence"][0]["evidence_id"]
    assert result["retrieval_completeness"] == "partial"


# ------------------------------------------- (d) 规划时未就绪：保持旧的可见行为

@respx.mock
async def test_generation_not_ready_at_plan_time_keeps_old_behavior(
        actor_client, session):
    mock_gateway(status="unknown")
    chat = mock_chat(chat_answer("不应被调用 [1]"))
    run = await run_answer_task(actor_client, session, key="not-ready")

    assert answer_step(run["plan"]) is None, "能力清单未知时不许保留 answer 步"
    assert run["plan"]["budget"]["max_generation_tokens"] == 0
    result = run["status"]["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "local_model_missing"
    assert result["claim_evidence_bindings"] == []
    assert result["validation_state"] == "pending"
    assert result["evidence"], "证据仍在，只是没有本地生成"
    assert chat.call_count == 0, "未就绪时一个生成请求都不许发"


@respx.mock
async def test_model_name_alone_is_not_readiness(actor_client, session, monkeypatch):
    """只把模型名配上不算就绪：OCR 专用模型听得见名字、干不了带引用的生成。"""
    monkeypatch.setattr(settings, "chat_model", "deepseek-ocr-2")
    mock_gateway(channels=[gateway_channel(model="deepseek-ocr-2", instruct=False)])
    chat = mock_chat(chat_answer("不应被调用 [1]"))
    run = await run_answer_task(actor_client, session, key="name-only")

    assert answer_step(run["plan"]) is None
    assert run["status"]["result"]["answer_reason"] == "local_model_missing"
    assert chat.call_count == 0


@respx.mock
async def test_long_local_block_is_bounded_like_the_evidence_set_exit(actor_client, session):
    """本节点自己的长块（表格/代码/公式不切分）按证据集出口同一把尺子截断。

    旧行为：本地路径把完整 `Evidence.content` 喂给 `excerpt_reason`，一个
    超过 2000 字的块让整份答案落 `excerpt_over_contract_bound` —— 而远端协调者
    读同一条证据（出口截到 2000）却能生成成功。对端越界仍然显式拒绝（上一条）。
    """
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("long block fact [1]"))
    long_text = "retrieval target text " + "| row | cell | value |" * 150
    assert len(long_text) > federation.EVIDENCE_EXCERPT_CHARS
    run = await run_answer_task(actor_client, session, key="long-local", texts=(long_text,))

    result = run["status"]["result"]
    assert result["answer_reason"] is None, result
    assert result["answer"] == "long block fact [1]"
    assert result["claim_evidence_bindings"][0]["evidence_refs"] \
        == [run["evidence_rows"][0].id]
    assert chat.call_count == 1
    prompt = json.loads(json.loads(chat.calls[0].request.content)["messages"][1]["content"])
    assert prompt["evidence"][0]["text"] == long_text[:federation.EVIDENCE_EXCERPT_CHARS]


@respx.mock
async def test_long_local_block_is_bounded_on_the_live_path_alone(actor_client, session,
                                                                  monkeypatch):
    """同一条规则单独钉住"本轮执行拿到的正文"这条路径（上一条两条路径都在喂）。"""
    async def _nothing_stored(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(federation_tasks, "_load_excerpts", _nothing_stored)
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("live block fact [1]"))
    long_text = "retrieval target text " + "| live | cell |" * 200
    run = await run_answer_task(actor_client, session, key="long-live", texts=(long_text,))
    assert run["status"]["result"]["answer_reason"] is None, run["status"]["result"]
    prompt = json.loads(json.loads(chat.calls[0].request.content)["messages"][1]["content"])
    assert prompt["evidence"][0]["text"] == long_text[:federation.EVIDENCE_EXCERPT_CHARS]


@respx.mock
async def test_generation_reported_conflict_marks_ledger_and_is_lifted_from_the_answer(
        actor_client, session):
    """模型标出矛盾引用对：账本 conflicting、结果与覆盖都带记录，标注行不进答案与绑定。"""
    mock_gateway(channels=[gateway_channel()])
    mock_chat(chat_answer("One source rates PM-2 at 240 V. [1]\n"
                          "Another rates PM-2 at 120 V. [2]\nCONFLICT: [1] [2]"))
    run = await run_answer_task(
        actor_client, session, key="conflict-generation",
        texts=("retrieval target text PM-2 is rated 240 V",
               "retrieval target detail PM-2 is rated 120 V"))
    status, result = run["status"], run["status"]["result"]
    ids = sorted(row.id for row in run["evidence_rows"])

    assert result["answer_reason"] is None, result
    assert "CONFLICT" not in result["answer"]
    assert [binding["claim_text"] for binding in result["claim_evidence_bindings"]] == [
        "One source rates PM-2 at 240 V.", "Another rates PM-2 at 120 V."]
    expected = [{"basis": "generation_reported", "evidence_refs": ids,
                 "semantic_review": "needs_review"}]
    assert result["conflicts"] == expected
    assert status["evidence_sufficiency"] == result["evidence_sufficiency"] == "conflicting"
    coverage = (await actor_client.get(f"/api/v1/tasks/{run['root']}/coverage")).json()
    assert coverage["evidence_sufficiency"] == "conflicting"
    assert coverage["conflicts"] == expected
    from test_federation_tasks import validate_ledger_contract
    validate_ledger_contract(coverage)
    delivery = (await actor_client.get(f"/api/v1/deliveries/{status['delivery_id']}")).json()
    assert delivery["result"]["conflicts"] == expected, "矛盾记录进交付文档与摘要"


@respx.mock
async def test_unverifiable_conflict_markup_rejects_the_answer(actor_client, session):
    """`CONFLICT:` 引用越界与伪造主张引用同罪：整份答案拒收，不悄悄丢掉那一行。"""
    mock_gateway(channels=[gateway_channel()])
    mock_chat(chat_answer("PM-2 is rated 240 V. [1]\nCONFLICT: [1] [7]"))
    run = await run_answer_task(actor_client, session, key="conflict-forged",
                                texts=("retrieval target text PM-2 is rated 240 V",))
    result = run["status"]["result"]
    assert result["answer"] is None
    assert result["answer_reason"] == "unsupported_generation"
    assert result["claim_evidence_bindings"] == [] and result["conflicts"] == []
    assert run["status"]["evidence_sufficiency"] == "sufficient_by_policy", \
        "拒收的标注不能把账本压成 conflicting"


@respx.mock
async def test_readiness_check_is_what_keeps_the_step(actor_client, session, monkeypatch):
    """变异确认的常驻版：判据被强制成"永远就绪"时，同一路径立刻出现 answer 步。

    真正的就绪判据由上面两条用例钉住；这条证明 `create_plan` 真的在读它，
    删掉那次调用会让本条依然绿、上面两条变红。
    """
    mock_gateway(status="unknown")

    async def _always_ready(_http, *, now):
        return True

    monkeypatch.setattr(federation_tasks, "_generation_available", _always_ready)
    mock_chat(chat_answer("forced answer [1]"))
    run = await run_answer_task(actor_client, session, key="forced-ready")
    step = answer_step(run["plan"])
    assert step is not None and step["executor_node_id"] == NODE
    assert run["plan"]["budget"]["max_generation_tokens"] \
        == federation_tasks.GENERATION_TOKEN_BUDGET
    assert run["status"]["result"]["answer_reason"] is None


# ------------------------------------------------------------------ (e) 生成预算

@respx.mock
async def test_output_over_generation_budget_is_rejected_visibly(actor_client, session,
                                                                  monkeypatch):
    monkeypatch.setattr(federation_tasks, "GENERATION_TOKEN_BUDGET", 3)
    mock_gateway(channels=[gateway_channel()])
    mock_chat(chat_answer("alpha beta gamma delta epsilon [1]"))
    run = await run_answer_task(actor_client, session, key="budget")

    assert run["plan"]["budget"]["max_generation_tokens"] == 3
    status = run["status"]
    assert status["status"] == "succeeded"
    result = status["result"]
    assert result["answer"] is None, "超预算不许截断冒充完整答案"
    assert result["answer_reason"] == "budget_exceeded"
    assert result["validation_state"] == "failed"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"]


# ------------------------------------- 证据不足：insufficient 时绑定必须为空

@respx.mock
async def test_insufficient_evidence_never_generates_and_keeps_bindings_empty(
        actor_client, session):
    """契约 allOf：证据不足时不许给出带绑定的答案，也不该白跑一次模型。"""
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("模型常识 [1]"))
    run = await run_answer_task(actor_client, session, key="insufficient",
                                query="completely unrelated zoology question")

    result = run["status"]["result"]
    assert result["evidence_sufficiency"] == "insufficient"
    assert result["answer"] is None
    assert result["answer_reason"] == "insufficient_evidence"
    assert result["claim_evidence_bindings"] == []
    assert result["evidence"] == []
    assert chat.call_count == 0, "没有证据就不给模型留凭常识补一句的机会"


# ------------------------------- 真双节点：B 的正文真的跨节点进入 prompt

@pytest.fixture
async def two_node(tmp_path, monkeypatch):
    # B 是固定身份 node-b、没有控制面的真子进程：绑不上持久身份、验不了
    # Ed25519 信任链，只能走开发档位 shared_token_insecure（B 读进程环境变量）。
    # 节点凭证形态由进程内 PeerCaller 用例与 test_federation_peer_client.py 覆盖。
    monkeypatch.setenv("FEDERATION_PEER_AUTH", "shared_token_insecure")
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULTS", "true")
    fixture = await TwoNodeFixture.create(
        tmp_path, b_texts=("beta federation keyword fact",))
    try:
        yield fixture
    finally:
        await fixture.stop()


@respx.mock
async def test_two_node_remote_excerpt_reaches_real_prompt(
        actor_client, two_node, monkeypatch):
    """F4：真实 subprocess 节点 B 的 excerpt 真的进入协调者的生成 prompt。

    这条路径**不经过任何 stub**：B 由 `read_evidence_set` 真正序列化 excerpt，
    A 通过生产的 `PeerClient` 走真实回环 HTTP 取回。旧行为下 A 拿到的信封
    没有正文，prompt 里只有 `(excerpt unavailable)`。
    """
    monkeypatch.setattr(settings, "bundle_node_id", "node-a")
    # 与 two_node 夹具同档位：真子进程 B 没有控制面，A 侧也必须用共享口令。
    monkeypatch.setattr(settings, "federation_peer_auth", "shared_token_insecure")
    monkeypatch.setattr(settings, "federation_peer_token", "peer-a")
    monkeypatch.setattr(settings, "federation_admissions_enabled", True)
    monkeypatch.setattr(settings, "federation_peers", two_node.peers_json())
    monkeypatch.setattr(settings, "federation_allow_loopback", True)
    monkeypatch.setattr(settings, "chat_url", "")
    monkeypatch.setattr(settings, "chat_model", "")

    async def _embed(_http, _text):
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(upstream, "embed_one", _embed)

    respx.route(url__startswith=two_node.b_endpoint).pass_through()
    mock_gateway(channels=[gateway_channel()])
    chat = mock_chat(chat_answer("beta fact [1]"))

    b = two_node.b_seed
    manifest = two_node_scope_manifest([two_node_member(b.collection_id, NODE_B)],
                                       enumeration="sealed")
    intent = await two_node_create(
        actor_client,
        spec=two_node_task_spec(mode="exhaustive_scope", query="federation keyword"),
        consent=two_node_exploration(recipients=(NODE_B,)), manifest=manifest)
    root = intent["root_task_id"]
    plan_body = await two_node_plan(actor_client, root)
    await two_node_approve(actor_client, root, plan_body)
    status = (await two_node_submit(actor_client, root, plan_body["plan_digest"],
                                    "real-excerpt")).json()

    assert status["status"] == "succeeded"
    assert status["result"]["answer"] == "beta fact [1]", status["result"]
    assert status["result"]["evidence"][0]["origin_node_id"] == NODE_B
    assert chat.calls, "生成必须真的发生过"
    prompt = chat.calls[0].request.content.decode()
    assert "beta federation keyword fact" in prompt, \
        "B 的真实正文必须跨节点进入 prompt（不是占位符）"
    assert "(excerpt unavailable)" not in prompt
