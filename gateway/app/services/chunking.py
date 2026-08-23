"""结构感知分块（v2/M4）：layout_json -> 携带页码+bbox 的 chunk 列表。

输入格式是 DDP-Layout v1，字段清单见 docs/layout-format.md ——
**只读承诺字段**，引擎塞进来的其它字段一律不碰（换引擎它们可能不存在）。

规则：
- 只在页内合并（chunk 永不跨页——出处必须能落到唯一页码）
- 相邻块合并至 max_chars 上限；bbox 取合并块的外接矩形
- **单块超过 max_chars 要切开**：不切的话它会被原样送进 embedding 运行时，
  由后者按模型最大长度静默截断——块尾内容从此检索不到，且全程没有报错
- 空文本块跳过

v1.1（块类型感知，随 DDP-Layout 的 type 进契约一起做）：
- **表格/公式/图片块独立成块，永不与正文合并**。合并循环只看字符数，
  一张表和它上下的正文会并进同一个 chunk：出处 bbox 横跨整片版心、
  行列关系拍平没了，抽取平面就找不到记录数组了
- **标题不单独成块**，作为后续块的上下文前缀。标题太短（"3.2 违约责任"），
  单独成块几乎检索不到，而它恰恰是判断下文属于哪一节的关键
- 每个 chunk 带 `block_type`，索引与检索据此分流
"""
# 块文本与类型的规范实现都在 normalizer 层，别在这里再抄一遍
# （block_text 这个循环历史上被抄过四遍）
from app.services.layout import (
    block_text as _block_text, normalize_type as _normalize_type,
    table_html as _table_html,
)

# 这些类型自成一块，不与邻居合并
_STANDALONE = {"table", "figure", "equation"}

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
        # 最近一个标题，作为后续块的上下文前缀。跨页清空 —— 出处必须落到唯一页，
        # 把上一页的标题带过来会让这一页的 chunk 文本里出现别的页的内容
        heading: str | None = None

        def emit(text: str, bbox: list | None, block_type: str,
                 html: str | None = None) -> None:
            chunk = {
                "text": text,
                "page_idx": page_idx,
                "bbox": bbox,
                # 页尺寸随 chunk 带走：检索命中后裁剪原图要用它换算坐标
                "page_size": page_size,
                "block_type": block_type,
            }
            if html:
                # 表格结构的唯一载体。block_text 拼出来的是拍平的单元格文字，
                # 行列关系已经没了 —— 抽取平面靠它把表格映射成记录数组
                chunk["table_html"] = html
            chunks.append(chunk)

        def flush() -> None:
            nonlocal current_text, current_bbox, current_len
            if current_text:
                emit("\n".join(current_text), current_bbox, "text")
            current_text, current_bbox, current_len = [], None, 0

        for block in page.get("para_blocks", []):
            # **过一遍归一化**，不要直接读原始 type：正常管线上 from_mineru 已经归一化过，
            # 但引擎直出的版面（未过 normalizer）里可能是 "Table" 这样的原生大小写，
            # 直接比字符串会把表格当成正文 —— 而 Web 侧是归一化过的，
            # 两边切出的块数就此不同，出处的 seq 对不上
            btype = _normalize_type(block.get("type"))
            text = _block_text(block)

            if btype in _STANDALONE:
                # 独立成块。**先 flush 再出块**：不 flush 的话表格前面攒着的正文
                # 会跟到表格后面那一段里去，页内阅读序就乱了
                flush()
                html = _table_html(block) if btype == "table" else None
                if text or html:
                    body = f"{heading}\n{text}" if heading and text else text
                    emit(body or "", block.get("bbox"), btype, html)
                continue

            if not text:
                continue
            if btype == "title":
                # 标题不单独成块（太短，检索不到），但要 flush：
                # 新标题意味着新一节，把上一节的尾巴并进来会让出处指错地方
                flush()
                heading = text
                continue

            # 先切超长块再进合并循环：合并只在"块前"判断是否 flush，
            # 一个超限块直接 append 就会原样出块（见模块 docstring）
            for piece in _split_oversized(text, max_chars):
                if current_len and current_len + len(piece) > max_chars:
                    flush()
                if not current_text and heading:
                    current_text.append(heading)
                    current_len += len(heading)
                current_text.append(piece)
                current_bbox = _union_bbox(current_bbox, block.get("bbox"))
                current_len += len(piece)
        flush()
    return chunks
