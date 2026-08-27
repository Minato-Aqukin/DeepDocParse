"""结构化抽取的单测（v1.1）。

三条不变式，每一条都对应一种**真实会发生且不会报错**的故障：

1. **not_found 与 error 分得开**。合成一个的话，"这份合同没写违约金"和
   "我们的检索挂了"在同一张表格里长得一模一样，而空值看起来像结论
2. **found 必须有出处**，且出处用的是稳定定位键 `(parse_job_id, seq)`
3. **导出的 CSV 不能是可执行文件**。抽取结果来自文档内容（不可信输入），
   Excel 会把 `=`/`+`/`-`/`@` 开头的单元格当公式执行
"""
import json

import pytest
import respx
from httpx import Response

from ddp_core.chunking import layout_to_chunks
from ddp_core.extract_format import parse_schema, validate_schema
from app.extraction import ExtractContext, run as run_extraction
from app.models import Chunk, Document, ParseJob
from app.routers.extractions import _csv_safe
from app.config import settings
from ddp_core.search import MemoryIndex
from ddp_core.tokenize import tokenized

from tests.conftest import CHAT, EMBEDDINGS

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


def _chat_reply(answer: dict) -> Response:
    return Response(200, json={"choices": [
        {"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})


@respx.mock
async def test_found_and_not_found_are_distinguished(session, app_state):
    """一个抽得到、一个文档里没有 —— **必须落到不同的 status**。"""
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [1.0, 0.0]}]}))

    def reply(request):
        prompt = json.loads(request.content)["messages"][-1]["content"]
        # **按字段名分支，不能按资料内容分支**：prompt 里嵌着检索到的资料，
        # 而资料本身就含"买方"二字 —— 按它分支会让两个字段走同一条回答
        if "名称：buyer" in prompt:
            return _chat_reply({"found": True, "value": "北极星科技有限公司", "source": 1})
        return _chat_reply({"found": False, "value": None, "source": None})

    respx.post(CHAT).mock(side_effect=reply)

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         user_id=document.uploaded_by, verify=False)
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
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    respx.post(CHAT).mock(return_value=Response(200, json={
        "choices": [{"message": {"content": "我觉得应该是那家公司吧"}}]}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         user_id=document.uploaded_by, verify=False)
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
        {"found": True, "value": "北极星科技有限公司", "source": 1}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         user_id=document.uploaded_by, verify=False)
    outcome = await run_extraction(ctx, parse_schema(SCHEMA))
    degraded = [f.get("degraded") for f in outcome.fields.values()]
    assert "embedding_unavailable" in degraded, degraded


async def _a_user(session, username: str = "ex") -> str:
    from app.models import User

    user = User(username=username, password_hash="x")
    session.add(user)
    await session.flush()
    return user.id


# --------------------------------------------------------------------- API

async def test_template_crud_and_schema_guard(auth_client):
    bad = await auth_client.post("/api/extractions/templates", json={
        "name": "坏模板", "description": "", "schema_json": {
            "type": "object", "properties": {"a": {"type": "string"}}}})
    assert bad.status_code == 400, "缺 description 的 schema 必须当场被拒"
    assert bad.json()["error"]["code"] == "invalid_schema"

    created = await auth_client.post("/api/extractions/templates", json={
        "name": "采购合同", "description": "关键条款", "schema_json": SCHEMA})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["field_count"] == 2 and body["kind"] == "object"
    # 线上字段名必须是 schema_json（pydantic 属性名换了，别名不能漏）
    assert "schema_json" in body and "doc_schema" not in body

    listed = (await auth_client.get("/api/extractions/templates")).json()
    assert len(listed) == 1

    duplicate = await auth_client.post("/api/extractions/templates", json={
        "name": "采购合同", "description": "", "schema_json": SCHEMA})
    assert duplicate.status_code == 409

    assert (await auth_client.delete(
        f"/api/extractions/templates/{body['id']}")).status_code == 204
    assert (await auth_client.get("/api/extractions/templates")).json() == []


async def test_soft_deleted_template_name_can_be_reused(auth_client):
    """回归：唯一约束是 (user_id, name) 且不含 deleted_at ——
    软删后重建同名模板直接插会撞约束报 500。"""
    first = (await auth_client.post("/api/extractions/templates", json={
        "name": "同名", "description": "", "schema_json": SCHEMA})).json()
    await auth_client.delete(f"/api/extractions/templates/{first['id']}")
    again = await auth_client.post("/api/extractions/templates", json={
        "name": "同名", "description": "复活", "schema_json": SCHEMA})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first["id"], "应该复活原行，而不是插一条新的"


async def test_run_rejects_documents_without_index(auth_client, session):
    """索引没就绪的文档抽不了。**当场说清楚**，别让它们跑完变成一堆空结果 ——
    空值看起来像"文档里没有"，那是抽取里最危险的误导。"""
    from app.models import User

    user = (await session.execute(__import__("sqlalchemy").select(User))).scalars().first()
    document = Document(uploaded_by=user.id, doc_id="z" * 64, filename="未索引.pdf",
                        mime="application/pdf", index_status="none")
    session.add(document)
    await session.commit()

    template = (await auth_client.post("/api/extractions/templates", json={
        "name": "t", "description": "", "schema_json": SCHEMA})).json()
    resp = await auth_client.post("/api/extractions/runs", json={
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
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "source": 1}))

    ctx = ExtractContext(session=session, index=MemoryIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         user_id=document.uploaded_by, verify=True)
    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"}}}

    # 裁图成功、但视觉核对返回 None（模型不可达）
    with patch("app.extraction.get_or_create_crop", return_value="results/x/crops/0_a.png"), \
         patch.object(app_state.storage, "get", return_value=b"png"), \
         patch("app.extraction.verify_parse_consistency", return_value=None):
        outcome = await run_extraction(ctx, parse_schema(schema))

    field = outcome.fields["buyer"]
    assert field["status"] == "found"
    assert field["verified"] is False
    assert field["degraded"] == "vision_unavailable", \
        "核对打了模型却没结果，必须打标 —— 否则与'预算用完'分不开"


@respx.mock
async def test_one_field_blowing_up_does_not_lose_the_others(session, app_state):
    """回归：单个字段的意外不能丢掉整批已抽好的字段。

    `asyncio.gather` 默认在第一个异常处抛出、其余结果作废 —— 于是
    "一个字段 error、其余照常"被升级成"整份文档白跑"，三态设计白设计。
    真实触发点：换 embedding 模型后 pgvector 报 different vector dimensions。
    """
    document, job = await _seed_document(session, (await _a_user(session)))
    respx.post(EMBEDDINGS).mock(return_value=Response(200, json={
        "data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    respx.post(CHAT).mock(side_effect=lambda r: _chat_reply(
        {"found": True, "value": "北极星科技有限公司", "source": 1}))

    class ExplodingIndex(MemoryIndex):
        async def search(self, session, **kwargs):        # noqa: ANN001
            if "boom" in kwargs.get("query", ""):
                raise RuntimeError("pgvector: different vector dimensions 1024 and 768")
            return await super().search(session, **kwargs)

    ctx = ExtractContext(session=session, index=ExplodingIndex(), http=app_state.http,
                         storage=app_state.storage, document=document, job=job,
                         user_id=document.uploaded_by, verify=False)
    schema = {"type": "object", "properties": {
        "buyer": {"type": "string", "description": "买方单位全称"},
        "boom": {"type": "string", "description": "boom 会炸的字段"},
    }}
    outcome = await run_extraction(ctx, parse_schema(schema))

    assert outcome.fields["boom"]["status"] == "error"
    # 关键：另一个字段的结果没被连累
    assert outcome.fields["buyer"]["status"] == "found"
    assert outcome.fields["buyer"]["value"] == "北极星科技有限公司"


# --------------------------------------------------------------------- 验收回归（M9 三轮）

async def test_stale_citation_is_marked_unresolved_not_pointed_at_the_wrong_block(session):
    """回归：`seq` 还在、但那个位置已经换了内容时，必须判 **未解析**。

    M9 改了分块规则（表格/公式/图片独立成块、标题作前缀），同一份归档重建索引
    会切出不同数量、不同 seq 的块。只查 `(parse_job_id, seq)` 存不存在的话，
    老 citation 照样"查得到"，指的却是另一段原文 —— 而 UI 只看 `resolved`，
    用户会看到一条可点开的出处，snippet 是旧文本、高亮指向别处。
    **这正是这个项目定义的最恶劣错误：带着已验证标记的假出处。**
    """
    from app.qa import attach_resolution, load_citation_targets

    document, job = await _seed_document(session, (await _a_user(session)))
    # 模拟"重建索引后 seq=0 上换了内容"：库里 seq=0 是买方那段
    citations = [
        {"parse_job_id": job.id, "seq": 0, "snippet": "买方：北极星科技有限公司", "page_idx": 0},
        {"parse_job_id": job.id, "seq": 0, "snippet": "这是重建索引前那段已经不在了的文本",
         "page_idx": 0},
        {"parse_job_id": job.id, "seq": 99, "snippet": "根本不存在的块", "page_idx": 0},
    ]
    lookup = await load_citation_targets(session, document.id, citations)
    resolved = [attach_resolution(c, lookup) for c in citations]

    assert resolved[0]["resolved"] is True, "内容还对得上的出处不该被误判失效"
    assert resolved[0]["chunk_id"], "对得上就要刷新 chunk_id"
    assert resolved[1]["resolved"] is False, \
        "seq 还在但内容换了 —— 必须说接不回去，绝不许指到错块"
    assert "chunk_id" not in resolved[1] or resolved[1].get("chunk_id") is None \
        or resolved[1]["resolved"] is False
    assert resolved[2]["resolved"] is False


async def test_hard_deleted_document_does_not_break_the_whole_run(session, app_state):
    """回归：抽取期间文档被**硬删**时不能插 ExtractionItem。

    `ExtractionItem.document_id` 是非空外键，指向已不存在的文档会撞 FK 约束 ->
    commit 抛 IntegrityError -> 外层 except 再插一次再抛 -> 整批 run 打成 failed。
    比"进度条差一格"糟得多。**单测能抓到全靠 conftest 开了 PRAGMA foreign_keys=ON**
    （SQLite 默认不强制，真 PG 一定会炸）。
    """
    from sqlalchemy import select as sa_select

    from ddp_core.extract_format import parse_schema as ps
    from app.models import ExtractionItem, ExtractionRun
    from app.routers.extractions import _extract_one

    user_id = await _a_user(session)
    run = ExtractionRun(user_id=user_id, name="t", schema_json=SCHEMA, kind="object",
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
    from app.models import ExtractionItem, ExtractionRun, UsageRecord
    from app.routers.extractions import _extract_one

    document, _job = await _seed_document(session, await _a_user(session))
    initiator = await _a_user(session, username="initiator")
    assert initiator != document.uploaded_by, "发起人要与上传者不同才测得出归属"

    run = ExtractionRun(user_id=initiator, name="t", schema_json=SCHEMA, kind="object",
                        status="running", document_count=1)
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

    billed = (await session.execute(
        sa_select(UsageRecord.user_id).where(UsageRecord.kind == "extract"))).scalars().all()
    assert billed == [initiator], \
        f"抽取要记在发起人头上，不是上传者（{document.uploaded_by}）头上，实际 {billed}"
