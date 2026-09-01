"""DDP-Compile v1：DDP-Layout -> 可检索原子与可比较 provider 指纹。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ddp_core.chunking import layout_to_chunks
from ddp_core.tokenize import backend as tokenizer_backend
from ddp_core.tokenize import code_tokenized, tokenized

COMPILER_VERSION = "ddp-compile/1"
CHUNKER_VERSION = "ddp-chunk/2"
VISUAL_KINDS = frozenset({"code", "equation", "table", "figure"})
CODE_DETECTION_VALUES = frozenset({"native", "heuristic", "unavailable"})
UNRESOLVED_MODEL = "<upstream-default:unresolved>"

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "phi": "φ", "psi": "ψ", "omega": "ω",
}
_SENTENCE = re.compile(r"[^。！？!?;；\n]+[。！？!?;；]?", re.UNICODE)


def provider_of(*, layout: dict, parse_options_hash: str, embedding_model: str,
                vision_model: str) -> dict:
    """构造字段完整、顺序无关的 provider。

    空模型名意味着调用方让上游注册表决定默认值；这时实际 provider 不可追溯，
    必须显式标 unresolved，版本校验也不得把两次空串误判成同一模型。
    """
    resolved = bool(embedding_model) and bool(vision_model)
    return {
        "layout_engine": str(layout.get("engine") or ""),
        "layout_version": str(layout.get("layout_version") or ""),
        "parse_options_hash": parse_options_hash,
        "compiler": COMPILER_VERSION,
        "chunker": CHUNKER_VERSION,
        "tokenizer": tokenizer_backend(),
        "embedding_model": embedding_model or UNRESOLVED_MODEL,
        "vision_model": vision_model or UNRESOLVED_MODEL,
        "provider_resolved": resolved,
    }


def fingerprint(provider: dict) -> str:
    payload = json.dumps(provider, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def equation_aliases(text: str) -> str:
    """同时加入 LaTeX 与 Unicode 希腊字母别名，不改写源文本。"""
    aliases: list[str] = []
    for latin, glyph in _GREEK.items():
        latex = f"\\{latin}"
        if latex in text:
            aliases.append(glyph)
        if glyph in text:
            aliases.append(latex)
    return " ".join(dict.fromkeys(aliases))


def _edge_sentence(text: str, *, last: bool) -> str:
    sentences = [m.group(0).strip() for m in _SENTENCE.finditer(text) if m.group(0).strip()]
    if not sentences:
        return ""
    return sentences[-1 if last else 0]


def compile_chunks(layout: dict[str, Any], *, max_chars: int, provider: dict,
                   descriptions: dict[int, str] | None = None) -> list[dict]:
    """生成源 chunk；`descriptions` 按 seq 注入，但源 `text` 永远不被改写。"""
    code_detection_of(layout)
    chunks = layout_to_chunks(layout, max_chars)
    descriptions = descriptions or {}
    fp = fingerprint(provider)

    for i, chunk in enumerate(chunks):
        kind = chunk["block_type"]
        source = chunk["text"]
        parts = [source] if source else []
        if kind == "equation":
            aliases = equation_aliases(source)
            prose = {"text", "list", "other"}
            previous = next((c for c in reversed(chunks[:i])
                             if c["page_idx"] == chunk["page_idx"] and c["text"]
                             and c["block_type"] in prose), None)
            following = next((c for c in chunks[i + 1:]
                              if c["page_idx"] == chunk["page_idx"] and c["text"]
                              and c["block_type"] in prose), None)
            context = " ".join(p for p in (
                _edge_sentence(previous["text"], last=True) if previous else "",
                _edge_sentence(following["text"], last=False) if following else "",
            ) if p)
            parts.extend(p for p in (aliases, context) if p)
        elif kind == "table" and chunk.get("table_html"):
            parts.append(chunk["table_html"])

        derived = (descriptions.get(chunk["seq"]) or "").strip()
        if derived:
            parts.append(derived)
        search_text = "\n".join(parts)
        chunk.update({
            "search_text": search_text,
            "derived_text": derived or None,
            "provider": dict(provider),
            "provider_fingerprint": fp,
            "text_tokenized": (code_tokenized(search_text) if kind == "code"
                               else tokenized(search_text)),
        })
    return chunks


def code_detection_of(layout: dict) -> str:
    if "code_detection" not in layout:
        return "unavailable"       # DDP-Layout v1.0/v1.1 老归档
    value = layout["code_detection"]
    if value not in CODE_DETECTION_VALUES:
        raise ValueError(f"invalid code_detection: {value!r}")
    return value


def source_anchor(*, seq: int, content_digest: str, page_idx: int,
                  bbox: list | None) -> tuple[int, str, int, str]:
    """源 Evidence 跨重建复用的唯一判据；预检与物化必须共用。"""
    return seq, content_digest, page_idx, json.dumps(bbox, sort_keys=True,
                                                     separators=(",", ":"))
