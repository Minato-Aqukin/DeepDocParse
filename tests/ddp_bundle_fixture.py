"""Fixed portable source/evidence fixture shared by pure validation and HTTP tests."""

from ddp_core.bundle import build_bundle, digest, json_bytes

ORIGINAL = b"%PDF-1.4\nA fixed source for transfer-contract tests.\n%%EOF\n"


def sample_parts(*, missing=False, filename="manual.pdf"):
    source = {
        "origin_node_id": "node-origin",
        "authority_node_id": "node-origin",
        "resource_id": "a" * 32,
        "source_version_id": "b" * 32,
        "source_digest": digest(ORIGINAL),
        "parse_revision": "parse-001",
        "filename": filename,
        "mime": "application/pdf",
        "original": "missing" if missing else "present",
        "missing_reason": "source_offline" if missing else None,
        "uploader_ref": "original-uploader",
        "policy_revision": "policy-001",
    }
    evidence = {
        "schema": "ddp-evidence/1#FederatedEvidence",
        "evidence_id": "stable-source-evidence",
        **{
            key: source[key]
            for key in (
                "origin_node_id",
                "authority_node_id",
                "resource_id",
                "source_version_id",
                "source_digest",
                "parse_revision",
            )
        },
        "excerpt_digest": digest(b"A fixed source"),
        "locator": {
            "kind": "page_block",
            "physical_page_index": 0,
            "seq": 0,
            "bbox": [10, 20, 200, 40],
            "page_size": {"width": 612, "height": 792},
        },
        "source_type": "source",
        "derived_from": None,
        "policy_revision": "policy-001",
    }
    files = {
        "layout.json": json_bytes(
            {
                "schema": "ddp-bundle-layout/1",
                "state": "present",
                "layout": {
                    "layout_version": "ddp-layout/1",
                    "pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": []}],
                },
                "reason": None,
            }
        ),
        "evidence.json": json_bytes([{"evidence": evidence, "excerpt": "A fixed source"}]),
        "provenance.json": json_bytes([{"activity": "parse", "engine": "fixture"}]),
    }
    if not missing:
        files["source.bin"] = ORIGINAL
    return source, files


def sample_bundle(**kwargs):
    return build_bundle(*sample_parts(**kwargs))
