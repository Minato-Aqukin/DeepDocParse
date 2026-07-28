"""用量聚合：前端图表的数据形态。"""
import respx

from tests.test_documents import _callback, _mock_service, _upload


@respx.mock
async def test_usage_aggregates_by_day_and_kind(auth_client):
    _mock_service(status="succeeded")
    await _upload(auth_client)
    await _callback(auth_client)

    body = (await auth_client.get("/api/usage")).json()
    assert body["total_pages"] == 2
    kinds = {row["kind"]: row for row in body["by_kind"]}
    assert kinds["parse"]["pages"] == 2
    assert "embed" in kinds, "向量化也要计量（它是真实成本）"
    assert len(body["daily"]) == 1 and body["daily"][0]["pages"] == 2


async def test_usage_is_per_user(auth_client, client):
    from tests.conftest import register
    other = await register(client, username="bob")
    resp = await client.get("/api/usage",
                            headers={"Authorization": f"Bearer {other['access_token']}"})
    assert resp.json()["total_pages"] == 0
