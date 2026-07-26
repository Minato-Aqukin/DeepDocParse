"""用量聚合：前端图表的数据形态。"""
import httpx
import respx
from sqlalchemy import select

from app.config import settings
from app.models import User
from tests.test_tasks import PDF, _mock_service, _upload


@respx.mock
async def test_usage_aggregates_by_day_and_kind(auth_client, session):
    _mock_service(status="succeeded")
    await _upload(auth_client)
    await auth_client.post("/internal/parse-callback",
                           json={"task_id": "s-1", "status": "succeeded"},
                           headers={"Authorization": f"Bearer {settings.service_token}"})

    resp = await auth_client.get("/api/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_pages"] == 2 and body["total_requests"] == 1
    assert body["by_kind"] == [{"kind": "parse", "pages": 2, "requests": 1}]
    assert len(body["daily"]) == 1 and body["daily"][0]["pages"] == 2


async def test_usage_is_per_user(auth_client, client):
    from tests.conftest import register
    other = await register(client, username="bob")
    resp = await client.get("/api/usage",
                            headers={"Authorization": f"Bearer {other['access_token']}"})
    assert resp.json()["total_pages"] == 0
