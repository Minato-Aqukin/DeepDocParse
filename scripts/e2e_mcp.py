"""v3 real MCP acceptance through control-api using two different users' API keys.

Requires an existing PRIVATE, indexed resource with evidence, a dependent Wiki entry
and graph entity owned by MCP_API_KEY's user. MCP_OTHER_API_KEY must belong to a
user with no access to that resource. No uploads or publication are performed.

Required environment:
  MCP_API_KEY, MCP_OTHER_API_KEY, MCP_E2E_QUERY, MCP_E2E_EXPECTED_TEXT,
  MCP_E2E_EVIDENCE_ID, MCP_E2E_WIKI, MCP_E2E_ENTITY
Optional: CONTROL_BASE_URL (http://127.0.0.1:8080), MCP_E2E_REQUIRE_CROP=1.

Example: .venv/bin/python scripts/e2e_mcp.py --report /tmp/mcp-v3-report.json
Exit 0 = all checks passed; 1 = observed failure; 2 = prerequisites unavailable.
The report contains check outcomes and runtime versions, never keys or source text.
"""
import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

TOOLS = ("search", "ask", "get_evidence", "read_wiki", "graph_neighbors")
REQUIRED = ("MCP_API_KEY", "MCP_OTHER_API_KEY", "MCP_E2E_QUERY", "MCP_E2E_EXPECTED_TEXT",
            "MCP_E2E_EVIDENCE_ID", "MCP_E2E_WIKI", "MCP_E2E_ENTITY")


@dataclass(frozen=True)
class Config:
    base_url: str
    owner_key: str
    other_key: str
    query: str
    expected: str
    evidence_id: str
    wiki: str
    entity: str
    require_crop: bool = False

    @property
    def mcp_url(self) -> str:
        return self.base_url + "/mcp/"


def configuration(env: dict) -> tuple[Config | None, list[str]]:
    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        return None, ["missing " + ", ".join(missing)]
    base = env.get("CONTROL_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        return None, ["CONTROL_BASE_URL is malformed"]
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        return None, ["CONTROL_BASE_URL must be an HTTP(S) control-api URL without credentials"]
    if parsed.query or parsed.fragment or port == 9100 or parsed.path.rstrip("/").endswith("/mcp"):
        return None, ["CONTROL_BASE_URL must identify control-api, not the internal MCP listener or /mcp path"]
    if not all(env[name].startswith("sk-") for name in ("MCP_API_KEY", "MCP_OTHER_API_KEY")):
        return None, ["MCP_API_KEY and MCP_OTHER_API_KEY must be user API keys (sk-), never SERVICE_TOKEN"]
    if env["MCP_API_KEY"] == env["MCP_OTHER_API_KEY"]:
        return None, ["two different users' API keys are required for privacy acceptance"]
    if env["MCP_E2E_EXPECTED_TEXT"] in env["MCP_E2E_QUERY"]:
        return None, ["MCP_E2E_QUERY must not contain the private answer marker"]
    return Config(base, env["MCP_API_KEY"], env["MCP_OTHER_API_KEY"], env["MCP_E2E_QUERY"],
                  env["MCP_E2E_EXPECTED_TEXT"], env["MCP_E2E_EVIDENCE_ID"],
                  env["MCP_E2E_WIKI"], env["MCP_E2E_ENTITY"], env.get("MCP_E2E_REQUIRE_CROP") == "1"), []


def structured(result) -> dict:
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        return payload
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) == "text":
            try:
                data = json.loads(item.text)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def evidence_ids(value) -> set[str]:
    if isinstance(value, list):
        return set().union(*(evidence_ids(item) for item in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    ids = {value["evidence_id"]} if isinstance(value.get("evidence_id"), str) else set()
    ids.update(item for item in value.get("evidence_ids", []) if isinstance(item, str))
    for item in value.values():
        ids.update(evidence_ids(item))
    return ids


def client_for(config: Config, key: str):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def factory(**kwargs):
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    return Client(StreamableHttpTransport(config.mcp_url,
        headers={"Authorization": "Bearer " + key}, httpx_client_factory=factory))


async def run(config: Config, report: dict) -> int:
    def check(name, condition):
        result = bool(condition)
        report["checks"].append({"name": name, "passed": result})
        print(("PASS " if result else "FAIL ") + name)

    calls = {"search": {"query": config.query, "limit": 10},
             "ask": {"question": config.query}, "get_evidence": {"evidence_id": config.evidence_id},
             "read_wiki": {"entry_id_or_title": config.wiki},
             "graph_neighbors": {"entity_id_or_name": config.entity, "depth": 1}}
    # Verify the real user-facing entry before connecting MCP. No trusted identity
    # headers or service credentials are constructed anywhere in this script.
    async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
        anonymous = await http.post(config.mcp_url, json={})
        check("control entry rejects anonymous requests", anonymous.status_code in (401, 403))

    async with client_for(config, config.owner_key) as client:
        names = {tool.name for tool in await client.list_tools()}
        check("five corpus tools and deprecated compatibility tool are registered", set(TOOLS) | {"ask_document"} <= names)
        for tool, arguments in calls.items():
            result = await client.call_tool(tool, arguments, raise_on_error=False)
            data = structured(result)
            check(tool + " owner call succeeds", not result.is_error and bool(data))
            check(tool + " reaches the expected evidence", config.evidence_id in evidence_ids(data))
            if tool == "search":
                check("search reports fixed parse scope", data.get("scope", {}).get("authorized_parse_revisions", 0) > 0)
                matches = [item for item in data.get("results", []) if item.get("evidence_id") == config.evidence_id]
                check("search returns resource, version and parse identities", matches and all(
                    item.get("resource_id") and item.get("source_version_id") and item.get("parse_revision") for item in matches))
            if tool in ("search", "ask", "get_evidence"):
                check(tool + " returns the expected private fact", config.expected in json.dumps(data, ensure_ascii=False))
            if tool == "ask":
                check("answer includes a supported assertion", any(not item.get("unsupported", True)
                    and config.evidence_id in item.get("evidence_ids", []) for item in data.get("assertions", [])))
            if tool == "get_evidence":
                check("evidence resolves to a source", data.get("resolved") is True)
                check("crop status is explicit", "crop_degraded" in data)
                if config.require_crop:
                    check("evidence includes native image pixels", any(item.type == "image" for item in result.content))

    async with client_for(config, config.other_key) as client:
        for tool, arguments in calls.items():
            result = await client.call_tool(tool, arguments, raise_on_error=False)
            data = structured(result)
            serialized = json.dumps(data, ensure_ascii=False) + "\n".join(
                item.text for item in result.content if getattr(item, "type", None) == "text")
            check(tool + " excludes private evidence and answer", bool(data) and config.evidence_id not in evidence_ids(data)
                  and config.expected not in serialized)
            if tool == "get_evidence":
                check("get_evidence withholds private pixels", not any(item.type == "image" for item in result.content))
            if tool in ("get_evidence", "read_wiki", "graph_neighbors"):
                check(tool + " conceals private existence", data.get("status") == "not_found")
            else:
                check(tool + " is an authorized non-error response", not result.is_error)
    return 0 if all(item["passed"] for item in report["checks"]) else 1


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write a redacted JSON acceptance report")
    parser.add_argument("--timeout", type=float, default=300, help="total timeout in seconds")
    args = parser.parse_args(argv)
    report = {"suite": "mcp-v3-five-tools-acl", "started_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "checks": []}
    config, reasons = configuration(os.environ if env is None else env)
    try:
        report["fastmcp"] = importlib.metadata.version("fastmcp")
    except importlib.metadata.PackageNotFoundError:
        reasons.append("missing fastmcp; install the services/mcp development environment")
    if reasons:
        report.update(status="blocked", prerequisites=reasons)
        print("BLOCKED: " + "; ".join(reasons))
        code = 2
    else:
        report["entry"] = config.mcp_url
        try:
            async def execute():
                return await asyncio.wait_for(run(config, report), timeout=args.timeout)
            code = asyncio.run(execute())
            report["status"] = "passed" if code == 0 else "failed"
        except Exception as exc:
            # Do not print exception bodies: tool payloads can contain private source text.
            report.update(status="failed", error_type=type(exc).__name__)
            print("FAIL: real MCP entry/tool execution failed (" + type(exc).__name__ + ")")
            code = 1
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
