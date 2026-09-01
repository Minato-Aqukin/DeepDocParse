"""vlm-ocr 引擎：用视觉语言模型做整页识别 —— 「基于大语言模型的文字识别」这条线的落点。

在它之前，三个解析引擎没有一个是 LLM 在做识别：
  mineru pipeline  专用小模型流水线（layout / OCR / 表格各一个）
  borndigital      纯 pypdfium2，零模型
  DeepSeek-OCR-2   只用在**下游核对**，从没当过解析引擎
这个引擎把 VQA 平面那个模型当整页识别器用：页图 -> VLM -> DDP-Layout。

**它不新增任何接缝**：normalizer 层（B1）与「加引擎 = 加容器 + 一行配置」（B2）
早就在了，这里只是第三个 normalizer。models.yaml 里长这样：

    parse_engines:
      vlm-ocr:
        endpoint: "http://vqa-dsocr:8000"    # 就是 VQA 那个容器，不用多起一个
        runtime: vlm-ocr
        options: { model: deepseek-ocr-2, dialect: deepseek-ocr2, scale: 2.0 }

## 两种方言（v1.2）

`options.dialect` 决定跟模型怎么说话：

    generic-json    通用视觉模型：让它按我们的 schema 吐 JSON（**缺省**，
                    与 v1.1 行为逐字一致，老注册表不用改）
    deepseek-ocr2   OCR 专用模型：走官方 prompt + grounding 标签，见 dsocr2.py

分方言不是为了好看：DeepSeek-OCR-2 只在两个官方 prompt 上训练过，
拿自定义 JSON schema 去问它是让它做没训练过的事 —— 识别质量下降，
而且下面"bbox 靠模型现编"那条代价会被放大。走官方 prompt 则 bbox 是原生输出。

## 已知代价（写在这儿，免得被当成 bug 反复排查）

- **bbox 靠模型自己报，精度远不如 mineru 的版面模型**（generic-json 方言尤甚；
  deepseek-ocr2 方言下 bbox 是模型的原生 grounding 输出，好得多但仍不是版面模型）。
  报不出来就落 `bbox: null`，
  契约允许（"块仍然有效，只是不能裁剪"），下游会照常打 crop_unsupported。
  **绝不编一个整页 bbox 顶上** —— 那会产出一个永远"命中"的假出处，
  比没有出处恶劣得多。
- **一页一次模型调用**，长文档很贵也很慢。它的定位是扫描件兜底，不是 mineru 的替代。
- 页尺寸取自 pdfium，坐标按 `page_size` 换算 —— 与 layout-format.md 的坐标系一致。
- 本机无 GPU，**只到 mock 单测为止**；真机行为待远程服务器验证。
"""
import asyncio
import base64
import json
import logging
import re

import httpx

from app.services import dsocr2, layout
from ddp_core import crops

logger = logging.getLogger(__name__)

# 方言：同一个引擎对不同视觉模型说不同的话。
#   generic-json   通用视觉模型：让它按我们的 schema 吐 JSON（缺省，行为与 v1.1 完全一致）
#   deepseek-ocr2  OCR 专用模型：走官方 prompt + grounding 标签（见 dsocr2.py）
# 缺省**必须**是 generic-json：注册表里没写 dialect 的老部署行为不能变。
GENERIC_JSON = "generic-json"
DEEPSEEK_OCR2 = "deepseek-ocr2"

# 让模型输出结构化版面。要点：
# 1. 明确 bbox 的坐标系与量纲（0~1000 归一化），否则每个模型一套自己的约定
# 2. **允许它说"这块我定不出位置"**（bbox: null）—— 不给这个出口，模型就会编一个
# 3. 块类型直接用契约词汇表，省掉一层映射
_PROMPT = """把这一页的内容识别成结构化版面，输出**纯 JSON**，不要任何解释文字。

格式：
{"blocks": [{"type": "...", "bbox": [x0, y0, x1, y1], "text": "..."}]}

规则：
- type 只能是：text（正文段落）、title（标题）、code（代码/配置/命令块）、table（表格）、figure（图片）、
  equation（行间公式）、list（列表）、other
- bbox 是该块在页面上的位置，坐标归一化到 0~1000（左上角为原点，x 向右、y 向下）。
  **定不出准确位置就写 null，不要估一个** —— 位置错的出处比没有出处更糟
- text 是该块的文字，按阅读顺序。表格请额外给 "html" 字段放表格的 HTML
- 按阅读顺序排列 blocks；空白区域不要输出
- 一个字都不要改写、翻译或总结，原样识别"""

# 模型可能把 bbox 归一化到 0~1 而不是 0~1000。两种都收：
# 判据是"四个值全 <= 1.5"（真实的 0~1000 坐标不可能整块挤在左上角 1.5 个单位里）
_UNIT_SCALE_MAX = 1.5

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

# DeepSeek-OCR-2 的官方采样参数，逐条抄自官方 vLLM 脚本
# （DeepSeek-OCR2-vllm/run_dpsk_ocr2_pdf.py），别凭感觉改：
#   ngram_size / window_size  防重复 logits processor 的窗口。OCR 模型在
#       表格、页眉页脚这类高度重复的版面上会陷进复读循环，一路吐到 max_tokens。
#       vLLM 侧要用 `--logits-processors` 把它挂上（见 deploy/autodl/ocr.bash），
#       **并且**每个请求带 ngram_size 才会生效 —— 没传就整个跳过
#       （见 vLLM 的 NGramPerReqLogitsProcessor.validate_params）。两边缺一不可。
#   whitelist  <td> / </td> 的 token id。表格里这两个标签本来就该反复出现，
#       不放行的话防重复机制会把表格结构本身掐断
_DSOCR2_NGRAM_SIZE = 20
_DSOCR2_WINDOW_SIZE = 50
_DSOCR2_TABLE_TOKEN_IDS = [128821, 128822]

# **不是 8192。** 官方离线脚本里写的是 8192，那是因为它用 LLM 类直接推理；
# 走 OpenAI 接口时 `prompt_tokens + max_tokens` 必须 <= max_model_len，
# 而这个模型的 max_model_len 就是 8192（config 的 max_position_embeddings）。
# 照抄 8192 的话，加上视觉 token（一页 256~1120 个）必然越界 ——
# **每个请求都 400**，而且错误信息里只说"上下文超了"，看不出是这里抄错了。
# 4096 对一页 markdown 绰绰有余（整页密排正文约 1000~1500 token，
# 带 grounding 标签也就 2000~3000）。
_DSOCR2_MAX_TOKENS = 4096


def dialect_of(options: dict) -> str:
    return str(options.get("dialect") or GENERIC_JSON).strip().lower()


def _dsocr2_body(options: dict) -> dict:
    """DeepSeek-OCR-2 请求体里除 model/messages 之外的部分。

    `skip_special_tokens: False` 是**这里最要命的一行**：OpenAI 接口缺省是 true，
    而 `<|ref|>` / `<|det|>` 就是特殊 token —— 不显式关掉的话，模型报出来的
    bbox 会在返回前被静默剥光，我们只会看到"每个块 bbox 都是 null"，
    没有任何报错。所以它**不接受注册表覆盖**：能配的东西迟早会有人配错，
    而配错的后果是出处功能整体失效且无人察觉。
    """
    return {
        "temperature": 0,
        "max_tokens": _capped_max_tokens(options),
        "skip_special_tokens": False,
        "include_stop_str_in_output": True,
        # vLLM 的自定义采样参数入口：原样落到 SamplingParams.extra_args，
        # 防重复 logits processor 从那里取值
        "vllm_xargs": {
            "ngram_size": int(options.get("ngram_size") or _DSOCR2_NGRAM_SIZE),
            "window_size": int(options.get("window_size") or _DSOCR2_WINDOW_SIZE),
            "whitelist_token_ids": list(options.get("table_token_ids")
                                        or _DSOCR2_TABLE_TOKEN_IDS),
        },
    }


def _capped_max_tokens(options: dict) -> int:
    """注册表可以把 max_tokens 调**小**，不能调大。

    调大的后果不是"慢一点"，是每个请求 400（prompt + max_tokens 越过
    max_model_len=8192，而一页视觉 token 就有 256~1120 个）。而 `_recognize_page`
    对单页失败是静默吞掉的，最终抛的是"模型不可达，或返回全为空" ——
    **一个配置错误被报成网络错误**，排查方向整个歪掉。
    所以在这里钳住，并把这件事说出来（不变式：任何降级都必须可见）。
    """
    want = int(options.get("max_tokens") or _DSOCR2_MAX_TOKENS)
    capped = max(1, min(want, _DSOCR2_MAX_TOKENS))
    if capped != want:
        # 两个方向都要挡：配大了每个请求 400（越过 max_model_len），
        # 配成 0/负数同样每个请求 400 —— 而单页失败是静默吞的，
        # 最终报的是"模型不可达，或返回全为空"，**配置错误伪装成网络错误**，
        # 正是这个函数存在的理由。
        logger.warning(
            "注册表把 max_tokens 配成了 %d，不在 [1, %d] 内，已钳到 %d",
            want, _DSOCR2_MAX_TOKENS, capped)
    return capped


def wants_grounding(dialect: str, options: dict) -> bool:
    """这次解析**要不要**模型报 bbox。

    只有 deepseek-ocr2 方言下的 `options.grounding`（缺省 true）说了算。
    单独抽出来是因为 `_engine_notes` 也要看它：没要过 grounding 的那一趟
    本来就不会有标签，拿"没有标签"去报警是纯误报。
    """
    return dialect == DEEPSEEK_OCR2 and bool(options.get("grounding", True))


def _request_shape(dialect: str, options: dict) -> tuple[str, dict]:
    """方言 -> (prompt, 请求体附加字段)。"""
    if dialect == DEEPSEEK_OCR2:
        prompt = (dsocr2.PROMPT if wants_grounding(dialect, options)
                  else dsocr2.PROMPT_FREE_OCR)
        return prompt, _dsocr2_body(options)
    return _PROMPT, {}


def _parse_blocks(raw: str) -> list[dict] | None:
    """从模型输出里取 blocks 数组。取不到返回 None -> 调用方退回整页纯文本。"""
    if not raw:
        return None
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
            return parsed["blocks"]
        if isinstance(parsed, list):
            return parsed
    return None


def _first_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def denormalize_bbox(raw, page_w: float, page_h: float) -> list[float] | None:
    """模型给的归一化 bbox -> 页面坐标。任何一处不对劲就返回 None。

    **宁可 None 也不要一个凑合的框**：契约里 bbox=None 是"不能裁剪"，
    下游会如实打降级标记；而一个错的框会裁出一张不相干的图，
    还带着"已验证"标记 —— 这是这个项目定义的最恶劣错误。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        values = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if any(v < 0 for v in values):
        return None
    scale = 1.0 if max(values) <= _UNIT_SCALE_MAX else 1000.0
    x0, y0, x1, y1 = (v / scale for v in values)
    if x1 <= x0 or y1 <= y0 or x1 > 1.001 or y1 > 1.001:
        return None
    return [round(x0 * page_w, 2), round(y0 * page_h, 2),
            round(x1 * page_w, 2), round(y1 * page_h, 2)]


def blocks_to_page(raw_blocks: list[dict], page_idx: int,
                   page_w: float, page_h: float) -> dict:
    """模型给的块 -> DDP-Layout 的一页。"""
    blocks = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        html = item.get("html")
        if not text and not html:
            continue
        block = {
            "type": layout.normalize_type(item.get("type")),
            "bbox": denormalize_bbox(item.get("bbox"), page_w, page_h),
            "lines": [{"spans": [{"content": line}]}
                      for line in text.splitlines() if line.strip()],
        }
        if html:
            # 表格结构走契约里的可选承诺字段。放进 span 的 html，与 mineru 同一个位置，
            # layout.table_html 因此不用为这个引擎写第二套取法
            block["lines"].append({"spans": [{"content": "", "html": str(html)}]})
        blocks.append(block)
    return {"page_idx": page_idx, "page_size": [page_w, page_h], "para_blocks": blocks}


def plain_text_page(text: str, page_idx: int, page_w: float, page_h: float) -> dict:
    """模型没吐出 JSON 时的兜底：整页当一个文本块，**bbox 留空**。

    留空是刻意的：这时我们确实不知道文字在哪。编一个整页的框会让每条出处
    都"命中"整页，指标上好看、实际毫无定位价值，而且用户点开会看到一整页图。
    """
    return {
        "page_idx": page_idx,
        "page_size": [page_w, page_h],
        "para_blocks": [{
            "type": "text",
            "bbox": None,
            "lines": [{"spans": [{"content": line}]}
                      for line in (text or "").splitlines() if line.strip()],
        }],
    }


async def recognize(http: httpx.AsyncClient, *, endpoint: str, model: str,
                    pdf_bytes: bytes, options: dict) -> dict:
    """整份 PDF -> layout_json（DDP-Layout v1）。"""
    scale = float(options.get("scale") or crops.RENDER_SCALE)
    max_pages = int(options.get("max_pages") or 0)
    concurrency = int(options.get("concurrency") or 2)

    sizes = crops.page_sizes(pdf_bytes)
    if not sizes:
        raise RuntimeError("vlm-ocr 引擎只处理 PDF；这份文件读不出页面")
    if max_pages:
        sizes = sizes[:max_pages]

    dialect = dialect_of(options)
    prompt, extra = _request_shape(dialect, options)
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def one(page_idx: int, size: tuple[float, float]) -> tuple[dict, bool]:
        async with semaphore:
            return await _recognize_page(http, endpoint, model, pdf_bytes,
                                         page_idx, size, scale,
                                         dialect=dialect, prompt=prompt, extra=extra)

    results = await asyncio.gather(*(one(i, s) for i, s in enumerate(sizes)))
    pages = [page for page, _ in results]
    if not any(page["para_blocks"] for page in pages):
        # 一页都没识别出来。**不返回空版面** —— 那会让下游以为"解析成功了，
        # 只是这份文档恰好没内容"，正是这个项目最忌讳的静默降级
        raise RuntimeError(
            "vlm-ocr 引擎没有从任何一页识别出内容（模型不可达，或返回全为空）")

    built = layout.build_pages(
        pages, engine="vlm-ocr",
        # 通用 VLM prompt 明确要求输出 code；OCR-2 官方方言只抄写/grounding，
        # 没有块类型能力，不能因为“它也是 VLM”就谎报 native。
        code_detection=("native" if dialect == GENERIC_JSON else "unavailable"),
    )
    notes = _engine_notes(dialect, options, results)
    if notes:
        built[layout.ENGINE_NOTES] = notes
    return built


def _engine_notes(dialect: str, options: dict,
                  results: list[tuple[dict, bool]]) -> list[str]:
    """把"识别出来了但明显不对劲"的情况记进版面 JSON，别让它烂在日志里。

    只有一种情况：**要过 grounding**、每一页都识别出了文字、
    却一个 grounding 标签都没有。这几乎只可能是
    `skip_special_tokens` 没生效（被中间的 OpenAI 代理吞了参数，
    或者上游根本不是 vLLM）—— 结果是所有 bbox 全为 null，
    功能上等于出处定位整体失效，而现有的每一条路径都不会报错。

    "要过 grounding"这个前提**不能省**：`options.grounding: false` 是受支持的用法
    （走官方 `Free OCR.`，全页一个块、本来就没有 bbox），那一趟压根没要过标签。
    不判这一条的话每次解析都会往归档的版面里写一条"静默失效"告警 ——
    **狼来了会毁掉这个信号本身**，而它是不变式 2 在这条链路上的唯一落点。

    不抛异常：文字是真识别出来的，整份作废太狠。
    但**必须留痕** —— engine_notes 会跟着 layout_json 一起归档，
    排查时一眼可见；同时打一条 warning 给正在看日志的人。
    """
    if not wants_grounding(dialect, options):
        return []
    if not any(page["para_blocks"] for page, _ in results):
        return []
    if any(grounded for _, grounded in results):
        return []
    message = (
        "dsocr2_no_grounding: 走 deepseek-ocr2 方言但没有任何一页返回 grounding 标签，"
        "所有 bbox 都会是 null。最常见的原因是 skip_special_tokens 没生效"
        "（上游不是 vLLM，或中间的 OpenAI 代理丢弃了非标准字段）")
    logger.warning(message)
    return [message]


async def _recognize_page(http: httpx.AsyncClient, endpoint: str, model: str,
                          pdf_bytes: bytes, page_idx: int,
                          size: tuple[float, float], scale: float, *,
                          dialect: str, prompt: str, extra: dict) -> tuple[dict, bool]:
    """一页 -> (DDP-Layout 的一页, 是否走通了 grounding)。

    第二个返回值只对 deepseek-ocr2 方言有意义，用来在 recognize 里
    识别"标签被上游吃掉了"这种静默失效（见 _engine_notes）。
    """
    page_w, page_h = size
    empty = {"page_idx": page_idx, "page_size": [page_w, page_h], "para_blocks": []}
    png = await asyncio.to_thread(crops.render_page, pdf_bytes, page_idx, scale)
    if png is None:
        return empty, False

    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            # 顺序有讲究：图片在前、文字在后。vLLM 按部件出现的位置插入 `<image>`
            # 占位符，这个顺序渲染出来才是官方那句 `<image>\n<|grounding|>…`
            {"type": "image_url", "image_url": {"url": uri}},
            {"type": "text", "text": prompt},
        ]}],
        "stream": False,
        **extra,
    }
    try:
        resp = await http.post(f"{endpoint}/v1/chat/completions", json=body)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        # 单页失败不拖垮整份文档：出一页空的，最后由 recognize 判断是不是全空
        return empty, False

    if dialect == DEEPSEEK_OCR2:
        page = dsocr2.page_from_output(raw, page_idx, page_w, page_h)
        if page is not None:
            return page, True
        # 没有 grounding 标签：退回整页纯文本，但**先把标签残渣去掉**
        # （一个都没匹配上时 strip_tags 是空操作，这里是为了兜住"只有半个标签"的情况）
        return plain_text_page(dsocr2.strip_tags(raw), page_idx, page_w, page_h), False

    parsed = _parse_blocks(raw)
    if parsed is None:
        return plain_text_page(raw, page_idx, page_w, page_h), False
    return blocks_to_page(parsed, page_idx, page_w, page_h), False


def to_markdown(layout_json: dict) -> str:
    """layout_json -> markdown。标题加 #，表格用 HTML 原样嵌入。"""
    lines: list[str] = []
    for page in layout_json.get("pdf_info") or []:
        for block in page.get("para_blocks") or []:
            html = layout.table_html(block)
            if html:
                lines.append(html)
                continue
            text = layout.block_text(block)
            if not text:
                continue
            lines.append(f"## {text}" if block.get("type") == "title" else text)
    return "\n\n".join(lines)
