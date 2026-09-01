"""标定视觉核对阈值（EXTRACT_MISMATCH_THRESHOLD / QA_PARSE_MISMATCH_THRESHOLD）。

    python scripts/calibrate_verify_threshold.py \
        --pdf tests/fixtures/contract.pdf \
        --endpoint http://127.0.0.1:18001 --model deepseek-ocr-2

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

- 一致组：block[i] 的图 vs block[i] 的文本。用 **borndigital** 引擎取版面 ——
  它的 bbox 是从 PDF 文字层直接算出来的**真坐标**，裁出来的图必然就是那块内容。
  于是这一组的比值只反映"模型抄写的保真度"，没有别的变量混进来。
- 不一致组：block[i] 的图 vs block[j] 的文本（j≠i）。这模拟"解析把内容搞错了"
  的情形 —— 正是这个阈值该抓住的那种错。

好的阈值应当落在两组分布之间。脚本会打印两组的分位数并给出建议值：
取「一致组的 5% 分位」与「不一致组的 95% 分位」的中点，
两组重叠时会明确说重叠（那意味着这个判据本身分不开，调阈值没用）。

**只读，不改任何配置**：它打印数字，改不改 .env 由人决定。
"""
import argparse
import asyncio
import base64
import difflib
import statistics
import sys
from pathlib import Path

import httpx

from ddp_gateway.services import borndigital, crops, extraction, layout

# **直接从 extraction 里取，不复制一份。**
# 标定的全部意义在于"复现线上那条路径"：prompt 换个说法、下限差两个字，
# 标出来的分布就不适用于线上了。抄一份放在这里迟早会漂移，
# 而漂移之后这个脚本会安静地给出一个错的建议值。
MIN_CHARS = extraction._MIN_TRANSCRIPT_CHARS
comparable = extraction._comparable


def transcribe_prompt_for(models_config: str | None) -> str:
    """该用哪句话去问模型"把字抄出来" —— **从注册表读，与线上一致**。

    OCR 专用模型只认自己的官方 prompt；拿缺省那句中文指令去问 DeepSeek-OCR-2，
    它会回应那句指令而不是抄写（真机实测），标出来的分布就毫无意义。
    注册表里用 `options.transcribe_prompt` 声明，这里照读。
    """
    if not models_config:
        return extraction._TRANSCRIBE_PROMPT
    from ddp_gateway.config import load_registry
    registry = load_registry(Path(models_config))
    if not registry.vqa_models:
        return extraction._TRANSCRIBE_PROMPT
    _, entry = registry.default_of(registry.vqa_models)
    return str((entry.options or {}).get("transcribe_prompt")
               or extraction._TRANSCRIBE_PROMPT)


def ratio(left: str, right: str) -> float:
    # autojunk=False：默认启发式会把中文高频字当垃圾忽略，一致度被压低
    return difflib.SequenceMatcher(None, comparable(left), comparable(right),
                                   autojunk=False).ratio()


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
    ap.add_argument("--models-config", default=None,
                    help="注册表路径。给了就按它取 transcribe_prompt（与线上一致）")
    args = ap.parse_args()

    prompt = transcribe_prompt_for(args.models_config)
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
            if block.get("bbox") and len(comparable(text)) >= MIN_CHARS:
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

    matched: list[float] = []
    mismatched: list[float] = []
    for i, got in enumerate(transcripts):
        if got is None or len(comparable(got)) < MIN_CHARS:
            continue
        matched.append(ratio(got, items[i][3]))
        # 不一致组：拿同一张图去和**别的块**的文本比
        for j, other in enumerate(items):
            if j != i and len(comparable(other[3])) >= MIN_CHARS:
                mismatched.append(ratio(got, other[3]))

    print("\n" + "=" * 68)
    print("一致组   （块图 vs 自己的文本，应当高）:", percentiles(matched))
    print("不一致组 （块图 vs 别人的文本，应当低）:", percentiles(mismatched))
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
