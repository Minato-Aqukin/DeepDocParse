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

# 契约承诺的字段——**只有这些**。消费方只准依赖它们，validate() 也照着这两张表检查
# （所以往这里加字段是真的有效的，别让它们变成没人读的常量）。
# 值是校验函数：拿到字段值，返回 True 表示合规
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
                        "type": block.get("type", "text"),
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
        if not isinstance(page.get("para_blocks"), list):
            page["para_blocks"] = []
        pages.append(page)
    layout["pdf_info"] = pages
    return layout


def block_text(block: dict) -> str:
    """按契约把一个块的文本拼出来：lines[].spans[].content。

    四处消费方各写了一遍同样的循环。这里给出**规范实现**，
    新的消费方照抄这一份，别再各自发挥。
    """
    parts: list[str] = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            content = span.get("content")
            if content:
                parts.append(str(content))
    return " ".join(parts).strip()


def page_count(layout: dict[str, Any]) -> int:
    return len(layout.get("pdf_info") or [])


def validate(layout: dict[str, Any]) -> list[str]:
    """结构自检，返回问题清单（空 = 通过）。

    给引擎适配器的作者用：新写一个 normalizer 时，跑一遍这个就知道有没有
    漏掉承诺字段。**不在请求路径上强制**——一个字段缺失不该让整份结果作废，
    但必须能被发现（这个项目吃过"静默不对劲"的亏）。
    """
    problems: list[str] = []
    pages = layout.get("pdf_info")
    if not isinstance(pages, list):
        return ["pdf_info 不是列表"]

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
