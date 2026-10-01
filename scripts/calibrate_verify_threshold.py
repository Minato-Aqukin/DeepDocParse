"""标定视觉核对阈值（EXTRACT_MISMATCH_THRESHOLD / QA_PARSE_MISMATCH_THRESHOLD）。

    python scripts/calibrate_verify_threshold.py \
        --pdf tests/fixtures/contract.pdf \
        --endpoint http://127.0.0.1:18001 --model deepseek-ocr-2 --prompt 'Free OCR.'

## 这个阈值是干什么的

出处核对：把块的 bbox 裁成一张图，让视觉模型**原样抄一遍**，
再和解析出来的块文本比一致度（difflib ratio）。低于阈值就打 `parse_mismatch`，
意思是"这块的解析结果可疑"。

这两个值原来都是没有依据的 0.35。2026-08-25 用本脚本在 4090D + DeepSeek-OCR-2 上
标过一次（一致组全部 1.000、不一致组 p95 0.382/max 0.643），已改为 **0.55**。

**但那次的样本是 born-digital 英文单栏，是最容易的一类。**
换文档类型（扫描件、中文、多栏、表格密集）就该重标一次 —— 这个脚本就是干这个的。

## 怎么标

关键是要同时拿到**该判一致**和**该判不一致**两组样本：

- 自配对组：block[i] 的图 vs block[i] 的文字层；它只是候选正例，
  不等于人工真值（旋转文字、公式等可能是文字层自己错了），必须另做人工核查。
- 异文配对组：block[i] 的图 vs block[j] 的文本（j≠i），排除归一化后同文的
  配对。重复页眉／页脚不是负例；高相似但不同的文字仍保留，不按分数删难例。

好的阈值应当落在两组分布之间。脚本会打印两组的分位数并给出建议值：
取「一致组的 5% 分位」与「不一致组的 95% 分位」的中点，
两组重叠时会明确说重叠（那意味着这个判据本身分不开，调阈值没用）。

**只读，不改任何配置**：它打印数字，改不改 .env 由人决定。
"""
import argparse
import asyncio
import base64
import statistics
from pathlib import Path

import httpx

from ddp_core import crops
from ddp_core.verification import (
    MIN_TRANSCRIPT_CHARS, TRANSCRIBE_PROMPT, comparable, similarity,
)
from ddp_gateway.services import borndigital, extraction, layout


def transcribe_prompt_for(models_config: str, model: str) -> str:
    """Use the requested visual model's prompt, never another model's default."""
    from ddp_gateway.config import load_registry

    registry = load_registry(Path(models_config))
    entry = registry.vqa_models.get(model)
    if entry is None or "vision" not in (entry.capabilities or []):
        raise ValueError(f"{model!r} is not a registered vision model")
    return str((entry.options or {}).get("transcribe_prompt") or TRANSCRIBE_PROMPT)


def score_pairs(texts: list[str], transcripts: list[str | None]
                ) -> tuple[list[float], list[float], int]:
    """Return self/cross pairing scores and the number of excluded same-text pairs."""
    normalized = [comparable(text) for text in texts]
    matched, mismatched = [], []
    excluded = 0
    for i, (text, got) in enumerate(zip(texts, transcripts, strict=True)):
        if got is None or len(comparable(got)) < MIN_TRANSCRIPT_CHARS:
            continue
        matched.append(similarity(got, text))
        for j, other in enumerate(texts):
            if j == i or len(normalized[j]) < MIN_TRANSCRIPT_CHARS:
                continue
            if normalized[i] == normalized[j]:
                excluded += 1
                continue
            mismatched.append(similarity(got, other))
    return matched, mismatched, excluded


async def transcribe(http: httpx.AsyncClient, endpoint: str, model: str,
                     png: bytes, prompt: str) -> str | None:
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    try:
        resp = await http.post(f"{endpoint}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": prompt},
            ]}],
            "stream": False,
            "temperature": 0,
            "max_tokens": 2048,
        }, timeout=300)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception as exc:                      # noqa: BLE001
        print(f"    (抄写失败: {exc})")
        return None


def percentiles(values: list[float]) -> str:
    if not values:
        return "（无样本）"
    ordered = sorted(values)

    def q(p: float) -> float:
        idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
        return ordered[idx]

    return (f"n={len(ordered)} min={ordered[0]:.3f} p5={q(0.05):.3f} "
            f"p50={statistics.median(ordered):.3f} p95={q(0.95):.3f} max={ordered[-1]:.3f}")


def quantile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="用来标定的 PDF（要有文字层）")
    ap.add_argument("--endpoint", default="http://127.0.0.1:18001")
    ap.add_argument("--model", default="deepseek-ocr-2")
    ap.add_argument("--max-blocks", type=int, default=20, help="最多测几个块（省 GPU 时间）")
    prompt_args = ap.add_mutually_exclusive_group(required=True)
    prompt_args.add_argument("--models-config", help="注册表路径；按 --model 选择视觉模型的提示")
    prompt_args.add_argument("--prompt", help="直连运行时的显式抄写提示，不猜测模型专用提示")
    args = ap.parse_args()

    prompt = args.prompt or transcribe_prompt_for(args.models_config, args.model)
    print(f"抄写用的 prompt: {prompt!r}\n")

    pdf_bytes = Path(args.pdf).read_bytes()
    pages = borndigital.extract_pages(pdf_bytes)
    if not pages:
        print("这份 PDF 没有文字层，borndigital 取不到版面 —— 换一份有文字层的")
        return 2
    built = layout.build(pages, engine="borndigital")

    # 只要文字够长、bbox 齐全的块
    items: list[tuple[int, list[float], list[float], str]] = []
    for page in built["pdf_info"]:
        for block in page["para_blocks"]:
            text = layout.block_text(block)
            if block.get("bbox") and len(comparable(text)) >= MIN_TRANSCRIPT_CHARS:
                items.append((page["page_idx"], block["bbox"], page["page_size"], text))
    items = items[:args.max_blocks]
    print(f"取到 {len(items)} 个可用块（有 bbox、文字够长）\n")
    if len(items) < 3:
        print("样本太少，标不出分布 —— 换一份内容更多的 PDF")
        return 2

    async with httpx.AsyncClient(trust_env=False) as http:
        transcripts: list[str | None] = []
        for i, (page_idx, bbox, page_size, text) in enumerate(items):
            png = crops.render_crop(pdf_bytes, page_idx, bbox, page_size)
            if png is None:
                transcripts.append(None)
                print(f"  [{i}] 裁不出图，跳过")
                continue
            got = await transcribe(http, args.endpoint, args.model, png, prompt)
            transcripts.append(got)
            head = (got or "").strip().replace("\n", " ")[:50]
            print(f"  [{i}] p{page_idx} 原文={text[:28]!r} 抄写={head!r}")

    matched, mismatched, excluded = score_pairs([item[3] for item in items], transcripts)
    print(f"\n排除同文异块配对 {excluded} 对；自配对正例仍需人工核查文字层质量。")

    print("\n" + "=" * 68)
    print("自配对组（候选正例，不替代人工真值）:", percentiles(matched))
    print("异文配对组（排除同文块）:", percentiles(mismatched))
    print("=" * 68)

    if not matched or not mismatched:
        print("样本不足，标不出阈值")
        return 2

    low = quantile(matched, 0.05)        # 一致组的下沿
    high = quantile(mismatched, 0.95)    # 不一致组的上沿
    print(f"\n一致组下沿 p5  = {low:.3f}")
    print(f"不一致组上沿 p95 = {high:.3f}")

    if low <= high:
        print("\n**两组重叠**：这个判据在这份样本上分不开一致与不一致。")
        print("调阈值解决不了 —— 要么模型抄写保真度不够，要么裁图/比对方式要改。")
        print(f"若必须给一个值，取一致组 p5 = {low:.2f}（宁可漏报，不要误报）。")
        suggestion = low
    else:
        suggestion = (low + high) / 2
        print(f"\n建议阈值 = {suggestion:.2f}（两组之间的中点，留有余量）")

    # **读当前配置值，别把数字写死** —— 写死的话阈值一改，这段输出就开始撒谎，
    # 而它正是用来判断"要不要改阈值"的依据
    current = extraction.settings.extract_mismatch_threshold
    print(f"\n当前生效的阈值是 {current}。按这份样本：")
    fp = sum(1 for r in mismatched if r >= current)
    fn = sum(1 for r in matched if r < current)
    print(f"  用 {current}：会把 {fp}/{len(mismatched)} 个**该报的不一致**放过去，"
          f"把 {fn}/{len(matched)} 个正常块误报成 parse_mismatch")
    fp2 = sum(1 for r in mismatched if r >= suggestion)
    fn2 = sum(1 for r in matched if r < suggestion)
    print(f"  用 {suggestion:.2f}：放过 {fp2}/{len(mismatched)}，误报 {fn2}/{len(matched)}")
    print("\n改的是两个配置项：EXTRACT_MISMATCH_THRESHOLD / QA_PARSE_MISMATCH_THRESHOLD")
    print("（本脚本只出数字，不动任何配置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
