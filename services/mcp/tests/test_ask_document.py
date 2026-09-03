"""MCP `ask_document`（deprecated 兼容工具）与向量检索路的用例。

合仓前这些用例挤在 service 仓库的 `tests/test_contract.py` 里 —— 那是因为
mcp_server 当时没有自己的测试目录，而不是因为它们验的是网关。
它们验的是 MCP 服务：解析中重试模式、裁图核对、分词兜底、向量路降级。
与网关的交互一律 respx mock（网关自身的行为由 model-gateway 的契约测试保证）。
"""
import asyncio
import json
import struct

import pytest
import respx
from httpx import Response

from ddp_paths import fixture


async def test_vector_retrieve_degrades_quietly(mcp_gateway, monkeypatch):
    """回归（M4 验收发现）：REDIS_URL 配错时 redis.from_url 抛 ValueError，
    必须被吞掉退回 BM25，而不是让整个 ask_document 崩掉。"""
    from ddp_mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_redis", None)
    monkeypatch.setattr(mcp_server, "REDIS_URL", "not-a-valid-redis-url")
    # 直接调用不得抛异常，且必须判定为不可用
    assert await mcp_server._vector_retrieve("deadbeef", "问题") is None
    assert mcp_server._get_redis_safe() is None



# ---------- M3: MCP ask_document（gateway 交互用 respx mock，gateway 行为由上面的测试保证） ----------

GW = "http://gw.test"


def _ask_fn():
    from ddp_mcp import server as mcp_server

    tool = mcp_server.ask_document
    return getattr(tool, "fn", tool)  # fastmcp FunctionTool -> 原始协程函数


@pytest.fixture
def mcp_gateway(monkeypatch):
    from ddp_mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "GATEWAY", GW)
    return mcp_server


@respx.mock
async def test_ask_document_retry_pattern(mcp_gateway):
    """未解析的大文档：首次调用返回'解析中'提示；解析完成后再调用返回带出处答案。"""
    ask = _ask_fn()
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t1"}))
    status = respx.get(f"{GW}/v1/parse/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "running",
                                         "progress": 0.5, "error": None}))

    out1 = await ask("http://files.example.com/big.pdf", "总收入是多少？")
    assert "解析中" in out1 and "t1" in out1 and "重试" in out1

    # 稍后重试：同 URL 幂等复用任务，已 succeeded -> 短文档全文作证据
    status.mock(return_value=Response(200, json={"task_id": "t1", "status": "succeeded",
                                                 "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t1/result").mock(
        return_value=Response(200, json={"markdown": "# 财报\n\n总收入 42 亿元。",
                                         "layout_json": {}, "images": []}))
    out2 = await ask("http://files.example.com/big.pdf", "总收入是多少？")
    assert "总收入 42 亿元" in out2 and "出处" in out2 and "/v1/parse/t1/result" in out2


@respx.mock
async def test_ask_document_image_direct(mcp_gateway):
    """图片 URL：不走解析平面，直接 VQA 秒回。"""
    ask = _ask_fn()
    png = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                        "01f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
    respx.get("http://files.example.com/chart.png").mock(
        return_value=Response(200, content=png, headers={"content-type": "image/png"}))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "图中数值为 42"}}]}))
    parse_route = respx.post(f"{GW}/v1/parse")

    out = await ask("http://files.example.com/chart.png", "图中数值？")
    assert "图中数值为 42" in out and "出处" in out
    assert vqa.called and not parse_route.called


@respx.mock
async def test_ask_document_bm25_crop_verify(mcp_gateway):
    """长文档：BM25 命中版面块（带页码+bbox）-> 裁剪原 PDF 区域 -> VQA 验证 -> 带出处返回。"""
    from pathlib import Path

    ask = _ask_fn()
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
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t2"}))
    respx.get(f"{GW}/v1/parse/t2").mock(
        return_value=Response(200, json={"task_id": "t2", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t2/result").mock(
        return_value=Response(200, json={"markdown": "x" * 4000, "layout_json": layout,
                                         "images": []}))
    respx.get("http://files.example.com/long.pdf").mock(
        return_value=Response(200, content=pdf_bytes))
    vqa = respx.post(f"{GW}/v1/chat/completions").mock(
        return_value=Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "42"}}]}))

    out = await ask("http://files.example.com/long.pdf", "what is the answer to everything")
    assert "42" in out and "第 1 页" in out and "bbox" in out
    assert "视觉验证" in out, "首个命中块必须走裁剪+VQA 验证路径"
    assert vqa.called
    # 发给 VQA 的必须是裁剪出的 PNG data URI
    sent = json.loads(vqa.calls.last.request.content)
    image_part = sent["messages"][0]["content"][0]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")


async def test_crop_runs_off_the_event_loop(mcp_gateway):
    """回归：裁剪必须丢线程池。

    整页渲染(2x)+PIL 裁剪+PNG 编码是纯 CPU，几百毫秒起步。跑在协程里会卡住整个
    MCP server 的事件循环，所有并发 ask_document 一起停摆。
    Web 层同款逻辑（backend/app/crops.py）一直是 to_thread，这边曾经漏了。
    """
    import threading
    from pathlib import Path

    pdf_bytes = fixture("sample.pdf").read_bytes()
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    original = mcp_gateway._render_crop

    def spy(*args, **kwargs):
        seen.append(threading.current_thread())
        return original(*args, **kwargs)

    mcp_gateway._render_crop = spy
    try:
        data_uri = await mcp_gateway._crop_page_region(pdf_bytes, 0, [69, 71, 375, 98], [612, 792])
    finally:
        mcp_gateway._render_crop = original

    assert data_uri and data_uri.startswith("data:image/png;base64,")
    assert seen and seen[0] is not loop_thread, \
        "渲染跑在了事件循环线程上——并发 ask_document 会被它整个卡住"


@respx.mock
async def test_ask_document_untokenizable_doc_no_crash(mcp_gateway):
    """回归（M3 验收发现）：全部版面块 token 化为空（非中英文种/全符号）时
    不得让 BM25 除零崩溃，应退化为开头片段。"""
    ask = _ask_fn()
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
    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t3"}))
    respx.get(f"{GW}/v1/parse/t3").mock(
        return_value=Response(200, json={"task_id": "t3", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    respx.get(f"{GW}/v1/parse/t3/result").mock(
        return_value=Response(200, json={"markdown": "허" * 4000, "layout_json": layout,
                                         "images": []}))

    out = await ask("http://files.example.com/korean.pdf", "질문")
    assert "开头片段" in out and "완전" not in out  # 正常返回退化文案，而不是崩溃


@respx.mock
async def test_ask_document_vector_retrieval(mcp_gateway, monkeypatch):
    """v2（M4）：向量检索可用时优先于 BM25；命中块（页码+bbox）进入证据与出处。"""
    from ddp_mcp import server as mcp_server

    ask = _ask_fn()

    async def fake_vector_retrieve(doc_hash, question, k=3):
        assert len(doc_hash) == 64, "doc_hash 必须与 gateway 同为 sha256"
        return [{"text": "向量命中块 zeta-42", "page_idx": 4,
                 "bbox": [10, 20, 30, 40], "page_size": None}]

    monkeypatch.setattr(mcp_server, "_vector_retrieve", fake_vector_retrieve)

    respx.post(f"{GW}/v1/parse").mock(return_value=Response(202, json={"task_id": "t4"}))
    respx.get(f"{GW}/v1/parse/t4").mock(
        return_value=Response(200, json={"task_id": "t4", "status": "succeeded",
                                         "progress": 1.0, "error": None}))
    # layout_json 置空：若错误地走 BM25 会命中"无可检索文本块"退化文案，测试即失败
    respx.get(f"{GW}/v1/parse/t4/result").mock(
        return_value=Response(200, json={"markdown": "y" * 4000, "layout_json": {},
                                         "images": []}))

    out = await ask("http://files.example.com/big.docx", "zeta 值是多少？")
    assert "zeta-42" in out and "第 5 页" in out and "开头片段" not in out


