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

v1.1（块类型感知，随 DDP-Layout 的 type 进契约一起做）：
- **表格/公式/图片块独立成块，永不与正文合并**。合并循环只看字符数，一张表和
  它上下的正文会并进同一个 chunk：出处 bbox 横跨整片版心、行列关系拍平没了，
  抽取平面就找不到记录数组了
- **标题不单独成块**，作为后续块的上下文前缀（标题太短，单独成块几乎检索不到，
  而它恰恰是判断下文属于哪一节的关键）
- 表格块带 `table_html`：拼出来的单元格文字已经丢了行列关系，结构只在 HTML 里
- 每块带 `text_tokenized`：D2 中文分词，关键词检索路直接查这一列

**必须容忍没有 type 的老版面**：2026-08-23 之前归档的 layout.json 里
para_blocks 没有 type 字段，而它们仍然要能重建索引 —— 缺 type 一律按 text 处理。
"""
from typing import Any

from app.tokenize import tokenized as _tokenized

# 优先在这些字符后断句；中日文没有空白，必须带上句读
_BREAK_AFTER = "\n。！？；…!?;. "

# 这些类型自成一块，不与邻居合并
_STANDALONE = {"table", "figure", "equation"}

# DDP-Layout v1.1 的块类型词汇表（本层按铁律 1 各写一份，不 import service 的代码）。
# **必须与 service 侧 `layout.py::normalize_type` 逐条对齐**，包括原生类型映射表 ——
# 只做"认不出来就当 text"是不够的：归档的 layout.json 未必都经过 service 的
# normalizer（外部提交、老版本、将来的新引擎），那时本层拿到的是引擎原生类型。
# 两边判据不一致 ⇒ 同一份版面切出不同的块，而出处的稳定定位键 `seq` 按块序算 ⇒
# **历史出处会指到错误的块**，且完全静默。
_KNOWN_TYPES = {"text", "title", "table", "figure", "equation", "list", "other"}

# 与 service 侧 `_MINERU_TYPE_MAP` 同一份内容
_NATIVE_TYPE_MAP = {
    "text": "text", "plain text": "text", "paragraph": "text",
    "title": "title", "header": "title", "sub_title": "title",
    "table": "table", "table_body": "table", "table_caption": "table",
    "table_footnote": "table",
    "image": "figure", "figure": "figure", "image_body": "figure",
    "image_caption": "figure", "figure_caption": "figure",
    "interline_equation": "equation", "equation": "equation", "isolate_formula": "equation",
    "list": "list", "index": "list",
}


def _block_type(block: dict) -> str:
    """引擎原生块类型 -> 契约词汇表。两种"不认识"要分开（与 service 侧同一判据）：

    - **压根没有 type**（2026-08-23 之前归档的老版面）-> `text`
    - **有 type 但不在映射表里** -> `other`
    """
    raw = block.get("type")
    key = str(raw if raw is not None else "").strip().lower()
    if not key:
        return "text"
    if key in _KNOWN_TYPES:
        return key
    return _NATIVE_TYPE_MAP.get(key, "other")


def _table_html(block: dict) -> str | None:
    """表格块的 HTML（mineru 塞在 table_body 的 span 的 html 字段）。

    **表格结构的唯一载体** —— _block_text 拼出来的是拍平的单元格文字，
    行列关系已经没了。引擎没给就是 None（born-digital 不做表格识别），
    消费方必须能处理 None。
    """
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            html = span.get("html")
            if html:
                return str(html)
    for nested in block.get("blocks") or []:
        if isinstance(nested, dict):
            html = _table_html(nested)
            if html:
                return html
    return None


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
    """块文本 = lines[].spans[].content，**并下潜嵌套 blocks**。

    下潜是 v1.1 补的洞：mineru 的表格/图片块把内容放在 `blocks` 子结构里
    （table_body / table_caption），块自身没有 lines —— 此前只读 lines，
    于是**整张表格的文字在分块阶段被静默丢弃**：表格解析出来了、索引里却没有，
    问表格里的数永远检索不到，全程没有任何报错。
    """
    parts: list[str] = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            content = span.get("content")
            if content:
                parts.append(str(content))
    for nested in block.get("blocks") or []:
        if isinstance(nested, dict):
            inner = _block_text(nested)
            if inner:
                parts.append(inner)
    return " ".join(parts).strip()


def _union_bbox(a: list | None, b: list | None) -> list | None:
    if not a:
        return b
    if not b:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def layout_to_chunks(layout_json: dict[str, Any], max_chars: int = 800) -> list[dict]:
    """返回 [{seq, text, page_idx, bbox, page_size, char_len, block_type,
              table_html, text_tokenized}]，seq 为全文档顺序。"""
    chunks: list[dict] = []

    for page in layout_json.get("pdf_info") or []:
        page_idx = page.get("page_idx", 0)
        page_size = page.get("page_size")
        buf: list[str] = []
        bbox: list | None = None
        length = 0
        # 最近一个标题，作为后续块的上下文前缀。**跨页清空**：出处必须落到唯一页，
        # 把上一页的标题带过来会让这一页 chunk 的文本里出现别的页的内容
        heading: str | None = None

        def emit(text: str, box: list | None, block_type: str,
                 html: str | None = None) -> None:
            chunks.append({
                "seq": len(chunks),
                "text": text,
                "page_idx": page_idx,
                "bbox": box,
                "page_size": page_size,
                "char_len": len(text),
                "block_type": block_type,
                "table_html": html,
                # 索引时切一次存起来。查询侧用同一个 tokenizer ——
                # 两边切法不同 = 关键词路永远匹配不上，而且没有任何报错
                "text_tokenized": _tokenized(text),
            })

        def flush() -> None:
            nonlocal buf, bbox, length
            if buf:
                emit("\n".join(buf), bbox, "text")
            buf, bbox, length = [], None, 0

        for block in page.get("para_blocks") or []:
            btype = _block_type(block)
            text = _block_text(block)

            if btype in _STANDALONE:
                # **先 flush 再出块**：不 flush 的话表格前面攒着的正文
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

            # 先把超长块切开再进合并循环：合并只在"块前"判断是否 flush，
            # 一个超限块直接 append 就会原样出块（见模块 docstring）
            for piece in _split_oversized(text, max_chars):
                if length and length + len(piece) > max_chars:
                    flush()
                if not buf and heading:
                    buf.append(heading)
                    length += len(heading)
                buf.append(piece)
                bbox = _union_bbox(bbox, block.get("bbox"))
                length += len(piece)
        flush()

    return chunks


def page_count_of(layout_json: dict[str, Any]) -> int:
    return len(layout_json.get("pdf_info") or [])
