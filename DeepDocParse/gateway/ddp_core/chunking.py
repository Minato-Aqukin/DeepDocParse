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
from typing import Any

# **块文本与类型的规范实现在 blocks.py，别在这里再抄一遍。**
# 这句警告是从被本次搬家删掉的 gateway/app/services/chunking.py 里继承来的，
# 那份的原话是「block_text 这个循环历史上被抄过四遍」——
# 阶段 1 第一版合并时恰好把这个 import 丢了、换成了自带副本，
# 于是 service 仓库内部从 1 份变成 2 份，被验收当场抓住。别再犯。
from ddp_core.blocks import (
    block_text as _block_text, normalize_type as _normalize_type, table_html as _table_html,
)
from ddp_core.tokenize import tokenized as _tokenized

# 优先在这些字符后断句；中日文没有空白，必须带上句读
_BREAK_AFTER = "\n。！？；…!?;. "

# 这些类型自成一块，不与邻居合并
_STANDALONE = {"table", "figure", "equation"}


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
            btype = _normalize_type(block.get("type"))
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
