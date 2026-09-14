"""The real e2e runner must not certify a missing or unauthenticated environment."""
import importlib.util
import json
from pathlib import Path
import sys

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "e2e_mcp.py"
spec = importlib.util.spec_from_file_location("ddp_e2e_mcp", SCRIPT)
e2e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e2e
spec.loader.exec_module(e2e)


def fixture_env():
    return {"MCP_API_KEY": "sk-owner-test-only", "MCP_OTHER_API_KEY": "sk-other-test-only",
            "MCP_E2E_QUERY": "annual result", "MCP_E2E_EXPECTED_TEXT": "private-marker-42",
            "MCP_E2E_EVIDENCE_ID": "ev-a", "MCP_E2E_WIKI": "entry-a", "MCP_E2E_ENTITY": "entity-a"}


def test_missing_user_credentials_is_blocked_even_with_service_token(tmp_path):
    report = tmp_path / "report.json"
    assert e2e.main(["--report", str(report)], env={"SERVICE_TOKEN": "secret-service-token"}) == 2
    data = json.loads(report.read_text())
    assert data["status"] == "blocked" and data["checks"] == []
    assert "MCP_API_KEY" in data["prerequisites"][0]
    assert "secret-service-token" not in report.read_text()


def test_default_transport_is_authenticated_control_entry():
    config, errors = e2e.configuration(fixture_env())
    assert errors == [] and config.mcp_url == "http://127.0.0.1:8080/mcp/"
    for value in ("http://127.0.0.1:9100", "http://control.test/mcp", "https://user:password@control.test"):
        assert e2e.configuration({**fixture_env(), "CONTROL_BASE_URL": value})[0] is None


def test_privacy_probe_requires_distinct_keys_and_a_marker_outside_the_query():
    env = fixture_env()
    assert e2e.configuration({**env, "MCP_OTHER_API_KEY": env["MCP_API_KEY"]})[0] is None
    assert e2e.configuration({**env, "MCP_E2E_QUERY": env["MCP_E2E_EXPECTED_TEXT"]})[0] is None
