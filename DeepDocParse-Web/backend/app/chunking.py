"""结构感知分块：layout.json -> 携带页码 + bbox 的 chunk 列表。

输入是**本层归档的** `results/{job_id}/layout.json`，不向 service 现取（ADR #16）：
契约保持冻结不新增端点，且不受 service 24h 暂存窗口约束——永久副本在手，
换 embedding 模型、调分块参数都能随时重建索引，不用重新解析。

只依赖契约承诺的版面字段：
    pdf_info[].page_idx / page_size / para_blocks[].bbox / lines[].spans[].content
mineru 升级导致这些字段变化时，tests/test_chunking.py 里的真实样本会先红。

规则（与出处定位强相关，改动前想清楚）：
- 只在页内合并，chunk **永不跨页** —— 出处必须能落到唯一页码
- 相邻块合并至 max_chars 上限；bbox 取合并块的外接矩形
- **单块超过 max_chars 要切开**：不切的话它会被原样送进 embedding 运行时，
  由后者按模型最大长度静默截断（bge-m3 是 8192 token）——块尾内容从此检索不到，
  且全程没有任何报错。静默降级是这个项目吃过大亏的地方
- 每块带 page_size：裁剪时按它换算坐标，缺它遇到 CropBox 偏移/旋转页会裁错区域
- 空文本块跳过；缺 bbox 仍出块（只是不能裁剪）
"""
from typing import Any

# 优先在这些字符后断句；中日文没有空白，必须带上句读
_BREAK_AFTER = "\n。！？；…!?;. "


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """把超过 max_chars 的整块切成若干段。

    优先在句读/空白处断；断点太靠前（会切出一堆碎片）就退回硬切。
    切出来的每段都 <= max_chars，因此后续合并逻辑无需再关心超限块。
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    rest = text
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max(window.rfind(ch) for ch in _BREAK_AFTER)
        if cut < max_chars // 2:        # 没有像样的断点：硬切
            cut = max_chars - 1
        pieces.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].lstrip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def _block_text(block: dict) -> str:
    parts: list[str] = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            content = span.get("content")
            if content:
                parts.append(str(content))
    return " ".join(parts).strip()


def _union_bbox(a: list | None, b: list | None) -> list | None:
    if not a:
        return b
    if not b:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def layout_to_chunks(layout_json: dict[str, Any], max_chars: int = 800) -> list[dict]:
    """返回 [{seq, text, page_idx, bbox, page_size, char_len}]，seq 为全文档顺序。"""
    chunks: list[dict] = []

    for page in layout_json.get("pdf_info") or []:
        page_idx = page.get("page_idx", 0)
        page_size = page.get("page_size")
        buf: list[str] = []
        bbox: list | None = None
        length = 0

        def flush() -> None:
            nonlocal buf, bbox, length
            if buf:
                text = "\n".join(buf)
                chunks.append({
                    "seq": len(chunks),
                    "text": text,
                    "page_idx": page_idx,
                    "bbox": bbox,
                    "page_size": page_size,
                    "char_len": len(text),
                })
            buf, bbox, length = [], None, 0

        for block in page.get("para_blocks") or []:
            text = _block_text(block)
            if not text:
                continue
            # 先把超长块切开再进合并循环：合并只在"块前"判断是否 flush，
            # 一个超限块直接 append 就会原样出块（见模块 docstring）
            for piece in _split_oversized(text, max_chars):
                if length and length + len(piece) > max_chars:
                    flush()
                buf.append(piece)
                bbox = _union_bbox(bbox, block.get("bbox"))
                length += len(piece)
        flush()

    return chunks


def page_count_of(layout_json: dict[str, Any]) -> int:
    return len(layout_json.get("pdf_info") or [])
