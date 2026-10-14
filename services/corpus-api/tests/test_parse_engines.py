"""`GET /api/parse-engines` —— 上传/重解析表单的可选引擎只来自网关注册表。

此前前端写死 mineru + borndigital：有 DeepSeek-OCR-2（vlm-ocr）的部署选不到它，
没有 mineru 的部署默认却选中 mineru，提交即 404 unknown_engine。这里钉住：
- 有什么列什么（按网关给的顺序），引擎名与真实请求的模型分开报；
- 拿不到、网关自己没证据、网关太旧没有 parse 通道 -> unknown + 空清单，绝不猜一张表；
- 过期 / 不认识的健康取值报 unknown，不透传成 ready；
- 形状合 content-v1 契约；没有身份 401。
"""
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema
import pytest
import respx
import yaml

from ddp_corpus.config import settings
from ddp_paths import CONTRACTS
from tests.conftest import SERVICE

GATEWAY_CAP = f"{SERVICE}/v1/capabilities"


@pytest.fixture(autouse=True)
def _default_engine(monkeypatch):
    monkeypatch.setattr(settings, "default_parse_engine", "borndigital")


def channel(kind: str, profile: str, model: str | None = None, *, readiness: str = "ready",
            age: timedelta = timedelta(0)) -> dict:
    observed = datetime.now(timezone.utc) - age
    return {"channel": kind, "model": model or profile, "profile": profile, "default": False,
            "readiness": readiness, "supports": {},
            "observed_at": observed.isoformat(),
            "valid_until": (observed + timedelta(seconds=60)).isoformat()}


def gateway(*channels: dict, status: str = "observed") -> None:
    respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(200, json={
        "capability_status": status, "profiles": [], "model_channels": list(channels)}))


def contract_schema() -> dict:
    spec = yaml.safe_load((CONTRACTS / "openapi" / "content-v1.yaml").read_text(encoding="utf-8"))
    return {"components": spec["components"], "$ref": "#/components/schemas/ParseEngines"}


async def fetch(client) -> dict:
    resp = await client.get("/api/parse-engines")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    jsonschema.validate(body, contract_schema())
    return body


@respx.mock
async def test_lists_exactly_what_the_registry_has(actor_client):
    gateway(channel("chat", "qwen3-4b-instruct"),
            channel("parse", "vlm-ocr", "deepseek-ocr-2"),
            channel("parse", "borndigital"),
            channel("embedding", "bge-m3"))
    body = await fetch(actor_client)
    assert body == {
        "default_engine": "borndigital",
        "capability_status": "observed",
        "engines": [{"engine": "vlm-ocr", "model": "deepseek-ocr-2", "readiness": "ready"},
                    {"engine": "borndigital", "model": "borndigital", "readiness": "ready"}],
    }


@respx.mock
async def test_unhealthy_engines_are_listed_as_unhealthy(actor_client):
    gateway(channel("parse", "vlm-ocr", "deepseek-ocr-2", readiness="unhealthy"),
            channel("parse", "borndigital"))
    engines = (await fetch(actor_client))["engines"]
    assert [(e["engine"], e["readiness"]) for e in engines] == [
        ("vlm-ocr", "unhealthy"), ("borndigital", "ready")]


@respx.mock
async def test_stale_or_unknown_readiness_is_not_reported_ready(actor_client):
    gateway(channel("parse", "vlm-ocr", "deepseek-ocr-2", age=timedelta(minutes=10)),
            channel("parse", "weird", readiness="kinda-ok"),
            channel("parse", "weird", readiness="ready"))
    engines = (await fetch(actor_client))["engines"]
    assert [(e["engine"], e["readiness"]) for e in engines] == [
        ("vlm-ocr", "unknown"), ("weird", "unknown")], "过期与不认识的取值不透传；同名只取第一条"


@pytest.mark.parametrize("mock", ["unreachable", "not_observed", "no_parse_channel", "error"])
@respx.mock
async def test_no_evidence_means_unknown_and_no_guessed_list(actor_client, mock):
    if mock == "unreachable":
        respx.get(GATEWAY_CAP).mock(side_effect=httpx.ConnectError("down"))
    elif mock == "not_observed":
        gateway(channel("parse", "borndigital"), status="unknown")
    elif mock == "no_parse_channel":
        gateway(channel("chat", "qwen3-4b-instruct"))     # 旧网关：还没有 parse 通道
    else:
        respx.get(GATEWAY_CAP).mock(return_value=httpx.Response(503))
    body = await fetch(actor_client)
    assert body["capability_status"] == "unknown"
    assert body["engines"] == []
    assert body["default_engine"] == "borndigital"


async def test_requires_an_actor(client):
    resp = await client.get("/api/parse-engines")
    assert resp.status_code == 401
