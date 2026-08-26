"""版面中间表示（DDP-Layout v1）——**这是一个契约，不是内部结构**。

背景：`layout_json` 一直是事实上的内部 schema，四处消费它却从没被承认为契约：
  - gateway/app/services/chunking.py
  - mcp_server/server.py::_layout_blocks
  - DeepDocParse-Web/backend/app/chunking.py（按铁律 1 各写一份）
  - openapi.yaml 的 layout_json 字段
后果是：注册表在传输层做到了引擎无关，数据格式层却写死了 mineru ——
"加引擎 = 加容器 + 一行配置"这个承诺只兑现了一半。

这一层把归一化显式化：**engine 原生输出 -> 归一化 -> layout_json**。
格式本身写在 docs/layout-format.md，改字段先改那份文档。

设计取舍：归一化是"**补齐并校验承诺字段**"，不是"重建结构"。
mineru 的 middle_json 里还有很多没进契约的字段（type、index、angle…），
原样留着——消费方不许依赖它们，但把它们删掉会让"下载版面 JSON"这类
排查手段凭空变差，得不偿失。
"""
from typing import Any

# 版面格式版本。放进 layout_json 让消费方能识别来源；
# 加字段（向后兼容）不改它，改语义/删字段才改
LAYOUT_VERSION = "ddp-layout/1"

# 归一化后的块类型词汇表 —— **契约的一部分**（v1.1 新增）。
# 以前 type 明确写着"不在承诺范围内"，消费方因此只能把所有块当正文对待：
# 表格被拆散揉进相邻段落、标题被并成正文，而这正是"结构化信息提取"最需要的信号。
# 升进契约的代价是**每个引擎的 normalizer 都必须产出这七个值之一**，
# 认不出来的一律归 other（不是丢弃——丢弃会让新引擎的块凭空消失）。
BLOCK_TYPES = ("text", "title", "table", "figure", "equation", "list", "other")

# mineru 原生类型 -> 契约词汇表。mineru 的类型体系比这里细
# （image_body / table_caption / interline_equation …），归一化只保留下游真正会分支的那几类。
_MINERU_TYPE_MAP = {
    "text": "text", "plain text": "text", "paragraph": "text",
    "title": "title", "header": "title", "sub_title": "title",
    "table": "table", "table_body": "table", "table_caption": "table",
    "table_footnote": "table",
    "image": "figure", "figure": "figure", "image_body": "figure",
    "image_caption": "figure", "figure_caption": "figure",
    "interline_equation": "equation", "equation": "equation", "isolate_formula": "equation",
    "list": "list", "index": "list",
}


def normalize_type(raw: object) -> str:
    """引擎原生块类型 -> 契约词汇表。永不抛异常。

    两种"不认识"要分开：
    - **压根没有 type**（2026-08-23 之前归档的老版面、不报类型的引擎）-> `text`。
      当正文处理是最保守也最正确的默认
    - **有 type 但不在映射表里** -> `other`。这是"这个引擎有我们没见过的类型"，
      值得在排查时区分出来

    Web 层的 `chunking._block_type` 必须与这里保持同一判据 ——
    两边不一致的话，同一份版面在 service 与产品层会切出不同的块，
    而出处的稳定定位键 `seq` 正是按块序算的：**历史出处会指到错误的块**。
    """
    key = str(raw if raw is not None else "").strip().lower()
    if not key:
        return "text"
    if key in BLOCK_TYPES:
        return key
    return _MINERU_TYPE_MAP.get(key, "other")

# 契约承诺的字段——**只有这些**。消费方只准依赖它们，validate() 也照着这两张表检查
# （所以往这里加字段是真的有效的，别让它们变成没人读的常量）。
# 值是校验函数：拿到字段值，返回 True 表示合规
# v1.2 顶层可选承诺：引擎对这一趟解析的自述（"识别出来了，但有理由怀疑不对劲"）。
# 每条形如 `<code>: <人话>`，跟着 layout_json 一起归档。见 docs/layout-format.md。
# 名字放这儿而不是散在各 normalizer 里：它是契约字段，拼错了就等于没写。
ENGINE_NOTES = "engine_notes"

PROMISED_PAGE_FIELDS = {
    "page_idx": lambda v: isinstance(v, int),
    # 缺 page_size 时裁剪只能拿 pdfium 的页尺寸凑合，遇到 CropBox 偏移或旋转页
    # 会裁到错误区域 —— 出处图对不上原文是最恶劣的一种错
    "page_size": lambda v: (isinstance(v, (list, tuple)) and len(v) == 2
                            and all(isinstance(x, (int, float)) and x > 0 for x in v)),
    "para_blocks": lambda v: isinstance(v, list),
}
PROMISED_BLOCK_FIELDS = {
    # bbox 允许为 None：缺它的块仍然有效，只是不能裁剪
    "bbox": lambda v: v is None or (isinstance(v, (list, tuple)) and len(v) == 4),
    # v1.1：块类型进契约。**必须是 BLOCK_TYPES 里的值**——normalizer 认不出来的
    # 已经归成 other 了，所以这里出现别的值只可能是 normalizer 漏跑，是真问题
    "type": lambda v: v in BLOCK_TYPES,
    # lines 允许缺失：契约只承诺"块文本 = lines[].spans[].content"，没说每个块都得有
    # lines —— mineru 的图片/表格块用的是嵌套 blocks。要求它存在会让自检对真 mineru
    # 输出报一串假问题，自检工具就成了狼来了（block_text/chunking 本来就容忍缺失）
    "lines": lambda v: v is None or isinstance(v, list),
}


def build(pages: list[dict], *, engine: str) -> dict:
    """从零构造 layout_json（born-digital 这类自产版面的引擎用）。

    pages: [{page_idx, page_size: [w, h], blocks: [{bbox, text}]}]
    """
    return {
        "layout_version": LAYOUT_VERSION,
        "engine": engine,
        "pdf_info": [
            {
                "page_idx": page["page_idx"],
                "page_size": list(page["page_size"]),
                "para_blocks": [
                    {
                        "type": normalize_type(block.get("type", "text")),
                        "bbox": list(block["bbox"]),
                        # 一行一个 span：契约只承诺 lines[].spans[].content 可拼成块文本，
                        # 不承诺行内还有更细的分段
                        "lines": [{"spans": [{"content": line}]}
                                  for line in block["text"].splitlines() if line.strip()],
                    }
                    for block in page["blocks"] if block.get("text", "").strip()
                ],
            }
            for page in pages
        ],
    }


def build_pages(pages: list[dict], *, engine: str) -> dict:
    """已经是 DDP-Layout 形状的页 -> 完整 layout_json（盖章 + 归一化块类型）。

    与 `build` 的区别是**输入形状**：`build` 收的是引擎自己的
    `{blocks: [{bbox, text}]}`，这里收的是已经带 `para_blocks/lines/spans` 的页。
    vlm-ocr 这类"模型直接吐出接近契约形状"的引擎用它，省掉一次无谓的来回转换。
    """
    return {
        "layout_version": LAYOUT_VERSION,
        "engine": engine,
        "pdf_info": [
            {
                "page_idx": page.get("page_idx", i),
                "page_size": list(page.get("page_size") or [0, 0]),
                "para_blocks": [_normalize_block(b) for b in (page.get("para_blocks") or [])
                                if isinstance(b, dict)],
            }
            for i, page in enumerate(pages)
        ],
    }


def from_mineru(middle_json: dict | None, *, engine: str = "mineru") -> dict:
    """mineru middle_json -> layout_json。

    mineru 的 middle_json 本来就是这个格式的来源，所以这里主要是**盖章与补齐**：
    打上版本标记、保证承诺字段存在且类型正确。真正的价值在于有一个明确的地方
    可以回答"契约承诺了什么" —— 以前这个问题只能靠读四处消费方的代码来回答。
    """
    layout = dict(middle_json or {})
    layout.setdefault("layout_version", LAYOUT_VERSION)
    layout.setdefault("engine", engine)

    pages = []
    for index, page in enumerate(layout.get("pdf_info") or []):
        page = dict(page)
        # page_idx 缺失就按出现顺序补：缺了它出处就落不到具体页，那是这套东西的立身之本
        page.setdefault("page_idx", index)
        blocks = page.get("para_blocks")
        if not isinstance(blocks, list):
            page["para_blocks"] = []
        else:
            # v1.1：type 进了契约，就必须在这里落成词汇表里的值。
            # **原生值不丢**（存进 type_native）：排查"为什么这块被归成 other"时
            # 需要它，而删掉它会让"下载版面 JSON"这个手段凭空变差
            page["para_blocks"] = [_normalize_block(b) for b in blocks
                                   if isinstance(b, dict)]
        pages.append(page)
    layout["pdf_info"] = pages
    return layout


def _normalize_block(block: dict) -> dict:
    out = dict(block)
    raw = out.get("type")
    normalized = normalize_type(raw)
    if raw is not None and str(raw) != normalized:
        out.setdefault("type_native", raw)
    out["type"] = normalized
    return out


def block_text(block: dict) -> str:
    """按契约把一个块的文本拼出来：lines[].spans[].content。

    四处消费方各写了一遍同样的循环。这里给出**规范实现**，
    新的消费方照抄这一份，别再各自发挥。

    **v1.1 起会下潜嵌套 blocks**。mineru 的表格/图片块把内容放在 `blocks`
    子结构里（table_body / table_caption / image_caption …），自身没有 lines ——
    此前这里只读 lines，于是**整张表格的文字在分块阶段被静默丢弃**：
    表格解析出来了、索引里却没有，问表格里的数永远检索不到，全程无报错。
    这是「结构化信息提取」这条线上第一个要堵的洞。
    """
    parts: list[str] = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            content = span.get("content")
            if content:
                parts.append(str(content))
    for nested in block.get("blocks") or []:
        if isinstance(nested, dict):
            inner = block_text(nested)
            if inner:
                parts.append(inner)
    return " ".join(parts).strip()


def table_html(block: dict) -> str | None:
    """表格块的 HTML（mineru 的表格模型产出），没有就返回 None。

    mineru 把它塞在 table_body 的 span 里（`html` 字段）。这是**表格结构的唯一载体**：
    block_text 拼出来的是拍平的单元格文字，行列关系已经没了。
    抽取平面要把表格映射成记录数组，靠的就是这里。

    契约地位：`table_html` 是**可选的承诺字段** —— 引擎有就给，没有不算违约
    （born-digital 就没有）。消费方必须能处理 None。
    """
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            html = span.get("html")
            if html:
                return str(html)
    for nested in block.get("blocks") or []:
        if isinstance(nested, dict):
            html = table_html(nested)
            if html:
                return html
    return None


def page_count(layout: dict[str, Any]) -> int:
    return len(layout.get("pdf_info") or [])


def validate(layout: dict[str, Any]) -> list[str]:
    """结构自检，返回问题清单（空 = 通过）。

    给引擎适配器的作者用：新写一个 normalizer 时，跑一遍这个就知道有没有
    漏掉承诺字段。**不在请求路径上强制**——一个字段缺失不该让整份结果作废，
    但必须能被发现（这个项目吃过"静默不对劲"的亏）。
    """
    problems: list[str] = []

    # v1.2 顶层可选承诺。缺省是对的（绝大多数解析没什么好说的）；
    # 给了就必须是字符串列表 —— 消费方按 `<code>: <人话>` 的前缀分支
    notes = layout.get(ENGINE_NOTES)
    if notes is not None and not (isinstance(notes, list)
                                  and all(isinstance(n, str) for n in notes)):
        problems.append(f"{ENGINE_NOTES} 不合规（值={notes!r}），应为字符串列表或缺省")

    pages = layout.get("pdf_info")
    if not isinstance(pages, list):
        return problems + ["pdf_info 不是列表"]

    for i, page in enumerate(pages):
        where = f"pdf_info[{i}]"
        for field, ok in PROMISED_PAGE_FIELDS.items():
            if not ok(page.get(field)):
                problems.append(f"{where}.{field} 不合规（值={page.get(field)!r}）")
        blocks = page.get("para_blocks")
        if not isinstance(blocks, list):
            continue        # 上面已经报过了
        for j, block in enumerate(blocks):
            for field, ok in PROMISED_BLOCK_FIELDS.items():
                if not ok(block.get(field)):
                    problems.append(
                        f"{where}.para_blocks[{j}].{field} 不合规（值={block.get(field)!r}）")
    return problems
