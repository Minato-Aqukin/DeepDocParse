"""出处区域截图：按 bbox 从原件里裁一块图。**各消费方共用的唯一实现。**

搬进 ddp_core 之前这套坐标换算有**三份**（gateway / Web / mcp_server），
靠注释互相叮嘱"这套规则只有一个正确写法"。而写错的后果不是崩，
是裁出一张与文本无关的图并带着"已验证"标记 —— 这个项目定义的最恶劣错误。
现在只剩一份，物理上不可能再漂。

语料层在这之上还有一层对象存储缓存，位于 `services/corpus-api/ddp_corpus/crops.py`。

坐标规则与 mcp_server / Web 层的裁剪完全一致，**这套规则只有一个正确写法**：
比例 = 渲染位图的像素宽 / layout 的 `page_size` 宽。

**不能图省事用 pdfium 报的页尺寸**：遇到 CropBox 偏移或旋转页会裁到错误区域，
产出"带着已验证标记的假出处" —— 这是这个项目最不能接受的一种错误
（同一句警告写在 packages/contracts/ddp/layout-format.md 的坐标系一节）。

渲染是 CPU 密集的同步代码，调用方一律 `asyncio.to_thread`。
"""
import functools
import io
import threading
from collections import defaultdict
from contextlib import closing

# **PDFium 不是线程安全的。** 下面三个入口都会被 `asyncio.to_thread` 并发调用
# （vlm_ocr.py 对整篇文档的每一页 gather 一次），两个线程同时开文档/渲染就段错误。
# 段错误杀掉的是整个 arq worker 进程：没有 traceback、任务永远停在 pending，
# 界面上表现为"一直在解析" —— 又一个静默出错。2026-09-01 在 4090D 上必现，
# 5 页文档串行渲染全好、并发渲染 100% core dump（tests 里有守卫钉着）。
# 渲染是 CPU 密集的，串行化不损失什么；GPU 推理那一段仍然是并发的。
_PDFIUM_LOCK = threading.RLock()


def _pdfium_serialized(fn):
    """把 PDFium 调用串起来。装饰的是"自己开 PdfDocument"的函数，
    委托型的 render_crop 不要加 —— 它会再进 render_crops。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _PDFIUM_LOCK:
            return fn(*args, **kwargs)
    return wrapper

# **依赖缺失是部署错误，不是"这一页渲染不出来"。**
# 三个入口原来都是 `except Exception: return None`，于是**没装 Pillow**
# 与**这页真的裁不出来**长得一模一样：调用方把它记成 crop_failed，
# 界面上如实显示"没能裁出图"，而真实原因是镜像少装了一个包。
# 2026-09-01 合仓时踩到：新 venv 没有 pillow，`render_page` 对所有 5 页
# 都返回 None，守卫报的却是"并发渲染有页面返回空"（指向线程安全，完全指错了方向）。
# 现在 ImportError 单独放行 —— 让它带着 traceback 炸在启动/首次调用，
# 而不是变成一条可见但归因错误的降级。
_DEP_NOTE = "ImportError 不吞：渲染依赖缺失是部署错误，必须炸而不是退化成 None"

CROP_MARGIN = 12        # bbox 外扩，避免把边缘文字切掉（页面坐标单位）
RENDER_SCALE = 2.0      # 72dpi 基准 x2 = 144dpi，够视觉模型看清小字


def render_crop(pdf_bytes: bytes, page_idx: int, bbox: list,
                page_size: list | None) -> bytes | None:
    """同步渲染并裁剪，返回 PNG 字节。

    失败一律返回 None —— 裁剪是增强路径，不该阻断抽取本身；
    但调用方必须把"裁不出来"打成 crop_failed / crop_unsupported，不许当没发生。
    """
    return render_crops(pdf_bytes, [(page_idx, bbox, page_size)])[0]


@_pdfium_serialized
def render_crops(
    pdf_bytes: bytes, requests: list[tuple[int, list, list | None]],
) -> list[bytes | None]:
    """一次打开 PDF，每页只渲染一次，裁完即释放整页位图。

    按页聚合请求而不缓存整篇文档的像素，避免长文档裁图的内存随页数增长。
    返回顺序与 requests 一致，单个失败为 None；依赖缺失必须抛出。
    """
    if not requests:
        return []
    out: list[bytes | None] = [None] * len(requests)
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            by_page: dict[int, list[int]] = defaultdict(list)
            for index, (page_idx, _bbox, _page_size) in enumerate(requests):
                if isinstance(page_idx, int) and 0 <= page_idx < len(doc):
                    by_page[page_idx].append(index)
            for page_idx, indices in by_page.items():
                try:
                    with (
                        closing(doc[page_idx]) as page,
                        closing(page.render(scale=RENDER_SCALE)) as bitmap,
                        closing(bitmap.to_pil()) as img,
                    ):
                        for index in indices:
                            _, bbox, page_size = requests[index]
                            try:
                                sx = img.width / (page_size[0] if page_size else page.get_width())
                                sy = img.height / (page_size[1] if page_size else page.get_height())
                                x0, y0, x1, y1 = bbox
                                box = (
                                    max(0, int((x0 - CROP_MARGIN) * sx)),
                                    max(0, int((y0 - CROP_MARGIN) * sy)),
                                    min(img.width, int((x1 + CROP_MARGIN) * sx)),
                                    min(img.height, int((y1 + CROP_MARGIN) * sy)),
                                )
                                if box[2] <= box[0] or box[3] <= box[1]:
                                    continue
                                with closing(img.crop(box)) as region, io.BytesIO() as buf:
                                    region.save(buf, format="PNG")
                                    out[index] = buf.getvalue()
                            except ImportError:
                                raise
                            except Exception:
                                # 畸形 bbox/page_size 只废掉这个原子。
                                continue
                except ImportError:
                    raise
                except Exception:
                    # 坏页不能抹掉其他页已经产出的裁图。
                    continue
        finally:
            doc.close()
    except ImportError:
        raise           # 见 _DEP_NOTE
    except Exception:
        return [None] * len(requests)
    return out


@_pdfium_serialized
def render_page(pdf_bytes: bytes, page_idx: int, scale: float = RENDER_SCALE) -> bytes | None:
    """整页渲染成 PNG —— vlm-ocr 引擎的输入（见 services/vlm_ocr.py）。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            if page_idx >= len(doc):
                return None
            with (
                closing(doc[page_idx]) as page,
                closing(page.render(scale=scale)) as bitmap,
                closing(bitmap.to_pil()) as img,
                io.BytesIO() as buf,
            ):
                img.save(buf, format="PNG")
                return buf.getvalue()
        finally:
            doc.close()
    except ImportError:
        raise           # 见 _DEP_NOTE
    except Exception:
        return None


@_pdfium_serialized
def page_sizes(pdf_bytes: bytes) -> list[tuple[float, float]]:
    """每页的 [宽, 高]（PDF 点）。vlm-ocr 要用它把归一化坐标还原成 bbox。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            sizes = []
            for index in range(len(doc)):
                with closing(doc[index]) as page:
                    sizes.append((page.get_width(), page.get_height()))
            return sizes
        finally:
            doc.close()
    except ImportError:
        raise           # 见 _DEP_NOTE
    except Exception:
        return []
