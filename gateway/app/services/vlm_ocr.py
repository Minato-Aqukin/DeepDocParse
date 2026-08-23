"""vlm-ocr 引擎：用视觉语言模型做整页识别 —— 「基于大语言模型的文字识别」这条线的落点。

在它之前，三个解析引擎没有一个是 LLM 在做识别：
  mineru pipeline  专用小模型流水线（layout / OCR / 表格各一个）
  borndigital      纯 pypdfium2，零模型
  DeepSeek-OCR     只用在**下游核对**，从没当过解析引擎
这个引擎把 VQA 平面那个模型当整页识别器用：页图 -> VLM -> DDP-Layout。

**它不新增任何接缝**：normalizer 层（B1）与「加引擎 = 加容器 + 一行配置」（B2）
早就在了，这里只是第三个 normalizer。models.yaml 里长这样：

    parse_engines:
      vlm-ocr:
        endpoint: "http://vqa-dsocr:8000"    # 就是 VQA 那个容器，不用多起一个
        runtime: vlm-ocr
        options: { model: deepseek-ocr, scale: 2.0 }

## 已知代价（写在这儿，免得被当成 bug 反复排查）

- **bbox 靠模型自己报，精度远不如 mineru 的版面模型。** 报不出来就落 `bbox: null`，
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
import re

import httpx

from app.services import crops, layout

# 让模型输出结构化版面。要点：
# 1. 明确 bbox 的坐标系与量纲（0~1000 归一化），否则每个模型一套自己的约定
# 2. **允许它说"这块我定不出位置"**（bbox: null）—— 不给这个出口，模型就会编一个
# 3. 块类型直接用契约词汇表，省掉一层映射
_PROMPT = """把这一页的内容识别成结构化版面，输出**纯 JSON**，不要任何解释文字。

格式：
{"blocks": [{"type": "...", "bbox": [x0, y0, x1, y1], "text": "..."}]}

规则：
- type 只能是：text（正文段落）、title（标题）、table（表格）、figure（图片）、
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

    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def one(page_idx: int, size: tuple[float, float]) -> dict:
        async with semaphore:
            return await _recognize_page(http, endpoint, model, pdf_bytes,
                                         page_idx, size, scale)

    pages = await asyncio.gather(*(one(i, s) for i, s in enumerate(sizes)))
    if not any(page["para_blocks"] for page in pages):
        # 一页都没识别出来。**不返回空版面** —— 那会让下游以为"解析成功了，
        # 只是这份文档恰好没内容"，正是这个项目最忌讳的静默降级
        raise RuntimeError(
            "vlm-ocr 引擎没有从任何一页识别出内容（模型不可达，或返回全为空）")
    return layout.build_pages(pages, engine="vlm-ocr")


async def _recognize_page(http: httpx.AsyncClient, endpoint: str, model: str,
                          pdf_bytes: bytes, page_idx: int,
                          size: tuple[float, float], scale: float) -> dict:
    page_w, page_h = size
    png = await asyncio.to_thread(crops.render_page, pdf_bytes, page_idx, scale)
    if png is None:
        return {"page_idx": page_idx, "page_size": [page_w, page_h], "para_blocks": []}

    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    try:
        resp = await http.post(f"{endpoint}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": _PROMPT},
            ]}],
            "stream": False,
        })
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        # 单页失败不拖垮整份文档：出一页空的，最后由 recognize 判断是不是全空
        return {"page_idx": page_idx, "page_size": [page_w, page_h], "para_blocks": []}

    parsed = _parse_blocks(raw)
    if parsed is None:
        return plain_text_page(raw, page_idx, page_w, page_h)
    return blocks_to_page(parsed, page_idx, page_w, page_h)


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
