"""MCP 平面 —— 单一复合工具 ask_document（决策 #7）。

传输：Streamable HTTP。对外经 DeepDocParse-Web/backend 代理（方案 A，key 鉴权在 backend），
本服务只对内网开放，调 gateway 时带 SERVICE_TOKEN。

设计要点：
- 工具越少、描述越清晰，agent 选择准确率越高 -> 只此一个工具
- 大文档解析耗时 -> "解析中即返回 + 请稍后重试"模式，不阻塞 MCP 同步调用
- 返回"证据 + 出处（页码/bbox）"而非只有结论
- v1 检索 = BM25（中文按二元组、英文按词切分）；v2 换向量检索 ——
  只改内部实现，工具签名永不变（铁律 6）
"""
import base64
import io
import os
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
import pypdfium2 as pdfium
from fastmcp import FastMCP
from rank_bm25 import BM25Okapi

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:9000")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "change-me")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
SHORT_DOC_CHARS = 3000   # 短于此的文档直接全文作证据
TOP_K = 3                # BM25 命中块数
CROP_MARGIN = 12         # bbox 裁剪外扩（页面坐标单位）
RENDER_SCALE = 2.0       # PDF 渲染倍率（72dpi 基准 x2 = 144dpi）

mcp = FastMCP("DeepDocParse")

_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), trust_env=False)


def _headers() -> dict:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


def _tokenize(text: str) -> list[str]:
    """中英混排的轻量切分：英文/数字按词，CJK 按二元组（无分词依赖）。"""
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    cjk = re.findall(r"[一-鿿]", text)
    tokens += ["".join(p) for p in zip(cjk, cjk[1:])] or cjk[:1]
    return tokens


def _layout_blocks(layout_json: dict) -> list[dict]:
    """middle_json -> [{text, page_idx, bbox, page_size}]，检索与出处的统一数据源。"""
    blocks = []
    for page in layout_json.get("pdf_info", []):
        for blk in page.get("para_blocks", []):
            spans_text = []
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("content"):
                        spans_text.append(span["content"])
            text = " ".join(spans_text).strip()
            if text:
                blocks.append({
                    "text": text,
                    "page_idx": page.get("page_idx", 0),
                    "bbox": blk.get("bbox"),
                    "page_size": page.get("page_size"),
                })
    return blocks


async def _vqa(image_data_uri: str, question: str) -> str:
    """经 gateway 调 VQA 运行时（模型走注册表 default）。"""
    resp = await _http.post(
        f"{GATEWAY}/v1/chat/completions",
        headers=_headers(),
        json={
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": question},
            ]}],
            "stream": False,
        },
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _crop_page_region(pdf_bytes: bytes, page_idx: int, bbox: list,
                            page_size: list) -> str | None:
    """按 layout bbox 裁剪 PDF 页面区域，返回 PNG data URI；失败返回 None。"""
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page = doc[page_idx]
            bitmap = page.render(scale=RENDER_SCALE)
            img = bitmap.to_pil()
            # bbox 坐标基于 middle_json 的 page_size，换算到渲染像素
            sx = img.width / (page_size[0] if page_size else page.get_width())
            sy = img.height / (page_size[1] if page_size else page.get_height())
            x0, y0, x1, y1 = bbox
            box = (max(0, int((x0 - CROP_MARGIN) * sx)), max(0, int((y0 - CROP_MARGIN) * sy)),
                   min(img.width, int((x1 + CROP_MARGIN) * sx)),
                   min(img.height, int((y1 + CROP_MARGIN) * sy)))
            region = img.crop(box)
            buf = io.BytesIO()
            region.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        finally:
            doc.close()
    except Exception:
        return None  # 裁剪属增强路径，失败不阻断文本证据返回


@mcp.tool()
async def ask_document(file_url: str, question: str) -> str:
    """对文档或图片提问，返回带出处的答案。

    支持 PDF/DOCX/PPTX/XLSX/图片的 URL。首次询问大文档时会触发解析，
    若返回"解析中"，请稍后用相同参数重试。
    """
    ext = PurePosixPath(urlparse(file_url).path).suffix.lower()

    # ---- 图片：直接走 VQA，秒回 ----
    if ext in IMAGE_EXTS:
        img_resp = await _http.get(file_url, follow_redirects=True)
        img_resp.raise_for_status()
        mime = img_resp.headers.get("content-type", f"image/{ext.lstrip('.')}")
        data_uri = f"data:{mime};base64," + base64.b64encode(img_resp.content).decode()
        answer = await _vqa(data_uri, question)
        return f"{answer}\n\n---\n出处：整张图片（{file_url}）"

    # ---- 文档：提交解析（gateway 按 file_url 哈希幂等，重复调用复用任务）----
    submit = await _http.post(f"{GATEWAY}/v1/parse", headers=_headers(),
                              json={"file_url": file_url})
    if submit.status_code == 429:
        return "解析队列已满，请稍后用相同参数重试。"
    submit.raise_for_status()
    task_id = submit.json()["task_id"]

    status_resp = await _http.get(f"{GATEWAY}/v1/parse/{task_id}", headers=_headers())
    status_resp.raise_for_status()
    status = status_resp.json()
    if status["status"] == "failed":
        return f"文档解析失败：{status.get('error') or '未知原因'}。请检查文件是否有效。"
    if status["status"] != "succeeded":
        return (f"文档正在解析中（任务 {task_id}，状态 {status['status']}），"
                "请稍后用完全相同的参数重试本工具。")

    result_resp = await _http.get(f"{GATEWAY}/v1/parse/{task_id}/result", headers=_headers())
    if result_resp.status_code == 409:  # 兜底：极小窗口内结果尚未归档完成
        return (f"文档解析已完成，结果归档中（任务 {task_id}），"
                "请稍后用完全相同的参数重试本工具。")
    result_resp.raise_for_status()
    result = result_resp.json()
    markdown: str = result.get("markdown", "")
    result_url = f"{GATEWAY}/v1/parse/{task_id}/result"

    # ---- 短文档：全文即证据，agent 自己的 LLM 综合 ----
    if len(markdown) <= SHORT_DOC_CHARS:
        return (f"文档全文（较短，直接给出）：\n\n{markdown}\n\n---\n"
                f"出处：{file_url} 全文；完整结果（markdown/版面/图片）：GET {result_url}")

    # ---- 长文档：BM25 检索版面块（自带页码+bbox）----
    blocks = _layout_blocks(result.get("layout_json", {}))
    # 空 token 块不入索引：_tokenize 只认英数+CJK，其他文种/全符号块会为空，
    # 全空语料会让 BM25Okapi 除零崩溃（M3 验收回归项）
    indexed = [(tokens, b) for b in blocks if (tokens := _tokenize(b["text"]))]
    if not indexed:
        return (f"文档已解析但无可检索文本块，返回开头片段：\n\n{markdown[:SHORT_DOC_CHARS]}\n\n---\n"
                f"完整结果：GET {result_url}")

    bm25 = BM25Okapi([tokens for tokens, _ in indexed])
    candidates = [b for _, b in indexed]
    scores = bm25.get_scores(_tokenize(question))
    top = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)[:TOP_K]
    hits = [candidates[i] for i in top if scores[i] > 0] or [candidates[top[0]]]

    # ---- 首个命中块：裁剪原图区域 -> VQA 针对性验证（仅 PDF 支持裁剪）----
    answer = None
    if ext == ".pdf" and hits[0].get("bbox"):
        try:
            pdf_resp = await _http.get(file_url, follow_redirects=True)
            pdf_resp.raise_for_status()
            data_uri = await _crop_page_region(
                pdf_resp.content, hits[0]["page_idx"], hits[0]["bbox"], hits[0].get("page_size"))
            if data_uri:
                answer = await _vqa(
                    data_uri, f"请仅根据这张文档区域截图回答：{question}")
        except httpx.HTTPError:
            pass  # 原文件不可达时退化为纯文本证据

    evidence = "\n\n".join(
        f"[第 {h['page_idx'] + 1} 页 bbox={h['bbox']}] {h['text']}" for h in hits)
    parts = []
    if answer:
        parts.append(f"答案（已对第 {hits[0]['page_idx'] + 1} 页命中区域做视觉验证）：{answer}")
    parts.append(f"相关证据：\n{evidence}")
    parts.append(f"完整结果（markdown/版面/图片）：GET {result_url}")
    return "\n\n---\n\n".join(parts)


if __name__ == "__main__":
    # Streamable HTTP，供 backend 反向代理
    mcp.run(transport="http", host="0.0.0.0", port=9100)
