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
- **只合并版面上真正相邻的块**（ddp-chunk/3）：与上一块竖直空隙超过
  `_MAX_GAP_LINES` 行高、往上跳（换栏/阅读序回跳）、或左右不重叠（另一栏），
  就另起一块。只按字数合并时，正文会和几百点之外的页脚页码并成一块，
  外接矩形撑满整页 —— 出处"有框"，却指不出是哪一段（2026-09-23 浏览器实测）
- 有坐标的块与没坐标的块不合并：否则 chunk 的 bbox 只盖住其中一部分文字
- **没有字母/数字的正文块直接跳过**（只有 •、◦、–、标点）：borndigital 常把项目符号
  单独拆成块，落在所属段落的 bbox 里面。它既没有可检索的内容（出成 chunk 就是
  一字证据 "•"），又会当"上一块"去跟下一段比相邻性 —— 拿一个 2pt 高的小点比，
  前后两段正文就被判成不相邻、切碎了（2026-09-24 用 pico/esp32 真版面实测）
- **页眉页脚跳过**：块中心在页面上下 8% 带内、去掉数字后同一文字在同一条带里出现
  至少 3 页（页码、"Raspberry Pi Pico Datasheet"、"ESP32 Series Datasheet v5.3"）。
  /3 把它们从正文里拆了出来，结果成了独立的小块：凡是问句里带产品名，它们就被检索
  出来占掉候选名额，Wiki 里还有结论绑到了 "About Raspberry Pi Pico 3" 这种页脚上
  （2026-09-24，D 阶段浏览器实测）。版面里它们照旧在，只是不进索引、不当证据
- **单块超过 max_chars 要切开**：不切的话它会被原样送进 embedding 运行时，
  由后者按模型最大长度静默截断（bge-m3 是 8192 token）——块尾内容从此检索不到，
  且全程没有任何报错。静默降级是这个项目吃过大亏的地方。切出来的每段只能带
  整块的 bbox（版面没有行坐标），所以**切开的块之后另起一块**，尾段不再并下一段
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

## 合并说明（阶段 1）

搬进来之前 gateway 与 Web 各有一份 `layout_to_chunks`，**结构逐语句相同、
只有产出的键不同**：gateway 5 个键，Web 9 个（多 `seq` / `char_len` /
`table_html` 恒在 / `text_tokenized`）。合并统一到 Web 那份超集 ——
gateway 只读前 5 个，多出来的字段它不碰。

代价比看起来小，核实过：
- **Redis 暂存不会变大。** `task_store.save_chunks` 用的是**显式字段白名单**
  （doc_hash / text / page_idx / bbox / block_type / table_html / page_size / vec），
  新增的 `seq` / `char_len` / `text_tokenized` **根本进不了 Redis**。
  （第一版这里写着"payload 变大"，是没核实就写的自责 —— 别照着它去排查一个
  不存在的问题。）
- `text_tokenized` 让 **jieba 这个软依赖延伸到了 gateway**。gateway 的 venv
  里没装 jieba，于是它走二元组兜底 —— `tokenize.backend()` 会如实报 `bigram`，
  不是静默降级。也就是说**同一份文档在两侧算出的 `text_tokenized` 可能不同**，
  但那一列只有产品层的持久索引在用，gateway 既不读也不落库。
  **影响不到出处定位**：service 侧的 `seq` 来自 Redis 键名
  `chunk:{doc_hash}:{i}` 的 enumerate 下标，不是 chunk dict 里的 `seq` 字段。
"""
import re
from collections import defaultdict
from typing import Any

# **块文本与类型的规范实现在 blocks.py，别在这里再抄一遍。**
# 这句警告是从被本次搬家删掉的 gateway/app/services/chunking.py 里继承来的，
# 那份的原话是「block_text 这个循环历史上被抄过四遍」——
# 阶段 1 第一版合并时恰好把这个 import 丢了、换成了自带副本，
# 于是 service 仓库内部从 1 份变成 2 份，被验收当场抓住。别再犯。
from ddp_core.blocks import (
    block_text as _block_text, normalize_type as _normalize_type, table_html as _table_html,
)
from ddp_core.tokenize import code_tokenized as _code_tokenized, tokenized as _tokenized

# 优先在这些字符后断句；中日文没有空白，必须带上句读
_BREAK_AFTER = "\n。！？；…!?;. "

# 这些类型自成一块，不与邻居合并
_STANDALONE = {"code", "table", "figure", "equation"}

# 同一 chunk 内相邻两块之间允许的最大竖直空隙，按两块中较大的行高计。
# 正文段间距约 1 行，章节间隔约 2 行；页脚/页眉、远处的孤立段落都远大于此
_MAX_GAP_LINES = 2.5

# 页眉页脚（running header/footer）：块中心落在页面上下这一比例的带内、去掉数字后同一段
# 文字在至少这么多页的同一条带里重复。页码本身（纯数字）也算。
_FURNITURE_BAND = 0.08
_FURNITURE_MIN_PAGES = 3
_FURNITURE_MAX_CHARS = 120


def _valid_box(box: object) -> bool:
    """bbox 形状守卫：必须是 4 个全数值的坐标。

    版面是外部输入（mineru / vlm-ocr / 老归档），缺位、截断、字符串混入都见过。
    不满足就返回 False，调用方按“没坐标”处理 —— 出处可以没有框，
    但不能在这里 IndexError/TypeError 炸掉整篇分块。
    """
    return (
        isinstance(box, (list, tuple))
        and len(box) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v for v in box)
    )


def _valid_size(size: object) -> bool:
    """page_size 形状守卫：[w, h] 全数值且都 > 0（h == 0 会除零，0 宽同样无意义）。"""
    return (
        isinstance(size, (list, tuple))
        and len(size) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v for v in size)
        and size[0] > 0
        and size[1] > 0
    )


def _furniture_key(block: dict, page: dict) -> tuple[str, str] | None:
    box, size = block.get("bbox"), page.get("page_size")
    text = _block_text(block)
    if not _valid_box(box) or not _valid_size(size) or not text or len(text) > _FURNITURE_MAX_CHARS:
        return None
    centre = (box[1] + box[3]) / 2 / size[1]
    band = "top" if centre < _FURNITURE_BAND else "bottom" if centre > 1 - _FURNITURE_BAND else None
    return (band, " ".join(re.sub(r"\d+", " ", text).split()).casefold()) if band else None


def _running_furniture(pages: list[dict]) -> set[tuple[str, str]]:
    pages_of: dict[tuple[str, str], set] = defaultdict(set)
    for page in pages:
        for block in page.get("para_blocks") or []:
            key = (None if _normalize_type(block.get("type")) in _STANDALONE
                   else _furniture_key(block, page))
            if key:
                pages_of[key].add(page.get("page_idx"))
    return {key for key, seen in pages_of.items() if len(seen) >= _FURNITURE_MIN_PAGES}


def _line_height(block: dict) -> float:
    box = block.get("bbox")
    if not _valid_box(box):
        return 0.0
    return max(box[3] - box[1], 0.0) / max(len(block.get("lines") or []), 1)


def _adjacent(prev: dict, block: dict) -> bool:
    """block 能否与紧挨在它前面的 prev 并进同一个 chunk（页面坐标，y 向下）。"""
    a, b = prev.get("bbox"), block.get("bbox")
    if not a and not b:
        return True        # 都没坐标：无从判断，维持按字数合并
    if not _valid_box(a) or not _valid_box(b):
        return False       # 有坐标与没坐标（或畸形坐标）不混
    unit = max(_line_height(prev), _line_height(block), 1.0)
    gap = b[1] - a[3]
    if gap > _MAX_GAP_LINES * unit or gap < -0.5 * unit:
        return False       # 隔得太远，或往上跳到了另一栏 / 回跳
    return min(a[2], b[2]) > max(a[0], b[0])   # 左右必须有重叠，否则是另一栏


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """把超过 max_chars 的整块切成若干段。

    优先在句读/空白处断；断点太靠前（会切出一堆碎片）就退回硬切。
    切出来的每段都 <= max_chars，因此后续合并逻辑无需再关心超限块。
    """
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError(f"max_chars 必须是不小于 1 的整数（收到 {max_chars!r}）")
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
    if not _valid_box(a):
        return b if _valid_box(b) else None
    if not _valid_box(b):
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def layout_to_chunks(layout_json: dict[str, Any], max_chars: int = 800) -> list[dict]:
    """返回 [{seq, text, page_idx, bbox, page_size, char_len, block_type,
              table_html, text_tokenized}]，seq 为全文档顺序。"""
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError(f"max_chars 必须是不小于 1 的整数（收到 {max_chars!r}）")
    chunks: list[dict] = []

    pages = layout_json.get("pdf_info") or []
    furniture = _running_furniture(pages)

    for page in pages:
        page_idx = page.get("page_idx", 0)
        page_size = page.get("page_size")
        buf: list[str] = []
        bbox: list | None = None
        length = 0
        # 缓冲区里最后一个正文块：相邻性只跟紧挨着的前一块比，不跟外接矩形比
        last: dict | None = None
        # 最近一个标题，作为后续块的上下文前缀。**跨页清空**：出处必须落到唯一页，
        # 把上一页的标题带过来会让这一页 chunk 的文本里出现别的页的内容
        heading: str | None = None

        def emit(text: str, box: list | None, block_type: str,
                 html: str | None = None) -> None:
            chunks.append({
                "seq": len(chunks),
                "text": text,
                "page_idx": page_idx,
                "printed_page_label": page.get("printed_page_label"),
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
            nonlocal buf, bbox, length, last
            if buf:
                emit("\n".join(buf), bbox, "text")
            buf, bbox, length, last = [], None, 0, None

        for block in page.get("para_blocks") or []:
            btype = _normalize_type(block.get("type"))
            text = _block_text(block)

            if btype in _STANDALONE:
                # **先 flush 再出块**：不 flush 的话表格前面攒着的正文
                # 会跟到表格后面那一段里去，页内阅读序就乱了
                flush()
                html = _table_html(block) if btype == "table" else None
                # figure 没 caption 也必须留下原子：阶段 5 的 VLM 理解正是为了让
                # “只有图、没有文字”的区域进入索引。把它丢掉会让视觉链路永远没输入。
                if text or html or btype == "figure":
                    body = f"{heading}\n{text}" if heading and text else text
                    emit(body or "", block.get("bbox"), btype, html)
                continue

            if not text:
                continue
            if not any(c.isalnum() for c in text):
                continue   # 只有项目符号/标点：不出块，也不当相邻性的"上一块"（见 docstring）
            if _furniture_key(block, page) in furniture:
                continue   # 页眉页脚：同上，见 docstring
            if btype == "title":
                # 标题不单独成块（太短，检索不到），但要 flush：
                # 新标题意味着新一节，把上一节的尾巴并进来会让出处指错地方
                flush()
                heading = text
                continue

            # 先把超长块切开再进合并循环：合并只在"块前"判断是否 flush，
            # 一个超限块直接 append 就会原样出块（见模块 docstring）
            if last is not None and not _adjacent(last, block):
                flush()
            pieces = _split_oversized(text, max_chars)
            for piece in pieces:
                if length and length + len(piece) > max_chars:
                    flush()
                if not buf and heading:
                    buf.append(heading)
                    length += len(heading)
                buf.append(piece)
                bbox = _union_bbox(bbox, block.get("bbox"))
                length += len(piece)
            last = block
            if len(pieces) > 1:
                # 切开的块每段只能带整块的 bbox（版面没有行坐标），框已经比文字大；
                # 尾段再并下一块，外接矩形会把上面整段一起带上 —— 切开的块不再往后并
                flush()
        flush()

    # code 用标识符感知分词。保留完整标识符并拆 camel/snake/dot，精确查询与
    # “只搜其中一段”都能命中；其它块继续走通用分词。
    for chunk in chunks:
        if chunk["block_type"] == "code":
            chunk["text_tokenized"] = _code_tokenized(chunk["text"])

    return chunks


def page_count_of(layout_json: dict[str, Any]) -> int:
    return len(layout_json.get("pdf_info") or [])
