"""DeepSeek-OCR-2 方言：官方 prompt + grounding 输出 -> DDP-Layout。

## 为什么需要单独一个方言

`vlm_ocr.py` 原来只有一条路：给模型一段"请输出 JSON 版面"的指令，
再把 JSON 解析成版面。那条路对**通用**视觉模型是对的，对 DeepSeek-OCR-2 是错的——
它是 OCR 专用模型，训练时只见过两个 prompt（见官方 README 的 Main Prompts）：

    <image>\\n<|grounding|>Convert the document to markdown.
    <image>\\nFree OCR.

拿自定义 JSON schema 去问它，等于让它做没训练过的事：识别质量下降，
而且 bbox 会退化成"模型现编的估计值"——正是 vlm_ocr 模块头注释里
列为已知代价的那一条。走官方 prompt 则相反：**bbox 是模型的原生输出**，
和它在 OmniDocBench 上被评测的那条路径完全一致。

## 输出格式（判据来自官方 vLLM 脚本，不是猜的）

`DeepSeek-OCR2-vllm/run_dpsk_ocr2_pdf.py` 里的正则就是权威：

    r'(<\\|ref\\|>(.*?)<\\|/ref\\|><\\|det\\|>(.*?)<\\|/det\\|>)'   # re.DOTALL

标签**在内容之前**，一个标签管到下一个标签为止：

    <|ref|>title<|/ref|><|det|>[[139, 45, 861, 78]]<|/det|>
    # 2024 年度采购合同

    <|ref|>text<|/ref|><|det|>[[100, 120, 890, 300]]<|/det|>
    甲方：……

坐标是 **0~999 归一化**（官方换算 `int(x1 / 999 * image_width)`），
不是 0~1000 —— 差 0.1%，像素上看不出来，但既然有权威值就照抄，
省得将来有人拿两套换算对不上账来排查。

## 一个必须传的采样参数

OpenAI 接口的 `skip_special_tokens` 缺省是 **true**，
而 `<|ref|>` / `<|det|>` 正是特殊 token —— 不显式传 false 的话，
标签在返回前就被剥掉了：**模型明明报了 bbox，我们却一个都拿不到，
而且没有任何报错**，只会看到"每个块 bbox 都是 null"。
这条在 vlm_ocr.py 的请求体里钉着，改那里之前先读这段。
"""
import re

from ddp_gateway.services import layout

# 官方 prompt（README 的 Main Prompts 一节）。
# **不带 `<image>` 前缀**：我们走 OpenAI chat 协议，图片是 content 里的
# image_url 部件，vLLM 会按 `get_placeholder_str()` 在渲染 prompt 时
# 把 `<image>` 插到部件所在位置。我们的 content 顺序是 [image_url, text]，
# 渲染出来正好是 `<image>\n<|grounding|>Convert...`，与官方逐字一致。
# 自己再写一个 `<image>` 会变成两个占位符，直接对不上视觉 token 数。
PROMPT = "<|grounding|>Convert the document to markdown."

# 不要版面、只要文字时的官方 prompt。留在这里是为了让"另一条官方路径"有名字可引用，
# 注册表里 options.grounding: false 会切到它（那时全页只有一个块、没有 bbox）。
PROMPT_FREE_OCR = "Free OCR."

_TAG = re.compile(r"<\|ref\|>(.*?)<\|/ref\|><\|det\|>(.*?)<\|/det\|>", re.DOTALL)

# 落单的标记。`_TAG` 认的是完整四元组，配不上的残缺标签要靠它收尾 —— 见 strip_tags。
_MARKER = re.compile(r"<\|/?(?:ref|det)\|>")

# 结束符。我们请求时带了 `include_stop_str_in_output: true`（官方脚本同款），
# 于是它会**留在返回文本里** —— 而最后一个块的正文一路取到字符串结尾，
# 不剥掉的话它就成了正文的一部分，跟着进检索索引和出处文本。
# 官方脚本也是先 replace 掉再解析的。
# 全角竖线是这个 tokenizer 的写法（tokenizer_config.json 里就是 `<｜end▁of▁sentence｜>`），
# 顺手把半角变体也收掉，免得换个词表就漏。
_EOS = re.compile(r"<[|｜]end[▁_ ]of[▁_ ]sentence[|｜]>")

# 官方换算的分母。**是 999 不是 1000**，见模块头。
_COORD_MAX = 999.0

# DeepSeek-OCR 的 label -> DDP-Layout 契约词汇表。
# layout.normalize_type() 认识的是 mineru 的类型体系，这两个它认不出来
# （会归成 other），所以在这里先翻译一道：
#   image   -> figure     （契约里图片块叫 figure）
#   formula -> equation   （契约里公式块叫 equation；mineru 那边叫 interline_equation）
# 其余 label（text / title / table / list）与契约同名，直接过 normalize_type。
_LABEL_ALIASES = {"image": "figure", "formula": "equation"}

# 表格块的正文是 HTML。判据放宽到"含 <table"，因为模型偶尔会在表格前
# 带一行说明文字，`startswith` 会漏掉。
_TABLE_HINT = "<table"

_HTML_TAG = re.compile(r"<[^>]+>")
_HEADING_PREFIX = re.compile(r"^\s{0,3}#{1,6}\s+")


def strip_tags(raw: str) -> str:
    """去掉 grounding 标签与结束符，留下纯 markdown。

    兜底路径用：一个标签都没解析出来时，至少别把 `<|ref|>` 这种东西
    当正文存进检索索引。

    **两遍不是冗余。** `_TAG` 只吃完整的四元组
    （`<|ref|>…<|/ref|><|det|>…<|/det|>`），而走到这个兜底路径的输出，
    十有八九正是**标签残缺**的那一类 —— 真机上少 BOS 时模型吐的就是
    `<|ref|>text compared with in 45 c` 这种半截货。只做第一遍的话
    这半个标签原样穿透，跟着进检索索引和出处文本，全程无报错。
    第二遍单独收掉任何落单的标记。
    """
    text = _MARKER.sub("", _TAG.sub("", raw or ""))
    return _EOS.sub("", text).strip()


def _boxes(raw: str) -> list[list[float]] | None:
    """`[[x1,y1,x2,y2], ...]` -> 数值框列表。任何一处不对劲返回 None。

    官方脚本用的是 `eval()`；这里手写解析——这段字符串来自模型输出，
    是不可信输入，`eval` 在服务端等于给模型开了一个执行入口。
    """
    if not raw:
        return None
    numbers = re.findall(r"-?\d+(?:\.\d+)?", raw)
    if len(numbers) < 4:
        return None
    values = [float(n) for n in numbers]
    # 多框时数量必须是 4 的整数倍；不是就说明格式不是我们认识的那种，宁可整块判无框
    if len(values) % 4:
        return None
    return [values[i:i + 4] for i in range(0, len(values), 4)]


def to_bbox(raw: str, page_w: float, page_h: float) -> list[float] | None:
    """det 字符串 -> 页面坐标的单个 bbox。定不出来返回 None。

    **多个框时取并集**（min/min/max/max）。理由：一个 ref 报多个框
    意味着这块内容确实横跨多个区域（跨栏的段落、断开的表格）。
    并集是"包含全部内容的最小矩形"——裁出来会多带一点周边，
    但读者一定能在图里看到那句话；只取第一个框则会裁到半句，
    出处指向一个不含证据的区域，那比框大一点恶劣得多。

    与 vlm_ocr.denormalize_bbox 同一条铁律：**宁可 None 也不要凑合的框**。
    """
    boxes = _boxes(raw)
    if not boxes:
        return None

    valid = []
    for x0, y0, x1, y1 in boxes:
        if x1 <= x0 or y1 <= y0:
            continue
        if min(x0, y0) < 0 or max(x1, y1) > _COORD_MAX + 1:
            continue
        valid.append((x0, y0, x1, y1))
    if not valid:
        return None

    x0 = min(b[0] for b in valid)
    y0 = min(b[1] for b in valid)
    x1 = max(b[2] for b in valid)
    y1 = max(b[3] for b in valid)
    return [round(x0 / _COORD_MAX * page_w, 2), round(y0 / _COORD_MAX * page_h, 2),
            round(x1 / _COORD_MAX * page_w, 2), round(y1 / _COORD_MAX * page_h, 2)]


def _block_type(label: str) -> str:
    key = (label or "").strip().lower()
    return layout.normalize_type(_LABEL_ALIASES.get(key, key))


def _table_text(html: str) -> str:
    """表格 HTML -> 可检索的纯文本。

    表格结构走 `html` 字段（契约的可选承诺字段），但**文本也得留一份**：
    分块与检索读的是 block_text，只存 HTML 的话，"表里那个数"永远检索不到 ——
    这正是 layout.block_text 的注释里记着的那个洞。
    """
    text = _HTML_TAG.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


def blocks_from_output(raw: str, page_w: float, page_h: float) -> list[dict] | None:
    """一页的模型原文 -> DDP-Layout 的 para_blocks。没有标签时返回 None。

    返回 None 表示"这页没走 grounding 路径"，调用方应退回整页纯文本。
    这与"解析出 0 个块"是两回事，别合并。
    """
    if not raw:
        return None
    # 先剥结束符：最后一个块的正文一路取到字符串结尾，
    # 不剥的话 `<｜end▁of▁sentence｜>` 会变成正文的一部分
    raw = _EOS.sub("", raw)
    matches = list(_TAG.finditer(raw))
    if not matches:
        return None

    blocks: list[dict] = []

    # 第一个标签**之前**的文字。正常输出里没有这一段，但模型偶尔会先说一句话
    # 再开始报版面。直接从 matches[0] 开始遍历会把它悄悄丢掉 ——
    # 丢掉的是文档内容本身，下游只会看到"这段原文检索不到"，不会有任何报错。
    # 收成一个 bbox=None 的文本块：位置确实不知道，但内容不能丢。
    preamble = _MARKER.sub("", raw[:matches[0].start()]).strip()
    if preamble:
        blocks.append({
            "type": "text",
            "bbox": None,
            "lines": [{"spans": [{"content": line}]}
                      for line in preamble.splitlines() if line.strip()],
        })

    for i, match in enumerate(matches):
        body = raw[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(raw)]
        # **落单标记也要剥。** `_TAG` 只吃完整四元组，配不上的残缺标签会留在正文里 ——
        # 而这不是臆想的输入：生成被 `max_tokens` 截断时尾部**必然**留半个标签，
        # 而防复读处理器存在的理由正是 OCR 模型会复读到上限。
        # 不剥的话 `<|ref|>` 跟着块文本进检索索引与出处文本，全程无报错。
        body = _MARKER.sub("", body).strip()
        block_type = _block_type(match.group(1))
        bbox = to_bbox(match.group(2), page_w, page_h)

        html = None
        if block_type == "table" and _TABLE_HINT in body.lower():
            html = body
            body = _table_text(body)
        elif block_type == "title":
            # 模型输出的是 markdown，标题自带 `#` 前缀。类型已经由 label 承载了，
            # 再留着 `#` 会在 to_markdown 里叠成 `## # 标题`
            body = _HEADING_PREFIX.sub("", body).strip()

        if not body and not html:
            # 纯图片块（label=image 且没有题注）走到这里。跳过而不是留一个空块：
            # 空块进不了检索，却会占掉一个 seq —— 而 seq 是出处的稳定定位键，
            # 平白多一个空位会让同一份文档在换引擎后出处对不上
            continue

        block = {
            "type": block_type,
            "bbox": bbox,
            "lines": [{"spans": [{"content": line}]}
                      for line in body.splitlines() if line.strip()],
        }
        if html:
            # 与 mineru 放在同一个位置（span 的 html 字段），
            # layout.table_html 因此不用为这个方言写第二套取法
            block["lines"].append({"spans": [{"content": "", "html": html}]})
        blocks.append(block)
    return blocks


def page_from_output(raw: str, page_idx: int, page_w: float, page_h: float) -> dict | None:
    """一页的模型原文 -> DDP-Layout 的一页。没有 grounding 标签时返回 None。"""
    blocks = blocks_from_output(raw, page_w, page_h)
    if blocks is None:
        return None
    return {"page_idx": page_idx, "page_size": [page_w, page_h], "para_blocks": blocks}
