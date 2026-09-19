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

import pytest

from ddp_core import crops
from ddp_paths import FIXTURES

FIXTURE = FIXTURES / "long-doc.pdf"


# 整页、批量裁图、尺寸读取交错运行，覆盖所有自己打开 PDF 的入口。
_CONCURRENT_RENDER = """
import asyncio, pathlib
from ddp_core import crops

pdf = pathlib.Path({fixture!r}).read_bytes()

async def main():
    out = await asyncio.gather(
        *(asyncio.to_thread(crops.render_page, pdf, i, 2.0) for i in range(5)),
        asyncio.to_thread(crops.render_crops, pdf,
                          [(i, [0, 0, 100, 100], [612, 792]) for i in range(5)]),
        asyncio.to_thread(crops.page_sizes, pdf),
    )
    signature = bytes((137, 80, 78, 71))
    assert all(p and p.startswith(signature) for p in out[:5])
    assert len(out[5]) == 5 and all(p and p.startswith(signature) for p in out[5])
    assert out[6] == [(612.0, 792.0)] * 5

asyncio.run(main())
"""


@pytest.mark.skipif(not FIXTURE.exists(), reason="缺少 long-doc.pdf 夹具")
def test_concurrent_render_page_does_not_segfault():
    """并发渲染整页不能把进程搞崩。去掉 crops 里的串行化时这条必红（returncode -11）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _CONCURRENT_RENDER.format(fixture=str(FIXTURE))],
        capture_output=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"并发渲染子进程异常退出 returncode={proc.returncode}"
        f"（-11 即 SIGSEGV，说明 PDFium 调用没有串行化）\n"
        f"stderr: {proc.stderr.decode(errors='replace')[-2000:]}"
    )



# ---- 渲染依赖缺失必须炸，不许伪装成"这页裁不出来" ----

@pytest.mark.parametrize("dependency", ["pypdfium2", "PIL"])
def test_missing_render_dependency_raises_instead_of_returning_none(dependency):
    # 独立进程阻止已缓存的 Pillow import 掩盖真实的依赖缺失。
    program = """
import sys
from pathlib import Path
from ddp_core import crops

sys.modules[sys.argv[2]] = None
pdf = Path(sys.argv[1]).read_bytes()
calls = [
    lambda: crops.render_crops(pdf, [(0, [0, 0, 100, 100], [612, 792])]),
    lambda: crops.render_page(pdf, 0),
]
if sys.argv[2] == "pypdfium2":
    calls.append(lambda: crops.page_sizes(pdf))
for call in calls:
    try:
        call()
    except ImportError:
        continue
    raise AssertionError("missing rendering dependency was reported as a bad page")
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(FIXTURE), dependency],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_genuine_render_failure_still_degrades_quietly():
    """反哨兵：真正裁不出来的时候仍然返回 None，不能改成抛异常。

    上一条只放行 ImportError。要是有人把 `except Exception` 一起删掉，
    畸形 PDF 就会把整条抽取链打断 —— 而裁剪本来是增强路径。
    """
    assert crops.render_page(b"not a pdf at all", 0) is None
    assert crops.page_sizes(b"not a pdf at all") == []
    assert crops.render_crops(b"not a pdf at all", [(0, [0, 0, 1, 1], None)]) == [None]
