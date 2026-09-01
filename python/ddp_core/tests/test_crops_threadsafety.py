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


# 与 vlm_ocr.py 里那次 gather 同形状：每页一个线程，同时开同一份 PDF。
_CONCURRENT_RENDER = """
import asyncio, pathlib
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
        [sys.executable, "-c", _CONCURRENT_RENDER.format(fixture=str(FIXTURE))],
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


# ---- 渲染依赖缺失必须炸，不许伪装成"这页裁不出来" ----

def test_missing_render_dependency_raises_instead_of_returning_none(monkeypatch):
    """三个入口都不得把 ImportError 吞成 None。

    **为什么这条比它看起来重要**：`except Exception: return None` 会让
    「镜像少装了 Pillow」与「这一页真的裁不出来」产生完全相同的可观测结果 ——
    调用方如实打上 crop_failed，界面如实显示降级，一切都"正常工作"，
    而真实原因是部署错误。2026-09-01 合仓时真的踩到：新 venv 没装 pillow，
    render_page 对全部 5 页返回 None，而报出来的是并发渲染的守卫（指错了方向）。

    这里直接让 pdfium 的入口抛 ImportError —— 验的是**我们这三个 except
    子句的形状**，不是 pypdfium2 内部怎么找 Pillow。
    """
    import pytest
    import pypdfium2 as pdfium

    from ddp_core import crops

    def _boom(*_a, **_kw):
        raise ImportError("No module named 'PIL' (伪造)")

    pdf = FIXTURE.read_bytes()
    monkeypatch.setattr(pdfium, "PdfDocument", _boom)
    for call in (lambda: crops.render_page(pdf, 0, 2.0),
                 lambda: crops.render_crops(pdf, [(0, [0, 0, 100, 100], [612, 792])]),
                 lambda: crops.page_sizes(pdf)):
        with pytest.raises(ImportError):
            call()


def test_genuine_render_failure_still_degrades_quietly():
    """反哨兵：真正裁不出来的时候仍然返回 None，不能改成抛异常。

    上一条只放行 ImportError。要是有人把 `except Exception` 一起删掉，
    畸形 PDF 就会把整条抽取链打断 —— 而裁剪本来是增强路径。
    """
    from ddp_core import crops

    assert crops.render_page(b"not a pdf at all", 0) is None
    assert crops.page_sizes(b"not a pdf at all") == []
    assert crops.render_crops(b"not a pdf at all", [(0, [0, 0, 1, 1], None)]) == [None]
