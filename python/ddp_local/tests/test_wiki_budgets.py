"""Wiki work limits exercised through the real loopback completion transport."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ddp_core.application.ports import ApplicationError
from ddp_core.application.wiki import generate_wiki, limits_for
from ddp_local.providers import LocalExecutionProvider, ModelSelection


@pytest.fixture
def model_endpoint():
    calls = []
    fault = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            stage = json.loads(body["messages"][-1]["content"])["stage"]
            calls.append((stage, body["max_tokens"]))
            if stage == "plan":
                value = {"pages": [{"title": title, "references": [1]}
                                   for title in ("Collector", "Validator")]}
            elif stage == "write":
                value = {"pages": [{"page": number, "sections": [{"heading": "Facts",
                    "sentences": [{"text": "Collector forwards batches to Validator.",
                                   "references": [1]}]}]} for number in (1, 2)]}
            else:
                value = {"selected_relations": [1]}
            broken = stage == fault.get("stage")
            response = {"choices": [{"message": {"content": json.dumps(value)},
                "finish_reason": "length" if broken and fault["kind"] == "length" else "stop"}],
                "usage": {"completion_tokens": body["max_tokens"] +
                          (1 if broken and fault["kind"] == "usage" else 0)}}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = LocalExecutionProvider(ModelSelection(
        f"http://127.0.0.1:{server.server_port}/v1", "budget-fixture"))
    try:
        yield provider, calls, fault
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def original_evidence():
    text = "Collector forwards batches to Validator."
    return [{"id": "original-1", "excerpt": text, "evidence": {
        "source_type": "source", "derived_from": None, "source_version_id": "fixed",
        "locator": {"physical_page_index": 0, "bbox": [1, 2, 3, 4]},
        "excerpt_digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest()}}]


async def build(provider):
    return await generate_wiki(provider, "Pipeline", original_evidence(),
        limits_for({"max_output_tokens": 512}), execution_policy="local_only",
        allow_remote=False, record_attempt=lambda *args: lambda **updates: None)


@pytest.mark.parametrize("stage", ["plan", "write", "relations"])
@pytest.mark.parametrize("kind", ["length", "usage"])
async def test_wiki_token_exhaustion_stops_at_failing_model_stage(model_endpoint, stage, kind):
    provider, calls, fault = model_endpoint
    fault.update(stage=stage, kind=kind)
    with pytest.raises(ApplicationError) as rejected:
        await build(provider)
    assert rejected.value.code == "generation_budget_exceeded"
    expected = {"plan": [("plan", 128)],
                "write": [("plan", 128), ("write", 256)],
                "relations": [("plan", 128), ("write", 256), ("relations", 128)]}
    assert calls == expected[stage]


async def test_wiki_all_stages_can_finish_at_total_completion_budget(model_endpoint):
    provider, calls, _ = model_endpoint
    result = await build(provider)
    assert [page["title"] for page in result["pages"]] == ["Collector", "Validator"]
    assert result["relations"][0]["evidence_ids"] == ["original-1"]
    assert calls == [("plan", 128), ("write", 256), ("relations", 128)]
    assert sum(tokens for _, tokens in calls) == 512
