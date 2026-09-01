"""守卫：PDFium 的三个入口必须串行化。

**为什么需要这条守卫**：PDFium 不是线程安全的，而 `vlm_ocr.py` 会对整篇文档
的每一页 `asyncio.gather` + `to_thread` 并发渲染。少了串行化，两个线程同时开
文档就段错误 —— 段错误杀的是整个 arq worker 进程：没有 traceback、解析任务
永远停在 pending、界面上表现为"一直在解析"。2026-09-01 在 4090D 上必现
（5 页文档串行渲染全好，并发 100% core dump）。

段错误会带走整个 pytest 进程，所以这条守卫必须在**子进程**里跑，
才能把崩溃变成一条干净的 FAIL 而不是让整轮测试没有结果。
"""
import subprocess
import sys
from pathlib import Path

import pytest

from ddp_core import crops

FIXTURE = Path(__file__).parent / "fixtures" / "long-doc.pdf"
GATEWAY = Path(__file__).resolve().parents[1] / "gateway"

# 与 vlm_ocr.py 里那次 gather 同形状：每页一个线程，同时开同一份 PDF。
_CONCURRENT_RENDER = """
import asyncio, pathlib, sys
sys.path.insert(0, {gateway!r})
from ddp_core import crops

pdf = pathlib.Path({fixture!r}).read_bytes()

async def main():
    out = await asyncio.gather(*(
        asyncio.to_thread(crops.render_page, pdf, i, 2.0) for i in range(5)
    ))
    assert all(p for p in out), "并发渲染有页面返回空"

asyncio.run(main())
"""


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺少 long-doc.pdf 夹具")
def test_concurrent_render_page_does_not_segfault():
    """并发渲染整页不能把进程搞崩。去掉 crops 里的串行化时这条必红（returncode -11）。"""
    proc = subprocess.run(
        [sys.executable, "-c",
         _CONCURRENT_RENDER.format(gateway=str(GATEWAY), fixture=str(FIXTURE))],
        capture_output=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"并发渲染子进程异常退出 returncode={proc.returncode}"
        f"（-11 即 SIGSEGV，说明 PDFium 调用没有串行化）\n"
        f"stderr: {proc.stderr.decode(errors='replace')[-2000:]}"
    )


@pytest.mark.parametrize("name", ["render_crops", "render_page", "page_sizes"])
def test_pdfium_entrypoints_are_serialized(name):
    """三个自己开 PdfDocument 的入口都必须被 _pdfium_serialized 包住。"""
    fn = getattr(crops, name)
    assert hasattr(fn, "__wrapped__"), f"{name} 没有被 _pdfium_serialized 装饰"


def test_render_crop_is_not_double_wrapped():
    """render_crop 只是委托给 render_crops，自己不开文档，不该再包一层。"""
    assert not hasattr(crops.render_crop, "__wrapped__"), \
        "render_crop 被重复装饰了 —— 它会再进 render_crops"
