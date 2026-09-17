"""DDP-NODE-CREDENTIAL v1 的共享验证内核（契约 `schemas/ddp-node-credential/v1.json`）。

**零 I/O**：不读库、不发请求、不碰私钥。它只回答四件事 ——

1. 一张凭证的**规范序列化**长什么样（签名覆盖的就是这串字节）；
2. 线上的 `payload.signature` 是不是严格、规范、结构合法（`decode`）；
3. 在**不看公钥**的前提下它能不能用于这次请求：audience、时间窗、操作、
   请求绑定（`inspect`）；
4. 拿到本节点控制面给的信任记录之后，签发节点是否可信、签名是否成立
   （`authenticate`），以及委托范围是否覆盖被读写的那一行（`require_constraints`）。

签名算法的实现通过参数注入（`verify_signature`），默认实现在
`ddp_core.node_signature`（依赖 `cryptography`，只有 `ddp-core[federation]` 装它）；
这样本模块仍然只依赖标准库与生成的契约常量。

## 为什么是这套规范化

签发方是 Go（`services/control-api/internal/discovery/credential.go`），验证方是
Python。两边各写一份 JSON 编码，**唯一**能保证不静默漂移的办法是：

- 把所有字符串收窄到两种编码器必然输出一致字节的 ASCII 子集
  （Go 会转义 `< > &` 与 U+2028，Python 不会 —— 那些字符根本不许出现）；
- 时间用整数秒，不用 RFC3339 文本；
- 键按字典序、无空白；
- 两边都对着同一份冻结夹具 `tests/fixtures/node-credential-v1.json` 断言
  字节、签名与 token 完全相等（Go：`credential_crosslang_test.go`，
  Python：`tests/test_node_credentials.py`）。

验证方**不信任线上字节是规范的**：解码后重新规范化，必须与收到的 payload
逐字节相等。重复键、多余空白、换序、非严格 base64 一律 `credential_invalid`。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

from ddp_contracts import ACTOR_KIND_VALUES, NODE_CREDENTIAL_OPERATION_VALUES

from ddp_core.application.ports import ApplicationError

SCHEMA = "ddp-node-credential/1#Claims"
ALG = "Ed25519"
#: 请求头名。凭证是一次性的秘密：不回显、不入日志、不进错误消息。
HEADER = "X-DDP-Node-Credential"
#: 签名输入的域分隔前缀。它让这类签名不可能被当成控制面 `ddp-node-proof/1`
#: 挑战证明（那是一个以 `[` 开头的 JSON 数组）或反过来。
DOMAIN = b"ddp-node-credential/1\n"
#: 单张凭证的最长有效期。控制面按请求签发，协调者没有理由持有更久的凭证。
MAX_LIFETIME_SECONDS = 120
#: 签发时间允许领先本机时钟的秒数（两台主机的时钟偏差）。过期判定不放宽。
CLOCK_SKEW_SECONDS = 30
MAX_TOKEN_CHARS = 4096
SIGNATURE_BYTES = 64
PUBLIC_KEY_BYTES = 32

#: 读操作走 GET，其余走 POST（契约 Claims.allOf 同一条规则）。
GET_OPERATIONS = frozenset({"probe_read", "execution_read", "evidence_set_read",
                            "catalog_read"})
#: 必须带 step_id 约束的操作。
STEP_BOUND_OPERATIONS = frozenset({"admission_create"})
#: 必须带 task_spec_digest 约束的操作。
SPEC_BOUND_OPERATIONS = frozenset({"probe_create", "probe_read"})

_NODE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}\Z")
_REF = re.compile(r"[A-Za-z0-9._:@+=-]{1,128}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PATH = re.compile(r"/api/v1/federation/[A-Za-z0-9._:@+=/-]{1,256}\Z")
_JTI = re.compile(r"[A-Za-z0-9_-]{22,64}\Z")
_B64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_EPOCH_MAX = 253402300799

_CLAIM_FIELDS = frozenset({"schema", "alg", "issuer_node_id", "audience_node_id", "actor",
                           "operation", "constraints", "request", "issued_at",
                           "expires_at", "jti"})
_ACTOR_FIELDS = frozenset({"organization_id", "subject", "kind"})
_REQUEST_FIELDS = frozenset({"method", "path", "body_digest"})
_CONSTRAINT_FIELDS = ("root_task_id", "step_id", "scope_ref", "task_spec_digest")

SignatureVerifier = Callable[[bytes, bytes, bytes], bool]


def _invalid(message: str) -> ApplicationError:
    return ApplicationError("credential_invalid", message)


def _text(value, pattern: re.Pattern, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid(f"{name} is missing or outside the credential character set")
    return value


def _epoch(value, name: str) -> int:
    # bool 是 int 的子类；True 不是一个时间。
    if type(value) is not int or not 1 <= value <= _EPOCH_MAX:
        raise _invalid(f"{name} must be integer epoch seconds")
    return value


def validate_claims(claims) -> dict:
    """结构校验：与契约 `Claims` 同一组规则，外加 JSON Schema 表达不了的三条
    （issuer≠audience、有效期上限、签发早于过期）。返回原对象。"""
    if not isinstance(claims, dict) or set(claims) != _CLAIM_FIELDS:
        raise _invalid("credential claims must contain exactly the contract fields")
    if claims["schema"] != SCHEMA or claims["alg"] != ALG:
        raise _invalid("unsupported credential schema or algorithm")
    issuer = _text(claims["issuer_node_id"], _NODE, "issuer_node_id")
    audience = _text(claims["audience_node_id"], _NODE, "audience_node_id")
    if issuer == audience:
        raise _invalid("a node cannot issue a credential to itself")
    actor = claims["actor"]
    if not isinstance(actor, dict) or set(actor) != _ACTOR_FIELDS:
        raise _invalid("actor must contain organization_id, subject and kind")
    _text(actor["organization_id"], _REF, "actor.organization_id")
    _text(actor["subject"], _REF, "actor.subject")
    if actor["kind"] not in ACTOR_KIND_VALUES:
        raise _invalid("actor.kind is not a contract actor kind")
    operation = claims["operation"]
    if operation not in NODE_CREDENTIAL_OPERATION_VALUES:
        raise _invalid("operation is not a contract node credential operation")
    request = claims["request"]
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise _invalid("request binding must contain method, path and body_digest")
    expected_method = "GET" if operation in GET_OPERATIONS else "POST"
    if request["method"] != expected_method:
        raise _invalid("request method does not match the operation")
    _text(request["path"], _PATH, "request.path")
    _text(request["body_digest"], _DIGEST, "request.body_digest")
    constraints = claims["constraints"]
    if not isinstance(constraints, dict) or not set(constraints) <= set(_CONSTRAINT_FIELDS):
        raise _invalid("constraints contain an unknown field")
    for name, value in constraints.items():
        _text(value, _DIGEST if name == "task_spec_digest" else _REF, f"constraints.{name}")
    if "root_task_id" not in constraints:
        raise _invalid("constraints.root_task_id is required")
    if operation in STEP_BOUND_OPERATIONS and "step_id" not in constraints:
        raise _invalid("this operation requires a step_id constraint")
    if operation in SPEC_BOUND_OPERATIONS and "task_spec_digest" not in constraints:
        raise _invalid("this operation requires a task_spec_digest constraint")
    issued = _epoch(claims["issued_at"], "issued_at")
    expires = _epoch(claims["expires_at"], "expires_at")
    if not 0 < expires - issued <= MAX_LIFETIME_SECONDS:
        raise _invalid(f"credential lifetime must be 1..{MAX_LIFETIME_SECONDS} seconds")
    _text(claims["jti"], _JTI, "jti")
    return claims


def canonical_bytes(claims: dict) -> bytes:
    """签名覆盖的规范字节。**先校验再编码**：不合法的正文没有规范形式。"""
    validate_claims(claims)
    return json.dumps(claims, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def signing_input(claims: dict) -> bytes:
    return DOMAIN + canonical_bytes(claims)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str, name: str) -> bytes:
    if not _B64URL.fullmatch(text):
        raise _invalid(f"{name} is not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise _invalid(f"{name} is not valid base64url") from None
    # 非规范尾比特（同一串字节有多种写法）也拒绝：线格式只有一种。
    if _b64url(raw) != text:
        raise _invalid(f"{name} is not canonical base64url")
    return raw


def encode(claims: dict, signature: bytes) -> str:
    if not isinstance(signature, bytes) or len(signature) != SIGNATURE_BYTES:
        raise _invalid("an Ed25519 signature is 64 bytes")
    return _b64url(canonical_bytes(claims)) + "." + _b64url(signature)


def _reject_duplicates(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise _invalid("credential payload repeats a key")
        seen[key] = value
    return seen


@dataclass(frozen=True)
class Decoded:
    claims: dict
    payload: bytes
    signature: bytes


def decode(token) -> Decoded:
    """严格解码线格式。任何不规范都是 `credential_invalid`，不做"宽容修正"。"""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise _invalid("credential is missing or too long")
    parts = token.split(".")
    if len(parts) != 2:
        raise _invalid("credential must be payload.signature")
    payload = _unb64url(parts[0], "payload")
    signature = _unb64url(parts[1], "signature")
    if len(signature) != SIGNATURE_BYTES:
        raise _invalid("an Ed25519 signature is 64 bytes")
    try:
        claims = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                            parse_constant=lambda _c: (_ for _ in ()).throw(
                                _invalid("non-finite number in credential")))
    except ApplicationError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise _invalid("credential payload is not JSON") from None
    if canonical_bytes(claims) != payload:
        raise _invalid("credential payload is not in canonical form")
    return Decoded(claims=claims, payload=payload, signature=signature)


def body_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content or b"").hexdigest()


def node_id_for_public_key(public_key: str) -> str:
    """与控制面 `discovery.NodeIDForPublicKey` 同一派生规则（夹具互钉）。"""
    raw = public_key_bytes(public_key)
    return "node-" + hashlib.sha256(raw).hexdigest()[:48]


def public_key_bytes(public_key) -> bytes:
    if not isinstance(public_key, str):
        raise _invalid("public key must be standard base64")
    try:
        raw = base64.b64decode(public_key, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid("public key must be standard base64") from None
    if len(raw) != PUBLIC_KEY_BYTES or base64.b64encode(raw).decode("ascii") != public_key:
        raise _invalid("public key must be a canonical 32-byte Ed25519 key")
    return raw


def inspect(token, *, audience_node_id: str, now: float, operation: str, method: str,
            path: str, body_digest: str) -> Decoded:
    """不看公钥就能做的全部判定，按"先便宜后昂贵"的顺序。

    这些判定对象是**未认证**的正文：它们只能导致拒绝，永远不会导致放行 ——
    放行必须再经过 `authenticate`。先做它们是为了让发错地方、过期、拿错
    操作的凭证不触发一次控制面查询。
    """
    decoded = decode(token)
    claims = decoded.claims
    if claims["audience_node_id"] != audience_node_id:
        raise ApplicationError("credential_audience_mismatch",
                               "credential was issued for another node")
    if claims["issued_at"] > now + CLOCK_SKEW_SECONDS or claims["expires_at"] <= now:
        raise ApplicationError("credential_expired", "credential is outside its validity window")
    if claims["operation"] != operation:
        raise ApplicationError("credential_operation_denied",
                               "credential does not authorize this operation")
    binding = claims["request"]
    if binding["method"] != method or binding["path"] != path \
            or binding["body_digest"] != body_digest:
        raise ApplicationError("credential_scope_denied",
                               "credential is bound to a different request")
    return decoded


def authenticate(decoded: Decoded, *, trust, verify_signature: SignatureVerifier) -> dict:
    """用本节点控制面给的信任记录认证签发节点与签名。

    `trust` 是 `PeerTrust` 形状的 dict，或 None（控制面说没登记）。
    **成员状态先于签名**：已撤销节点的有效签名也不放行。**公钥派生复核**：
    记录里的 node_id 必须由记录里的公钥派生而来 —— 否则一行被改坏的成员
    记录就能让任意公钥冒充这个节点。
    """
    claims = decoded.claims
    issuer = claims["issuer_node_id"]
    if trust is None or not isinstance(trust, dict) or trust.get("node_id") != issuer:
        raise ApplicationError("node_unknown", "issuer node is not a registered member")
    state = trust.get("state")
    if state == "revoked":
        raise ApplicationError("node_revoked", "issuer node has been revoked")
    if state != "approved":
        raise ApplicationError("node_unknown", "issuer node is not approved")
    try:
        derived = node_id_for_public_key(trust.get("public_key"))
    except ApplicationError:
        raise ApplicationError("node_unknown", "issuer trust record has no usable key") from None
    if derived != issuer:
        raise ApplicationError("node_unknown", "issuer trust record key does not derive its id")
    if not verify_signature(public_key_bytes(trust["public_key"]),
                            DOMAIN + decoded.payload, decoded.signature):
        raise _invalid("credential signature does not verify")
    return claims


def require_constraints(claims: dict, **observed) -> None:
    """委托范围比对：每个给出的名字，凭证里的值必须与观测值逐字相等。

    `None` 也是一个观测值：请求没有 scope_ref 时凭证也不许带 —— 否则一张
    为范围 A 签的凭证可以拿去做"不限范围"的探测。凭证缺某个约束而请求有值
    同样拒绝：缺省不是通配。
    """
    constraints = claims.get("constraints") or {}
    for name, value in observed.items():
        if name not in _CONSTRAINT_FIELDS:
            raise ValueError(f"unknown constraint {name}")
        if constraints.get(name) != value:
            raise ApplicationError("credential_scope_denied",
                                   f"credential {name} does not cover this request")


def peer_actor_id(claims: dict) -> str:
    """远端主体在本节点的调用者 id：**不与任何本地用户合并**（T32）。

    由 (issuer, 发行者组织, kind, subject) 派生，`peer-` 前缀 + 27 位十六进制
    恰好 32 字符（本地 actor_id 列宽）。本地用户 id 是 32 位纯十六进制，
    不含 `-`，两者不可能相等。
    """
    actor = claims["actor"]
    material = json.dumps([claims["issuer_node_id"], actor["organization_id"], actor["kind"],
                           actor["subject"]], separators=(",", ":")).encode("utf-8")
    return "peer-" + hashlib.sha256(material).hexdigest()[:27]


def sign_request(*, audience_node_id: str, actor: dict, operation: str, constraints: dict,
                 method: str, path: str, body: bytes, ttl_seconds: int) -> dict:
    """组装给本节点控制面的 `SignRequest`（形状校验与 `validate_claims` 同一套规则）。

    用一个占位 issuer/时间/jti 走一遍 `validate_claims`，保证"语料侧申请的
    东西"在控制面签出之后必然是合法凭证 —— 申请阶段就发现字符集越界，
    而不是签出来之后对端才说 credential_invalid。
    """
    request = {"method": method, "path": path, "body_digest": body_digest(body)}
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_LIFETIME_SECONDS:
        raise _invalid(f"ttl_seconds must be 1..{MAX_LIFETIME_SECONDS}")
    probe_issuer = "node-" + "0" * 48
    if audience_node_id == probe_issuer:
        probe_issuer = "node-" + "1" * 48
    validate_claims({
        "schema": SCHEMA, "alg": ALG, "issuer_node_id": probe_issuer,
        "audience_node_id": audience_node_id, "actor": dict(actor), "operation": operation,
        "constraints": dict(constraints), "request": request, "issued_at": 1,
        "expires_at": 1 + ttl_seconds, "jti": "A" * 22})
    return {"audience_node_id": audience_node_id, "actor": dict(actor),
            "operation": operation, "constraints": dict(constraints), "request": request,
            "ttl_seconds": ttl_seconds}
