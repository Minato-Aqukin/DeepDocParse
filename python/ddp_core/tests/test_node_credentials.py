"""DDP-NODE-CREDENTIAL v1 验证内核（`ddp_core.application.node_credentials`）。

跨语言冻结夹具 `tests/fixtures/node-credential-v1.json` 与 Go 的
`credential_crosslang_test.go` 共用：这里重签必须得到同一串字节、同一个签名、
同一个 token，并且逐条拒绝夹具里的反例。任何一侧编码漂移，两边必有一边红。
"""
from __future__ import annotations

import base64
import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from ddp_paths import fixture

from ddp_core.application import node_credentials as nc
from ddp_core.application.ports import ApplicationError
from ddp_core.node_signature import ed25519_verify

VECTOR = json.loads(fixture("node-credential-v1.json").read_text(encoding="utf-8"))
NOW = VECTOR["claims"]["issued_at"] + 5


def _issuer_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTOR["issuer_seed_hex"]))


def _trust(**over) -> dict:
    value = {"node_id": VECTOR["issuer_node_id"], "state": "approved",
             "public_key": VECTOR["issuer_public_key"],
             "key_fingerprint": "sha256:" + "0" * 64, "organization_id": "org-b",
             "authority_node_id": VECTOR["audience_node_id"], "revision": 1}
    value.update(over)
    return value


def _sign(claims: dict, key: Ed25519PrivateKey | None = None) -> str:
    key = key or _issuer_key()
    return nc.encode(claims, key.sign(nc.signing_input(claims)))


def _inspect(token: str, **over):
    request = VECTOR["claims"]["request"]
    kwargs = {"audience_node_id": VECTOR["audience_node_id"], "now": NOW,
              "operation": VECTOR["claims"]["operation"], "method": request["method"],
              "path": request["path"], "body_digest": request["body_digest"]}
    kwargs.update(over)
    return nc.inspect(token, **kwargs)


def _code(call) -> str:
    with pytest.raises(ApplicationError) as info:
        call()
    return info.value.code


# ---------------------------------------------------------------- 跨语言夹具

def test_frozen_vector_reproduces_bytes_signature_and_token():
    claims = VECTOR["claims"]
    assert nc.canonical_bytes(claims).decode() == VECTOR["canonical_payload"]
    assert nc.node_id_for_public_key(VECTOR["issuer_public_key"]) == VECTOR["issuer_node_id"]
    assert nc.node_id_for_public_key(VECTOR["audience_public_key"]) == VECTOR["audience_node_id"]
    assert nc.body_digest(VECTOR["request_body"].encode()) == claims["request"]["body_digest"]
    signature = _issuer_key().sign(nc.signing_input(claims))
    assert base64.urlsafe_b64encode(signature).rstrip(b"=").decode() \
        == VECTOR["signature_b64url"]
    assert nc.encode(claims, signature) == VECTOR["credential"]
    assert nc.peer_actor_id(claims) == VECTOR["peer_actor_id"]


def test_frozen_vector_verifies_end_to_end():
    decoded = _inspect(VECTOR["credential"])
    claims = nc.authenticate(decoded, trust=_trust(), verify_signature=ed25519_verify)
    assert claims == VECTOR["claims"]


@pytest.mark.parametrize("case", VECTOR["invalid"], ids=lambda case: case["name"])
def test_frozen_invalid_vectors_are_refused_with_the_stated_code(case):
    def run():
        decoded = _inspect(case["credential"])
        nc.authenticate(decoded, trust=_trust(), verify_signature=ed25519_verify)
    assert _code(run) == case["code"], case["why"]


# ---------------------------------------------------------- inspect（无公钥）

def test_wrong_audience_is_refused_before_any_key_lookup():
    other = "node-" + "9" * 48
    assert _code(lambda: _inspect(VECTOR["credential"], audience_node_id=other)) \
        == "credential_audience_mismatch"


def test_expired_and_future_credentials_are_refused():
    claims = VECTOR["claims"]
    assert _code(lambda: _inspect(VECTOR["credential"], now=claims["expires_at"])) \
        == "credential_expired"
    assert _code(lambda: _inspect(
        VECTOR["credential"], now=claims["issued_at"] - nc.CLOCK_SKEW_SECONDS - 1)) \
        == "credential_expired"
    # 时钟偏差之内的"未来签发"放行（两台主机的钟不会完全一致）。
    _inspect(VECTOR["credential"], now=claims["issued_at"] - nc.CLOCK_SKEW_SECONDS)


def test_operation_and_request_binding_must_match_the_endpoint():
    token = VECTOR["credential"]
    assert _code(lambda: _inspect(token, operation="execution_read")) \
        == "credential_operation_denied"
    assert _code(lambda: _inspect(token, method="GET")) == "credential_scope_denied"
    assert _code(lambda: _inspect(token, path="/api/v1/federation/admissions/lookup")) \
        == "credential_scope_denied"
    assert _code(lambda: _inspect(token, body_digest=nc.body_digest(b"{}"))) \
        == "credential_scope_denied"


# ------------------------------------------------------ authenticate（信任）

def test_unknown_pending_and_revoked_issuers_are_distinct_refusals():
    decoded = _inspect(VECTOR["credential"])
    assert _code(lambda: nc.authenticate(decoded, trust=None,
                                         verify_signature=ed25519_verify)) == "node_unknown"
    assert _code(lambda: nc.authenticate(decoded, trust=_trust(state="pending"),
                                         verify_signature=ed25519_verify)) == "node_unknown"
    assert _code(lambda: nc.authenticate(decoded, trust=_trust(state="revoked"),
                                         verify_signature=ed25519_verify)) == "node_revoked"
    assert _code(lambda: nc.authenticate(
        decoded, trust=_trust(node_id="node-" + "8" * 48),
        verify_signature=ed25519_verify)) == "node_unknown"


def test_revoked_issuer_is_refused_even_with_a_perfect_signature():
    """成员状态先于签名：签名成立不能替撤销翻案。"""
    decoded = _inspect(VECTOR["credential"])
    calls = []

    def verifier(*args):
        calls.append(args)
        return True

    assert _code(lambda: nc.authenticate(decoded, trust=_trust(state="revoked"),
                                         verify_signature=verifier)) == "node_revoked"
    assert calls == []


def test_trust_record_whose_key_does_not_derive_the_node_id_is_not_trusted():
    """一行被改坏的成员记录（换了公钥）不能让任意密钥冒充这个节点。"""
    attacker = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    raw = attacker.public_key().public_bytes_raw()
    forged = nc.encode(VECTOR["claims"], attacker.sign(nc.signing_input(VECTOR["claims"])))
    decoded = _inspect(forged)
    trust = _trust(public_key=base64.b64encode(raw).decode())
    assert _code(lambda: nc.authenticate(decoded, trust=trust,
                                         verify_signature=ed25519_verify)) == "node_unknown"


def test_signature_from_another_key_is_invalid():
    attacker = Ed25519PrivateKey.from_private_bytes(b"\x09" * 32)
    decoded = _inspect(_sign(VECTOR["claims"], attacker))
    assert _code(lambda: nc.authenticate(decoded, trust=_trust(),
                                         verify_signature=ed25519_verify)) \
        == "credential_invalid"


# ------------------------------------------------------------- 结构与规范化

def _claims(**over) -> dict:
    claims = copy.deepcopy(VECTOR["claims"])
    claims.update(over)
    return claims


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(extra="x"),
    lambda c: c.pop("jti"),
    lambda c: c.update(alg="EdDSA"),
    lambda c: c.update(schema="ddp-node-credential/2#Claims"),
    lambda c: c["actor"].update(kind="robot"),
    lambda c: c["actor"].update(role="admin"),
    lambda c: c["actor"].update(subject="ユーザー"),
    lambda c: c.update(operation="admin_everything"),
    lambda c: c["request"].update(method="GET"),
    lambda c: c["request"].update(path="/internal/capabilities"),
    lambda c: c["constraints"].pop("step_id"),
    lambda c: c["constraints"].pop("root_task_id"),
    lambda c: c["constraints"].update(tenant="any"),
    lambda c: c.update(issued_at=True),
    lambda c: c.update(expires_at=c["issued_at"]),
    lambda c: c.update(jti="short"),
], ids=["extra", "missing_jti", "alg", "schema", "actor_kind", "actor_role", "non_ascii",
        "operation", "method", "path", "admission_without_step", "no_root",
        "unknown_constraint", "bool_time", "zero_lifetime", "short_jti"])
def test_structure_violations_are_credential_invalid(mutate):
    claims = copy.deepcopy(VECTOR["claims"])
    mutate(claims)
    assert _code(lambda: nc.validate_claims(claims)) == "credential_invalid"


def test_probe_operations_require_the_task_spec_digest_constraint():
    base = _claims(operation="probe_create",
                   request={**VECTOR["claims"]["request"], "path": "/api/v1/federation/probes"},
                   constraints={"root_task_id": "root-1"})
    assert _code(lambda: nc.validate_claims(base)) == "credential_invalid"
    base["constraints"]["task_spec_digest"] = "sha256:" + "a" * 64
    nc.validate_claims(base)


@pytest.mark.parametrize("token", [
    "", "a.b.c", "!!!.xyz", "e30=.AAAA", "a" * (nc.MAX_TOKEN_CHARS + 1),
])
def test_malformed_wire_forms_are_invalid(token):
    assert _code(lambda: nc.decode(token)) == "credential_invalid"


def test_non_canonical_base64_trailing_bits_are_refused():
    payload, signature = VECTOR["credential"].split(".")
    # 修改最后一个字符的低位：解码后字节不变，但不是规范写法。
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = signature[-1]
    twin = alphabet[(alphabet.index(last) + 1) % 64]
    assert base64.urlsafe_b64decode(signature[:-1] + twin + "=" * (-len(signature) % 4)) \
        == base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    assert _code(lambda: nc.decode(payload + "." + signature[:-1] + twin)) \
        == "credential_invalid"


# ----------------------------------------------------------------- 范围约束

def test_constraints_compare_exactly_and_absence_is_not_a_wildcard():
    claims = VECTOR["claims"]
    nc.require_constraints(claims, root_task_id="root-1", step_id="retrieve-1")
    assert _code(lambda: nc.require_constraints(claims, root_task_id="root-2")) \
        == "credential_scope_denied"
    assert _code(lambda: nc.require_constraints(claims, step_id="answer-1")) \
        == "credential_scope_denied"
    # 请求带 scope_ref、凭证没有 -> 拒绝；请求没有、凭证有 -> 也拒绝。
    assert _code(lambda: nc.require_constraints(claims, scope_ref="scope-1")) \
        == "credential_scope_denied"
    scoped = _claims(constraints={**claims["constraints"], "scope_ref": "scope-1"})
    assert _code(lambda: nc.require_constraints(scoped, scope_ref=None)) \
        == "credential_scope_denied"
    nc.require_constraints(claims, scope_ref=None)
    with pytest.raises(ValueError):
        nc.require_constraints(claims, tenant="x")


# ------------------------------------------------------------ 跨中心主体映射

def test_peer_actor_never_merges_same_named_subjects_across_issuers():
    claims = VECTOR["claims"]
    same_subject_other_issuer = _claims(issuer_node_id="node-" + "e" * 48)
    same_subject_other_org = _claims(actor={**claims["actor"], "organization_id": "org-z"})
    ids = {nc.peer_actor_id(claims), nc.peer_actor_id(same_subject_other_issuer),
           nc.peer_actor_id(same_subject_other_org)}
    assert len(ids) == 3
    for value in ids:
        assert value.startswith("peer-") and len(value) == 32
        assert value != claims["actor"]["subject"]


def test_sign_request_refuses_what_could_never_become_a_valid_credential():
    ok = nc.sign_request(audience_node_id=VECTOR["audience_node_id"],
                         actor=VECTOR["claims"]["actor"], operation="execution_read",
                         constraints={"root_task_id": "root-1"}, method="GET",
                         path="/api/v1/federation/tasks/exec-1", body=b"", ttl_seconds=60)
    assert ok["request"]["body_digest"] == nc.body_digest(b"")
    for over in ({"ttl_seconds": 0}, {"ttl_seconds": 121}, {"method": "POST"},
                 {"path": "/api/v1/federation/tasks/<script>"}):
        kwargs = {"audience_node_id": VECTOR["audience_node_id"],
                  "actor": VECTOR["claims"]["actor"], "operation": "execution_read",
                  "constraints": {"root_task_id": "root-1"}, "method": "GET",
                  "path": "/api/v1/federation/tasks/exec-1", "body": b"", "ttl_seconds": 60}
        kwargs.update(over)
        assert _code(lambda kwargs=kwargs: nc.sign_request(**kwargs)) == "credential_invalid"
