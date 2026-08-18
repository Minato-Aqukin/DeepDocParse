"""版面中间表示（DDP-Layout v1）与 born-digital 引擎。

这两件事一起测：normalizer 层存在的全部意义，就是让**第二个引擎**产出同样的格式。
只有一个引擎时，"格式是契约"这句话是没法证伪的。
"""
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.services import borndigital, layout
from app.services.engines import BORNDIGITAL_RUNTIME, BornDigitalEngine, MineruEngine, resolve

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LONG_DOC = FIXTURES / "long-doc.pdf"

# make_fixtures.py 埋进第 3 页（page_idx=2）的唯一事实
FACT = "The launch code of project Zephyr is 8712."


def test_promised_fields_survive_normalization_of_mineru_output():
    """mineru 的 middle_json 过一遍归一化后，承诺字段一个不少。"""
    middle = {"pdf_info": [{"page_size": [612, 792], "para_blocks": [
        {"type": "text", "bbox": [1, 2, 3, 4],
         "lines": [{"spans": [{"content": "甲"}, {"content": "乙"}]}]}]}]}

    result = layout.from_mineru(middle)
    assert layout.validate(result) == []
    assert result["layout_version"] == layout.LAYOUT_VERSION
    # page_idx 原文没给，归一化要补上——缺它出处就落不到具体页
    assert result["pdf_info"][0]["page_idx"] == 0
    assert layout.block_text(result["pdf_info"][0]["para_blocks"][0]) == "甲 乙"


def test_normalization_keeps_unpromised_engine_fields():
    """不在契约里的字段不删。

    删掉它们不会让任何消费方变正确（它们本来就不该被依赖），
    却会让"下载版面 JSON"这条排查路径凭空变差。
    """
    middle = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"type": "table", "index": 7, "bbox": [1, 2, 3, 4], "lines": []}]}]}
    result = layout.from_mineru(middle)
    assert result["pdf_info"][0]["para_blocks"][0]["type"] == "table"
    assert result["pdf_info"][0]["para_blocks"][0]["index"] == 7


def test_validate_catches_missing_page_size():
    """page_size 缺失必须报出来：裁剪要靠它换算，缺了会裁到错误区域。"""
    broken = {"pdf_info": [{"page_idx": 0, "para_blocks": []}]}
    problems = layout.validate(broken)
    assert any("page_size" in p for p in problems), problems


@pytest.mark.skipif(not LONG_DOC.exists(), reason="缺 tests/fixtures/long-doc.pdf")
def test_borndigital_produces_valid_layout_with_locatable_facts():
    """兜底引擎的产物必须能直接喂给所有消费方，并且出处定位是对的。"""
    pages = borndigital.extract_pages(LONG_DOC.read_bytes())
    assert len(pages) == 5, "5 页文档要出 5 页版面（xref 坏掉时只会读出 1 页）"

    result = layout.build(pages, engine="borndigital")
    assert layout.validate(result) == []
    assert layout.page_count(result) == 5

    # 埋点事实必须落在它真正所在的那一页——页码错了，整套"可验证出处"就是假的
    hit_pages = [
        page["page_idx"]
        for page in result["pdf_info"]
        for block in page["para_blocks"]
        if FACT.split(" is ")[0] in layout.block_text(block)
    ]
    assert hit_pages == [2], f"事实应只出现在第 3 页，实际 {hit_pages}"


@pytest.mark.skipif(not LONG_DOC.exists(), reason="缺 tests/fixtures/long-doc.pdf")
def test_borndigital_bbox_is_top_left_origin():
    """坐标必须是左上原点、y 向下（与 mineru / 裁剪代码一致）。

    pypdfium2 给的是 PDF 空间（左下原点、y 向上）。翻错了不会报任何错，
    只会让"出处截图"裁到页面上另一块区域——带着"已做视觉验证"标记的假出处。
    这里用"页顶的标题块 y 值应当小于页中部的块"来钉死方向。
    """
    pages = borndigital.extract_pages(LONG_DOC.read_bytes())
    blocks = pages[0]["blocks"]
    height = pages[0]["page_size"][1]

    heading = blocks[0]          # 版面第一块 = 页面最上方那行（"Page 1 section heading"）
    assert "heading" in heading["text"]
    assert heading["bbox"][1] < height * 0.2, \
        f"页顶的块 y0 应当很小，实际 {heading['bbox'][1]}（多半是 y 轴翻反了）"
    assert blocks[-1]["bbox"][1] > heading["bbox"][1], "块要按从上到下排"
    assert all(0 <= b["bbox"][1] < b["bbox"][3] <= height for b in blocks)


def test_borndigital_keeps_two_columns_apart_under_a_full_width_title():
    """回归（2026-08-18 验收抓到）：全宽标题不得把整页塌成一个块。

    旧实现拿**块的并集 bbox** 做水平重叠判定：整页宽的标题一旦并进左栏第一行，
    并集就变成整页宽，此后左右两栏的每一行都与它"重叠"，于是
    整页并成一个块 —— 文本左右交错、bbox 退化成整片版心。
    双栏论文正是 born-digital 最典型的输入，而"bbox 级出处"是这个项目的立身之本。

    现在拿**块里最后一行**比。跨栏标题仍会被并进它下面那一栏的第一段
    （已知限制，写在 docs/layout-format.md 里），但两栏不会再串。
    """
    lines = [{"bbox": [60, 40, 517, 52], "text": "A FULL WIDTH PAPER TITLE"}]
    y = 64
    for i in range(4):
        lines.append({"bbox": [60, y, 250, y + 12], "text": f"left {i}"})
        lines.append({"bbox": [320, y, 517, y + 12], "text": f"right {i}"})
        y += 16

    blocks = borndigital._merge_lines(lines)
    assert len(blocks) >= 2, f"两栏被并成了一块：{blocks}"
    for block in blocks:
        rows = block["text"].splitlines()
        left = [r for r in rows if r.startswith("left")]
        right = [r for r in rows if r.startswith("right")]
        assert not (left and right), f"同一个块里混进了两栏的文本：{rows}"
    # 每一栏内部仍然要合成段，不能碎成一行一块
    assert any(len(b["text"].splitlines()) >= 4 for b in blocks), \
        f"栏内的行没有合并：{[b['text'] for b in blocks]}"


def test_borndigital_merges_lines_into_paragraphs_but_keeps_columns_apart():
    """行距近的合并成段；左右分栏（水平不重叠）不许粘在一起。"""
    lines = [
        {"bbox": [50, 100, 250, 112], "text": "左栏第一行"},
        {"bbox": [50, 114, 250, 126], "text": "左栏第二行"},
        {"bbox": [320, 100, 520, 112], "text": "右栏第一行"},
    ]
    blocks = borndigital._merge_lines(lines)
    texts = [b["text"] for b in blocks]
    assert "左栏第一行\n左栏第二行" in texts, texts
    assert "右栏第一行" in texts, texts


async def test_registry_runtime_picks_the_adapter(app_state):
    """注册表的 runtime 字段决定用哪个适配器 —— 这就是铁律 3 的落点。"""
    registry = app_state.registry
    mineru_engine = resolve(registry.parse_engines["mineru"],
                            mineru_client=app_state.mineru_client, http=app_state.http)
    assert isinstance(mineru_engine, MineruEngine)

    born = registry.parse_engines["borndigital"]
    assert born.runtime == BORNDIGITAL_RUNTIME
    assert isinstance(resolve(born, mineru_client=app_state.mineru_client,
                              http=app_state.http), BornDigitalEngine)


def test_unknown_runtime_is_loud():
    """注册表写了个不存在的 runtime 要立刻炸，不能悄悄退回默认引擎。"""
    class Entry:
        runtime = "does-not-exist"

    with pytest.raises(LookupError):
        resolve(Entry(), mineru_client=None, http=None)


@respx.mock
async def test_borndigital_runs_the_whole_registry_path(client, worker_ctx, app_state,
                                                        monkeypatch):
    """dogfooding：换个引擎，除了请求里的 engine 字段之外什么都不用改。

    铁律 3 说"加引擎 = 加容器 + 一行配置"，但在 born-digital 之前注册表里
    只有 mineru 一个解析引擎 —— 这句话从来没有被第二个引擎验证过。
    这条用例走完整条链：受理 -> worker 归档 -> 取结果，并断言结果是**归一化后**的版面。
    """
    from app.config import settings as cfg
    from app.worker.tasks import poll_and_archive

    monkeypatch.setattr(cfg, "poll_initial_delay", 0.01)
    file_url = "https://files.example.com/long-doc.pdf"
    respx.get(file_url).mock(return_value=Response(200, content=LONG_DOC.read_bytes()))

    resp = await client.post("/v1/parse", json={"file_url": file_url, "engine": "borndigital"})
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]

    await poll_and_archive(worker_ctx, task_id)

    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["status"] == "succeeded", status

    result = (await client.get(f"/v1/parse/{task_id}/result")).json()
    assert layout.validate(result["layout_json"]) == []
    assert result["layout_json"]["engine"] == "borndigital"
    assert len(result["layout_json"]["pdf_info"]) == 5
    assert FACT.split(" is ")[0] in result["markdown"]
    # 没有远端引擎被打扰过 —— 整条路都在进程内
    assert result["images"] == []


@respx.mock
async def test_borndigital_fails_loudly_on_a_scanned_pdf(client, worker_ctx, app_state,
                                                         monkeypatch):
    """没有文字层就明确失败，不返回一份空版面。

    返回空版面的话下游会以为"解析成功了，只是这份文档恰好没内容"——
    正是这个项目最忌讳的那种静默降级。
    """
    import io

    import pypdfium2 as pdfium

    from app.config import settings as cfg
    from app.worker.tasks import poll_and_archive

    monkeypatch.setattr(cfg, "poll_initial_delay", 0.01)
    blank = pdfium.PdfDocument.new()
    blank.new_page(612, 792)
    buf = io.BytesIO()
    blank.save(buf)
    blank.close()

    file_url = "https://files.example.com/scan.pdf"
    respx.get(file_url).mock(return_value=Response(200, content=buf.getvalue()))

    task_id = (await client.post("/v1/parse", json={
        "file_url": file_url, "engine": "borndigital"})).json()["task_id"]
    await poll_and_archive(worker_ctx, task_id)

    status = (await client.get(f"/v1/parse/{task_id}")).json()
    assert status["status"] == "failed"
    assert "文字层" in status["error"], status["error"]


def _one_line_pdf(rotate: int) -> bytes:
    """一页 PDF，在未旋转坐标系的左上角放一行字，页面带 /Rotate。

    手写而不是用库：要精确控制 /Rotate 与文字位置，才谈得上验坐标变换。
    """
    text = "TOPLEFT MARKER"
    stream = f"BT /F1 12 Tf 60 730 Td ({text}) Tj ET".encode()
    objs = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: (f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Rotate {rotate}"
            f"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>").encode(),
        4: b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        5: b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += f"{n} 0 obj\n".encode() + objs[n] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for n in sorted(objs):
        out += f"{offsets[n]:010d} 00000 n \n".encode()
    out += (f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    return bytes(out)


# rotate -> 那行字在**显示后**的页面上应该出现在哪个角
# （原文在未旋转坐标系的左上角，页面顺时针转 r 度后角落跟着走）
_ROTATION_CORNERS = {0: "左上", 90: "右上", 180: "右下", 270: "左下"}


@pytest.mark.parametrize("rotate,corner", sorted(_ROTATION_CORNERS.items()))
def test_borndigital_handles_page_rotation(rotate, corner):
    """旋转页的 bbox 必须跟着转。

    pypdfium2 的字符矩形在**未旋转**的 PDF 空间里（左下原点），而 page_size
    和裁剪用的是显示尺寸（含旋转）。不做变换的话，横排页的每个 bbox 都会错位 ——
    而错位的 bbox 会裁出一张与文本无关的"出处截图"，还带着"已做视觉验证"的标记。
    这种错不抛任何异常，只能靠测试钉死。
    """
    pages = borndigital.extract_pages(_one_line_pdf(rotate))
    assert pages[0]["blocks"], f"rotate={rotate} 时一个块都没抽到"
    x0, y0, x1, y1 = pages[0]["blocks"][0]["bbox"]
    width, height = pages[0]["page_size"]

    # 页面尺寸随旋转交换
    assert (width, height) == ((792.0, 612.0) if rotate in (90, 270) else (612.0, 792.0))
    # bbox 必须整个落在页内
    assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height, (rotate, [x0, y0, x1, y1])

    near_left, near_top = x0 < width / 2, y0 < height / 2
    assert (near_left, near_top) == {
        "左上": (True, True), "右上": (False, True),
        "右下": (False, False), "左下": (True, False),
    }[corner], f"rotate={rotate} 时那行字应在{corner}，实际 bbox={[x0, y0, x1, y1]}"


@respx.mock
async def test_readyz_is_not_permanently_red_with_an_inprocess_engine(client, app_state):
    """回归：注册表里有进程内引擎时，/readyz 不能永远 503。

    born-digital 的 endpoint 是 `inproc://borndigital`，httpx 对它抛
    UnsupportedProtocol（HTTPError 子类）—— 探针不特判的话这一项永远 down，
    而 `ready = all(up)`，于是就绪探针恒假。models.cpu.yaml 里 born-digital 是
    **唯一**的解析引擎，无 GPU 路径的 k8s readiness / compose healthcheck 会永远红。
    """
    # 其它引擎全部探通，把变量收敛到"进程内引擎会不会被误判为 down"这一件事上
    respx.get("http://mineru:8000/health").mock(return_value=Response(200))
    respx.get("http://vqa-dsocr:8000/v1/models").mock(return_value=Response(200))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))

    resp = await client.get("/readyz")
    body = resp.json()
    assert body["checks"]["engine:borndigital"] == "up", body
    assert resp.status_code == 200, body


def test_merge_lines_never_loses_or_duplicates_a_line():
    """性质测试：合并只重组行，不许丢行、不许重复，bbox 必须是成员行的外接矩形。

    `_merge_lines` 现在会把"再也接不上"的块从 open_blocks 摘到 done 里
    （既为正确性也为复杂度）。分流写错就会丢块 —— 而丢掉的是文档内容本身，
    下游只会看到"这段原文检索不到"，不会有任何报错。随机输入比手写几个用例管用。
    """
    import random

    random.seed(11)
    for _ in range(200):
        count = random.randint(1, 60)
        lines = []
        for i in range(count):
            x0 = random.choice([50, 60, 300, 320]) + random.randint(0, 30)
            y0 = random.randint(0, 700)
            lines.append({"bbox": [x0, y0, x0 + random.randint(40, 260),
                                   y0 + random.choice([10, 11, 12, 24])],
                          "text": f"L{i}"})

        blocks = borndigital._merge_lines(lines)
        produced = [t for b in blocks for t in b["text"].splitlines()]
        assert sorted(produced) == sorted(f"L{i}" for i in range(count)), "丢行或重复"

        for block in blocks:
            members = [int(t[1:]) for t in block["text"].splitlines()]
            assert block["bbox"] == [
                min(lines[i]["bbox"][0] for i in members),
                min(lines[i]["bbox"][1] for i in members),
                max(lines[i]["bbox"][2] for i in members),
                max(lines[i]["bbox"][3] for i in members),
            ], "bbox 不是成员行的外接矩形"
