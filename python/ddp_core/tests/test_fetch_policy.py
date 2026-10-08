"""出站目的地策略（SSRF 封锁）与逐跳抓取的判据单测 —— 唯一套件。

判据住在 ``ddp_core.fetch_policy``；各服务只测自己的配置装配与端到端行为
（网关私网 file_url 拒收、mcp 重定向到内网拒收、corpus 外部 /v1/parse
拒收私网 file_url），见各服务的测试。

不走网络（DNS 用 monkeypatch 钉 getaddrinfo），只验判据本身：
- `/files/` 段边界（/files-evil 不受信）；
- 元数据 IP 显式拒绝（即使平台标 global；字面/压缩/异形写法都拒）；
- 非全局 IP 拒绝；DNS 失败拒绝；userinfo/scheme 拒绝；
- 缺省不跟重定向；允许时逐跳重验、超跳拒绝；
- 字节上限：Content-Length 声明超限一字节都不下，流式超限立刻中断。
"""
import socket

import httpx
import pytest
import respx
from httpx import Response

from ddp_core import fetch_policy as fp
from ddp_core.fetch_policy import (
    FetchNotAllowedError,
    FetchPolicyConfig,
    FileTooLargeError,
)

OPEN = FetchPolicyConfig()
ALLOW = FetchPolicyConfig(allow_redirects=True)
TRUSTED = FetchPolicyConfig(trusted_bases=("http://control-api:8080/files/",))


def _infos(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
             6, "", (ip, 0)) for ip in ips]


def _dns(monkeypatch, mapping):
    def fake(host, *args, **kwargs):
        if host in mapping:
            return _infos(*mapping[host])
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _global(monkeypatch, host="public.example.com"):
    _dns(monkeypatch, {host: ["93.184.216.34"]})


def test_trusted_requires_files_segment_boundary():
    base = "http://control-api:8080/files/"
    assert fp.is_trusted("http://control-api:8080/files/tok-abc", base)
    assert fp.is_trusted("http://control-api:8080/files/tok-abc?token=x", base)
    assert not fp.is_trusted("http://control-api:8080/files-evil/x", base)
    assert not fp.is_trusted("http://control-api:8080/healthz", base)
    assert not fp.is_trusted("http://other:8080/files/tok", base)


def test_trusted_comma_split_and_query_opaque():
    cfg = FetchPolicyConfig(
        trusted_bases=("http://other:9/files/", "http://control-api:8080/files/"))
    assert fp.is_trusted_url("http://control-api:8080/files/abc123?sig=x", cfg)
    assert not fp.is_trusted_url("http://control-api:8080/healthz", cfg)


def test_public_url_rejects_non_global(monkeypatch):
    _dns(monkeypatch, {"public.example.com": ["93.184.216.34"],
                       "inner.example.com": ["10.1.2.3"]})
    fp.check_destination("http://public.example.com/a.pdf", OPEN)
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination("http://inner.example.com/a.pdf", OPEN)


def test_metadata_ip_denied_even_if_global(monkeypatch):
    # 100.100.100.200 在某些平台 is_global 为 True：必须显式拒绝。
    _dns(monkeypatch, {"meta.example.com": ["100.100.100.200"],
                       "meta6.example.com": ["fd00:ec2::254"],
                       "aws.example.com": ["169.254.169.254"]})
    for url in ("http://meta.example.com/", "http://meta6.example.com/",
                "http://aws.example.com/latest/"):
        with pytest.raises(FetchNotAllowedError):
            fp.check_destination(url, OPEN)


def test_metadata_literal_forms_denied(monkeypatch):
    # 字面 IP 不走 DNS：压缩/异形写法也不能绕过元数据表。
    for url in ("http://169.254.169.254/latest/",
                "http://[fd00:ec2::254]/",
                "http://100.100.100.200/"):
        with pytest.raises(FetchNotAllowedError):
            fp.check_destination(url, OPEN)


def test_metadata_blocklist_complete():
    assert {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"} <= set(fp.METADATA_IPS)


def test_unresolvable_is_deny(monkeypatch):
    _dns(monkeypatch, {})
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination("http://no-such-host.invalid/a.pdf", OPEN)


def test_userinfo_and_scheme_rejected(monkeypatch):
    _dns(monkeypatch, {"public.example.com": ["93.184.216.34"]})
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination("http://user:pw@public.example.com/a.pdf", OPEN)
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination("ftp://public.example.com/a.pdf", OPEN)


def test_validate_file_url_empty_is_deny():
    for bad in ("", "   ", None, 123):
        with pytest.raises(FetchNotAllowedError):
            fp.validate_file_url(bad, OPEN)


def test_trusted_url_skips_dns(monkeypatch):
    # 受信基座之下直接放行：DNS 坏了也不影响（内部回源本就解析不了公网）。
    def _boom(host, *args, **kwargs):
        raise AssertionError("trusted path must not touch DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    fp.check_destination("http://control-api:8080/files/tok-abc?token=x", TRUSTED)


def test_join_redirect_relative_location():
    assert fp.join_redirect("http://a.example/x/y", "/z") == "http://a.example/z"
    assert fp.join_redirect("http://a.example/x", "http://b.example/q") == \
        "http://b.example/q"


@respx.mock
async def test_default_redirects_not_followed(monkeypatch):
    _global(monkeypatch)
    url = "http://public.example.com/a"
    respx.get(url).mock(return_value=Response(
        302, headers={"location": "http://public.example.com/b"}))
    nxt = respx.get("http://public.example.com/b").mock(
        return_value=Response(200, content=b"nope"))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FetchNotAllowedError):
            await fp.fetch_bytes_capped(http, url, OPEN, 1024)
    assert not nxt.called


@respx.mock
async def test_redirect_to_internal_revalidated(monkeypatch):
    _dns(monkeypatch, {"public.example.com": ["93.184.216.34"]})
    url = "http://public.example.com/pic.png"
    respx.get(url).mock(return_value=Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"}))
    internal = respx.get("http://169.254.169.254/latest/meta-data/")
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FetchNotAllowedError):
            await fp.fetch_bytes_for_verify(http, url, ALLOW)
    assert not internal.called


@respx.mock
async def test_redirect_hop_limit(monkeypatch):
    # 3 跳链（0->1->2->3 landing）：正好在上限内，放行。
    _global(monkeypatch)
    respx.get("http://public.example.com/0").mock(return_value=Response(
        302, headers={"location": "http://public.example.com/1"}))
    respx.get("http://public.example.com/1").mock(return_value=Response(
        302, headers={"location": "http://public.example.com/2"}))
    respx.get("http://public.example.com/2").mock(return_value=Response(
        302, headers={"location": "http://public.example.com/3"}))
    respx.get("http://public.example.com/3").mock(
        return_value=Response(200, content=b"x"))
    async with httpx.AsyncClient(trust_env=False) as http:
        body, _ = await fp.fetch_bytes_for_verify(
            http, "http://public.example.com/0", ALLOW)
        assert body == b"x"


@respx.mock
async def test_redirect_over_limit_refused_before_fourth_hop(monkeypatch):
    # 4 跳链（0->1->2->3->4 landing）：第 4 跳超上限，还没取最终地址就拒。
    _global(monkeypatch)
    for i in range(4):
        respx.get(f"http://public.example.com/{i}").mock(return_value=Response(
            302, headers={"location": f"http://public.example.com/{i + 1}"}))
    final = respx.get("http://public.example.com/4").mock(
        return_value=Response(200, content=b"y"))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FetchNotAllowedError):
            await fp.fetch_bytes_for_verify(
                http, "http://public.example.com/0", ALLOW)
    assert not final.called

@respx.mock
async def test_relative_redirect_location_resolves(monkeypatch):
    # 相对 Location（"/b"）必须拼回同源绝对 URL 再逐跳重验（旧实现 str()
    # SplitResult 的 bug 会让这一跳直接判据拒绝）。
    _global(monkeypatch)
    respx.get("http://public.example.com/a").mock(return_value=Response(
        302, headers={"location": "/b"}))
    respx.get("http://public.example.com/b").mock(
        return_value=Response(200, content=b"rel-ok"))
    async with httpx.AsyncClient(trust_env=False) as http:
        body, _ = await fp.fetch_bytes_for_verify(
            http, "http://public.example.com/a", ALLOW)
        assert body == b"rel-ok"


@respx.mock
async def test_content_length_over_cap_downloads_nothing(monkeypatch):
    _global(monkeypatch)
    url = "http://public.example.com/huge.pdf"
    route = respx.get(url).mock(return_value=Response(
        200, content=b"%PDF-1.4 tiny",
        headers={"content-length": "104857601"}))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FileTooLargeError):
            await fp.fetch_bytes_capped(http, url, OPEN, 100 * 1024 * 1024)
    assert route.called  # 必须发起请求才能看到 Content-Length


@respx.mock
async def test_streaming_over_cap_stops_early(monkeypatch):
    _global(monkeypatch)
    url = "http://public.example.com/stream.pdf"
    respx.get(url).mock(return_value=Response(200, content=b"x" * 2048))
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FileTooLargeError):
            await fp.fetch_bytes_capped(http, url, OPEN, 1024)


@respx.mock
async def test_verify_returns_body_and_content_type(monkeypatch):
    _global(monkeypatch)
    url = "http://public.example.com/pic.png"
    respx.get(url).mock(return_value=Response(
        200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    async with httpx.AsyncClient(trust_env=False) as http:
        body, ctype = await fp.fetch_bytes_for_verify(http, url, OPEN)
    assert body == b"\x89PNG"
    assert ctype == "image/png"


# 稳定文件 URL 的真实形状：control-api 的 `/files/{token}` 只 302 到它自己签发的
# **内网** MinIO 预签名（`http://minio:9000/<bucket>/<key>?X-Amz-...`）。
# 两跳都在内网：第二跳必须落在同样显式登记的受信基座之下才放行。
STABLE_FILE = FetchPolicyConfig(trusted_bases=(
    "http://control-api:8080/files/", "http://minio:9000/deepdocparse/"))


@respx.mock
async def test_stable_file_redirect_to_registered_object_store_is_followed(monkeypatch):
    _dns(monkeypatch, {"control-api": ["172.18.0.4"], "minio": ["172.18.0.5"]})
    presigned = "http://minio:9000/deepdocparse/docs/a.pdf?X-Amz-Signature=abc"
    respx.get("http://control-api:8080/files/tok-1").mock(return_value=Response(
        302, headers={"location": presigned}))
    respx.get(presigned).mock(return_value=Response(200, content=b"%PDF-1.7"))
    async with httpx.AsyncClient(trust_env=False) as http:
        body = await fp.fetch_bytes_capped(
            http, "http://control-api:8080/files/tok-1", STABLE_FILE, 1024)
    assert body == b"%PDF-1.7"


@respx.mock
async def test_trusted_hop_cannot_redirect_to_unregistered_internal_host(monkeypatch):
    # 受信不传染：受信基座 302 到没登记的内网地址照样拒，且一字节都不去取。
    _dns(monkeypatch, {"control-api": ["172.18.0.4"], "redis": ["172.18.0.6"]})
    respx.get("http://control-api:8080/files/tok-1").mock(return_value=Response(
        302, headers={"location": "http://redis:6379/"}))
    internal = respx.get("http://redis:6379/")
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(FetchNotAllowedError):
            await fp.fetch_bytes_capped(
                http, "http://control-api:8080/files/tok-1", STABLE_FILE, 1024)
    assert not internal.called


def test_trusted_base_is_a_segment_aligned_prefix():
    base = "http://minio:9000/deepdocparse/"
    assert fp.is_trusted("http://minio:9000/deepdocparse/docs/a.pdf?X-Amz-Signature=x", base)
    assert not fp.is_trusted("http://minio:9000/deepdocparse-evil/a.pdf", base)
    assert not fp.is_trusted("http://minio:9000/other-bucket/a.pdf", base)
    assert not fp.is_trusted("http://minio:9001/deepdocparse/a.pdf", base)


def test_root_base_trusts_nothing():
    # 根路径基座等于信任整台主机（含它的管理/内网端点），一律不算受信。
    assert not fp.is_trusted("http://control-api:8080/files/tok", "http://control-api:8080/")
    assert not fp.is_trusted("http://control-api:8080/healthz", "http://control-api:8080")


def test_malformed_port_fails_closed_as_fetch_not_allowed():
    # 回归：`.port` 在端口畸形时抛裸 ValueError（不是 urlsplit 时抛），
    # 旧 is_trusted 没包住，调用方 `except FetchNotAllowedError` 接不住 → 500。
    # 现在：is_trusted 返回 False，check/validate 抛 FetchNotAllowedError。
    bad = "http://control-api:8080:bad/files/tok"
    assert fp.is_trusted(bad, "http://control-api:8080/files/") is False
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination(bad, TRUSTED)
    with pytest.raises(FetchNotAllowedError):
        fp.validate_file_url(bad, TRUSTED)
    # 越界端口同理（`Port out of range` 同样是裸 ValueError）。
    over = "http://control-api:99999/files/tok"
    assert fp.is_trusted(over, "http://control-api:8080/files/") is False
    with pytest.raises(FetchNotAllowedError):
        fp.validate_file_url(over, TRUSTED)


def test_malformed_host_fails_closed_as_fetch_not_allowed():
    # IPv6 缺括号：urlsplit 直接抛，同样必须落成 FetchNotAllowedError 而不是裸错。
    bad = "http://[::1]extra/files/tok"
    assert fp.is_trusted(bad, "http://control-api:8080/files/") is False
    with pytest.raises(FetchNotAllowedError):
        fp.check_destination(bad, TRUSTED)
    with pytest.raises(FetchNotAllowedError):
        fp.validate_file_url(bad, TRUSTED)


def test_malformed_base_never_trusts():
    assert fp.is_trusted("http://control-api:8080/files/tok",
                         "http://control-api:8080:bad/files/") is False
    assert fp.is_trusted("http://control-api:8080/files/tok", "http://[::1]extra/") is False


def test_empty_location_is_rejected_not_root():
    # 回归：空 Location 曾拼成当前 URL 的根路径 `/` 再去抓 —— 上游坏了或
    # 有人在探时，"回根"等于替它补了一个它没说过的请求。
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", "")
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", "   ")
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", None)


def test_invalid_location_is_rejected():
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", "http://[::1]extra/z")
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", "http://h:99999/z")
    with pytest.raises(FetchNotAllowedError):
        fp.join_redirect("http://a.example/x/y", "http://user:pass@a.example/z")


@respx.mock
async def test_fetch_strips_default_authorization_and_cookie(monkeypatch):
    # 调用方传进来的 client 可能带了默认 Authorization/Cookie（服务间复用
    # 同一个 client 时最常见）—— 抓取第三方 URL 时必须逐跳剥掉，不能靠
    # "调用方保证传无凭据 client"的约定。
    _global(monkeypatch)
    url = "http://public.example.com/a.bin"
    nxt = respx.get("http://public.example.com/b.bin").mock(
        return_value=Response(200, content=b"hop2"))
    respx.get(url).mock(return_value=Response(
        302, headers={"location": "/b.bin"}, content=b""))
    async with httpx.AsyncClient(headers={"Authorization": "Bearer s3cr3t",
                                           "Cookie": "sess=abc",
                                           "X-Keep": "yes"},
                                 trust_env=False, follow_redirects=False) as http:
        body = await fp.fetch_bytes_capped(http, url, ALLOW, 1024)
    assert body == b"hop2"
    assert nxt.called
    seen = nxt.calls.last.request.headers
    assert "authorization" not in seen and "cookie" not in seen
    assert seen["x-keep"] == "yes"
