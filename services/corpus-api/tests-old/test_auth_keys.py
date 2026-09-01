"""用户与 API key 的契约：注册/登录/JWT、key 签发与吊销、错误体形态。"""
import pytest

from ddp_corpus.models import ApiKey
from ddp_corpus.security import hash_api_key
from tests.conftest import register


async def test_register_login_me(client):
    created = await register(client)
    assert created["access_token"] and created["username"] == "alice"

    # 重名 -> 409，且是 OpenAI 风格错误体
    dup = await client.post("/api/auth/register",
                            json={"username": "alice", "password": "another password"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "user_exists"

    login = await client.post("/api/auth/login",
                              json={"username": "alice", "password": "correct horse battery"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["username"] == "alice"


@pytest.mark.parametrize("payload,expected_code", [
    ({"username": "alice", "password": "wrong password"}, "invalid_credentials"),
    ({"username": "nobody", "password": "correct horse battery"}, "invalid_credentials"),
])
async def test_login_failures_are_indistinguishable(client, payload, expected_code):
    """密码错与用户不存在必须同码同文案，否则可枚举用户名。"""
    await register(client)
    resp = await client.post("/api/auth/login", json=payload)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == expected_code


async def test_me_requires_valid_token(client):
    assert (await client.get("/api/auth/me")).status_code == 401
    bad = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_token"


async def test_create_key_returns_plaintext_once(auth_client, session):
    resp = await auth_client.post("/api/keys", json={"name": "ci", "quota_pages": 50})
    assert resp.status_code == 201
    body = resp.json()
    plain = body["key"]
    assert plain.startswith("sk-") and body["key_prefix"] == plain[:11]
    assert body["quota_pages"] == 50 and body["used_pages"] == 0

    # 库里只有哈希，明文不落库
    key = await session.get(ApiKey, body["id"])
    assert key.key_hash == hash_api_key(plain)
    assert plain not in (key.key_prefix + (key.name or ""))

    listed = await auth_client.get("/api/keys")
    assert listed.status_code == 200
    assert [k["id"] for k in listed.json()] == [body["id"]]
    assert "key" not in listed.json()[0], "列表接口不得回吐明文"


async def test_revoke_key_and_isolation(auth_client, client):
    key_id = (await auth_client.post("/api/keys", json={})).json()["id"]

    assert (await auth_client.delete(f"/api/keys/{key_id}")).status_code == 204
    assert (await auth_client.get("/api/keys")).json()[0]["revoked_at"] is not None

    # 换个用户：看不到也删不掉别人的 key（统一 404，不泄露存在性）
    other = await register(client, username="bob")
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert (await client.get("/api/keys", headers=headers)).json() == []
    resp = await client.delete(f"/api/keys/{key_id}", headers=headers)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "key_not_found"
