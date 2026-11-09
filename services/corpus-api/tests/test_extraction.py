from tests.conftest import ACTOR, ORG
"""结构化抽取的单测（v1.1）。

三条不变式，每一条都对应一种**真实会发生且不会报错**的故障：

1. **not_found 与 error 分得开**。合成一个的话，"这份合同没写违约金"和
   "我们的检索挂了"在同一张表格里长得一模一样，而空值看起来像结论
2. **found 必须有出处**，且出处用的是稳定定位键 `(parse_job_id, seq)`
3. **导出的 CSV 不能是可执行文件**。抽取结果来自文档内容（不可信输入），
   Excel 会把 `=`/`+`/`-`/`@` 开头的单元格当公式执行
"""
import asyncio
import json

import pytest
import respx
from httpx import Response

from ddp_core.chunking import layout_to_chunks
from ddp_core.extract_format import parse_schema, validate_schema
from ddp_corpus.extraction import ExtractContext, run as run_extraction
from ddp_corpus.models import Chunk, Document, ParseJob
from ddp_corpus.routers.extractions import _csv_safe
from ddp_corpus.config import settings
from ddp_core.search import MemoryIndex
from ddp_core.tokenize import tokenized

from tests.conftest import CHAT, EMBEDDINGS, SERVICE, usage_events

SCHEMA = {
    "type": "object",
    "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "penalty": {"type": "number", "description": "逾期付款违约金比率"},
    },
    "required": ["buyer"],
}


# --------------------------------------------------------------------- 分块

def test_table_stays_its_own_chunk_with_structure():
    """表格独立成块并保住 HTML。

    合并进正文的话，出处 bbox 会横跨整片版心、行列关系也拍平没了 ——
    抽取平面就再也找不到记录数组。
    """
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"type": "title", "bbox": [0, 0, 100, 20],
         "lines": [{"spans": [{"content": "第三章 价款"}]}]},
        {"type": "text", "bbox": [0, 30, 100, 60],
         "lines": [{"spans": [{"content": "以下为明细。"}]}]},
        {"type": "table", "bbox": [0, 70, 100, 200], "blocks": [
            {"type": "table_body", "lines": [{"spans": [
                {"content": "项目 金额", "html": "<table><tr><td>项目</td></tr></table>"}]}]}]},
    ]}]}
    chunks = layout_to_chunks(layout)
    assert [c["block_type"] for c in chunks] == ["text", "table"]
    assert chunks[1]["bbox"] == [0, 70, 100, 200], "表格 bbox 不能被邻居撑大"
    assert chunks[1]["table_html"] == "<table><tr><td>项目</td></tr></table>"
    # 标题作上下文前缀而不是独立成块（标题太短，单独成块检索不到）
    assert all("第三章 价款" in c["text"] for c in chunks)


def test_old_layout_without_type_still_chunks():
    """回归：2026-08-23 之前归档的 layout.json 没有 type 字段，仍然要能重建索引。

    缺 type 一律按 text 处理 —— **与 service 侧 layout.normalize_type 同一判据**。
    两边不一致的话同一份版面会切出不同的块，而出处的稳定定位键 seq 正是按块序算的，
    历史出处会指到错误的块。
    """
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"bbox": [0, 0, 100, 20], "lines": [{"spans": [{"content": "没有 type 的老块"}]}]},
    ]}]}
    chunks = layout_to_chunks(layout)
    assert len(chunks) == 1 and chunks[0]["block_type"] == "text"


def test_chunks_carry_tokenized_text():
    """D2：中文分词列必须在分块阶段算好。

    `to_tsvector('simple', text)` 会把整段中文当成**一个 token**，
    于是"混合检索"在中文文档上实际只有向量一条腿。
    """
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"type": "text", "bbox": [0, 0, 100, 20],
         "lines": [{"spans": [{"content": "价税合计人民币壹万贰仟元整"}]}]},
    ]}]}
    chunk = layout_to_chunks(layout)[0]
    assert " " in chunk["text_tokenized"], "整段中文没被切开 = 关键词路等于没有"
    assert "合计" in chunk["text_tokenized"].split()


# --------------------------------------------------------------------- 抽取编排

async def _seed_document(session, user_id: str) -> tuple[Document, ParseJob]:
    document = Document(uploaded_by=user_id, doc_id="d" * 64, filename="contract.pdf",
                        mime="application/pdf", object_key="", index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/x/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id

    texts = ["买方：北极星科技有限公司，注册地址上海市浦东新区。",
             "合同总价为人民币肆拾捌万陆仟贰佰元整，含税。"]
    for seq, text in enumerate(texts):
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=seq, bbox=[72, 100, 500, 130], page_size=[612, 792],
                          text=text, char_len=len(text), block_type="text",
                          text_tokenized=tokenized(text),
                          # MemoryIndex 走纯 Python 余弦；给一个可控向量即可
                          embedding=[1.0, 0.0] if seq == 0 else [0.0, 1.0]))
    await session.commit()
    return document, job


def _embedding_reply(request) -> Response:
    inputs = json.loads(request.content)["input"]
    return Response(200, json={"data": [
        {"index": index, "embedding": [1.0, 0.0]} for index in range(len(inputs))]})


def _chat_reply(answer: dict) -> Response:
    return Response(200, json={"choices": [
        {"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})


@respx.mock
async def test_found_and_not_found_are_distinguished(session, app_state):
    """一个抽得到、一个文档里没有 —— **必须落到不同的 status**。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)

    def reply(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        # Branch on the requested field, never on source text or sibling definitions.
        if payload["field"]["name"] == "buyer":
            return _chat_reply({"found": True, "value": "北极星科技有限公司", "support": {"value_source": 1, "context_sources": []}})
        return _chat_reply({"found": False, "value": None, "support": None})

    respx.post(CHAT).mock(side_effect=reply)

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    outcome = await run_extraction(ctx, parse_schema(SCHEMA))

    assert outcome.fields["buyer"]["status"] == "found"
    assert outcome.fields["buyer"]["value"] == "北极星科技有限公司"
    # 出处必须是稳定定位键，不是 chunk_id（chunk_id 每次 reindex 都会重铸）
    citation = outcome.fields["buyer"]["citations"][0]
    assert citation["parse_job_id"] == job.id and citation["seq"] is not None
    # 文档里没有的字段：not_found，**不是 error 也不是编一个值**
    assert outcome.fields["penalty"]["status"] == "not_found"
    assert outcome.fields["penalty"]["value"] is None
    assert outcome.status == "ok", "可选字段没抽到不该把整体降成 partial"


@respx.mock
async def test_garbage_model_output_is_schema_violation_not_not_found(session, app_state):
    """**绝不能把系统故障伪装成"文档里没有"**：空值在结果表里看起来像一个结论。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(return_value=Response(200, json={
        "choices": [{"message": {"content": "我觉得应该是那家公司吧"}}]}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    outcome = await run_extraction(ctx, parse_schema(SCHEMA))
    assert outcome.fields["buyer"]["status"] == "error"
    assert outcome.fields["buyer"]["degraded"] == "schema_violation"
    assert outcome.status == "partial"


@respx.mock
async def test_embedding_down_is_visible_on_the_field(session, app_state):
    """向量化挂了要打标。静默退回关键词路是这个项目吃过大亏的地方。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(return_value=Response(503))
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "support": {"value_source": 1, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    outcome = await run_extraction(ctx, parse_schema(SCHEMA))
    degraded = [f.get("degraded") for f in outcome.fields.values()]
    assert "embedding_unavailable" in degraded, degraded


async def _a_user(session, username: str = "ex") -> str:
    """"一个用户"现在就是一个 actor id —— 用户住在 control schema。"""
    return f"actor-{username}"


# --------------------------------------------------------------------- API

async def test_template_crud_and_schema_guard(actor_client):
    bad = await actor_client.post("/api/extractions/templates", json={
        "name": "坏模板", "description": "", "schema_json": {
            "type": "object", "properties": {"a": {"type": "string"}}}})
    assert bad.status_code == 400, "缺 description 的 schema 必须当场被拒"
    assert bad.json()["error"]["code"] == "invalid_schema"

    created = await actor_client.post("/api/extractions/templates", json={
        "name": "采购合同", "description": "关键条款", "schema_json": SCHEMA})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["field_count"] == 2 and body["kind"] == "object"
    # 线上字段名必须是 schema_json（pydantic 属性名换了，别名不能漏）
    assert "schema_json" in body and "doc_schema" not in body

    listed = (await actor_client.get("/api/extractions/templates")).json()
    assert len(listed) == 1

    duplicate = await actor_client.post("/api/extractions/templates", json={
        "name": "采购合同", "description": "", "schema_json": SCHEMA})
    assert duplicate.status_code == 409

    assert (await actor_client.delete(
        f"/api/extractions/templates/{body['id']}")).status_code == 204
    assert (await actor_client.get("/api/extractions/templates")).json() == []


async def test_soft_deleted_template_name_can_be_reused(actor_client):
    """回归：唯一约束是 (actor_id, name) 且不含 deleted_at ——
    软删后重建同名模板直接插会撞约束报 500。"""
    first = (await actor_client.post("/api/extractions/templates", json={
        "name": "同名", "description": "", "schema_json": SCHEMA})).json()
    await actor_client.delete(f"/api/extractions/templates/{first['id']}")
    again = await actor_client.post("/api/extractions/templates", json={
        "name": "同名", "description": "复活", "schema_json": SCHEMA})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first["id"], "应该复活原行，而不是插一条新的"


async def test_run_rejects_documents_without_index(actor_client, session):
    """索引没就绪的文档抽不了。**当场说清楚**，别让它们跑完变成一堆空结果 ——
    空值看起来像"文档里没有"，那是抽取里最危险的误导。"""
    
    document = Document(uploaded_by=ACTOR, organization_id=ORG, doc_id="z" * 64, filename="未索引.pdf",
                        mime="application/pdf", index_status="none")
    session.add(document)
    await session.commit()

    template = (await actor_client.post("/api/extractions/templates", json={
        "name": "t", "description": "", "schema_json": SCHEMA})).json()
    resp = await actor_client.post("/api/extractions/runs", json={
        "document_ids": [document.id], "template_id": template["id"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "index_not_ready"


# --------------------------------------------------------------------- 导出安全

@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-2+3", "@import", "\tx", "\rx"])
def test_csv_formula_injection_is_neutralized(value):
    """抽取结果来自文档内容（不可信输入）。Excel/WPS 会把这些开头的单元格当公式执行。

    **不能只在前端做**：导出的文件会被转发、归档、二次打开，
    防护必须在产生文件的地方。
    """
    assert _csv_safe(value).startswith("'")


@pytest.mark.parametrize("value", ["正常文本", "486200.5", "2026-03-14", None])
def test_csv_leaves_normal_values_alone(value):
    out = _csv_safe(value)
    assert not out.startswith("'")
    assert out == ("" if value is None else str(value))


def test_schema_validator_rejects_unsupported_constructs():
    for schema in (
        {"type": "object", "properties": {"a": {"type": "object", "description": "x"}}},
        {"type": "object", "oneOf": [], "properties": {"a": {"type": "string",
                                                             "description": "x"}}},
        {"type": "array"},
    ):
        assert validate_schema(schema), f"这个 schema 本该被拒：{schema}"


# --------------------------------------------------------------------- 验收回归（M9 二轮）

def test_keyword_query_is_or_not_and():
    """回归：关键词路必须是 **OR**。

    这一条此前**一条测试都盖不到**，而它是静默的：
    `websearch_to_tsquery` 把多个词拼成 AND，分词上线后
    「字段名 + description」切出四五个词，要求一个块同时含全部 —— 真 PG 实测恒不命中。
    而单测的 MemoryIndex 是 OR，于是单测绿、生产红。
    """
    from ddp_core.search import _or_tsquery

    assert _or_tsquery("buyer 买方单位全称") == "buyer | 买方 | 单位 | 全称"
    # 切不出词时不能返回空串（to_tsquery 会抛语法错，整条关键词路被 except 吞掉）
    assert _or_tsquery("") and "|" not in _or_tsquery("")
    # tsquery 的元字符必须剥掉，否则 to_tsquery 直接语法错
    assert all(ch not in _or_tsquery("a&b (c):d") for ch in "&():")


def test_keyword_sql_does_not_use_websearch_to_tsquery():
    """SQL 形状守卫：`websearch_to_tsquery` 是 AND 语义，不许再出现在关键词路里。

    单测跑的是 MemoryIndex，摸不到这段 SQL —— 所以只能守形状。
    真正的语义验证在真 PG 上做过（见 docs/EVAL-extraction.md 与提交说明）。
    """
    import inspect
    import re

    from ddp_core.search import PgVectorIndex

    source = inspect.getsource(PgVectorIndex.search)
    # **先剥掉注释行**：那段长注释里正记着"曾经是 websearch_to_tsquery"这条教训，
    # 为了让守卫通过而删掉注释是本末倒置 —— 守卫要查的是真的 SQL
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert not re.search(r"websearch_to_tsquery\s*\(", code), "关键词路又变回 AND 语义了"
    assert "to_tsquery('simple', :q)" in code


async def test_memory_index_keyword_path_is_or_by_behaviour(session, app_state):
    """MemoryIndex 的关键词匹配必须是 **OR**（行为断言，不是名字存在性检查）。

    早先这条写成「源码里有没有出现 `_query_tokens`」—— 那**防不住它自己声称要防的事**：
    tokenizer 一样但匹配语义不一样，正是上一轮的真 bug。
    一个查不出它要查的东西的测试，比没有测试更糟（同 EVAL 的恒真指标）。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    # query 里只有一个词命中得了（"买方"），其余词文档里没有。
    # AND 语义下应当零命中，OR 语义下应当命中
    hits = await MemoryIndex().search(
        session, vector=None, query="买方 完全不存在的词 另一个不存在的词",
        document_id=document.id, limit=4, candidates=8,
        min_similarity=settings.qa_min_similarity)
    assert hits, "关键词路变成 AND 了 —— 与 PgVectorIndex 的 OR 语义对不上"


def test_pg_keyword_path_uses_or_tsquery():
    """PgVectorIndex 摸不到（单测跑 MemoryIndex），只能守 SQL 形状。

    真正的语义验证在真 PG 上做过：AND 对 400 块文档命中 0/400，OR 正常命中且
    ts_rank_cd 把真命中排在第一。
    """
    import inspect
    import re

    from ddp_core.search import PgVectorIndex

    code = "\n".join(line for line in inspect.getsource(PgVectorIndex.search).splitlines()
                     if not line.lstrip().startswith("#"))
    assert not re.search(r"websearch_to_tsquery\s*\(", code)
    assert "_or_tsquery" in inspect.getsource(__import__("ddp_core.search", fromlist=["x"]))


def test_rerank_unavailable_is_in_the_contract_vocabulary():
    """回归：`rerank_unavailable` 是本层真会产出的降级值。

    不在词汇表里的话，一份完全合法的抽取结果会被自己的 `validate_result` 判成不合规
    （service 侧据此会把任务直接标 failed）。
    """
    from ddp_core.extract_format import DEGRADED_VALUES, EXTRACT_VERSION, validate_result

    assert "rerank_unavailable" in DEGRADED_VALUES
    result = {
        "extract_version": EXTRACT_VERSION, "status": "ok",
        "degraded": "rerank_unavailable",
        "fields": {"a": {"status": "found", "value": "x",
                         "citations": [{"page_idx": 0}], "verified": False,
                         "degraded": "rerank_unavailable"}},
    }
    assert validate_result(result) == []


def test_block_type_matches_the_service_side_vocabulary():
    """回归：块类型判据必须与 service 侧逐条一致。

    不一致 ⇒ 同一份版面切出不同的块 ⇒ 出处的稳定定位键 seq 对不上 ⇒
    **历史出处指到错误的块，且完全静默**。
    """
    from ddp_core.blocks import normalize_type

    expected = {
        "text": "text", "plain text": "text", "Table": "table", "table_body": "table",
        "image_body": "figure", "figure_caption": "figure",
        "interline_equation": "equation", "isolate_formula": "equation",
        "index": "list", "sub_title": "title", "TITLE": "title",
        # 有 type 但不认识 -> other；压根没有 type -> text。两种"不认识"是不同的事
        "footnote": "other", "某个没见过的": "other",
    }
    for raw, want in expected.items():
        assert normalize_type(raw) == want, f"{raw!r} 应归成 {want}"
    assert normalize_type(None) == "text"
    assert normalize_type("") == "text"


@respx.mock
async def test_vision_model_down_is_visible_not_just_unverified(session, app_state):
    """回归：视觉模型挂了必须打 `vision_unavailable`。

    此前这一路**一个标都不打**：用户看到的是一整表 `verified: false`，
    与"核对预算用完所以没核对"长得一模一样 —— 而 extract_verify 默认是开的。
    """
    from unittest.mock import patch

    document, job = await _seed_document(session, (await _a_user(session)))
    document.object_key = "sources/x.pdf"
    await session.commit()
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "support": {"value_source": 1, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=True)
    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"}}}

    # 裁图成功、但视觉核对返回 None（模型不可达）
    with patch("ddp_corpus.extraction.get_or_create_crop", return_value="results/x/crops/0_a.png"), \
         patch.object(app_state.storage, "get", return_value=b"png"), \
         patch("ddp_corpus.extraction.verify_parse_consistency", return_value=None):
        outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["buyer"]
    assert field["status"] == "found"
    assert field["verified"] is False
    assert field["degraded"] == "vision_unavailable", \
        "核对打了模型却没结果，必须打标 —— 否则与'预算用完'分不开"


@pytest.mark.parametrize("kind", ["object", "array"])
@respx.mock
async def test_table_transcription_checks_visible_cells_not_html(session, app_state, kind):
    from unittest.mock import patch

    from sqlalchemy import delete, select

    document, job = await _seed_document(session, await _a_user(session))
    document.object_key = "sources/table.pdf"
    chunk = await session.scalar(select(Chunk).where(
        Chunk.parse_job_id == job.id, Chunk.seq == 0))
    await session.execute(delete(Chunk).where(
        Chunk.parse_job_id == job.id, Chunk.seq != 0))
    text = " ".join(str(value) for value in range(1, 11))
    chunk.text = text
    chunk.text_tokenized = tokenized(text)
    chunk.char_len = len(text)
    chunk.block_type = "table"
    chunk.table_html = "<table><tr>" + "".join(
        f"<td>{value}</td>" for value in range(1, 11)) + "</tr></table>"
    await session.commit()
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.get(f"{SERVICE}/v1/models").mock(return_value=Response(200, json={"data": [{
        "id": "test-vision", "default": True, "capabilities": ["vision"],
        "transcribe_prompt": "把图里的文字原样抄写出来",
    }]}))

    def reply(request):
        messages = json.loads(request.content)["messages"]
        if any(isinstance(message["content"], list) for message in messages):
            return Response(200, json={"choices": [{"message": {"content": text}}]})
        if kind == "array":
            return _chat_reply({"records": [{"source": 1, "values": {"reading": 10}}]})
        return _chat_reply({"found": True, "value": 10,
                            "support": {"value_source": 1, "context_sources": []}})

    respx.post(CHAT).mock(side_effect=reply)
    schema = {"type": "object", "properties": {
        "reading": {"type": "integer", "description": "tenth measurement reading"}}}
    if kind == "array":
        schema = {"type": "array", "items": schema}
    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=True)
    with patch("ddp_corpus.extraction.get_or_create_crop", return_value="results/table.png"), \
         patch.object(app_state.storage, "get", return_value=b"png"):
        outcome = await run_extraction(ctx, parse_schema(schema))
    field = (outcome.records[0]["fields"]["reading"] if kind == "array"
             else outcome.fields["reading"])
    assert field["status"] == "found" and field["value"] == 10
    assert field["verified"] is True, (field, outcome.degraded, ctx.usage)
    assert field["degraded"] is None


@respx.mock
async def test_one_field_blowing_up_does_not_lose_the_others(session, app_state):
    """回归：单个字段的意外不能丢掉整批已抽好的字段。

    `asyncio.gather` 默认在第一个异常处抛出、其余结果作废 —— 于是
    "一个字段 error、其余照常"被升级成"整份文档白跑"，三态设计白设计。
    真实触发点：换 embedding 模型后 pgvector 报 different vector dimensions。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "support": {"value_source": 1, "context_sources": []}}))

    class ExplodingIndex(MemoryIndex):
        async def search(self, session, **kwargs):        # noqa: ANN001
            if "boom" in kwargs.get("query", ""):
                raise RuntimeError("pgvector: different vector dimensions 1024 and 768")
            return await super().search(session, **kwargs)

    ctx = ExtractContext(session=session, index=ExplodingIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "boom": {"type": "string", "description": "boom 会炸的字段"},
    }}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert outcome.fields["boom"]["status"] == "error"
    # 关键：另一个字段的结果没被连累
    assert outcome.fields["buyer"]["status"] == "found"
    assert outcome.fields["buyer"]["value"] == "北极星科技有限公司"


@respx.mock
async def test_concurrent_fields_do_not_share_the_session_at_once(session, app_state):
    """回归：字段并发检索时不能同时动同一个 AsyncSession。

    真库上后到的那个字段在 SQLAlchemy 里炸（"Can't operate on closed transaction
    inside context manager"），被兜底吞成 upstream_error：两字段的 schema 稳定丢一个
    （2026-10-15 生产实测）。MemoryIndex 不碰 session，所以这里用一个会在并发进入时
    报错的 index 模拟真库的约束。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "support": {"value_source": 1, "context_sources": []}}))

    class OneAtATimeIndex(MemoryIndex):
        in_use = False

        async def search(self, session, **kwargs):        # noqa: ANN001
            if self.in_use:
                raise RuntimeError("concurrent operations on one AsyncSession")
            self.in_use = True
            try:
                await asyncio.sleep(0.01)                 # 一次真库往返期间会让出事件循环
                return await super().search(session, **kwargs)
            finally:
                self.in_use = False

    ctx = ExtractContext(session=session, index=OneAtATimeIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "seller": {"type": "string", "description": "卖方单位全称"},
    }}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert {name: f["status"] for name, f in outcome.fields.items()} == {
        "buyer": "found", "seller": "found"}


# --------------------------------------------------------------------- 验收回归（M9 三轮）

async def test_stale_citation_is_marked_unresolved_not_pointed_at_the_wrong_block(session):
    """回归：`seq` 还在、但那个位置已经换了内容时，必须判 **未解析**。

    M9 改了分块规则（表格/公式/图片独立成块、标题作前缀），同一份归档重建索引
    会切出不同数量、不同 seq 的块。只查 `(parse_job_id, seq)` 存不存在的话，
    老 citation 照样"查得到"，指的却是另一段原文 —— 而 UI 只看 `resolved`，
    用户会看到一条可点开的出处，snippet 是旧文本、高亮指向别处。
    **这正是这个项目定义的最恶劣错误：带着已验证标记的假出处。**

    阶段 4 起走的是 `app.evidence.load_citations`（老的那套接回逻辑连同
    `messages.citations` 列一起删了）。判据没变，且严格了一档：
    指纹对得上才算数，不再只看 snippet 还在不在块里。
    """
    from sqlalchemy import select

    from ddp_corpus.evidence import load_citations, record_evidence

    document, job = await _seed_document(session, (await _a_user(session)))
    chunks = (await session.execute(
        select(Chunk).where(Chunk.parse_job_id == job.id).order_by(Chunk.seq))).scalars().all()

    def cite(seq, snippet):
        return {"chunk_id": "x", "parse_job_id": job.id, "seq": seq, "page_idx": 0,
                "bbox": [72, 100, 500, 130], "crop_key": None, "snippet": snippet,
                "score": 0.03, "similarity": 0.7}

    # 三条出处，写下时全都指得好好的
    await record_evidence(session, [cite(0, chunks[0].text[:20]), cite(1, chunks[1].text[:20])],
                          source_kind="extract_field", source_id="item-1:buyer")
    # 第三条指向一个根本不存在的块 —— 写的时候就该被跳过
    await record_evidence(session, [cite(99, "根本不存在的块")],
                          source_kind="extract_field", source_id="item-1:missing")
    await session.commit()

    # 模拟"重建索引后 seq=1 上换了内容"
    chunks[1].text = "这是重建索引之后换上去的另一段文本，与当初作证的那段无关。"
    await session.commit()

    out = await load_citations(session, source_kind="extract_field",
                               source_ids=["item-1:buyer", "item-1:missing"])
    by_seq = {c["seq"]: c for c in out["item-1:buyer"]}

    assert by_seq[0]["resolved"] is True, "内容还对得上的出处不该被误判失效"
    assert by_seq[0]["chunk_id"], "对得上就要刷新 chunk_id"
    assert by_seq[1]["resolved"] is False, \
        "seq 还在但内容换了 —— 必须说接不回去，绝不许指到错块"
    assert by_seq[1]["chunk_id"] is None, "接不回去就不许给 chunk_id"
    assert "item-1:missing" not in out, "指向不存在的块的出处，写的时候就该被跳过"


async def test_hard_deleted_document_does_not_break_the_whole_run(session, app_state):
    """回归：抽取期间文档被**硬删**时不能插 ExtractionItem。

    `ExtractionItem.document_id` 是非空外键，指向已不存在的文档会撞 FK 约束 ->
    commit 抛 IntegrityError -> 外层 except 再插一次再抛 -> 整批 run 打成 failed。
    比"进度条差一格"糟得多。**单测能抓到全靠 conftest 开了 PRAGMA foreign_keys=ON**
    （SQLite 默认不强制，真 PG 一定会炸）。
    """
    from sqlalchemy import select as sa_select

    from ddp_core.extract_format import parse_schema as ps
    from ddp_corpus.models import ExtractionItem, ExtractionRun
    from ddp_corpus.routers.extractions import _extract_one

    user_id = await _a_user(session)
    run = ExtractionRun(actor_id=user_id, name="t", schema_json=SCHEMA, kind="object",
                        status="running", document_count=1)
    session.add(run)
    await session.commit()

    # 传一个根本不存在的 document_id
    await _extract_one(session, run.id, "ffffffffffffffffffffffffffffffff", ps(SCHEMA),
                       storage=app_state.storage, http=app_state.http,
                       index=MemoryIndex(), verify=False)

    # 直接查列而不是取 ORM 实例：_extract_one 走的是 Core UPDATE，
    # 身份映射里的那个实例是陈旧的（且触发惰性刷新会撞 MissingGreenlet）
    done, error = (await session.execute(
        sa_select(ExtractionRun.done_count, ExtractionRun.error)
        .where(ExtractionRun.id == run.id))).one()
    assert done == 1, "进度必须推进，否则进度条永远差一格"
    assert "被删除" in (error or ""), "原因要记下来"
    items = (await session.execute(sa_select(ExtractionItem))).scalars().all()
    assert not items, "不能给已删除的文档插 item（外键会炸）"


async def test_extract_one_runs_against_a_real_document(session, app_state):
    """**抽取平面对真实文档必须真的跑得起来。**

    验收（1b）抓到：`_extract_one` 里还留着 `document.user_id`，而语料共享化把
    那一列改名成了 `uploaded_by` —— 于是**任何一次对真实文档的抽取都必然
    AttributeError**，被外层 `except Exception` 接住、整批 run 打成 failed。

    143 个用例全绿是因为唯一碰 `_extract_one` 的那条喂的是**不存在的
    document_id**，在 `if document is None` 就 return 了，走不到出事那行。
    这条补上"真文档"这一半。

    顺带钉住计量归属：抽取记在**发起 run 的人**头上，不是上传者头上 ——
    语料共享后任何人都能对任一文档发起抽取，按上传者计费等于别人能随意
    花掉他的额度。
    """
    from sqlalchemy import select as sa_select

    from ddp_core.extract_format import parse_schema as ps
    from ddp_corpus.models import ExtractionItem, ExtractionRun
    from ddp_corpus.routers.extractions import _extract_one

    document, _job = await _seed_document(session, await _a_user(session))
    _job.index_status = "ready"
    initiator = await _a_user(session, username="initiator")
    assert initiator != document.uploaded_by, "发起人要与上传者不同才测得出归属"

    # This test intentionally uses another actor's content: grant an explicit public resource.
    from ddp_corpus.models import Resource, ResourceVersion
    resource = Resource(owner_id=document.uploaded_by, uploaded_by=document.uploaded_by,
                        organization_id=document.organization_id, publication="published")
    session.add(resource)
    await session.flush()
    session.add(ResourceVersion(resource_id=resource.id, document_id=document.id,
                                source_digest=document.doc_id, parse_job_id=_job.id))
    await session.flush()
    run = ExtractionRun(actor_id=initiator, name="t", schema_json=SCHEMA, kind="object",
                        status="running", document_count=1,
                        resource_context={"principal_id": initiator, "resources": {document.id: resource.id}})
    session.add(run)
    await session.commit()

    await _extract_one(session, run.id, document.id, ps(SCHEMA),
                       storage=app_state.storage, http=app_state.http,
                       index=MemoryIndex(), verify=False)

    error = await session.scalar(
        sa_select(ExtractionRun.error).where(ExtractionRun.id == run.id))
    assert not error, f"抽取不该报错，实际：{error}"
    items = (await session.execute(
        sa_select(ExtractionItem).where(ExtractionItem.run_id == run.id))).scalars().all()
    assert items, "至少要落一条结果（哪怕是 not_found）"

    billed = [e["actor_id"] for e in await usage_events(session, "extract")]
    assert billed == [initiator], \
        f"抽取要记在发起人头上，不是上传者（{document.uploaded_by}）头上，实际 {billed}"

# --------------------------------------------------------------------- 引用派生/指针/分面


def test_derived_hit_cites_generated_with_derived_from():
    """派生命中必须引用派生 evidence：source_type="generated"，derived_from 指回源原子。

   此前 `_citation` 硬编码 `"source_type": "source"`，视觉块的派生理解被当成
    原文引用 —— 生成物与原文不可区分（不变式 3），且 snippet 取的是 OCR 文本
    而不是引用指向的那份派生文本。
    """
    from ddp_core.hits import Hit
    from ddp_corpus.extraction import _citation

    derived = Hit(chunk_id="c1", parse_job_id="j1", seq=0, page_idx=0, text="OCR 原文",
                  derived_text="VLM 对表格的理解", evidence_id="src-atom",
                  derived_evidence_id="derived-atom", block_type="table",
                  score=0.03, similarity=0.8)
    citation = _citation(derived, None)
    assert citation["source_type"] == "generated"
    assert citation["evidence_id"] == "derived-atom"
    assert citation["derived_from"] == "src-atom"
    assert "VLM" in citation["snippet"], "snippet 必须是引用指向的派生文本"


def test_source_hit_citation_is_unchanged_shape():
    """普通命中形状不变：source_type="source"，derived_from=None（显式给 None，
    不是省略 —— 消费方在两个平面之间不写两套取字段代码）。"""
    from ddp_core.hits import Hit
    from ddp_corpus.extraction import _citation

    hit = Hit(chunk_id="c1", parse_job_id="j1", seq=0, page_idx=0, text="买方正文",
              evidence_id="src-atom", block_type="text", score=0.03, similarity=0.8)
    citation = _citation(hit, None)
    assert citation["source_type"] == "source"
    assert citation["evidence_id"] == "src-atom"
    assert citation["derived_from"] is None
    assert "买方" in citation["snippet"]




@respx.mock
@pytest.mark.parametrize("pointer", [999, "abc", None, True, 1.9])
async def test_bad_source_pointer_is_error_with_schema_violation(
        session, app_state, pointer):
    """端到端：source 指到不存在的资料 -> error + schema_violation，无 top-1 出处。

    判 error 而不是 not_found：错的是模型输出的格式契约（source 必须是
    1..N 的整数），不是文档内容；若判 not_found，会把生成失败伪装成
    “文档里没有”。已有更严重的标时不覆盖（degraded or 语义）。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "support": {"value_source": pointer, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    outcome = await run_extraction(ctx, parse_schema(SCHEMA))

    field = outcome.fields["buyer"]
    assert field["status"] == "error"
    assert field["value"] is None
    assert field["citations"] == [], "幻觉引用不许挂 top-1 出处"
    assert field["degraded"] == "schema_violation"


@respx.mock
async def test_retrieve_fans_out_compound_query_through_shared_path(session, app_state):
    """_retrieve 走共享 search_query：复合问题按分面拆查，每面一批向量。

    此前直调 `index.search` 只发一个 query + 一个向量，复合问题的第二个
    分面在抽取平面永远拿不到专属候选（问答与 /api/search 早已分面）。
    """
    from ddp_core.search import search_query
    from ddp_corpus import extraction as extraction_mod

    document, job = await _seed_document(session, (await _a_user(session)))

    queries, vectors = [], []

    class RecordingIndex(MemoryIndex):
        async def search(self, session, **kwargs):        # noqa: ANN001
            queries.append(kwargs.get("query"))
            return await super().search(session, **kwargs)

    async def recording_embed(texts):
        vectors.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    question = ("What supply voltage does the device use "
                "and which wireless protocol does it support?")

    # 共享路径本身确实拆分面（对照组：直接调 search_query 至少两路查询）
    _, _ = await search_query(
        session, RecordingIndex(), embed=recording_embed, query=question,
        document_id=document.id, limit=4, candidates=8,
        min_similarity=settings.qa_min_similarity)
    assert len(queries) > 1, f"复合问题该拆分面，实际只查了 {queries}"
    assert len(vectors[0]) > 1, "分面查询该批量向量化"
    assert len(queries) == len(vectors[0]), "每面一路检索、一个向量"

    # 抽取的 _retrieve 同样扇出（而非直调 index.search 只查一次）
    queries.clear()
    embed_calls = []
    real_batched = extraction_mod.embed_batched

    async def spy_batched(http, texts):                   # noqa: ANN001
        embed_calls.append(list(texts))
        return await real_batched(http, texts)

    respx.post(EMBEDDINGS).mock(side_effect=lambda r: Response(200, json={
        "data": [{"index": i, "embedding": [1.0, 0.0]}
                 for i in range(len(json.loads(r.content)["input"]))]}))
    ctx = ExtractContext(session=session, index=RecordingIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    from unittest.mock import patch
    with patch.object(extraction_mod, "embed_batched", spy_batched):
        await extraction_mod._retrieve(ctx, question, k=4)
    assert len(queries) > 1, f"_retrieve 该经 search_query 扇出，实际只查了 {queries}"
    assert embed_calls and len(embed_calls[0]) > 1, "扇出后该批量向量化所有分面"


def test_degraded_priority_orders_keyword_after_embedding():
    """keyword_unavailable 排在 embedding_unavailable 之后、vision 之前。

    关键词腿瘸了（只走向量路）比整条语义路没跑轻，但比 vision/rerank 这类
    后处理缺席重。字面量来自契约 enums.yaml。
    """
    from ddp_corpus.extraction import _DEGRADED_PRIORITY, rollup_degraded

    assert _DEGRADED_PRIORITY.index("keyword_unavailable") == \
        _DEGRADED_PRIORITY.index("embedding_unavailable") + 1
    assert _DEGRADED_PRIORITY.index("keyword_unavailable") < \
        _DEGRADED_PRIORITY.index("vision_unavailable")
    assert rollup_degraded([{"degraded": "keyword_unavailable"},
                            {"degraded": "vision_unavailable"}]) == "keyword_unavailable"
    assert rollup_degraded([{"degraded": "keyword_unavailable"},
                            {"degraded": "embedding_unavailable"}]) == "embedding_unavailable"




@respx.mock
async def test_number_without_source_support_is_error_not_found(session, app_state):
    """数值在所引块原文里找不到 -> error + schema_violation，不许 found。

    R7 实测的失败缝：CPU 对 unsupervised_language_count 输出 12，
    引用的 seq142（数据源表）原文里根本没有 12 —— 带出处的错值。
    只做机械必要条件，不宣称语义验证。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": 12, "support": {"value_source": 1, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "unsupervised_language_count": {
            "type": "integer",
            "description": "Number of languages represented in the unsupervised training data."}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["unsupervised_language_count"]
    assert field["status"] == "error"
    assert field["value"] is None
    assert field["citations"] == []
    assert field["degraded"] == "schema_violation"


@respx.mock
async def test_normalized_number_in_source_still_found(session, app_state):
    """原文 `8,192` 与值 `8192` 视为同一数值：归一化核验不许误杀真命中。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)

    def reply(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        if "maximum_input_length" in prompt:
            return _chat_reply({"found": True, "value": 8192, "support": {"value_source": 1, "context_sources": []}})
        return _chat_reply({"found": False, "value": None, "support": None})

    respx.post(CHAT).mock(side_effect=reply)

    from sqlalchemy import select as sa_select
    chunk = (await session.execute(sa_select(Chunk).where(
        Chunk.document_id == document.id, Chunk.seq == 0))).scalar_one()
    chunk.text = "long documents of up to 8,192 input tokens."
    chunk.text_tokenized = tokenized(chunk.text)
    await session.commit()

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "maximum_input_length": {
            "type": "integer",
            "description": "Maximum supported input length stated by the paper, as a number."}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["maximum_input_length"]
    assert field["status"] == "found"
    assert field["value"] == 8192
    assert field["citations"], "归一化对得上就必须有出处"


@respx.mock
async def test_decorated_number_value_still_found(session, app_state):
    """模型给带单位装饰的单数字（"3 times"）时不判死：与字面见证共用记号写法。

    旧 coerce 整体 strip 后 float() 逐字解析，"3 times" 直接 CoerceError ——
    而见证侧认得出同一个记号。两道门必须同进退，否则同一个值一前一后两个答案。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "3 times", "support": {"value_source": 1, "context_sources": []}}))

    from sqlalchemy import select as sa_select
    chunk = (await session.execute(sa_select(Chunk).where(
        Chunk.document_id == document.id, Chunk.seq == 0))).scalar_one()
    chunk.text = "memory requirement by 3 times compared to baseline."
    chunk.text_tokenized = tokenized(chunk.text)
    await session.commit()

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "memory_reduction_factor": {
            "type": "integer",
            "description": "Factor of memory requirement reduction stated in the abstract."}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["memory_reduction_factor"]
    assert field["status"] == "found"
    assert field["value"] == 3
    assert field["citations"], "单数字装饰回答对得上见证就必须有出处"


@respx.mock
async def test_ambiguous_number_value_is_error_not_first_number(session, app_state):
    """模型给含多个数字的字符串（"−40∼105"）时不许静默取第一个：整体判 error。

    猜出来的值没法追溯 —— 区间里取 -40 还是 105 都是编，两道门都必须拒绝。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "−40∼105 °C", "support": {"value_source": 1, "context_sources": []}}))

    from sqlalchemy import select as sa_select
    chunk = (await session.execute(sa_select(Chunk).where(
        Chunk.document_id == document.id, Chunk.seq == 0))).scalar_one()
    chunk.text = "operating range −40∼105 °C for the module."
    chunk.text_tokenized = tokenized(chunk.text)
    await session.commit()

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "operating_temperature": {
            "type": "integer",
            "description": "Operating temperature of the module."}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["operating_temperature"]
    assert field["status"] == "error"
    assert field["value"] is None
    assert field["citations"] == []
    assert field["degraded"] == "schema_violation"
@respx.mock
async def test_field_retrieve_appends_adjacent_source_atom(session, app_state, monkeypatch):
    """字段检索把截断句的下一块作为独立资料追加：不挤占命中、可被引用。

    R7 实测的失败缝：seq36 在 “1.2 billion text pairs” 处截断，
    194 在同页下一块 —— 有界检索只给种子块时模型永远看不到 194。
    断言走真实 SQL 的 helper 边界：种子命中保留原位，邻块以独立原子身份
    续编号并标注接续关系；引用邻块编号落到邻块自己的 (parse_job_id, seq)。
    """
    import hashlib
    from ddp_corpus.models import Evidence

    monkeypatch.setattr(settings, "extract_candidates", 1)
    from ddp_corpus import extraction as extraction_mod

    document = Document(uploaded_by=(await _a_user(session)), doc_id="c" * 64,
                        filename="paper.pdf", mime="application/pdf", object_key="",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/x/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    texts = [
        "In total, it brings in 1.2 billion text pairs",
        "of 194 languages and 2655 cross-lingual correspondences.",
    ]
    for seq, text in enumerate(texts):
        evidence = Evidence(document_id=document.id, parse_job_id=job.id, seq=seq,
                            atom_key=f"text-{seq}", content=text,
                            content_digest=hashlib.sha256(text.encode()).hexdigest(),
                            kind="text", page_idx=0, bbox=[72, 100, 500, 130],
                            page_size=[612, 792])
        session.add(evidence)
        await session.flush()
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=0, bbox=[72, 100, 500, 130], page_size=[612, 792],
                          text=text, char_len=len(text), block_type="text",
                          text_tokenized=tokenized(text),
                          evidence_id=evidence.id,
                          embedding=[1.0, 0.0] if seq == 0 else [0.0, 1.0]))
    await session.commit()
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": 194, "support": {"value_source": 2, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "unsupervised_language_count": {
            "type": "integer",
            "description": "Language count in the training corpus text pairs."}}}

    hits, _ = await extraction_mod._retrieve(
        ctx, "text pairs", k=1)
    assert [h["seq"] for h in hits] == [0, 1], "种子在前、邻块续后，不挤占"
    assert hits[1].get("context_of_evidence_id") == hits[0]["evidence_id"]
    assert hits[1]["similarity"] is None, "邻块不冒充高相似度命中"

    outcome = await run_extraction(ctx, parse_schema(schema))
    field = outcome.fields["unsupervised_language_count"]
    assert field["status"] == "found"
    assert field["value"] == 194
    assert field["citations"][0]["seq"] == 1, "引用邻块编号必须落到邻块自己身上"
    assert field["citations"][0]["parse_job_id"] == job.id


@respx.mock
async def test_field_selection_keeps_hybrid_scope_when_reranker_disagrees(
        session, app_state, monkeypatch):
    """A relevant scope fragment must survive disagreement within the same four slots."""
    from ddp_corpus import extraction as extraction_mod

    document, job = await _seed_document(session, await _a_user(session))
    pool = [{
        "chunk_id": f"candidate-{index:02d}",
        "document_id": document.id, "parse_job_id": job.id,
        "seq": 10 * (index + 1), "page_idx": index,
        "text": f"Source fragment {index:02d}", "similarity": 0.7,
    } for index in range(24)]
    # Observed rank disagreement, stripped of paper-specific text and answers.
    order = [4, 8, 12, 7, 0, 16, 14, 2, 15, 9, 5, 10,
             17, 22, 1, 19, 13, 20, 6, 18, 11, 23, 21, 3]

    async def search_pool(*args, **kwargs):
        return pool, None

    monkeypatch.setattr(extraction_mod, "search_query", search_pool)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    respx.post(settings.rerank_endpoint).mock(return_value=Response(
        200, json=[{"index": index, "score": 24 - rank}
                   for rank, index in enumerate(order)]))
    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)

    hits, degraded = await extraction_mod._retrieve(ctx, "requested scope", k=4)

    seed_ids = {hit["chunk_id"] for hit in hits[:4]}
    assert pool[1]["chunk_id"] in seed_ids  # Scope must survive in the original seed slots.
    assert pool[4]["chunk_id"] in seed_ids  # Retain the cross-encoder's strongest candidate.
    assert len({hit["chunk_id"] for hit in hits}) == len(hits)
    assert degraded is None

def _context_hit(index, text, **overrides):
    return {
        "chunk_id": f"chunk-{index}", "evidence_id": f"evidence-{index}",
        "document_id": "doc", "parse_job_id": "parse", "page_idx": 0,
        "seq": index, "bbox": [0, 0, 100, 100], "page_size": [612, 792],
        "text": text, **overrides,
    }


def test_field_context_retains_recalled_continuation_before_lower_ranked_noise():
    from ddp_corpus.extraction import _fill_field_context

    seed = _context_hit(1, "selected")
    preamble = _context_hit(5, "preamble")
    subject = _context_hit(10, "quantity is")
    value = _context_hit(11, "42")
    noise = _context_hit(20, "miscellaneous")
    pool = [seed, preamble, subject, noise, value]
    budget = sum(len(hit["text"]) for hit in (seed, preamble, subject, value))
    admitted = _fill_field_context([seed], pool, pool, max_chars=budget, max_sources=5)

    assert admitted == [seed, preamble, subject, value]
    assert sum(len(hit["text"]) for hit in admitted) == budget
    assert len({hit["chunk_id"] for hit in admitted}) == len(admitted)


@pytest.mark.parametrize("boundary", [
    {"document_id": "another-doc"}, {"parse_job_id": "another-parse"},
    {"page_idx": 1}, {"bbox": [0, 0, 100, 101]},
    {"page_size": [595, 842]}, {"seq": 12},
])
def test_field_context_never_groups_across_a_source_boundary(boundary):
    from ddp_corpus.extraction import _fill_field_context

    seed = _context_hit(1, "seed")
    first = _context_hit(10, "subject")
    second = _context_hit(11, "value", **boundary)
    admitted = _fill_field_context(
        [seed], [first, second], [first, second], max_chars=100, max_sources=2)
    assert admitted == [seed, first]


@pytest.mark.parametrize("budget,slots", [(100, 2), (12, 4)])
def test_field_context_skips_unfitting_whole_region_but_keeps_later_source(budget, slots):
    from ddp_corpus.extraction import _fill_field_context

    seed = _context_hit(1, "seed")
    subject = _context_hit(10, "subject")
    value = _context_hit(11, "value")
    later = _context_hit(20, "other")
    assert _fill_field_context(
        [seed], [subject, later, value], [subject, later, value], max_chars=budget, max_sources=slots,
    ) == [seed, later]


def test_field_context_budgets_the_actual_table_projection():
    from ddp_corpus.extraction import _fill_field_context

    seed = _context_hit(1, "seed")
    table = _context_hit(10, "x", table_html="<table><tr><td>12345</td></tr></table>")
    later = _context_hit(20, "other")
    assert _fill_field_context(
        [seed], [table, later], [table, later], max_chars=12, max_sources=4,
    ) == [seed, later]


def test_field_context_prefers_higher_hybrid_complement_over_pool_order():
    """A lower-rerank/higher-hybrid complementary component stays reachable.

    The pool interleaves rerank and hybrid heads, so a component the
    cross-encoder buries can sit behind rerank-favored noise in pool order.
    Backfill follows the hybrid order instead of re-asking the reranker.
    """
    from ddp_corpus.extraction import _fill_field_context

    first = _context_hit(1, "seed")
    second = _context_hit(10, "seed")
    noise = _context_hit(20, "unrelated")
    complement = _context_hit(30, "conversion rule")
    pool = [first, second, noise, complement]
    hybrid = [first, second, complement, noise]
    assert _fill_field_context(
        [first, second], pool, hybrid, max_chars=100, max_sources=3,
    ) == [first, second, complement]


@pytest.mark.parametrize("slots, expected", [
    (4, ["first", "second", "head", "fresh"]),
    (5, ["first", "second", "head", "fresh", "tail"]),
])
def test_field_context_defers_weaker_completion_behind_fresh_regions(slots, expected):
    """Whole-group diversity: strongest completion, fresh regions, deferred rest.

    The best-hybrid seed-region completion is admitted first, then fresh
    recalled regions in hybrid order; other seed-region completions wait for
    leftover capacity instead of crowding out ranked depth. Groups stay atomic.
    """
    from ddp_corpus.extraction import _fill_field_context

    first = _context_hit(1, "first")
    head = _context_hit(2, "head")
    second = _context_hit(10, "second")
    tail = _context_hit(11, "tail")
    fresh = _context_hit(20, "fresh")
    pool = [first, second, head, tail, fresh]
    hybrid = [first, second, fresh, head, tail]
    by_text = {"first": first, "second": second, "head": head,
               "tail": tail, "fresh": fresh}
    assert _fill_field_context(
        [first, second], pool, hybrid, max_chars=100, max_sources=slots,
    ) == [by_text[name] for name in expected]




async def test_adjacent_budget_prefers_original_block_continuation(session):
    """An unrelated early neighbor must not starve a later hit's split sentence."""
    import hashlib
    from ddp_core.search import adjacent_context_hits
    from ddp_corpus.models import Evidence

    document = Document(uploaded_by=(await _a_user(session)), doc_id="d" * 64,
                        filename="context.pdf", mime="application/pdf", object_key="",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/context/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    texts = ["First ranked hit.", "Other paragraph", "of 194 languages.", "Training data scope."]
    boxes = [[30, 30, 100, 40], [30, 100, 100, 110],
             [30, 200, 100, 220], [30, 200, 100, 220]]
    seeds = []
    for seq, text in enumerate(texts):
        evidence = Evidence(document_id=document.id, parse_job_id=job.id, seq=seq,
                            atom_key=f"context-{seq}", content=text,
                            content_digest=hashlib.sha256(text.encode()).hexdigest(),
                            kind="text", page_idx=0, bbox=boxes[seq], page_size=[612, 792])
        session.add(evidence)
        await session.flush()
        chunk = Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                      page_idx=0, bbox=boxes[seq], page_size=[612, 792],
                      text=text, char_len=len(text), block_type="text",
                      text_tokenized=tokenized(text), evidence_id=evidence.id)
        session.add(chunk)
        await session.flush()
        if seq in (0, 3):
            seeds.append({"chunk_id": chunk.id, "document_id": document.id,
                          "parse_job_id": job.id, "seq": seq, "page_idx": 0,
                          "bbox": boxes[seq], "page_size": [612, 792],
                          "evidence_id": evidence.id, "text": text})
    await session.commit()

    admitted = await adjacent_context_hits(session, seeds, max_chars=len(texts[2]))

    assert [hit["seq"] for hit in admitted] == [2]
    assert admitted[0]["context_of_evidence_id"] == seeds[1]["evidence_id"]
    assert admitted[0]["text"] == texts[2]
    assert admitted[0]["similarity"] is None


@pytest.mark.parametrize("same_region, fit_continuation", [
    (True, True), (False, True), (True, False),
])
async def test_heading_context_reaches_bounded_original_continuation(
        session, same_region, fit_continuation):
    """A heading's first body atom is not the whole original paragraph."""
    import hashlib
    from ddp_core.search import adjacent_context_hits
    from ddp_corpus.models import Evidence

    document, job = await _seed_document(session, await _a_user(session))
    texts = ["Abstract", "The method reduces memory by", "a factor of three."]
    boxes = [[30, 30, 100, 40], [30, 100, 100, 180],
             [30, 100, 100, 180] if same_region else [30, 200, 100, 240]]
    seed = None
    for offset, content in enumerate(texts):
        seq = offset + 10
        evidence = Evidence(
            document_id=document.id, parse_job_id=job.id, seq=seq,
            atom_key=f"heading-{seq}", content=content,
            content_digest=hashlib.sha256(content.encode()).hexdigest(),
            kind="text", page_idx=2, bbox=boxes[offset], page_size=[612, 792])
        session.add(evidence)
        await session.flush()
        chunk = Chunk(
            document_id=document.id, parse_job_id=job.id, seq=seq,
            page_idx=2, bbox=boxes[offset], page_size=[612, 792], text=content,
            char_len=len(content), block_type="text",
            text_tokenized=tokenized(content), evidence_id=evidence.id)
        session.add(chunk)
        await session.flush()
        if offset == 0:
            seed = {"chunk_id": chunk.id, "document_id": document.id,
                    "parse_job_id": job.id, "seq": seq, "page_idx": 2,
                    "bbox": boxes[0], "page_size": [612, 792],
                    "evidence_id": evidence.id, "text": content}
    await session.commit()
    budget = len(texts[1]) + (len(texts[2]) if fit_continuation else 0)

    admitted = await adjacent_context_hits(session, [seed], max_chars=budget)

    assert [hit["seq"] for hit in admitted] == (
        [11, 12] if same_region and fit_continuation else [11])
    assert sum(len(hit["text"]) for hit in admitted) <= budget
    assert all(hit["parse_job_id"] == job.id and hit["similarity"] is None
               for hit in admitted)


async def _seed_mechanism_document(session, user_id: str):
    """机制名与触发条件分处两个原子的文档：primary 钉机制，context 钉条件。"""
    import hashlib
    from ddp_corpus.models import Evidence

    document = Document(uploaded_by=user_id, doc_id="e" * 64,
                        filename="mechanism.pdf", mime="application/pdf",
                        object_key="", index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/m/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    texts = [
        "Paged Optimizers use the NVIDIA unified memory feature for transfers.",
        "This prevents memory spikes during gradient checkpointing on long sequences.",
        "An unrelated memory log was archived separately.",
    ]
    for seq, text in enumerate(texts):
        evidence = Evidence(document_id=document.id, parse_job_id=job.id, seq=seq,
                            atom_key=f"mech-{seq}", content=text,
                            content_digest=hashlib.sha256(text.encode()).hexdigest(),
                            kind="text", page_idx=seq, bbox=[72, 100, 500, 130],
                            page_size=[612, 792])
        session.add(evidence)
        await session.flush()
        session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=seq,
                          page_idx=seq, bbox=[72, 100, 500, 130], page_size=[612, 792],
                          text=text, char_len=len(text), block_type="text",
                          text_tokenized=tokenized(text),
                          evidence_id=evidence.id,
                          embedding=[1.0, 0.0] if seq == 0 else [0.9, 0.1]))
    await session.commit()
    return document, job


def _context_selection_reply(request, *, value, primary_excerpt, context_excerpt):
    payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
    fragments = [fragment for group in payload["source_groups"] for fragment in group["fragments"]]
    primary = next(fragment for fragment in fragments if primary_excerpt in fragment["text"])
    context = next(fragment for fragment in fragments if context_excerpt in fragment["text"])
    return _chat_reply({"found": True, "value": value, "support": {"value_source": primary["source_id"], "context_sources": [context["source_id"]]}})


@respx.mock
async def test_field_support_projects_primary_then_context(session, app_state):
    """Only explicitly selected original identities become citations, in selected order."""
    document, job = await _seed_mechanism_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda request: _context_selection_reply(
        request, value="NVIDIA unified memory; gradient checkpointing",
        primary_excerpt="NVIDIA unified memory",
        context_excerpt="memory spikes during gradient checkpointing"))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "memory_mechanism": {"type": "string", "description": "Memory mechanism name"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["memory_mechanism"]
    assert field["status"] == "found"
    assert [c["seq"] for c in field["citations"]] == [0, 1]
    assert len(field["citations"]) == 2, "只投影显式选择，不附带其余候选"


@respx.mock
@pytest.mark.parametrize("support", [
    {"value_source": 1, "context_sources": [99]},
    {"value_source": 1, "context_sources": [1]},
    {"value_source": 1, "context_sources": [True]},
    {"value_source": 1, "context_sources": [2, 3, 4]},
    {"value_source": 1, "context_sources": "not-a-list"}, {},
])
async def test_bad_field_support_is_error_not_partial(session, app_state, support):
    """Reject the whole selection; never salvage a valid prefix or attach top-1."""
    document, job = await _seed_mechanism_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "NVIDIA unified memory", "support": support}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "memory_mechanism": {"type": "string", "description": "Memory mechanism name"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["memory_mechanism"]
    assert field["status"] == "error"
    assert field["value"] is None
    assert field["citations"] == []
    assert field["degraded"] == "schema_violation"


@respx.mock
async def test_absence_with_selected_support_is_error(session, app_state):
    """Absence and selected supporting originals are contradictory."""
    document, job = await _seed_mechanism_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": False, "value": None, "support": {"value_source": 1, "context_sources": []}}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "memory_mechanism": {"type": "string", "description": "Memory mechanism name"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["memory_mechanism"]
    assert field["status"] == "error"
    assert field["degraded"] == "schema_violation"


@respx.mock
async def test_context_cannot_rescue_false_primary_number(session, app_state):
    """数值见证只认 primary 文本：context 里有数也救不了 primary 无数的出处。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    from sqlalchemy import select as sa_select
    chunk0 = (await session.execute(sa_select(Chunk).where(
        Chunk.document_id == document.id, Chunk.seq == 0))).scalar_one()
    chunk0.text = "买方：北极星科技有限公司。"
    chunk0.text_tokenized = tokenized(chunk0.text)
    chunk0.char_len = len(chunk0.text)
    chunk1 = (await session.execute(sa_select(Chunk).where(
        Chunk.document_id == document.id, Chunk.seq == 1))).scalar_one()
    chunk1.text = "共 48 条记录在案。"
    chunk1.text_tokenized = tokenized(chunk1.text)
    chunk1.char_len = len(chunk1.text)
    chunk1.embedding = [0.9, 0.1]  # both citations must actually reach the model
    await session.commit()
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda request: _context_selection_reply(
        request, value=48, primary_excerpt="北极星科技有限公司", context_excerpt="48 条记录"))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "record_count": {"type": "integer", "description": "记录条数"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["record_count"]
    assert field["status"] == "error"
    assert field["value"] is None
    assert field["citations"] == []
    assert field["degraded"] == "schema_violation"


@respx.mock
async def test_partial_verification_cannot_be_true(session, app_state, monkeypatch):
    """两条出处只核对上 primary：verified 必须 false，不许部分为 true。

    预算只够核对一条 —— 第二条没测出来（None）不能被当成一致。
    """
    from ddp_corpus import extraction as extraction_mod

    document, job = await _seed_mechanism_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda request: _context_selection_reply(
        request, value="NVIDIA unified memory; gradient checkpointing",
        primary_excerpt="NVIDIA unified memory",
        context_excerpt="memory spikes during gradient checkpointing"))

    async def fake_crop_and_verify(ctx, hit):
        if hit.get("seq") == 0:
            return "crop-primary", True, True
        return "crop-context", None, True

    monkeypatch.setattr(extraction_mod, "_crop_and_verify", fake_crop_and_verify)
    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=True)
    schema = {"type": "object", "properties": {
        "memory_mechanism": {"type": "string", "description": "Memory mechanism name"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["memory_mechanism"]
    assert field["status"] == "found", field
    assert len(field["citations"]) == 2
    assert field["verified"] is False, "context 没测出来时整体不许标 true"
    assert field["degraded"] == "vision_unavailable"


@respx.mock
async def test_generated_value_cites_its_own_derived_evidence(session, app_state):
    """派生块展开成两个可引用视图：引用派生正文的值必须落到派生 ID 上。

    回归的是“合并文本、单 ID 引用”：模型从合并文本里读到原文的正确数值，
    引用却只能挂到与该值矛盾的 generated 体上 —— 值与被引证据自相矛盾，
    而系统没有任何信号。展开后：原文值引原文 ID，派生值引派生 ID。
    """
    import hashlib

    from ddp_core.hits import Hit
    from ddp_corpus import extraction as extraction_mod
    from ddp_corpus.models import Evidence

    document = Document(uploaded_by=(await _a_user(session)), doc_id="v" * 64,
                        filename="spec.pdf", mime="application/pdf", object_key="",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/v/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    source_text = "Supply voltage MAX 6 V; Common-mode MIN -0.3 V MAX 26 V"
    source = Evidence(document_id=document.id, parse_job_id=job.id, seq=0,
                      atom_key="src-0", content=source_text,
                      content_digest=hashlib.sha256(source_text.encode()).hexdigest(),
                      kind="table", page_idx=0, bbox=[72, 100, 500, 130],
                      page_size=[612, 792])
    session.add(source)
    await session.flush()
    derived_text = "Supply voltage MIN 6 V; Common-mode -0.3 to 26 V"
    derived = Evidence(document_id=document.id, parse_job_id=job.id, seq=0,
                       atom_key="vision-0", content=derived_text,
                       content_digest=hashlib.sha256(derived_text.encode()).hexdigest(),
                       kind="table", page_idx=0, bbox=[72, 100, 500, 130],
                       page_size=[612, 792], derived_from=source.id)
    session.add(derived)
    await session.flush()
    session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=0,
                      page_idx=0, bbox=[72, 100, 500, 130], page_size=[612, 792],
                      text=source_text, char_len=len(source_text), block_type="table",
                      text_tokenized=tokenized(source_text), evidence_id=source.id,
                      derived_text=derived_text, derived_evidence_id=derived.id,
                      embedding=[1.0, 0.0]))
    await session.commit()
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)

    seen: dict = {}

    def reply(request):
        payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
        fragments = [fragment for group in payload["source_groups"]
                     for fragment in group["fragments"]]
        seen["fragments"] = [fragment["text"] for fragment in fragments]
        original = next(fragment for fragment in fragments
                        if "MAX 6 V" in fragment["text"] and "MIN 6 V" not in fragment["text"])
        return _chat_reply({"found": True, "value": "VS MAX 6 V",
                            "support": {"value_source": original["source_id"],
                                        "context_sources": []}})

    respx.post(CHAT).mock(side_effect=reply)
    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "supply_limit": {"type": "string", "description": "Supply voltage limit"}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["supply_limit"]
    assert field["status"] == "found", field
    assert len(seen["fragments"]) == 2, "原文与派生必须是两个独立可引用资料"
    assert not any("MAX 6 V" in text and "MIN 6 V" in text for text in seen["fragments"]), \
        "两份正文不许合并进同一个资料 ID 名下"
    citation = field["citations"][0]
    assert citation["source_type"] == "source"
    assert citation["evidence_id"] == source.id
    assert "MIN 6 V" not in citation["snippet"], "原文引用的摘要不许混入派生正文"

    # 同槽位的派生引用必须能与原文引用并存：按 evidence_id 落库，不互相覆盖。
    from ddp_corpus.evidence import load_citations, record_evidence

    generated_hit = Hit(chunk_id="c1", parse_job_id=job.id, seq=0, page_idx=0,
                        text=source_text, derived_text=derived_text,
                        evidence_id=source.id, derived_evidence_id=derived.id,
                        block_type="table", score=0.03, similarity=0.8)
    generated_citation = extraction_mod._citation(
        extraction_mod.expand_source_views([generated_hit])[1], None)
    assert generated_citation["source_type"] == "generated"
    assert generated_citation["evidence_id"] == derived.id
    assert generated_citation["derived_from"] == source.id
    await record_evidence(session, [citation, generated_citation],
                          source_kind="extract_field", source_id="item-v:supply_limit")
    await session.commit()
    loaded = await load_citations(session, source_kind="extract_field",
                                  source_ids=["item-v:supply_limit"])
    by_evidence = {c["evidence_id"]: c for c in loaded["item-v:supply_limit"]}
    assert set(by_evidence) == {source.id, derived.id}, "同槽原文/派生两条引用都要落库"
    assert by_evidence[source.id]["resolved"] is True
    assert by_evidence[derived.id]["resolved"] is True


async def _seed_derived_table_document(session, user_id: str):
    """同一槽位原文正确、派生矛盾的表格文档：行引用必须能区分两者。"""
    import hashlib

    from ddp_corpus.models import Evidence

    document = Document(uploaded_by=user_id, doc_id="r" * 64,
                        filename="rows.pdf", mime="application/pdf", object_key="",
                        index_status="ready")
    session.add(document)
    await session.flush()
    job = ParseJob(document_id=document.id, engine="borndigital", options={},
                   options_hash="h", status="succeeded", result_prefix="results/r/")
    session.add(job)
    await session.flush()
    document.current_job_id = job.id
    source_text = "Supply voltage MAX 6 V; Common-mode MIN -0.3 V MAX 26 V"
    source = Evidence(document_id=document.id, parse_job_id=job.id, seq=0,
                      atom_key="row-src-0", content=source_text,
                      content_digest=hashlib.sha256(source_text.encode()).hexdigest(),
                      kind="table", page_idx=0, bbox=[72, 100, 500, 130],
                      page_size=[612, 792])
    session.add(source)
    await session.flush()
    derived_text = "Supply voltage MIN 6 V; Common-mode -0.3 to 26 V"
    derived = Evidence(document_id=document.id, parse_job_id=job.id, seq=0,
                       atom_key="row-vision-0", content=derived_text,
                       content_digest=hashlib.sha256(derived_text.encode()).hexdigest(),
                       kind="table", page_idx=0, bbox=[72, 100, 500, 130],
                       page_size=[612, 792], derived_from=source.id)
    session.add(derived)
    await session.flush()
    session.add(Chunk(document_id=document.id, parse_job_id=job.id, seq=0,
                      page_idx=0, bbox=[72, 100, 500, 130], page_size=[612, 792],
                      text=source_text, char_len=len(source_text), block_type="table",
                      text_tokenized=tokenized(source_text), evidence_id=source.id,
                      derived_text=derived_text, derived_evidence_id=derived.id,
                      embedding=[1.0, 0.0]))
    await session.commit()
    return document, job, source, derived


@respx.mock
async def test_records_row_cites_only_its_selected_view(session, app_state):
    """多记录行在生产时选择自己的资料：只挂选中的一份，不无条件挂两份。

    回归的是“输入可见即支持”：同块原文/生成两份正文都给了模型，
    行出处却把两份都挂上 —— 正确原文值的 citations 里混进已知矛盾的
    派生摘要。展开后每行必须写 `source`，落库只投影该行选中的视图。
    """
    document, job, source, derived = await _seed_derived_table_document(
        session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply({"records": [
        {"source": 1, "values": {"supply": "VS MAX 6 V"}},
        {"source": 2, "values": {"supply": "Supply voltage MIN 6 V"}},
    ]}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "array", "items": {"type": "object", "properties": {
        "supply": {"type": "string", "description": "Supply voltage limit"}}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert len(outcome.records) == 2, outcome.records
    first = outcome.records[0]["fields"]["supply"]
    second = outcome.records[1]["fields"]["supply"]
    assert [c["evidence_id"] for c in first["citations"]] == [source.id]
    assert first["citations"][0]["source_type"] == "source"
    assert [c["evidence_id"] for c in second["citations"]] == [derived.id]
    assert second["citations"][0]["source_type"] == "generated"
    assert second["citations"][0]["derived_from"] == source.id
    assert outcome.degraded is None


@respx.mock
async def test_records_row_without_source_is_dropped_not_rescued(session, app_state):
    """行缺 `source`/`values`、values 非对象或指到不存在的资料：只丢该行。"""
    document, job, source, _ = await _seed_derived_table_document(
        session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply({"records": [
        {"values": {"supply": "VS MAX 6 V"}},
        {"source": 99, "values": {"supply": "VS MAX 6 V"}},
        {"source": 1},
        {"source": 1, "values": ["not-an-object"]},
        {"source": 1, "values": {"supply": "VS MAX 6 V"}},
    ]}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "array", "items": {"type": "object", "properties": {
        "supply": {"type": "string", "description": "Supply voltage limit"}}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert len(outcome.records) == 1, outcome.records
    kept = outcome.records[0]["fields"]["supply"]
    assert kept["value"] == "VS MAX 6 V"
    assert [c["evidence_id"] for c in kept["citations"]] == [source.id]
    assert outcome.degraded == "schema_violation"


@respx.mock
async def test_records_reserved_column_names_stay_business_values(session, app_state):
    """用户列名 `source`/`values` 仍是业务值：封套选择器不与其碰撞。

    回归的是扁平行把来源元数据与用户字段混在同一对象里：schema 一旦有
    `source` 列，行值就会被当成来源指针或反之。封套把选择器放在 values
    之外，任意用户列名都不再需要保留字限制。
    """
    document, job, source, _ = await _seed_derived_table_document(
        session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(side_effect=_embedding_reply)
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply({"records": [
        {"source": 1, "values": {"supply": "VS MAX 6 V", "source": "table-note",
                                "values": 7}},
    ]}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         actor_id=document.uploaded_by, verify=False)
    schema = {"type": "array", "items": {"type": "object", "properties": {
        "supply": {"type": "string", "description": "Supply voltage limit"},
        "source": {"type": "string", "description": "Row note column named source"},
        "values": {"type": "integer", "description": "Row count column named values"}}}}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert len(outcome.records) == 1, outcome.records
    row = outcome.records[0]["fields"]
    assert row["supply"]["value"] == "VS MAX 6 V"
    assert row["source"]["value"] == "table-note"
    assert row["values"]["value"] == 7
    assert [c["evidence_id"] for c in row["source"]["citations"]] == [source.id]
    assert [c["evidence_id"] for c in row["values"]["citations"]] == [source.id]
    assert outcome.degraded is None
