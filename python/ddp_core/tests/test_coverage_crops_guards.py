"""覆盖账本与裁图守卫：None 版本不算矛盾、缺尺寸不裁。"""
from ddp_core.application.coverage import version_conflicts


def envelope(evidence_id, *, version, digest, resource="res-1", origin="node-b",
             page=0, seq=3, source_type="source"):
    item = {"evidence_id": evidence_id, "origin_node_id": origin, "resource_id": resource,
            "source_type": source_type,
            "locator": {"kind": "page_block", "physical_page_index": page, "seq": seq}}
    if version is not None:
        item["source_version_id"] = version
    if digest is not None:
        item["excerpt_digest"] = digest
    return item


def test_missing_version_or_digest_never_conflicts():
    no_versions = [envelope("e1", version=None, digest="a"),
                   envelope("e2", version=None, digest="b")]
    assert version_conflicts(no_versions) == []

    one_sided = [envelope("e1", version="v1", digest="a"),
                 envelope("e2", version=None, digest="b")]
    assert version_conflicts(one_sided) == []

    no_digests = [envelope("e1", version="v1", digest=None),
                  envelope("e2", version="v2", digest=None)]
    assert version_conflicts(no_digests) == []


def test_duplicate_same_version_same_digest_dedupes_refs():
    items = [envelope("e1", version="v1", digest="a"),
             envelope("e2", version="v1", digest="a"),
             envelope("e3", version="v2", digest="b")]
    assert version_conflicts(items) == [
        {"basis": "version_divergence", "evidence_refs": ["e1", "e3"],
         "semantic_review": "needs_review"}]


def test_real_two_version_two_digest_still_conflicts():
    items = [envelope("ev-new", version="v2", digest="b"),
             envelope("ev-old", version="v1", digest="a")]
    assert version_conflicts(items) == [
        {"basis": "version_divergence", "evidence_refs": ["ev-new", "ev-old"],
         "semantic_review": "needs_review"}]


def test_missing_page_size_returns_none_without_pdfium(monkeypatch):
    from ddp_core import crops

    monkeypatch.setattr(crops, "RENDER_SCALE", 2.0)

    class _Bitmap:
        def __init__(self, img):
            self._img = img

        def to_pil(self):
            return self._img

        def close(self):
            pass

    class _Region:
        def save(self, buf, format=None):
            buf.write(b"fake-png")

        def close(self):
            pass

    class _Img:
        width = 1224
        height = 1584

        def crop(self, box):
            assert box[2] > box[0] and box[3] > box[1]
            return _Region()

        def close(self):
            pass

    class _Page:
        def render(self, scale=None):
            return _Bitmap(_Img())

        def get_width(self):
            return 612.0

        def get_height(self):
            return 792.0

        def close(self):
            pass

    class _Doc:
        def __init__(self, _bytes):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, idx):
            return _Page()

        def close(self):
            pass

    import pypdfium2 as pdfium

    monkeypatch.setattr(pdfium, "PdfDocument", _Doc)

    pdf = b"fake-pdf-bytes"
    for bad_size in (None, [], [612], [612, 0], ["612", 792], [0, 0]):
        got = crops.render_crops(pdf, [(0, [10, 10, 100, 100], bad_size)])
        assert got == [None], bad_size

    good = crops.render_crops(pdf, [(0, [10, 10, 100, 100], [612, 792])])
    assert good == [b"fake-png"]
