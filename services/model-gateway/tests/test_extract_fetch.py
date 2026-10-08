"""抽取平面 PDF 抓取的 SSRF/上限测试。

_load_pdf 必须走 `ddp_core.fetch_policy`：先 check_destination 再
fetch_bytes_capped（判据唯一套件见 ddp_core/tests/test_fetch_policy.py）。
这里覆盖端到端行为三件事：
1. SSRF 拒绝的 file_url（云元数据 IP）-> None，且一次 HTTP 都不发；
2. Content-Length 已超限 -> None，body 一字节都不下；
3. 正常 PDF 照样经过同一条路装进来（上限与目的地检查不断正常链）。
"""
import socket

import respx
from httpx import Response

from ddp_gateway.services import extraction
from ddp_gateway.services.engines import BORNDIGITAL_MAX_BYTES


def _fake_global_dns(monkeypatch):
    """把 DNS 固定解析到一个公网 IP：目的地检查照常执行并通过，HTTP 层由 respx 拦截。

    不固定的话测试依赖真实 DNS（沙箱/CI 解析结果不同）。
    """

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@respx.mock
async def test_ssrf_denied_file_url_returns_none_without_http(app_state):
    """云元数据 IP 的 file_url 在目的地检查就被拒：返回 None，且不出站。"""
    url = "http://169.254.169.254/latest/meta-data/"
    route = respx.get(url).mock(return_value=Response(200, content=b"%PDF-1.4 fake"))
    ctx = extraction.ExtractContext(
        store=None, http=app_state.http, registry=app_state.registry,
        doc_hash="d" * 64, file_url=url)

    assert await extraction._load_pdf(ctx) is None
    assert ctx._pdf is None
    assert not route.called
    # 缓存语义：第二次直接返回，不再走检查/抓取。
    assert await extraction._load_pdf(ctx) is None
    assert not route.called


@respx.mock
async def test_over_cap_content_length_returns_none_without_download(app_state, monkeypatch):
    """响应头声明已超 200MB 上限：直接 None，body 不进内存。"""
    _fake_global_dns(monkeypatch)
    url = "http://example.com/huge.pdf"
    respx.get(url).mock(return_value=Response(
        200,
        headers={"content-length": str(BORNDIGITAL_MAX_BYTES + 1)},
        content=b"%PDF-1.4 tiny"))
    ctx = extraction.ExtractContext(
        store=None, http=app_state.http, registry=app_state.registry,
        doc_hash="d" * 64, file_url=url)

    assert await extraction._load_pdf(ctx) is None
    assert ctx._pdf is None
    assert ctx._pdf_tried is True


@respx.mock
async def test_normal_pdf_still_loads_through_fetch_policy(app_state, monkeypatch):
    """正常 PDF 走同一条受限路径照样装进来，且只抓一次（缓存语义）。"""
    _fake_global_dns(monkeypatch)
    url = "http://example.com/doc.pdf"
    body = b"%PDF-1.4 fake-body"
    route = respx.get(url).mock(return_value=Response(200, content=body))
    ctx = extraction.ExtractContext(
        store=None, http=app_state.http, registry=app_state.registry,
        doc_hash="d" * 64, file_url=url)

    assert await extraction._load_pdf(ctx) == body
    assert route.called
    assert await extraction._load_pdf(ctx) == body
    assert route.call_count == 1
