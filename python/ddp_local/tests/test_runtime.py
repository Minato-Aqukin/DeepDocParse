import asyncio
import io
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import BundleError, read_bundle
from ddp_local.http import create_app
from ddp_local.providers import LocalExecutionProvider, ModelSelection
from ddp_local.runtime import LocalRuntime

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


@pytest.fixture
def runtime(tmp_path):
    value = LocalRuntime(tmp_path / "workspace")
    yield value
    value.close()


async def parsed(runtime, filename="sample.pdf"):
    task = runtime.upload_file(str(FIXTURES / filename), operation_key="source:" + filename)
    finished = await runtime.work_once()
    assert finished["id"] == task["id"] and finished["status"] == "succeeded", finished
    return task


@pytest.mark.asyncio
async def test_real_cpu_parse_search_evidence_bundle_restart(runtime, tmp_path):
    task = await parsed(runtime)
    hits = runtime.search("contract absent_keyword")["hits"]
    assert hits and "Answer: 42" in hits[0]["text"]  # OR keyword semantics
    evidence = runtime.store.evidence(hits[0]["evidence_id"])
    assert (
        evidence["evidence"]["locator"]["bbox"]
        and evidence["evidence"]["locator"]["physical_page_index"] == 0
    )
    data = runtime.export_bundle(task["version_id"])
    bundle = read_bundle(io.BytesIO(data))
    assert bundle.source["origin_node_id"] == runtime.store.environment_id
    assert bundle.evidence[0]["excerpt"] == evidence["excerpt"]
    replica = LocalRuntime(tmp_path / "replica")
    try:
        imported = replica.import_bundle(io.BytesIO(data), operation_key="import1")
        assert imported["status"] == "succeeded"
        assert imported["version_id"] != task["version_id"]
        hit = replica.search("contract")["hits"][0]
        assert replica.store.evidence(hit["evidence_id"])["evidence"] == evidence["evidence"]
        second = read_bundle(io.BytesIO(replica.export_bundle(imported["version_id"])))
        assert second.source == bundle.source and second.evidence == bundle.evidence
        assert (
            replica.import_bundle(io.BytesIO(data), operation_key="import1")["id"] == imported["id"]
        )
    finally:
        replica.close()
    reopened = LocalRuntime(
        runtime.store.db.execute("PRAGMA database_list").fetchone()[2].rsplit("/", 1)[0]
    )
    try:
        assert reopened.search("contract")["hits"][0]["evidence_id"] == evidence["id"]
        names = {
            r[0]
            for r in reopened.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not names.intersection({"users", "api_keys", "organizations"})
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_real_cpu_code_identifier_uses_common_compiler(runtime):
    await parsed(runtime, "code-corpus.pdf")
    hits = runtime.search("HttpRequestParser")["hits"]
    assert hits and any("HttpRequestParser" in h["text"] for h in hits)
    assert any(h["block_type"] == "code" for h in hits)
    assert runtime.search("HttpRequestParser")["degraded"] == ["embedding_unavailable"]


@pytest.mark.asyncio
async def test_real_chinese_pdf_keyword(runtime):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    stream = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    doc = canvas.Canvas(stream)
    doc.setFont("STSong-Light", 14)
    doc.drawString(50, 700, "中文知识检索：系统保留原始证据。")
    doc.save()
    stream.seek(0)
    runtime.upload_stream(stream, filename="chinese.pdf", operation_key="chinese")
    task = await runtime.work_once()
    assert task["status"] == "succeeded", task
    hits = runtime.search("原始证据 missingtoken")["hits"]
    assert hits and "原始证据" in hits[0]["text"]


def test_snapshots_idempotency_and_changed_source(runtime, tmp_path, monkeypatch):
    filename = tmp_path / "input.pdf"
    filename.write_bytes((FIXTURES / "sample.pdf").read_bytes())
    first = runtime.upload_file(str(filename), operation_key="stable")
    assert runtime.upload_file(str(filename), operation_key="stable")["id"] == first["id"]
    filename.write_bytes(b"changed")
    with pytest.raises(ApplicationError, match="different input"):
        runtime.upload_file(str(filename), operation_key="stable")
    original = runtime.blobs.put_stream

    def changing(stream, **kwargs):
        result = original(stream, **kwargs)
        filename.write_bytes(b"changed again")
        return result

    monkeypatch.setattr(runtime.blobs, "put_stream", changing)
    with pytest.raises(ApplicationError, match="changed during snapshot"):
        runtime.upload_file(str(filename), operation_key="new")
    assert len(runtime.store.versions()) == 1


def test_symlink_and_arbitrary_blob_key_denied(runtime, tmp_path):
    link = tmp_path / "link.pdf"
    link.symlink_to(FIXTURES / "sample.pdf")
    with pytest.raises(OSError):
        runtime.upload_file(str(link), operation_key="link")
    with pytest.raises(ApplicationError):
        runtime.blobs.path("../../etc/passwd")
    with pytest.raises(ApplicationError):
        runtime.upload_stream(io.BytesIO(b"bad"), filename="../source.pdf", operation_key="bad")


@pytest.mark.asyncio
async def test_invalid_pdf_has_visible_failed_task(runtime):
    task = runtime.upload_stream(
        io.BytesIO(b"not a pdf"), filename="broken.pdf", operation_key="bad"
    )
    finished = await runtime.work_once()
    assert finished["status"] == "failed" and finished["error"] == "invalid_pdf"
    assert runtime.store.version(task["version_id"])["state"] == "failed"
    assert not runtime.search("broken")["hits"]


@pytest.mark.asyncio
async def test_cancel_fences_late_completion(runtime):
    task = runtime.upload_file(str(FIXTURES / "sample.pdf"), operation_key="cancel")
    claim = runtime.store.claim()
    runtime.store.cancel(task["id"])
    assert not runtime.store.renew(claim["id"], claim["generation"])
    assert await runtime._execute(claim) is False
    assert runtime.store.task(task["id"])["status"] == "cancelled"
    assert not runtime.search("contract")["hits"]


@pytest.mark.asyncio
async def test_expired_lease_restart_and_generation_fence(runtime):
    task = runtime.upload_file(str(FIXTURES / "sample.pdf"), operation_key="crash")
    old = runtime.store.claim()
    with runtime.store.tx():
        runtime.store.db.execute(
            "UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, task["id"])
        )
    fresh = runtime.store.claim()
    assert fresh["generation"] == old["generation"] + 1
    assert await runtime._execute(old) is False
    assert await runtime._execute(fresh) is True
    assert runtime.store.task(task["id"])["attempts"] == 2


@pytest.mark.asyncio
async def test_missing_model_and_remote_denied_before_network(runtime, respx_mock):
    await parsed(runtime)
    with pytest.raises(ApplicationError) as exc:
        await runtime.answer("contract")
    assert exc.value.code == "model_unavailable"
    selected = LocalExecutionProvider(
        ModelSelection("https://model.example/v1", "explicit-model", "remote")
    )
    for policy, consent in [("local_only", False), ("local_only", True), ("remote_allowed", False)]:
        with pytest.raises(ApplicationError) as exc:
            await selected.generate([], execution_policy=policy, allow_remote=consent)
        assert exc.value.code == "remote_execution_denied"
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_provider_protocol_failure_and_oom_are_explicit(respx_mock):
    selected = LocalExecutionProvider(ModelSelection("http://127.0.0.1:18761/v1", "local-instruct"))
    route = respx_mock.post("http://127.0.0.1:18761/v1/chat/completions")
    route.mock(return_value=httpx.Response(503, json={"error": "out of memory"}))
    with pytest.raises(ApplicationError) as exc:
        await selected.generate([], execution_policy="local_only", allow_remote=False)
    assert exc.value.code == "out_of_memory"
    route.mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "A statement [1]"}}]}
        )
    )
    text, provider = await selected.generate([], execution_policy="local_only", allow_remote=False)
    assert text == "A statement [1]" and provider["location"] == "local"


@pytest.mark.asyncio
async def test_generated_wiki_is_citation_bound_and_marked_unreviewed(runtime, respx_mock):
    await parsed(runtime)
    runtime.provider.model = ModelSelection("http://127.0.0.1:18761/v1", "fixture-protocol-only")
    route = respx_mock.post("http://127.0.0.1:18761/v1/chat/completions")
    route.mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "The answer is 42 [1]"}}]}
        )
    )
    result = await runtime.answer("contract", wiki=True)
    assert result["source_type"] == "generated" and result["semantic_review"] == "needs_review"
    assert result["assertions"][0]["evidence_ids"] and result["pages"]
    route.mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "Unsupported statement"}}]}
        )
    )
    with pytest.raises(ApplicationError) as exc:
        await runtime.answer("contract")
    assert exc.value.code == "unsupported_generation"


@pytest.mark.asyncio
async def test_http_boundary_and_full_upload_path(runtime):
    token = "a" * 48
    app = create_app(
        runtime, session_token=token, allowed_hosts={"127.0.0.1:18763"}, start_worker=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18763"
    ) as client:
        assert (await client.get("/api/v1/capabilities")).status_code == 401
        client.headers["Authorization"] = "Bearer " + token
        assert (
            await client.get("/api/v1/capabilities", headers={"Host": "evil.example"})
        ).status_code == 403
        assert (
            await client.get("/api/v1/capabilities", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (
            await client.get("/api/v1/capabilities", headers={"Origin": "null"})
        ).status_code == 403
        uploaded = await client.post(
            "/api/v1/resources/upload",
            content=(FIXTURES / "sample.pdf").read_bytes(),
            headers={"X-Filename": "sample.pdf", "Idempotency-Key": "http"},
        )
        assert uploaded.status_code == 202, uploaded.text
        source_url = "/api/v1/versions/" + uploaded.json()["version_id"] + "/source"
        assert (await client.get(source_url)).status_code == 409
        assert (await client.get("/api/v1/versions/not-in-this-workspace/source")).status_code == 404
        await runtime.work_once()
        source = await client.get(source_url, params={"path": "/etc/passwd"})
        assert source.status_code == 200 and source.headers["content-type"] == "application/pdf"
        assert source.content == (FIXTURES / "sample.pdf").read_bytes()
        assert (await client.get(source_url, headers={"Authorization": ""})).status_code == 401
        search = await client.post("/api/v1/search", json={"query": "contract"})
        assert search.status_code == 200 and search.json()["hits"]
        hit = search.json()["hits"][0]
        assert (await client.get("/api/v1/evidence/" + hit["evidence_id"])).status_code == 200
        assert (
            await client.get("/api/v1/versions/" + uploaded.json()["version_id"] + "/bundle")
        ).status_code == 200
        assert (await client.post("/api/v1/answer", json={"query": "contract"})).status_code == 503
        assert (
            await client.post("/api/v1/search", json={"query": "contract", "path": "/etc/passwd"})
        ).status_code == 422


async def test_http_chinese_filename_percent_encoding_is_strict(runtime):
    from urllib.parse import quote

    app = create_app(runtime, session_token="a" * 48, allowed_hosts={"127.0.0.1:18763"}, start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18763",
        headers={"Authorization": "Bearer " + "a" * 48},
    ) as client:
        name = "中文证据 手册.pdf"
        valid = await client.post("/api/v1/resources/upload", content=(FIXTURES / "sample.pdf").read_bytes(),
                                  headers={"X-Filename-Encoded": quote(name, safe=""), "Idempotency-Key": "encoded"})
        assert valid.status_code == 202, valid.text
        assert runtime.store.version(valid.json()["version_id"])["filename"] == name
        for header in ("bad%GG.pdf", "bad%.pdf", "%FF.pdf", "%2Fetc%2Fpasswd", "file%00.pdf"):
            bad = await client.post("/api/v1/resources/upload", content=b"%PDF-",
                                     headers={"X-Filename-Encoded": header, "Idempotency-Key": "bad"})
            assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_filename"
        for fields in ([('X-Filename', 'one.pdf'), ('X-Filename', 'two.pdf')],
                       [('X-Filename-Encoded', 'one.pdf'), ('X-Filename-Encoded', 'two.pdf')],
                       [('X-Filename', 'one.pdf'), ('X-Filename-Encoded', 'two.pdf')]):
            bad = await client.post("/api/v1/resources/upload", content=b"%PDF-",
                                     headers=[*fields, ("Idempotency-Key", "ambiguous")])
            assert bad.status_code == 400
        assert len(runtime.store.versions()) == 1


@pytest.mark.parametrize(
    "member", ["../outside", "/etc/passwd", "safe/../../bad", "source.bin/extra"]
)
def test_malicious_bundle_never_publishes(runtime, member):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(member, b"x")
    raw.seek(0)
    with pytest.raises(BundleError):
        runtime.import_bundle(raw, operation_key="malicious")
    assert not runtime.store.versions() and not runtime.store.tasks()


@pytest.mark.asyncio
async def test_generation_task_durable_idempotency_and_cancel(runtime, respx_mock):
    await parsed(runtime)
    runtime.provider.model = ModelSelection("http://127.0.0.1:18761/v1", "fixture-protocol-only")
    route = respx_mock.post("http://127.0.0.1:18761/v1/chat/completions")
    route.mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer 42 [1]"}}]}
        )
    )
    first = await runtime.answer("contract", operation_key="answer1")
    second = await runtime.answer("contract", operation_key="answer1")
    assert first == second and route.call_count == 1
    assert runtime.store.task(first["task_id"])["status"] == "succeeded"
    with pytest.raises(ApplicationError) as exc:
        await runtime.answer("different", operation_key="answer1")
    assert exc.value.code == "idempotency_conflict"
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_generate(*args, **kwargs):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    runtime.provider.generate = pending_generate
    pending = asyncio.create_task(runtime.answer("contract", operation_key="cancel-generation"))
    await entered.wait()
    job = next(t for t in runtime.store.tasks() if t["operation_key"] == "cancel-generation")
    runtime.store.cancel(job["id"])
    with pytest.raises(ApplicationError) as exc:
        await asyncio.wait_for(pending, 3)
    assert exc.value.code == "execution_cancelled" and cancelled.is_set()
    assert runtime.store.task(job["id"])["status"] == "cancelled"


def test_interrupted_generation_is_not_silently_reexecuted(runtime):
    task, created = runtime.store.begin_generation("answer", {"scope": []}, "generation-crash")
    assert created
    with runtime.store.tx():
        runtime.store.db.execute(
            "UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, task["id"])
        )
    assert runtime.store.claim() is None
    assert runtime.store.task(task["id"])["error"] == "execution_interrupted"


@pytest.mark.asyncio
async def test_remote_generation_requires_explicit_policy_and_discloses_payload(
    runtime, respx_mock
):
    await parsed(runtime)
    runtime.provider.model = ModelSelection(
        "https://selected.example/v1", "explicit-instruct", "remote"
    )
    route = respx_mock.post("https://selected.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "Answer 42 [1]"}}]}
        )
    )
    result = await runtime.answer("contract", execution_policy="remote_allowed", allow_remote=True)
    assert result["disclosure"] == {"remote": True, "payload": ["question", "selected_evidence"]}
    events = runtime.store.events()
    selected = next(e for e in events if e["kind"] == "execution.selected")
    assert (
        selected["payload"]["allow_remote"]
        and selected["payload"]["endpoint"] == "https://selected.example/v1"
    )
    assert route.call_count == 1


def test_import_has_no_server_database_or_network_side_effects():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    source = """import socket, sys
def deny(*a, **k):
    raise AssertionError('network during import')
socket.socket.connect = deny
import ddp_local, ddp_local.runtime, ddp_local.cli, ddp_local.http
for module in sys.modules:
    assert not module.startswith(('ddp_corpus','ddp_gateway','sqlalchemy','redis','torch'))
"""
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(root / "python/ddp_local"), str(root / "python/ddp_core")]
        ),
    }
    result = subprocess.run(
        [sys.executable, "-c", source], env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_explicit_unready_scope_does_not_claim_complete_snapshot(runtime):
    task = runtime.upload_file(str(FIXTURES / "sample.pdf"), operation_key="unready")
    with pytest.raises(ApplicationError) as exc:
        runtime.search("contract", version_ids=[task["version_id"]])
    assert exc.value.code == "version_not_ready"


@pytest.mark.asyncio
async def test_json_request_budget_applies_to_chunked_body(runtime):
    token = "b" * 48
    app = create_app(
        runtime, session_token=token, allowed_hosts={"127.0.0.1:18763"}, start_worker=False
    )

    async def chunks():
        yield b'{"query":"'
        for _ in range(9):
            yield b"a" * 8192
        yield b'"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:18763",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    ) as client:
        response = await client.post("/api/v1/search", content=chunks())
        assert response.status_code == 400
        assert not runtime.store.tasks()


@pytest.mark.asyncio
async def test_source_only_bundle_keeps_missing_revision_and_can_roundtrip(runtime, tmp_path):
    from ddp_core.bundle import build_bundle, json_bytes

    task = await parsed(runtime)
    full = read_bundle(io.BytesIO(runtime.export_bundle(task["version_id"])))
    source = {**full.source, "parse_revision": None}
    files = {
        **full.files,
        "evidence.json": json_bytes([]),
        "layout.json": json_bytes(
            {
                "schema": "ddp-bundle-layout/1",
                "state": "missing",
                "layout": None,
                "reason": "parse_not_available",
            }
        ),
    }
    archive = build_bundle(source, files)
    other = LocalRuntime(tmp_path / "source-only")
    try:
        imported = other.import_bundle(io.BytesIO(archive), operation_key="source-only")
        version = other.store.version(imported["version_id"])
        assert version["state"] == "unparsed" and version["parse_revision"] is None
        assert not other.search("contract")["hits"]
        restored = read_bundle(io.BytesIO(other.export_bundle(imported["version_id"])))
        assert restored.source == source
    finally:
        other.close()
