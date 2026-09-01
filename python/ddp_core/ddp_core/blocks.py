"""DDP-Layout 的块级规范实现 —— **块类型、块文本、表格 HTML 只此一份。**

这三样是全项目被抄得最多的东西：`block_text` 那个循环历史上被抄过**四遍**，
块类型映射表在两个仓库各有一份。抄得多是因为它们看起来平凡 ——
而代价一点也不平凡：

- 块类型判据两边差一点，同一份版面就切出不同的块，
  而出处的稳定定位键 `seq` 按块序算 -> **历史出处指到错误的块**
- `block_text` 少下潜一层嵌套 blocks，**整张表格的文字在分块阶段被静默丢弃**：
  表格解析出来了、索引里却没有，问表格里的数永远检索不到，全程无报错

所以它们住在 ddp_core 这个**叶子模块**里：不 import 任何 `app.*`，
两个仓库、gateway 的 normalizer 层与分块层，全都从这里取。

新增消费方**照用这一份，不要再抄**。
"""
# 归一化后的块类型词汇表 —— **契约的一部分**（v1.1 新增）。
# 以前 type 明确写着"不在承诺范围内"，消费方因此只能把所有块当正文对待：
# 表格被拆散揉进相邻段落、标题被并成正文，而这正是"结构化信息提取"最需要的信号。
# 升进契约的代价是**每个引擎的 normalizer 都必须产出这八个值之一**，
# 认不出来的一律归 other（不是丢弃——丢弃会让新引擎的块凭空消失）。
BLOCK_TYPES = ("text", "title", "code", "table", "figure", "equation", "list", "other")

# mineru 原生类型 -> 契约词汇表。mineru 的类型体系比这里细
# （image_body / table_caption / interline_equation …），归一化只保留下游真正会分支的那几类。
_MINERU_TYPE_MAP = {
    "text": "text", "plain text": "text", "paragraph": "text",
    "title": "title", "header": "title", "sub_title": "title",
    "code": "code", "code_block": "code", "source_code": "code",
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
