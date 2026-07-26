"""结构感知分块（v2/M4）：layout_json -> 携带页码+bbox 的 chunk 列表。

规则：
- 只在页内合并（chunk 永不跨页——出处必须能落到唯一页码）
- 相邻块合并至 max_chars 上限；bbox 取合并块的外接矩形
- 空文本块跳过
"""


def _block_text(block: dict) -> str:
    parts = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("content"):
                parts.append(span["content"])
    return " ".join(parts).strip()


def _union_bbox(a: list | None, b: list | None) -> list | None:
    if not a:
        return b
    if not b:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def layout_to_chunks(layout_json: dict, max_chars: int = 800) -> list[dict]:
    chunks: list[dict] = []
    for page in layout_json.get("pdf_info", []):
        page_idx = page.get("page_idx", 0)
        page_size = page.get("page_size")
        current_text: list[str] = []
        current_bbox: list | None = None
        current_len = 0

        def flush() -> None:
            nonlocal current_text, current_bbox, current_len
            if current_text:
                chunks.append({
                    "text": "\n".join(current_text),
                    "page_idx": page_idx,
                    "bbox": current_bbox,
                    # 页尺寸随 chunk 带走：检索命中后裁剪原图要用它换算坐标
                    "page_size": page_size,
                })
            current_text, current_bbox, current_len = [], None, 0

        for block in page.get("para_blocks", []):
            text = _block_text(block)
            if not text:
                continue
            if current_len and current_len + len(text) > max_chars:
                flush()
            current_text.append(text)
            current_bbox = _union_bbox(current_bbox, block.get("bbox"))
            current_len += len(text)
        flush()
    return chunks
