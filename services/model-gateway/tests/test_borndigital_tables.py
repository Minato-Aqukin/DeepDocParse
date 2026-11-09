"""Native-table parsing exercises the public lifecycle and invalid-document boundaries."""

import io

import httpx
import pytest
import respx
from httpx import Response

from ddp_gateway.services import layout
from ddp_gateway.services.engines import BORNDIGITAL_TABLES_RUNTIME


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Keep destination-policy validation real while HTTP stays inside respx."""
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, family=0, type=0, proto=0, flags=0:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])


def _ruled_pdf():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595.2756, 841.8898))
    c.setFont("Helvetica", 12)
    c.drawString(60, 780, "OUTSIDE marker gamma")
    x0, top, cw, rh = 60, 700, 90, 30
    xs = [x0 + i * cw for i in range(3)]
    ys = [top - i * rh for i in range(3)]
    c.setLineWidth(0.8)
    p = c.beginPath()
    for x in xs:
        p.moveTo(x, ys[2])
        p.lineTo(x, ys[0])
    for y in ys:
        p.moveTo(xs[0], y)
        p.lineTo(xs[2], y)
    c.drawPath(p, stroke=1, fill=0)
    c.setFont("Helvetica", 9)
    c.drawString(xs[0] + 4, ys[1] + 10, "G0C0")
    c.drawString(xs[1] + 4, ys[1] + 10, "G0C1")
    c.drawString(xs[0] + 4, ys[2] + 10, "G1C0")
    c.drawString(xs[1] + 4, ys[2] + 10, "G1C1")
    c.showPage()
    c.save()
    return buf.getvalue()


@respx.mock
async def test_tables_engine_runs_the_whole_registry_path(
        client, worker_ctx, app_state, monkeypatch):
    """受理 -> worker 归档 -> 取结果，版面过 validate 且带表格块。"""
    from ddp_gateway.config import ModelEntry
    from ddp_gateway.config import settings as cfg
    from ddp_gateway.worker.tasks import poll_and_archive

    monkeypatch.setattr(cfg, "poll_initial_delay", 0.01)
    app_state.registry.parse_engines["borndigital-tables"] = ModelEntry(
        endpoint="inproc://borndigital-tables",
        runtime=BORNDIGITAL_TABLES_RUNTIME, capabilities=["parse"])
    worker_ctx["registry"] = app_state.registry

    pdf = _ruled_pdf()
    file_url = "https://files.example.com/ruled.pdf"
    respx.get(file_url).mock(return_value=Response(200, content=pdf))

    resp = await client.post("/v1/parse", json={
        "file_url": file_url, "engine": "borndigital-tables"})
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    await poll_and_archive(worker_ctx, task_id)

    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["status"] == "succeeded", status
    result = (await client.get(f"/v1/parse/{task_id}/result")).json()
    assert layout.validate(result["layout_json"]) == []
    assert result["layout_json"]["engine"] == "borndigital-tables"
    blocks = result["layout_json"]["pdf_info"][0]["para_blocks"]
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 1, [b.get("type") for b in blocks]
    assert layout.table_html(tables[0]).startswith("<table>")
    assert "G0C0" in layout.table_html(tables[0])
    flat = " ".join(layout.block_text(b) for b in blocks)
    assert "gamma" in flat
    assert "<table>" in result["markdown"]


@respx.mock
async def test_tables_engine_rejects_non_pdf(
        client, worker_ctx, app_state, monkeypatch):
    from ddp_gateway.config import ModelEntry
    from ddp_gateway.config import settings as cfg
    from ddp_gateway.worker.tasks import poll_and_archive

    monkeypatch.setattr(cfg, "poll_initial_delay", 0.01)
    app_state.registry.parse_engines["borndigital-tables"] = ModelEntry(
        endpoint="inproc://borndigital-tables",
        runtime=BORNDIGITAL_TABLES_RUNTIME, capabilities=["parse"])
    worker_ctx["registry"] = app_state.registry

    file_url = "https://files.example.com/not-a-pdf.bin"
    respx.get(file_url).mock(return_value=Response(200, content=b"not a pdf"))
    task_id = (await client.post("/v1/parse", json={
        "file_url": file_url, "engine": "borndigital-tables"})).json()["task_id"]
    await poll_and_archive(worker_ctx, task_id)
    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["status"] == "failed"


@respx.mock
async def test_tables_engine_fails_loudly_on_a_scanned_pdf(
        client, worker_ctx, app_state, monkeypatch):
    import pypdfium2 as pdfium

    from ddp_gateway.config import ModelEntry
    from ddp_gateway.config import settings as cfg
    from ddp_gateway.worker.tasks import poll_and_archive

    monkeypatch.setattr(cfg, "poll_initial_delay", 0.01)
    app_state.registry.parse_engines["borndigital-tables"] = ModelEntry(
        endpoint="inproc://borndigital-tables",
        runtime=BORNDIGITAL_TABLES_RUNTIME, capabilities=["parse"])
    worker_ctx["registry"] = app_state.registry

    blank = pdfium.PdfDocument.new()
    blank.new_page(612, 792)
    buf = io.BytesIO()
    blank.save(buf)
    blank.close()
    file_url = "https://files.example.com/scan.pdf"
    respx.get(file_url).mock(return_value=Response(200, content=buf.getvalue()))
    task_id = (await client.post("/v1/parse", json={
        "file_url": file_url, "engine": "borndigital-tables"})).json()["task_id"]
    await poll_and_archive(worker_ctx, task_id)
    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["status"] == "failed"
