"""结构感知分块（v2/M4）：layout_json -> 携带页码+bbox 的 chunk 列表。

输入格式是 DDP-Layout v1，字段清单见 docs/layout-format.md ——
**只读承诺字段**，引擎塞进来的其它字段一律不碰（换引擎它们可能不存在）。

规则：
- 只在页内合并（chunk 永不跨页——出处必须能落到唯一页码）
- 相邻块合并至 max_chars 上限；bbox 取合并块的外接矩形
- **单块超过 max_chars 要切开**：不切的话它会被原样送进 embedding 运行时，
  由后者按模型最大长度静默截断——块尾内容从此检索不到，且全程没有报错
- 空文本块跳过
"""
# 块文本的规范实现在 normalizer 层，别在这里再抄一遍（这个循环历史上被抄过四遍）
from app.services.layout import block_text as _block_text

# 优先在这些字符后断句；中日文没有空白，必须带上句读
_BREAK_AFTER = "\n。！？；…!?;. "


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """把超过 max_chars 的整块切成若干段（与 Web 层 app/chunking.py 同规则）。

    优先在句读/空白处断；断点太靠前就退回硬切。
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
            # 先切超长块再进合并循环：合并只在"块前"判断是否 flush，
            # 一个超限块直接 append 就会原样出块（见模块 docstring）
            for piece in _split_oversized(text, max_chars):
                if current_len and current_len + len(piece) > max_chars:
                    flush()
                current_text.append(piece)
                current_bbox = _union_bbox(current_bbox, block.get("bbox"))
                current_len += len(piece)
        flush()
    return chunks
