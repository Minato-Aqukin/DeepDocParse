"""真环境 e2e：MCP 客户端 -> mcp_server -> gateway -> mineru / VQA / TEI 全链路。

覆盖 ask_document 的三条路径 + v2 向量索引物证：
  1. 图片 URL        -> 直接 VQA，不碰解析平面
  2. 文档首次询问     -> 触发解析，返回"解析中"，重试后得到带出处的答案
  3. 长文档跨页事实   -> 检索定位到正确页码（v2 走向量检索，未配 embedding 则 BM25 兜底）
  4. REDIS_URL 已配时 -> 断言 worker 确实写入了 chunk 向量、FT 索引存在

前置（宿主机混合模式，见 ../CLAUDE.md「dev 环境陷阱」）：
  redis-stack / mineru / TEI 用容器；gateway、arq worker、mcp_server 用 venv 起；
  fixtures 用 `python -m http.server 18081 --directory tests/fixtures` 暴露。

用法：
  python scripts/e2e_mcp.py                    # 全部场景
  python scripts/e2e_mcp.py --skip-image       # 跳过图片（VQA 未启动时）
环境变量：MCP_URL / FIXTURE_BASE / REDIS_URL
"""
import argparse
import asyncio
import hashlib
import os
import sys

from fastmcp import Client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:9100/mcp")
FIXTURE_BASE = os.environ.get("FIXTURE_BASE", "http://127.0.0.1:18081")
REDIS_URL = os.environ.get("REDIS_URL", "")

RETRY_LIMIT = 60        # 最多等 60 * 5s = 5 分钟解析
RETRY_INTERVAL = 5
INDEX_WAIT_LIMIT = 24   # 分块索引最多再等 24 * 5s = 2 分钟
PENDING_MARKERS = ("解析中", "归档中")


def text_of(result) -> str:
    return "\n".join(getattr(b, "text", "") for b in (getattr(result, "content", None) or []))


async def ask_until_ready(client: Client, file_url: str, question: str) -> str:
    """按 ask_document 的契约重试：同参数轮询直到不再返回"解析中"。"""
    out = text_of(await client.call_tool("ask_document",
                                         {"file_url": file_url, "question": question}))
    first = out
    for i in range(RETRY_LIMIT):
        if not any(m in out for m in PENDING_MARKERS):
            return out
        await asyncio.sleep(RETRY_INTERVAL)
        out = text_of(await client.call_tool("ask_document",
                                             {"file_url": file_url, "question": question}))
        print(f"    retry {i + 1}: {'等待中' if any(m in out for m in PENDING_MARKERS) else '完成'}")
    raise AssertionError(f"解析未在 {RETRY_LIMIT * RETRY_INTERVAL}s 内完成，首次返回：{first[:200]}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    return condition


def _redis():
    import redis.asyncio as redis

    return redis.from_url(REDIS_URL)


def doc_hash_of(file_url: str) -> str:
    """必须与 gateway parse.py / mcp_server 的算法一致。"""
    return hashlib.sha256(file_url.encode()).hexdigest()


async def wait_for_index(file_url: str) -> int | None:
    """等 chunk_and_index 把分块写入（最终一致）。未配 REDIS_URL 返回 None。"""
    if not REDIS_URL:
        return None
    r = _redis()
    try:
        for _ in range(INDEX_WAIT_LIMIT):
            keys = await r.keys(f"chunk:{doc_hash_of(file_url)}:*")
            if keys:
                return len(keys)
            await asyncio.sleep(RETRY_INTERVAL)
        return 0
    finally:
        await r.aclose()


async def retrieval_counters() -> dict[str, int]:
    """读 mcp_server 记录的检索路径计数（vector / bm25），用于判别真实走了哪条路。"""
    if not REDIS_URL:
        return {}
    r = _redis()
    try:
        out = {}
        for mode in ("vector", "bm25"):
            raw = await r.get(f"metrics:retrieval:{mode}")
            out[mode] = int(raw) if raw else 0
        return out
    finally:
        await r.aclose()


async def main(skip_image: bool) -> int:
    ok = True
    async with Client(MCP_URL) as client:
        tools = [t.name for t in await client.list_tools()]
        print("== 工具清单 ==")
        ok &= check("只暴露 ask_document（铁律 6）", tools == ["ask_document"], str(tools))

        if not skip_image:
            print("\n== 场景 1：图片直答 ==")
            out = text_of(await client.call_tool(
                "ask_document", {"file_url": f"{FIXTURE_BASE}/vqa-test.png",
                                 "question": "Read all text in this image."}))
            ok &= check("VQA 读出图中数字 42", "42" in out, out[:120].replace("\n", " "))
            ok &= check("返回出处", "出处" in out)

        print("\n== 场景 2：长文档跨页事实检索 ==")
        pdf = f"{FIXTURE_BASE}/long-doc.pdf"
        out = await ask_until_ready(client, pdf, "What is the launch code of project Zephyr?")
        ok &= check("检索到埋在第 3 页的 launch code 8712", "8712" in out,
                    out[:160].replace("\n", " "))
        ok &= check("出处页码正确（第 3 页）", "第 3 页" in out)

        # 分块索引是归档后的独立后续任务，上面那问几乎必然跑在 BM25 上；
        # 下面的向量检索断言必须等索引真正就绪后再发问，否则测不到 v2 路径
        indexed = await wait_for_index(pdf)
        if indexed is None:
            print("  (未设 REDIS_URL，跳过向量检索判别)")
        else:
            ok &= check("分块索引已就绪", indexed > 0, f"{indexed} 个 chunk")

            print("\n== 场景 3：向量检索判别（语义化提问，与原文无词面重叠）==")
            before = await retrieval_counters()
            # 原文是 "The annual revenue of Acme Corp reached 42 million dollars in 2025."
            # 提问与原文**零词面重叠**（连 "the" 都不能有——它在埋点句里出现，
            # 会让 BM25 把第 3/5 页一起捞进证据串从而蒙对页码）。
            # 零重叠时 BM25 全零分只会回落到第 1 页首块，唯有语义向量能定位第 5 页。
            out2 = await ask_until_ready(client, pdf, "How much did they earn?")
            after = await retrieval_counters()
            used_vector = after.get("vector", 0) > before.get("vector", 0)
            ok &= check("本次确实走了向量检索（而非 BM25 兜底）", used_vector,
                        f"vector {before.get('vector', 0)}→{after.get('vector', 0)}, "
                        f"bm25 {before.get('bm25', 0)}→{after.get('bm25', 0)}")
            ok &= check("语义提问定位到第 5 页的金额事实",
                        "42" in out2 and "第 5 页" in out2, out2[:160].replace("\n", " "))

    if REDIS_URL:
        print("\n== v2 向量索引物证 ==")
        import redis.asyncio as redis

        doc_hash = hashlib.sha256(f"{FIXTURE_BASE}/long-doc.pdf".encode()).hexdigest()
        r = redis.from_url(REDIS_URL)
        try:
            keys = await r.keys(f"chunk:{doc_hash}:*")
            if keys:
                ttl = await r.ttl(keys[0])
                ok &= check("chunk 带 TTL（可重建缓存，铁律 5）", ttl > 0, f"TTL={ttl}s")
                fields = await r.hgetall(keys[0])
                fields = {(k.decode() if isinstance(k, bytes) else k) for k in fields}
                ok &= check("chunk 存了裁剪所需的 page_size", "page_size" in fields,
                            str(sorted(fields)))
            names = await r.execute_command("FT._LIST")
            names = [n.decode() if isinstance(n, bytes) else n for n in names]
            hit = [n for n in names if n.startswith("chunks_idx_d")]
            ok &= check("FT 向量索引已建立（名字带维度）", bool(hit), str(names))
            if hit:
                info = await r.execute_command("FT.INFO", hit[0])
                meta = {(k.decode() if isinstance(k, bytes) else k): v
                        for k, v in zip(info[::2], info[1::2])}
                print(f"        {hit[0]} num_docs={meta.get('num_docs')}")
        finally:
            await r.aclose()
    else:
        print("\n(未设 REDIS_URL，跳过向量索引物证)")

    print("\n" + ("M4 E2E PASSED" if ok else "M4 E2E FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-image", action="store_true", help="跳过图片直答场景（VQA 未启动时）")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.skip_image)))
