"""PDF page labels are display aliases, never physical locator replacements."""
import io
import json

import pytest
import respx
from sqlalchemy import select
from jsonschema import Draft202012Validator

from ddp_core.application.borndigital import extract_pages, to_markdown
from ddp_core.application.layout import build
from ddp_corpus.config import settings
from ddp_core.bundle import read_bundle
from ddp_corpus.models import Evidence, ResourceVersion
from ddp_corpus import federation
from ddp_corpus.deps import Actor
from ddp_corpus.evidence import load_citations, record_evidence
from ddp_corpus.models import utcnow
from tests.conftest import ACTOR, ORG
from ddp_paths import FIXTURES
from tests.test_documents import _callback, _mock_service, _upload


@pytest.mark.parametrize("fixture,expected", [
    ("page-labels.pdf", ["i", "ii", "iii", "iv", "1", "2"]),
    ("long-doc.pdf", [None] * 5),
])
@respx.mock
async def test_fixed_evidence_and_bundle_keep_printed_labels_and_physical_pages(
        actor_client, session, monkeypatch, fixture, expected):
    monkeypatch.setattr(settings, "bundle_node_id", "node-" + "a" * 48)
    pdf = (FIXTURES / fixture).read_bytes()
    pages = extract_pages(pdf)
    _mock_service(result={"markdown": to_markdown(pages),
                          "layout_json": build(pages, engine="borndigital"), "images": []})
    document = await _upload(actor_client, pdf)
    assert (await _callback(actor_client)).status_code == 200
    status = (await actor_client.get(f"/api/documents/{document['id']}")).json()
    assert status["index_status"] == "ready", status.get("index_error")
    evidence = list((await session.execute(select(Evidence).order_by(Evidence.seq))).scalars())
    for row in evidence:
        response = await actor_client.get(f"/api/evidence/{row.id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail.get("printed_page_label") == expected[row.page_idx]
        assert detail["page_idx"] == row.page_idx
        assert detail["parse_job_id"] == row.parse_job_id
        assert detail["bbox"] == row.bbox
        envelope = await federation.resolve_evidence(
            session, Actor(ACTOR, "user", ORG, "contributor"),
            evidence_ref=row.id, now=utcnow())
        assert envelope["locator"].get("printed_page_label") == expected[row.page_idx]
        assert envelope["locator"]["physical_page_index"] == row.page_idx
        assert envelope["source_version_id"] == detail["source_version_id"]
        assert await record_evidence(session, [{"parse_job_id": row.parse_job_id, "seq": row.seq,
            "evidence_id": row.id, "snippet": row.content}], source_kind="message",
            source_id="labelled-answer") == 1
    await session.commit()
    citations = await load_citations(session, source_kind="message", source_ids=["labelled-answer"])
    assert {item["page_idx"]: item.get("printed_page_label") for item in citations["labelled-answer"]} == dict(enumerate(expected))
    version = await session.scalar(select(ResourceVersion).where(
        ResourceVersion.document_id == document["id"]))
    exported = await actor_client.get(
        f"/api/resources/{version.resource_id}/versions/{version.id}/bundle")
    assert exported.status_code == 200, exported.text
    bundle = read_bundle(io.BytesIO(exported.content))
    for record in bundle.evidence:
        loc = record["evidence"]["locator"]
        assert loc.get("printed_page_label") == expected[loc["physical_page_index"]]
    fixed = json.loads(bundle.files["layout.json"])["layout"]
    assert [p.get("printed_page_label") for p in fixed["pdf_info"]] == expected
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parents[3] /
        "packages/contracts/generated/schemas-resolved.json").read_text())["schemas"]["ddp-evidence/v1.json"]
    validator = Draft202012Validator({"$ref": "#/$defs/FederatedEvidence", "$defs": schema["$defs"]})
    for record in bundle.evidence:
        validator.validate(record["evidence"])
