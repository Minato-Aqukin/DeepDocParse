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
- 每块带 page_size：裁剪时按它换算坐标，缺它遇到 CropBox 偏移/旋转页会裁错区域
- 空文本块跳过；缺 bbox 仍出块（只是不能裁剪）
"""
from typing import Any


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
            if length and length + len(text) > max_chars:
                flush()
            buf.append(text)
            bbox = _union_bbox(bbox, block.get("bbox"))
            length += len(text)
        flush()

    return chunks


def page_count_of(layout_json: dict[str, Any]) -> int:
    return len(layout_json.get("pdf_info") or [])
