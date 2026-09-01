from ddp_core.compilation import (
    CHUNKER_VERSION, COMPILER_VERSION, code_detection_of, compile_chunks,
    equation_aliases, fingerprint, provider_of,
)
from ddp_core.crops import render_crops
from ddp_core.tokenize import code_tokens


def _layout() -> dict:
    def block(kind, text, y):
        return {"type": kind, "bbox": [20, y, 580, y + 30],
                "lines": [{"spans": [{"content": text}]}]}

    return {
        "layout_version": "ddp-layout/1", "engine": "borndigital",
        "code_detection": "heuristic",
        "pdf_info": [{"page_idx": 0, "page_size": [600, 800], "para_blocks": [
            block("text", "前一句说明参数含义。", 20),
            block("code", "client.fetchUser(user_id)", 60),
            block("equation", r"E = \alpha + β", 100),
            block("text", "后一句解释公式用途。", 140),
            block("figure", "图 1 延迟曲线", 180),
        ]}],
    }


def test_compile_keeps_source_separate_from_generated_description():
    layout = _layout()
    provider = provider_of(layout=layout, parse_options_hash="p", embedding_model="bge-m3",
                           vision_model="qwen-vl")
    chunks = compile_chunks(layout, max_chars=800, provider=provider,
                            descriptions={4: "曲线在 80ms 后趋于平稳"})
    figure = chunks[4]
    assert figure["text"] == "图 1 延迟曲线"
    assert figure["derived_text"] == "曲线在 80ms 后趋于平稳"
    assert "曲线在 80ms 后趋于平稳" in figure["search_text"]
    assert figure["provider_fingerprint"] == fingerprint(provider)


def test_code_identifier_tokens_preserve_original_and_split_parts():
    terms = code_tokens("client.fetchUser(user_id) HTTPServer")
    assert "client.fetchuser" in terms
    assert "client" in terms and "fetch" in terms and "user" in terms
    assert "user_id" in terms and "id" in terms
    assert "http" in terms and "server" in terms


def test_equation_adds_unicode_latex_aliases_and_adjacent_sentences():
    layout = _layout()
    provider = provider_of(layout=layout, parse_options_hash="p", embedding_model="",
                           vision_model="")
    equation = compile_chunks(layout, max_chars=800, provider=provider)[2]
    assert "α" in equation["search_text"]
    assert r"\beta" in equation["search_text"]
    assert "前一句说明参数含义" in equation["search_text"]
    assert "后一句解释公式用途" in equation["search_text"]
    assert equation_aliases(r"\alpha + β") == r"α \beta"


def test_provider_shape_and_old_layout_code_detection_are_explicit():
    provider = provider_of(layout=_layout(), parse_options_hash="p", embedding_model="",
                           vision_model="")
    assert provider["compiler"] == COMPILER_VERSION
    assert provider["chunker"] == CHUNKER_VERSION
    assert set(provider) == {
        "layout_engine", "layout_version", "parse_options_hash", "compiler", "chunker",
        "tokenizer", "embedding_model", "vision_model", "provider_resolved",
    }
    assert provider["provider_resolved"] is False
    assert code_detection_of({}) == "unavailable"


def test_unknown_code_detection_is_contract_violation():
    layout = _layout()
    layout["code_detection"] = "best-effort"
    provider = provider_of(layout=layout, parse_options_hash="p", embedding_model="bge",
                           vision_model="vlm")
    import pytest
    with pytest.raises(ValueError, match="invalid code_detection"):
        compile_chunks(layout, max_chars=800, provider=provider)


def test_render_crops_isolates_bad_atom_and_bad_page(monkeypatch):
    import sys
    from types import SimpleNamespace

    class Image:
        width = height = 100

        def crop(self, _box):
            return self

        def save(self, buf, *, format):
            buf.write(b"png")

    class Page:
        def __init__(self, broken=False):
            self.broken = broken

        def render(self, *, scale):
            if self.broken:
                raise RuntimeError("bad page")
            return SimpleNamespace(to_pil=lambda: Image())

        def get_width(self):
            return 100

        def get_height(self):
            return 100

    class Doc:
        pages = [Page(), Page(broken=True), Page()]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2",
                        SimpleNamespace(PdfDocument=lambda _data: Doc()))
    result = render_crops(b"pdf", [
        (0, [0, 0, 10], [100, 100]),
        (0, [0, 0, 10, 10], [100, 100]),
        (1, [0, 0, 10, 10], [100, 100]),
        (2, [0, 0, 10, 10], [100, 100]),
    ])
    assert result == [None, b"png", None, b"png"]


def test_figure_without_caption_is_still_a_visual_atom():
    layout = _layout()
    layout["pdf_info"][0]["para_blocks"][-1]["lines"] = []
    provider = provider_of(layout=layout, parse_options_hash="p", embedding_model="",
                           vision_model="")
    chunks = compile_chunks(layout, max_chars=800, provider=provider)
    assert chunks[-1]["block_type"] == "figure"
    assert chunks[-1]["text"] == ""
