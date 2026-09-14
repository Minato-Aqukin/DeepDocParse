"""MCP `ask_document`（deprecated 兼容工具）与向量检索路的用例。

## 本轮改了什么，为什么

`ask_document` 以前直连模型网关的解析平面，带的是 MCP 自己的服务凭据。
网关按文档哈希做幂等复用 —— 于是"猜中一个已经被解析过的地址"就能拿到
**别人**那次解析的全文，一个授权判据都不经过。

现在它走语料域的 `/v1/parse*`，带调用者的 actor 上下文：那一层按主体分域
重写 doc_id、取结果前要求这次任务对本人可见，并把归属与计量记上。
另外它只受理**本部署之外**的地址 —— 裸 URL 表达不了"我有权读它"。

所以这里的 mock 目标从 `GW` 变成了 `CORPUS`（**不是换个变量名**：换回去
就是把那条越权路径接回来），并且每次调用都真的走一遍 HTTP 传输，
因为身份是从请求头里取的。

与网关的交互（VQA / embeddings，纯算力）仍然直连，仍然 mock 在 `GW` 上。
"""
import json

import pytest
import respx
from httpx import Response

from ddp_paths import fixture

from tests.conftest import (
    CORPUS, GW, IDENTITY, TOKEN, call_text, entry_client, mcp_client, passthrough_mcp,
)

PDF = "http://files.example.com/long.pdf"
DOC_HASH = "9" * 64          # 语料域返回的文档身份（按主体分域算出来的）


async def ask(client, file_url: str, question: str) -> str:
    return await call_text(client, "ask_document", file_url=file_url, question=question)


# ---------------------------------------------------------------- 身份与地址边界

async def test_ask_document_without_identity_is_rejected(mcp_env):
    """没有可信身份就不受理 —— 它现在会以调用者的名义去提交解析。"""
    async with mcp_client(IDENTITY) as client:      # 有身份头、没有服务凭据
        result = await client.call_tool(
            "ask_document", {"file_url": PDF, "question": "多少"}, raise_on_error=False)
    assert result.is_error and "服务凭据" in result.content[0].text


@respx.mock
@pytest.mark.parametrize("url", [
    "http://corpus.test/v1/parse/t1/result",         # 本部署的语料 API
    "https://ddp.example.com/files/tok-abc",         # 本部署的稳定文件 URL
    "http://gw.test/v1/parse/t1/result",             # 本部署的模型网关
    "http://127.0.0.1:9000/x.pdf",                   # 回环
    "http://10.1.2.3/private.pdf",                    # 内网
    "http://minio.internal/bucket/obj.pdf",           # 内部域名
    "file:///etc/passwd",                             # 非 http(s)
    "http://user:pw@files.example.com/a.pdf",         # 带凭据的 URL
])
async def test_ask_document_refuses_addresses_inside_this_deployment(mcp_env, url):
    """裸 URL 证明不了授权，所以本部署自己的存储面一律不受理。

    放行任何一条的后果都一样：用 MCP 的服务身份替调用方把受 ACL 保护的内容
    取出来。**最要命的是 `/files/{token}` 那条** —— 那正是已入库文档的下载面。
    """
    passthrough_mcp(respx)
    parse = respx.post(f"{CORPUS}/v1/parse")
    fetch = respx.get(url)
    async with entry_client() as client:
        result = await client.call_tool("ask_document", {"file_url": url, "question": "多少"},
                                        raise_on_error=False)
    assert result.is_error, f"{url} 竟然被受理了"
    assert not parse.called, "拒绝之前不许先去提交解析"
    assert not fetch.called, "拒绝之前不许先去取文件"


@respx.mock
async def test_ask_document_does_not_fall_back_to_the_gateway_when_denied(mcp_env):
    """语料域说"这个任务对你不可见"时，**不许绕过去直连网关再试一次**。

    直连那条路就是本轮要堵的越权：网关不认识用户，只要哈希对得上就给全文。
    """
    passthrough_mcp(respx)
    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t9"}))
    respx.get(f"{CORPUS}/v1/parse/t9").mock(return_value=Response(200, json={
        "task_id": "t9", "status": "succeeded", "progress": 1.0, "error": None,
        "doc_hash": DOC_HASH}))
    respx.get(f"{CORPUS}/v1/parse/t9/result").mock(return_value=Response(404, json={
        "error": {"message": "parse job not found", "type": "invalid_request_error",
                  "code": "job_not_found"}}))
    gateway_result = respx.get(f"{GW}/v1/parse/t9/result")

    async with entry_client() as client:
        result = await client.call_tool("ask_document", {"file_url": PDF, "question": "多少"},
                                        raise_on_error=False)
    assert result.is_error and "不可见" in result.content[0].text
    assert not gateway_result.called, "越权兜底路径又长回来了"


@respx.mock
async def test_ask_document_submits_with_the_callers_identity(mcp_env):
    """解析是**以调用者的名义**提交的：归属、配额与可见性都靠这组头。"""
    passthrough_mcp(respx)
    submit = respx.post(f"{CORPUS}/v1/parse").mock(
        return_value=Response(202, json={"task_id": "t0"}))
    respx.get(f"{CORPUS}/v1/parse/t0").mock(return_value=Response(200, json={
        "task_id": "t0", "status": "running", "progress": 0.5, "error": None}))

    async with entry_client() as client:
        await ask(client, PDF, "多少")

    sent = submit.calls.last.request.headers
    for name, value in IDENTITY.items():
        assert sent[name] == value, f"{name} 没有随解析提交一起转发"
    assert sent["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(submit.calls.last.request.content) == {"file_url": PDF}


# ---------------------------------------------------------------- 原有行为（换了传输）

@respx.mock
async def test_ask_document_retry_pattern(mcp_env):
    """未解析的大文档：首次调用返回"解析中"提示；解析完成后再调用返回带出处答案。"""
    passthrough_mcp(respx)
    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t1"}))
    status = respx.get(f"{CORPUS}/v1/parse/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "running",
                                         "progress": 0.5, "error": None}))

    async with entry_client() as client:
        out1 = await ask(client, "http://files.example.com/big.pdf", "总收入是多少？")
        assert "解析中" in out1 and "t1" in out1 and "重试" in out1

        # 稍后重试：同 URL 幂等复用任务，已 succeeded -> 短文档全文作证据
        status.mock(return_value=Response(200, json={"task_id": "t1", "status": "succeeded",
                                                     "progress": 1.0, "error": None}))
        respx.get(f"{CORPUS}/v1/parse/t1/result").mock(
            return_value=Response(200, json={"markdown": "# 财报\n\n总收入 42 亿元。",
                                             "layout_json": {}, "images": []}))
        out2 = await ask(client, "http://files.example.com/big.pdf", "总收入是多少？")
    assert "总收入 42 亿元" in out2 and "出处" in out2 and "/v1/parse/t1/result" in out2
    # 取结果的地址要给**调用方能访问的**那个（入口），不是内网语料地址
    assert "https://ddp.example.com/v1/parse/t1/result" in out2


@respx.mock
async def test_ask_document_image_direct(mcp_env):
    """图片 URL：不走解析平面，直接 VQA 秒回。"""
    passthrough_mcp(respx)
    png = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                        "01f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
    respx.get("http://files.example.com/chart.png").mock(
        return_value=Response(200, content=png, headers={"content-type": "image/png"}))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "图中数值为 42"}}]}))
    parse_route = respx.post(f"{CORPUS}/v1/parse")

    async with entry_client() as client:
        out = await ask(client, "http://files.example.com/chart.png", "图中数值？")
    assert "图中数值为 42" in out and "出处" in out
    assert vqa.called and not parse_route.called


@respx.mock
async def test_ask_document_bm25_crop_verify(mcp_env):
    """长文档：BM25 命中版面块（带页码+bbox）-> 裁剪原 PDF 区域 -> VQA 验证 -> 带出处返回。"""
    passthrough_mcp(respx)
    pdf_bytes = fixture("sample.pdf").read_bytes()
    layout = {"pdf_info": [{
        "page_idx": 0,
        "page_size": [612, 792],
        "para_blocks": [
            {"type": "title", "bbox": [69, 71, 375, 98],
             "lines": [{"spans": [{"content": "DeepDocParse contract test"}]}]},
            {"type": "text", "bbox": [69, 118, 200, 140],
             "lines": [{"spans": [{"content": "The answer to everything is 42"}]}]},
        ],
    }]}
    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t2"}))
    respx.get(f"{CORPUS}/v1/parse/t2").mock(
        return_value=Response(200, json={"task_id": "t2", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{CORPUS}/v1/parse/t2/result").mock(
        return_value=Response(200, json={"markdown": "x" * 4000, "layout_json": layout,
                                         "images": []}))
    respx.get(PDF).mock(return_value=Response(200, content=pdf_bytes))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "42"}}]}))

    async with entry_client() as client:
        out = await ask(client, PDF, "what is the answer to everything")
    assert "42" in out and "第 1 页" in out and "bbox" in out
    assert "视觉验证" in out, "首个命中块必须走裁剪+VQA 验证路径"
    assert vqa.called
    # 发给 VQA 的必须是裁剪出的 PNG data URI
    sent = json.loads(vqa.calls.last.request.content)
    image_part = sent["messages"][0]["content"][0]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")


@respx.mock
async def test_ask_document_untokenizable_doc_no_crash(mcp_env):
    """回归（M3 验收发现）：全部版面块 token 化为空（非中英文种/全符号）时
    不得让 BM25 除零崩溃，应退化为开头片段。"""
    passthrough_mcp(respx)
    layout = {"pdf_info": [{
        "page_idx": 0,
        "page_size": [612, 792],
        "para_blocks": [
            {"type": "text", "bbox": [10, 10, 100, 30],
             "lines": [{"spans": [{"content": "안녕하세요 세계"}]}]},
            {"type": "text", "bbox": [10, 40, 100, 60],
             "lines": [{"spans": [{"content": "©®™ ★☆ ……"}]}]},
        ],
    }]}
    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t3"}))
    respx.get(f"{CORPUS}/v1/parse/t3").mock(
        return_value=Response(200, json={"task_id": "t3", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{CORPUS}/v1/parse/t3/result").mock(
        return_value=Response(200, json={"markdown": "허" * 4000, "layout_json": layout,
                                         "images": []}))

    async with entry_client() as client:
        out = await ask(client, "http://files.example.com/korean.pdf", "질문")
    assert "开头片段" in out and "완전" not in out  # 正常返回退化文案，而不是崩溃


@respx.mock
async def test_ask_document_vector_retrieval(mcp_env, monkeypatch):
    """v2（M4）：向量检索可用时优先于 BM25；命中块（页码+bbox）进入证据与出处。"""
    from ddp_mcp import server as mcp_server

    passthrough_mcp(respx)
    seen: list[str] = []

    async def fake_vector_retrieve(doc_hash, question, k=3):
        seen.append(doc_hash)
        return [{"text": "向量命中块 zeta-42", "page_idx": 4,
                 "bbox": [10, 20, 30, 40], "page_size": None}]

    monkeypatch.setattr(mcp_server, "_vector_retrieve", fake_vector_retrieve)

    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t4"}))
    respx.get(f"{CORPUS}/v1/parse/t4").mock(
        return_value=Response(200, json={"task_id": "t4", "status": "succeeded",
                                         "progress": 1.0, "error": None,
                                         "doc_hash": DOC_HASH}))
    # layout_json 置空：若错误地走 BM25 会命中"无可检索文本块"退化文案，测试即失败
    respx.get(f"{CORPUS}/v1/parse/t4/result").mock(
        return_value=Response(200, json={"markdown": "y" * 4000, "layout_json": {},
                                         "images": []}))

    async with entry_client() as client:
        out = await ask(client, "http://files.example.com/big.docx", "zeta 值是多少？")
    assert "zeta-42" in out and "第 5 页" in out and "开头片段" not in out
    # **身份只认状态响应里的那个值**：自己 sha256(file_url) 算一个会命中
    # 别的主体建的索引（语料域已按主体分域重写 doc_id）
    assert seen == [DOC_HASH]


@respx.mock
async def test_no_doc_hash_means_no_vector_index_lookup(mcp_env, monkeypatch):
    """状态响应里没有 doc_hash 时**不查向量索引**，退回 BM25。

    以前这里有个兜底：`sha256(file_url)`。语料域把外部提交的 doc_id 重写成
    按主体分域的哈希之后，那个兜底算出来的已经不是任何人的身份 ——
    拿它去查 Redis，命中的会是别的主体建的分块索引。
    """
    from ddp_mcp import server as mcp_server

    passthrough_mcp(respx)
    called: list[str] = []

    async def never(doc_hash, question, k=3):
        called.append(doc_hash)
        return [{"text": "别人的块", "page_idx": 0, "bbox": None, "page_size": None}]

    monkeypatch.setattr(mcp_server, "_vector_retrieve", never)
    layout = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"type": "text", "bbox": [10, 10, 100, 30],
         "lines": [{"spans": [{"content": "the answer is 42"}]}]}]}]}
    respx.post(f"{CORPUS}/v1/parse").mock(return_value=Response(202, json={"task_id": "t5"}))
    respx.get(f"{CORPUS}/v1/parse/t5").mock(
        return_value=Response(200, json={"task_id": "t5", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{CORPUS}/v1/parse/t5/result").mock(
        return_value=Response(200, json={"markdown": "z" * 4000, "layout_json": layout,
                                         "images": []}))

    async with entry_client() as client:
        out = await ask(client, "http://files.example.com/nohash.docx", "answer")
    assert called == [], "没有 doc_hash 也去查了向量索引"
    assert "the answer is 42" in out, "应当退回 BM25 并给出这次授权拿到的证据"


# ---------------------------------------------------------------- 内部实现的回归

async def test_vector_retrieve_degrades_quietly(mcp_env, monkeypatch):
    """回归（M4 验收发现）：REDIS_URL 配错时 redis.from_url 抛 ValueError，
    必须被吞掉退回 BM25，而不是让整个 ask_document 崩掉。"""
    from ddp_mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_redis", None)
    monkeypatch.setattr(mcp_server, "REDIS_URL", "not-a-valid-redis-url")
    assert await mcp_server._vector_retrieve("deadbeef", "问题") is None
    assert mcp_server._get_redis_safe() is None


async def test_crop_runs_off_the_event_loop(mcp_env):
    """回归：裁剪必须丢线程池。

    整页渲染(2x)+PIL 裁剪+PNG 编码是纯 CPU，几百毫秒起步。跑在协程里会卡住整个
    MCP server 的事件循环，所有并发 ask_document 一起停摆。
    """
    import threading

    pdf_bytes = fixture("sample.pdf").read_bytes()
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    original = mcp_env._render_crop

    def spy(*args, **kwargs):
        seen.append(threading.current_thread())
        return original(*args, **kwargs)

    mcp_env._render_crop = spy
    try:
        data_uri = await mcp_env._crop_page_region(pdf_bytes, 0, [69, 71, 375, 98], [612, 792])
    finally:
        mcp_env._render_crop = original

    assert data_uri and data_uri.startswith("data:image/png;base64,")
    assert seen and seen[0] is not loop_thread, \
        "渲染跑在了事件循环线程上——并发 ask_document 会被它整个卡住"
