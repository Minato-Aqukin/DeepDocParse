"""出站抓取的目的地策略（SSRF 封锁）与带上限的字节抓取 —— 唯一实现。

三服务（corpus-api / model-gateway / mcp）共用这一份；各服务只保留自己的
配置装配（读各自 Settings/env，建成 :class:`FetchPolicyConfig`），判据改动
只改这里。铁律 4：共享的是同一份实现，不是复制品。

语义（取三份旧实现中最严的并集）：
- 仅允许 http/https，不许带 userinfo；
- 受信基座（``trusted_bases``，逗号分隔可配多个）之下直接放行：scheme+host+port
  一致、路径落在基座路径之下（按段对齐，``/files/`` 不覆盖 ``/files-evil/``）；
  根路径基座等于信任整台主机，一律不算受信；query 不透明，不参与比较
  （``?token=``、``X-Amz-Signature`` 这类签名参数原样透传）；
- 其余一律目的地策略：host 解析成 IP（字面 IP 不走 DNS），**全部**地址全局
  可路由才放行；云元数据 IP 显式拒绝（即使某平台把它标成 global）；DNS 失败
  即拒（fail closed）；
- 重定向缺省不跟（``allow_redirects=False``）；允许时手工逐跳、每跳重验、
  非受信至多 3 跳。受信链至多 5 跳，但**受信不传染**：每一跳的目标都必须落在
  受信基座之下或通过公网检查（登记了基座的内网站走前者，公网址走后者）。
  部署时把内部回源链上的每一站都登记进去 —— ``/files/{token}`` 302 到对象存储
  的内网预签名，所以对象存储的 ``<endpoint>/<bucket>/`` 也要登记，否则这一跳
  按内网地址被拒；全程不转发 Authorization 与 Cookie（每跳显式剥掉，不依赖
  调用方传无凭据 client）。

三份旧实现分歧点的取舍（都取更严的一边）：
1. 元数据 IP 比对：corpus 只比 ``str(ip)``、gateway 只比 ``ip.compressed``、
   mcp 比 ``raw`` 或 ``str(ip)``。这里取并集再加对象相等：字面写法、
   压缩写法、异形写法（如 ``0x``/前导零）任一命中即拒。
2. 字面 IP：corpus 不走 DNS 直接判，gateway/mcp 走 ``getaddrinfo``。
   取 corpus 的 fast path（不依赖 DNS、不受本机解析器影响）， verdict 相同。
3. IPv6 zone id（``%eth0``）：gateway 拼接 ``info[4][0]`` 不剥 zone，
   corpus/mcp 剥。这里一律剥。
4. 受信链逐跳重验：gateway 受信链不重验，mcp 每跳都过 ``check_destination``。
   取 mcp 的逐跳重验（外部 302 到内网即拒）。
5. ``FileTooLargeError`` 与双上限只在 gateway 有，mcp 的 verify 抓取无上限。
   这里同一套循环：``max_bytes=None`` 时不设限（mcp 行为不变），传了就双保险。

本模块只依赖标准库 + httpx：不 import 任何服务包，不读环境变量、不读
Settings —— 调用方把装配好的 :class:`FetchPolicyConfig` 传进来。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class FetchNotAllowedError(ValueError):
    """目的地策略拒绝（SSRF 封锁）：调用方映射成 400 fetch_not_allowed。"""


class FileTooLargeError(RuntimeError):
    """超过引擎的字节上限：调用方映射成 413 file_too_large。"""


#: 云元数据地址：即使某平台把它标成 global 也显式拒绝。
METADATA_IPS = frozenset({"169.254.169.254", "100.100.100.200", "fd00:ec2::254"})

#: 非受信链允许跟随重定向时的上限（仅 allow_redirects=True 时生效）。
MAX_REDIRECTS = 3

#: 受信链（内部回源）上限：/files/{token} -> 预签名是一跳，本身不再转。
TRUSTED_MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_METADATA_IP_OBJS = frozenset(ipaddress.ip_address(x) for x in METADATA_IPS)


@dataclass(frozen=True)
class FetchPolicyConfig:
    """目的地策略的配置快照：调用方从各自 Settings/env 装配后传进来。

    ``trusted_bases``：受信基座前缀（已拆成元组，空 = 不信任任何内部地址）；
    ``allow_redirects``：非受信链是否允许手工逐跳（缺省 False，一跳都不跟）。
    """

    trusted_bases: tuple[str, ...] = ()
    allow_redirects: bool = False


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get((scheme or "").lower())


def is_trusted(url: str, base: str) -> bool:
    """url 是否落在受信基座 base 之下。

    scheme+host+port 一致，且路径是基座路径本身或按段对齐落在其下（query 视为
    不透明，不参与比较）。基座 ``http://control-api:8080/files/`` 下
    ``/files/<token>`` 受信而 ``/files-evil/x``、``/healthz`` 不受信。
    根路径基座（``http://host/``）会把那台主机的所有端点都放进来，不算受信。
    """
    if not base or not url:
        return False
    try:
        b, c = urlsplit(base), urlsplit(url)
    except ValueError:
        return False
    # `.hostname`/`.port` 在端口畸形（`host:8080:bad`、越界端口）时抛 ValueError，
    # 而不是 urlsplit 时抛 —— 访问它们同样要 fail closed，否则调用方
    # `except FetchNotAllowedError` 接不住，畸形 URL 会以 500 而不是 400 收场。
    try:
        c_userinfo = bool(c.username or c.password)
        b_userinfo = bool(b.username or b.password)
        c_scheme, b_scheme = (c.scheme or "").lower(), (b.scheme or "").lower()
        c_host, b_host = (c.hostname or "").lower(), (b.hostname or "").lower()
        c_port, b_port = c.port, b.port
    except ValueError:
        return False
    if c_userinfo or b_userinfo:
        return False
    if c_scheme not in ("http", "https"):
        return False
    if c_scheme != b_scheme:
        return False
    if c_host != b_host:
        return False
    if (c_port or _default_port(c_scheme)) != (b_port or _default_port(b_scheme)):
        return False
    base_path = (b.path or "/").rstrip("/")
    if not base_path:
        return False
    target_path = c.path or "/"
    return target_path == base_path or target_path.startswith(base_path + "/")


def is_trusted_url(url: str, config: FetchPolicyConfig) -> bool:
    """url 是否落在配置中任一受信基座之下。"""
    return any(is_trusted(url, base) for base in config.trusted_bases)


def _reject_unroutable(
    ip: ipaddress._BaseAddress, host: str, raw: str | None = None
) -> None:
    # 分歧点 1 的并集：字面写法 / 压缩写法 / 对象相等任一命中即拒。
    if raw is not None and raw in METADATA_IPS:
        raise FetchNotAllowedError(f"不允许抓取这个地址（云元数据地址）：{host}")
    if ip in _METADATA_IP_OBJS:
        raise FetchNotAllowedError(f"不允许抓取这个地址（云元数据地址）：{host}")
    if ip.compressed in METADATA_IPS or str(ip) in METADATA_IPS:
        raise FetchNotAllowedError(f"不允许抓取这个地址（云元数据地址）：{host}")
    if not ip.is_global:
        raise FetchNotAllowedError(f"不允许抓取这个地址（目标不是公网地址）：{host}")


def _check_public_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise FetchNotAllowedError(f"不允许抓取这个地址：{url}")
    if (parts.scheme or "").lower() not in ("http", "https"):
        raise FetchNotAllowedError(f"不允许抓取这个地址（仅 http/https）：{url}")
    # `.username`/`.hostname`/`.port` 在 userinfo 非法、IPv6 缺括号、端口畸形时
    # 抛裸 ValueError —— 必须转成 FetchNotAllowedError，否则调用方按 400 映射的
    # `except FetchNotAllowedError` 接不住（FetchNotAllowedError 是 ValueError
    # 的子类，反过来裸 ValueError 不是它）。
    try:
        userinfo = bool(parts.username or parts.password)
        host = parts.hostname or ""
        _ = parts.port
    except ValueError:
        raise FetchNotAllowedError(f"不允许抓取这个地址（URL 畸形）：{url}")
    if userinfo:
        raise FetchNotAllowedError("不允许抓取这个地址（URL 不许带 userinfo）")
    if not host:
        raise FetchNotAllowedError("不允许抓取这个地址（host 为空）")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        # 字面 IP 不走 DNS：内网字面 IP 照样拒。
        _reject_unroutable(literal, host, raw=host)
        return
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchNotAllowedError(f"不允许抓取这个地址（DNS 解析失败）：{host}")
    if not infos:
        raise FetchNotAllowedError(f"不允许抓取这个地址（DNS 无记录）：{host}")
    for info in infos:
        raw = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise FetchNotAllowedError(f"不允许抓取这个地址（解析结果非法）：{host}")
        _reject_unroutable(ip, host, raw=raw)


def check_destination(url: str, config: FetchPolicyConfig) -> None:
    """非受信 url 的目的地检查，不过即 `FetchNotAllowedError`。

    受信基座之下直接放行（内部回源，不做 DNS 封锁）；否则仅 http/https、
    无 userinfo、host 可解析且**全部**地址全局可路由，云元数据 IP 显式拒绝。
    字面 IP 不走 DNS（内网字面 IP 照样拒）；DNS 失败即拒（fail closed）。
    """
    if is_trusted_url(url, config):
        return
    _check_public_url(url)


def validate_file_url(url: str, config: FetchPolicyConfig) -> None:
    """转发前的 file_url 校验：空值 fail closed，否则走目的地策略。"""
    if not isinstance(url, str) or not url.strip():
        raise FetchNotAllowedError("file_url 为空")
    check_destination(url, config)


def join_redirect(current_url: str, location: str) -> str:
    """手工逐跳用的下一跳拼接：相对 Location 按当前 URL 补齐。

    只做拼接不做请求；调用方每跳都要重新 `check_destination`，跳数不得超过
    `MAX_REDIRECTS`（受信链 `TRUSTED_MAX_REDIRECTS`），且永不转发 Authorization。
    空 Location 与拼不回合法 URL 的 Location 直接拒绝 —— 它们不是"回根路径"，
    而是上游坏了或有人在探。
    """
    if not isinstance(location, str) or not location.strip():
        raise FetchNotAllowedError("重定向缺少 Location")
    try:
        nxt = urlsplit(location)
    except ValueError:
        raise FetchNotAllowedError(f"重定向 Location 非法：{location!r}")
    if nxt.username or nxt.password:
        raise FetchNotAllowedError("重定向 Location 不许带 userinfo")
    try:
        _ = nxt.hostname, nxt.port
        cur = urlsplit(current_url)
    except ValueError:
        raise FetchNotAllowedError(f"重定向 Location 非法：{location!r}")
    if nxt.netloc:
        return location
    # 注意：`str(SplitResult)` 得到的是 namedtuple 的 repr，不是 URL ——
    # 必须用 `.geturl()` 拼回字符串（三份旧实现在这里都有同一个 bug：
    # 相对 Location 会拼成 `"SplitResult(...)"`，下一跳直接判据拒绝）。
    return cur._replace(path=nxt.path or "/", query=nxt.query or "").geturl()


async def _fetch_loop(
    http: httpx.AsyncClient,
    url: str,
    config: FetchPolicyConfig,
    max_bytes: int | None,
) -> tuple[bytes, str | None]:
    """逐跳抓取的唯一循环：两套公开入口共用（判据见模块 docstring）。

    全程 ``follow_redirects=False``，每次只发无凭据 GET：显式剥掉
    ``Authorization`` 与 ``Cookie``（调用方传进来的 client 可能带了默认头，
    "调用方保证无凭据"的约定靠不住）；每一跳（首跳 + 每个 Location）都过
    ``check_destination``；``max_bytes`` 为 None 时不设限，否则
    Content-Length 声明与流式累计双保险。
    返回 ``(body, content_type)``，content_type 可空（调用方按扩展名兜底）。
    """
    check_destination(url, config)
    trusted = is_trusted_url(url, config)
    if trusted:
        max_redirects = TRUSTED_MAX_REDIRECTS
    else:
        max_redirects = MAX_REDIRECTS if config.allow_redirects else 0
    current = url
    hops = 0
    while True:
        # 默认头合并发生在 build_request 里：传 headers={...} 只能覆盖同名头，
        # 删不掉 client 自带的 Authorization —— 必须先建请求再逐个删掉凭据头。
        # httpx.stream 不接受预建 Request，所以这里按它的同一套装配
        # （build_request 减去凭据头、再手动管 Response 的开关）。
        request = http.build_request("GET", current)
        for name in ("authorization", "cookie"):
            if name in request.headers:
                del request.headers[name]
        response = await http.send(request, follow_redirects=False, stream=True)
        try:
            if response.status_code in _REDIRECT_STATUSES:
                if hops >= max_redirects:
                    raise FetchNotAllowedError(
                        f"不允许跟随重定向（已到上限 {max_redirects} 跳）：{current}")
                location = response.headers.get("location") or ""
                current = join_redirect(current, location)
                hops += 1
                check_destination(current, config)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            if max_bytes is None:
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                return b"".join(chunks), content_type
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FileTooLargeError(
                    f"文件声明 {int(declared) // 1024 // 1024}MB，超过处理上限"
                    f"（{max_bytes // 1024 // 1024}MB），一字节都不会下载")
            body: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(
                        f"文件超过处理上限（{max_bytes // 1024 // 1024}MB）。"
                        f"它要完整进内存，请换更大的上限或改用 mineru 引擎")
                body.append(chunk)
            return b"".join(body), content_type
        finally:
            await response.aclose()


async def fetch_bytes_capped(
    http: httpx.AsyncClient, url: str, config: FetchPolicyConfig, max_bytes: int
) -> bytes:
    """按目的地策略抓取 url，边下边计字节，超限即停（网关引擎用）。

    抛 FetchNotAllowedError / FileTooLargeError / httpx.HTTPError，
    调用方按自己的上下文映射（请求路径->HTTP 码，worker 路径->落终态）。
    """
    body, _ = await _fetch_loop(http, url, config, max_bytes)
    return body


async def fetch_bytes_for_verify(
    http: httpx.AsyncClient,
    url: str,
    config: FetchPolicyConfig,
    max_bytes: int | None = None,
) -> tuple[bytes, str | None]:
    """按目的地策略抓取 url 的字节与 content-type（MCP 图片/PDF 验证用）。

    先判目的地（内网/元数据/不可解析一律拒，**一次请求都不发**）；需要上限时
    传 ``max_bytes``（语义与 :func:`fetch_bytes_capped` 相同），缺省不设限。
    """
    return await _fetch_loop(http, url, config, max_bytes)


__all__ = [
    "FetchNotAllowedError",
    "FileTooLargeError",
    "FetchPolicyConfig",
    "METADATA_IPS",
    "MAX_REDIRECTS",
    "TRUSTED_MAX_REDIRECTS",
    "is_trusted",
    "is_trusted_url",
    "check_destination",
    "validate_file_url",
    "join_redirect",
    "fetch_bytes_capped",
    "fetch_bytes_for_verify",
]
