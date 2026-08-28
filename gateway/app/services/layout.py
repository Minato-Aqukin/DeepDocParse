"""版面中间表示（DDP-Layout v1）——**这是一个契约，不是内部结构**。

背景：`layout_json` 一直是事实上的内部 schema，四处消费它却从没被承认为契约：
  - gateway/ddp_core/chunking.py（**唯一一份**分块实现，两个仓库共用）
  - mcp_server/server.py::_layout_blocks
  - DeepDocParse-Web（产品层分块，import 上面那一份 —— 曾经各写一份，2026-08-26 已合并）
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
# v1.2 顶层可选承诺：引擎对这一趟解析的自述（"识别出来了，但有理由怀疑不对劲"）。
# 每条形如 `<code>: <人话>`，跟着 layout_json 一起归档。见 docs/layout-format.md。
# 名字放这儿而不是散在各 normalizer 里：它是契约字段，拼错了就等于没写。
# 块类型/块文本/表格 HTML 的规范实现在 ddp_core.blocks（两个仓库共用同一份）。
# 这里**原样再导出**，是为了让既有消费方的 `from app.services.layout import block_text`
# 一字不用改 —— 但**别在这里重新实现它们**：这三样历史上被抄过四遍，
# 而抄错的后果是出处指到错块、或整张表格的文字被静默丢弃。
from ddp_core.blocks import (  # noqa: F401
    BLOCK_TYPES, _MINERU_TYPE_MAP, block_text, normalize_type, table_html,
)

ENGINE_NOTES = "engine_notes"
CODE_DETECTION = "code_detection"
CODE_DETECTION_STATES = ("native", "heuristic", "unavailable")

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


def build(pages: list[dict], *, engine: str,
          code_detection: str = "unavailable") -> dict:
    """从零构造 layout_json（born-digital 这类自产版面的引擎用）。

    pages: [{page_idx, page_size: [w, h], blocks: [{bbox, text}]}]
    """
    return {
        "layout_version": LAYOUT_VERSION,
        "engine": engine,
        CODE_DETECTION: code_detection,
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


def build_pages(pages: list[dict], *, engine: str,
                code_detection: str = "unavailable") -> dict:
    """已经是 DDP-Layout 形状的页 -> 完整 layout_json（盖章 + 归一化块类型）。

    与 `build` 的区别是**输入形状**：`build` 收的是引擎自己的
    `{blocks: [{bbox, text}]}`，这里收的是已经带 `para_blocks/lines/spans` 的页。
    vlm-ocr 这类"模型直接吐出接近契约形状"的引擎用它，省掉一次无谓的来回转换。
    """
    return {
        "layout_version": LAYOUT_VERSION,
        "engine": engine,
        CODE_DETECTION: code_detection,
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
    # MinerU pipeline 没有稳定的代码类型信号。原生输出偶尔出现未知 type 也不能
    # 被消费方误解成“已经检测过且没有代码”。
    layout.setdefault(CODE_DETECTION, "unavailable")

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

    code_detection = layout.get(CODE_DETECTION)
    if code_detection is not None and code_detection not in CODE_DETECTION_STATES:
        problems.append(
            f"{CODE_DETECTION} 不合规（值={code_detection!r}），"
            f"应为 {' | '.join(CODE_DETECTION_STATES)} 或缺省")

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
