"""抽取平面的契约测试（v1.1）。

覆盖三条最要紧的不变式，每一条都对应一种真实会发生、且**不会报错**的故障：

1. **坏 schema 当场 400**，不是跑完一轮抽取（N 次检索 + N 次模型调用）再说
2. **not_found 与 error 分得开** —— 合成一个的话，"文档里没有"和"我们挂了"
   在结果里长得一模一样，而空值看起来像结论
3. **found 必须有出处** —— 抽到值却指不回原文，这个项目就没有存在意义
"""
import json

import pytest
import respx
from httpx import Response

from app.services import extract_format as fmt
from app.services import extraction
from app.services.task_store import TaskStore
from app.worker.tasks import run_extraction

VQA = "http://vqa-dsocr:8000"

GOOD_SCHEMA = {
    "type": "object",
    "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "amount": {"type": "number", "description": "合同总价，只要数字"},
    },
    "required": ["buyer"],
}


# --------------------------------------------------------------------- schema 校验

@pytest.mark.parametrize("schema, hint", [
    ({"type": "object", "properties": {"a": {"type": "string"}}}, "description"),
    ({"type": "object", "properties": {"a": {"type": "object", "description": "x"}}}, "嵌套"),
    ({"type": "object", "properties": {}}, "properties"),
    ({"type": "string"}, "顶层 type"),
    ({"type": "object", "oneOf": [], "properties": {"a": {"type": "string",
                                                          "description": "x"}}}, "oneOf"),
])
def test_bad_schema_is_rejected(schema, hint):
    problems = fmt.validate_schema(schema)
    assert problems, f"这个 schema 本该被拒：{schema}"
    assert any(hint in p for p in problems), problems


def test_good_schema_passes():
    assert fmt.validate_schema(GOOD_SCHEMA) == []


async def test_submit_rejects_bad_schema(client):
    """**必须在受理时就拒**：跑完再说不合规是在烧调用方的钱。"""
    resp = await client.post("/v1/extract", json={
        "doc_hash": "abc", "schema": {"type": "object",
                                      "properties": {"a": {"type": "string"}}}})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_schema"


async def test_submit_requires_a_document(client):
    resp = await client.post("/v1/extract", json={"schema": GOOD_SCHEMA})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_document"


# --------------------------------------------------------------------- 值转换

@pytest.mark.parametrize("raw, expected", [
    ("¥1,234.00 元", 1234.0),
    (1234, 1234.0),
    ("  486,200.50 ", 486200.5),
])
def test_number_coercion(raw, expected):
    spec = fmt.FieldSpec(name="amount", type="number", description="金额")
    assert fmt.coerce_value(raw, spec) == expected


def test_number_coercion_rejects_garbage():
    spec = fmt.FieldSpec(name="amount", type="number", description="金额")
    with pytest.raises(fmt.CoerceError):
        fmt.coerce_value("待定", spec)


def test_bool_is_not_silently_a_number():
    """bool 是 int 的子类。不拦的话 True 会变成 1，而"是"与"1"是两回事。"""
    spec = fmt.FieldSpec(name="qty", type="integer", description="数量")
    with pytest.raises(fmt.CoerceError):
        fmt.coerce_value(True, spec)


def test_enum_outside_is_rejected():
    """**不许把没有的选项硬塞一个**：枚举字段下游多半是分类统计，
    塞个近似值会让统计悄悄错掉，比空着更糟。"""
    spec = fmt.FieldSpec(name="kind", type="string", description="类型",
                         enum=["采购", "服务"])
    assert fmt.coerce_value("采购", spec) == "采购"
    with pytest.raises(fmt.CoerceError):
        fmt.coerce_value("租赁", spec)


@pytest.mark.parametrize("raw", [
    '{"found": true, "value": 5}',
    '```json\n{"found": true, "value": 5}\n```',
    '好的，结果如下：{"found": true, "value": 5} 以上。',
])
def test_json_extraction_from_chatty_models(raw):
    assert fmt.parse_json_object(raw) == {"found": True, "value": 5}


def test_unparseable_output_returns_none():
    """抠不出来就是抠不出来。**不许蒙混**——调用方要靠 None 去打 schema_violation，
    静默把字段当成"文档里没有"会让系统故障伪装成事实。"""
    assert fmt.parse_json_object("我觉得应该是 5 吧") is None


# --------------------------------------------------------------------- 结果自检

def test_result_validator_catches_value_without_status():
    bad = {
        "extract_version": fmt.EXTRACT_VERSION, "status": "ok", "degraded": None,
        "fields": {"a": {"status": "not_found", "value": "编的",
                         "citations": [], "verified": False, "degraded": None}},
    }
    problems = fmt.validate_result(bad)
    assert any("非 found 状态却带着值" in p for p in problems), problems


def test_result_validator_requires_citations_for_found():
    bad = {
        "extract_version": fmt.EXTRACT_VERSION, "status": "ok", "degraded": None,
        "fields": {"a": {"status": "found", "value": "x", "citations": [],
                         "verified": False, "degraded": None}},
    }
    problems = fmt.validate_result(bad)
    assert any("没有出处" in p for p in problems), problems


def test_overall_status_only_counts_required_fields():
    """可选字段没抽到不算 partial —— 否则这个标记永远是 partial，从此没人看它。"""
    spec = fmt.parse_schema(GOOD_SCHEMA)
    fields = {"buyer": fmt.field_result(status="found", value="X",
                                        citations=[{"page_idx": 0}]),
              "amount": fmt.field_result(status="not_found")}
    assert fmt.overall_status(fields, spec) == "ok"
    fields["buyer"] = fmt.field_result(status="not_found")
    assert fmt.overall_status(fields, spec) == "partial"


# --------------------------------------------------------------------- 端到端（mock 上游）

async def _seed_corpus(store: TaskStore, doc_hash: str) -> None:
    """往 Redis 里塞一份已解析文档的分块（无 embedding 部署下的形态）。"""
    chunks = [
        {"text": "买方：北极星科技有限公司，注册地址上海市。", "page_idx": 0,
         "bbox": [72, 100, 500, 130], "page_size": [612, 792], "block_type": "text"},
        {"text": "合同总价为人民币 486,200.50 元，含税。", "page_idx": 1,
         "bbox": [72, 200, 500, 230], "page_size": [612, 792], "block_type": "text"},
    ]
    # 没有向量也要能存（save_chunks 要求等长的 vectors，给零长向量即可：
    # 无 embedding 部署下检索走关键词路，压根不读 vec）
    await store.save_chunks(doc_hash, chunks, [[0.0], [0.0]])


@respx.mock
async def test_extraction_end_to_end(app_state, worker_ctx):
    """两个字段：一个抽得到、一个文档里没有。**两者必须落到不同的 status**。"""
    doc_hash = "d" * 64
    await _seed_corpus(app_state.task_store, doc_hash)

    def reply(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        if "买方" in prompt:
            answer = {"found": True, "value": "北极星科技有限公司", "source": 1}
        else:
            answer = {"found": False, "value": None, "source": None}
        return Response(200, json={"choices": [
            {"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})

    respx.post(f"{VQA}/v1/chat/completions").mock(side_effect=reply)

    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "penalty": {"type": "number", "description": "逾期违约金比率"},
    }}
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http, registry=app_state.registry,
        doc_hash=doc_hash, corpus=await app_state.task_store.load_chunks(doc_hash))
    result = await extraction.run(ctx, fmt.parse_schema(schema))

    assert fmt.validate_result(result) == [], fmt.validate_result(result)
    assert result["fields"]["buyer"]["status"] == "found"
    assert result["fields"]["buyer"]["value"] == "北极星科技有限公司"
    # 抽到值必须指得回原文 —— 这是整个平面的立身之本
    assert result["fields"]["buyer"]["citations"][0]["seq"] is not None
    assert result["fields"]["buyer"]["citations"][0]["doc_hash"] == doc_hash
    # 文档里没有的字段：not_found，**不是 error，也不是编一个值**
    assert result["fields"]["penalty"]["status"] == "not_found"
    assert result["fields"]["penalty"]["value"] is None


@respx.mock
async def test_unparseable_model_output_becomes_schema_violation(app_state):
    """模型反复吐不合规输出 -> error + schema_violation。

    **绝不能变成 not_found**：那会让系统故障伪装成"文档里没有这个字段"，
    而空值在抽取结果里看起来像一个结论。
    """
    doc_hash = "e" * 64
    await _seed_corpus(app_state.task_store, doc_hash)
    respx.post(f"{VQA}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "我觉得是这样"}}]}))

    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"}}}
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http, registry=app_state.registry,
        doc_hash=doc_hash, corpus=await app_state.task_store.load_chunks(doc_hash))
    result = await extraction.run(ctx, fmt.parse_schema(schema))

    assert result["fields"]["buyer"]["status"] == "error"
    assert result["fields"]["buyer"]["degraded"] == "schema_violation"


@respx.mock
async def test_upstream_down_becomes_error_not_not_found(app_state):
    doc_hash = "f" * 64
    await _seed_corpus(app_state.task_store, doc_hash)
    respx.post(f"{VQA}/v1/chat/completions").mock(return_value=Response(503))

    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"}}}
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http, registry=app_state.registry,
        doc_hash=doc_hash, corpus=await app_state.task_store.load_chunks(doc_hash))
    result = await extraction.run(ctx, fmt.parse_schema(schema))
    assert result["fields"]["buyer"]["status"] == "error"
    assert result["fields"]["buyer"]["degraded"] == "upstream_error"


async def test_worker_fails_loudly_when_document_never_parsed(app_state, worker_ctx):
    """没有语料又没有 file_url -> 落 failed 并说清楚原因，不是静默出一份空结果。"""
    store = app_state.task_store
    await store.create_extract("t1", doc_hash="0" * 64, callback_url=None,
                               payload={"schema": GOOD_SCHEMA, "file_url": "",
                                        "engine": "", "options": {}})
    await run_extraction(worker_ctx, "t1")
    task = await store.get_extract("t1")
    assert task["status"] == "failed"
    assert "尚未解析" in task["error"]


async def test_extract_status_and_result_endpoints(client, app_state):
    store = app_state.task_store
    await store.create_extract("t2", doc_hash="1" * 64, callback_url=None, payload={})
    resp = await client.get("/v1/extract/t2")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    # 结果还没落地 -> 409，不是 404（任务在，只是没就绪）
    assert (await client.get("/v1/extract/t2/result")).status_code == 409
    assert (await client.get("/v1/extract/nope")).status_code == 404


# --------------------------------------------------------------------- rerank

async def test_rerank_404_when_not_registered(client, app_state):
    """未注册 rerank_models 时返回 404，**不是静默不重排**。

    悄悄跳过会让"上了 rerank 之后没变好"变成查不出原因的悬案。
    """
    app_state.registry.rerank_models = {}
    resp = await client.post("/v1/rerank", json={"query": "x", "texts": ["a", "b"]})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


@respx.mock
async def test_rerank_passthrough_sorts_and_trims(client, app_state):
    from app.config import ModelEntry

    app_state.registry.rerank_models = {
        "r": ModelEntry(endpoint="http://rerank:8080", default=True)}
    respx.post("http://rerank:8080/rerank").mock(return_value=Response(200, json=[
        {"index": 0, "score": 0.1, "text": "整段原文会被透传回来"},
        {"index": 2, "score": 0.9},
        {"index": 1, "score": 0.5},
    ]))
    resp = await client.post("/v1/rerank",
                             json={"query": "x", "texts": ["a", "b", "c"], "top_n": 2})
    assert resp.status_code == 200
    ranked = resp.json()
    assert [r["index"] for r in ranked] == [2, 1]        # 按分数降序
    # 只保留契约承诺的两个字段：透传 text 会让响应体翻好几倍，而调用方手上本来就有
    assert set(ranked[0]) == {"index", "score"}


@respx.mock
async def test_one_field_blowing_up_does_not_lose_the_others(app_state):
    """回归：单个字段的意外不能丢掉整批已抽好的字段（service 侧）。

    **这条此前零覆盖**：验收把 service 侧的兜底整段拆掉后 96 条测试仍全绿。
    按铁律 1 两份实现是刻意重复的，没有各自的守卫就一定会再漂回去。
    """
    doc_hash = "a" * 64
    await _seed_corpus(app_state.task_store, doc_hash)
    respx.post(f"{VQA}/v1/chat/completions").mock(side_effect=lambda r: Response(
        200, json={"choices": [{"message": {"content": json.dumps(
            {"found": True, "value": "北极星科技有限公司", "source": 1})}}]}))

    corpus = await app_state.task_store.load_chunks(doc_hash)
    ctx = extraction.ExtractContext(
        store=app_state.task_store, http=app_state.http, registry=app_state.registry,
        doc_hash=doc_hash, corpus=corpus)

    # 让其中一个字段的检索炸掉
    original = extraction.retrieve

    async def exploding(*args, **kwargs):
        if "boom" in kwargs.get("query", ""):
            raise RuntimeError("模拟：向量维度对不上")
        return await original(*args, **kwargs)

    extraction.retrieve = exploding
    try:
        schema = {"type": "object", "properties": {
            "buyer": {"type": "string", "description": "买方单位全称"},
            "boom": {"type": "string", "description": "boom 会炸的字段"},
        }}
        result = await extraction.run(ctx, fmt.parse_schema(schema))
    finally:
        extraction.retrieve = original

    assert result["fields"]["boom"]["status"] == "error"
    # 关键：另一个字段的结果没被连累
    assert result["fields"]["buyer"]["status"] == "found"
