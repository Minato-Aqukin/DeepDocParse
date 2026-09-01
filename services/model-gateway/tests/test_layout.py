"""版面中间表示（DDP-Layout v1）与 born-digital 引擎。

这两件事一起测：normalizer 层存在的全部意义，就是让**第二个引擎**产出同样的格式。
只有一个引擎时，"格式是契约"这句话是没法证伪的。
"""
import json

import httpx
import pytest
import respx
from httpx import Response

from ddp_gateway.services import borndigital, layout
from ddp_gateway.services.engines import BORNDIGITAL_RUNTIME, BornDigitalEngine, MineruEngine, resolve

from ddp_paths import FIXTURES, REGISTRY, REPO_ROOT

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


def test_validate_checks_the_v12_engine_notes_shape():
    """v1.2 顶层承诺 `engine_notes` 的形状要真的被检查。

    这条是 DDP-Layout 升 v1.2 之后**新承诺字段的唯一执行点** ——
    没有它，契约文档里写了一行、代码里加了个常量，却没有任何东西保证
    产出方按那个形状写。二次验收把 `validate()` 里那段整个删掉，
    144 条用例一条不红（与本轮刚修掉的两条假守卫是同一类问题）。
    """
    # 类型不对：字符串不是字符串列表
    assert any("engine_notes" in p
               for p in layout.validate({"pdf_info": [], "engine_notes": "oops"}))
    # 元素类型不对
    assert any("engine_notes" in p
               for p in layout.validate({"pdf_info": [], "engine_notes": [1]}))
    # 合规的形状与"根本没给"都必须干净通过（缺省才是常态）
    assert layout.validate({"pdf_info": [], "engine_notes": ["code: 人话"]}) == []
    assert layout.validate({"pdf_info": []}) == []
    assert layout.validate({"pdf_info": [], "engine_notes": None}) == []


def test_code_detection_contract_and_code_type():
    built = layout.build_pages([{
        "page_idx": 0, "page_size": [600, 800],
        "para_blocks": [{"type": "code", "bbox": [1, 2, 3, 4], "lines": []}],
    }], engine="vlm-ocr", code_detection="native")
    assert layout.validate(built) == []
    assert built["code_detection"] == "native"
    assert built["pdf_info"][0]["para_blocks"][0]["type"] == "code"
    problems = layout.validate({"pdf_info": [], "code_detection": "maybe"})
    assert any("code_detection" in problem for problem in problems)


def test_borndigital_code_heuristic_requires_two_signals():
    assert borndigital._looks_like_code(
        "  client.fetchUser(user_id);", {"Courier New"}, indented=True)
    assert borndigital._looks_like_code(
        "  client.fetchUser(user_id);", set(), indented=True)
    assert not borndigital._looks_like_code(
        "This is ordinary prose without programming syntax.", {"Times New Roman"},
        indented=False)
    assert borndigital._looks_like_code(
        'TARGET_IDENTIFIER = "HttpRequestParser"', {"Courier New"}, indented=False)


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
    from ddp_gateway.config import settings as cfg
    from ddp_gateway.worker.tasks import poll_and_archive

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

    from ddp_gateway.config import settings as cfg
    from ddp_gateway.worker.tasks import poll_and_archive

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
    vqa = respx.get("http://vqa-dsocr:8000/v1/models").mock(return_value=Response(200))
    # 抽取平面的指令模型是 vqa_models 的第二个条目，探针照样会去连它
    respx.get("http://chat-instruct:8000/v1/models").mock(return_value=Response(200))
    respx.get("http://embed:8080/health").mock(return_value=Response(200))
    respx.get("http://rerank:8080/health").mock(return_value=Response(200))

    resp = await client.get("/readyz")
    body = resp.json()
    assert body["checks"]["engine:borndigital"] == "up", body
    assert resp.status_code == 200, body

    # v1.1 两条回归：
    # 1. vlm-ocr 挂在 parse_engines 段，但它的 endpoint 是 OpenAI 运行时。
    #    探针路径按 runtime 而不是段名推断，否则会去打不存在的 /health，
    #    把一个健康的模型容器报成 down —— 而 readyz 恒 503 = 这个副本永不接流量
    assert body["checks"]["engine:vlm-ocr"] == "up", body
    # 2. vlm-ocr 与 vqa 指向同一个容器，**只该探一次**
    assert vqa.call_count == 1, f"同一个 endpoint 被探了 {vqa.call_count} 次"


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


# --------------------------------------------------------------------- v1.1：块类型进契约

def test_block_type_is_normalized_into_the_vocabulary():
    """引擎原生类型五花八门，归一化后只能是七个值之一。认不出来归 other，**不许丢块**。"""
    raw = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792], "para_blocks": [
        {"type": "plain text", "bbox": [0, 0, 10, 10], "lines": []},
        {"type": "table_body", "bbox": [0, 0, 10, 10], "lines": []},
        {"type": "interline_equation", "bbox": [0, 0, 10, 10], "lines": []},
        {"type": "某个没见过的类型", "bbox": [0, 0, 10, 10], "lines": []},
        {"bbox": [0, 0, 10, 10], "lines": []},          # 压根没有 type 的老版面
    ]}]}
    out = layout.from_mineru(raw)
    types = [b["type"] for b in out["pdf_info"][0]["para_blocks"]]
    # 有 type 但不认识 -> other；压根没有 type -> text（两种"不认识"是不同的事）
    assert types == ["text", "table", "equation", "other", "text"]
    assert len(types) == 5, "归一化不许丢块"
    # 原生值留着排查用（不进契约，但删掉会让"下载版面 JSON"这个手段变差）
    assert out["pdf_info"][0]["para_blocks"][0]["type_native"] == "plain text"
    assert layout.validate(out) == []


def test_validate_rejects_unnormalized_type():
    """type 进了契约，就必须是词汇表里的值 —— 出现别的值只可能是 normalizer 漏跑。"""
    bad = {"pdf_info": [{"page_idx": 0, "page_size": [612, 792],
                         "para_blocks": [{"type": "plain text", "bbox": None, "lines": []}]}]}
    problems = layout.validate(bad)
    assert any("type" in p for p in problems), problems


def test_block_text_descends_into_nested_blocks():
    """回归：mineru 的表格块把内容放在 blocks 子结构里，自身没有 lines。

    v1.1 之前 block_text 只读 lines —— **整张表格的文字在分块阶段被静默丢弃**：
    表格解析出来了、索引里却没有，问表格里的数永远检索不到，全程没有报错。
    """
    table_block = {"type": "table", "bbox": [0, 0, 10, 10], "blocks": [
        {"type": "table_caption", "lines": [{"spans": [{"content": "表 1 价格表"}]}]},
        {"type": "table_body", "lines": [{"spans": [
            {"content": "项目 金额", "html": "<table><tr><td>项目</td></tr></table>"}]}]},
    ]}
    assert "表 1 价格表" in layout.block_text(table_block)
    assert "项目 金额" in layout.block_text(table_block)
    assert layout.table_html(table_block) == "<table><tr><td>项目</td></tr></table>"
    # 非表格块没有 html —— 消费方必须能处理 None
    assert layout.table_html({"lines": [{"spans": [{"content": "正文"}]}]}) is None


def test_table_never_merges_with_surrounding_text():
    """表格独立成块。合并进正文的话，出处 bbox 会横跨整片版心、行列关系也拍平没了。"""
    from ddp_core.chunking import layout_to_chunks

    lay = layout.from_mineru({"pdf_info": [{"page_idx": 0, "page_size": [612, 792],
                                            "para_blocks": [
        {"type": "title", "bbox": [0, 0, 100, 20],
         "lines": [{"spans": [{"content": "第三章 价款"}]}]},
        {"type": "text", "bbox": [0, 30, 100, 60],
         "lines": [{"spans": [{"content": "以下为价款明细。"}]}]},
        {"type": "table", "bbox": [0, 70, 100, 200], "blocks": [
            {"type": "table_body", "lines": [{"spans": [
                {"content": "项目 金额", "html": "<table></table>"}]}]}]},
        {"type": "text", "bbox": [0, 210, 100, 240],
         "lines": [{"spans": [{"content": "以上为附表。"}]}]},
    ]}]})
    chunks = layout_to_chunks(lay)
    kinds = [c["block_type"] for c in chunks]
    assert kinds == ["text", "table", "text"], kinds
    table = chunks[1]
    assert table["bbox"] == [0, 70, 100, 200], "表格块的 bbox 必须是它自己的，不含邻居"
    assert table["table_html"] == "<table></table>"
    # 标题不单独成块，而是作为后续块的上下文前缀（标题太短，单独成块检索不到）
    assert all("第三章 价款" in c["text"] for c in chunks)


# --------------------------------------------------------------------- v1.1：vlm-ocr

@pytest.mark.parametrize("raw, expected", [
    ([100, 200, 900, 300], [61.2, 158.4, 550.8, 237.6]),      # 0~1000 归一化
    ([0.1, 0.2, 0.9, 0.3], [61.2, 158.4, 550.8, 237.6]),      # 0~1 归一化
])
def test_vlm_bbox_denormalization(raw, expected):
    from ddp_gateway.services import vlm_ocr

    assert vlm_ocr.denormalize_bbox(raw, 612, 792) == expected


@pytest.mark.parametrize("raw", [None, [900, 200, 100, 300], [0, 0, 2000, 100], "左上角", [1, 2]])
def test_vlm_bad_bbox_becomes_none_not_a_guess(raw):
    """**宁可 None 也不要一个凑合的框**。

    契约里 bbox=None 是"不能裁剪"，下游会如实打降级标记；
    而一个错的框会裁出不相干的图，还带着"已验证"标记 —— 这个项目定义的最恶劣错误。
    """
    from ddp_gateway.services import vlm_ocr

    assert vlm_ocr.denormalize_bbox(raw, 612, 792) is None


def test_vlm_plain_text_fallback_leaves_bbox_empty():
    """模型没吐 JSON 时整页当一个文本块，**bbox 留空而不是编一个整页框**。

    编一个整页框会让每条出处都"命中"整页：指标上好看、实际毫无定位价值，
    用户点开还会看到一整页图。
    """
    from ddp_gateway.services import vlm_ocr

    page = vlm_ocr.plain_text_page("一段没有结构的识别结果", 0, 612, 792)
    assert page["para_blocks"][0]["bbox"] is None
    assert layout.block_text(page["para_blocks"][0]) == "一段没有结构的识别结果"


def test_vlm_blocks_normalize_through_the_contract():
    from ddp_gateway.services import vlm_ocr

    page = vlm_ocr.blocks_to_page([
        {"type": "标题", "bbox": [0, 0, 1000, 50], "text": "合同"},
        {"type": "table", "bbox": [0, 60, 1000, 400], "text": "项目 金额",
         "html": "<table><tr><td>项目</td></tr></table>"},
        {"type": "text", "bbox": None, "text": "定不出位置的一段"},
        {"type": "text", "bbox": [0, 0, 10, 10], "text": ""},      # 空块要被跳过
    ], 0, 612, 792)
    built = layout.build_pages([page], engine="vlm-ocr")
    assert layout.validate(built) == []
    blocks = built["pdf_info"][0]["para_blocks"]
    assert [b["type"] for b in blocks] == ["other", "table", "text"]
    assert layout.table_html(blocks[1]) == "<table><tr><td>项目</td></tr></table>"
    assert blocks[2]["bbox"] is None


# ------------------------------------------------- v1.2：deepseek-ocr2 方言
#
# 判据全部来自官方 vLLM 脚本（DeepSeek-OCR2-vllm/run_dpsk_ocr2_pdf.py）：
# 正则、坐标分母 999、采样参数，都不是猜的。改这一节前先去核对那份脚本。

# 一页典型的 grounding 输出。标签在内容之前，一个标签管到下一个标签为止。
DSOCR2_PAGE = """<|ref|>title<|/ref|><|det|>[[139, 45, 861, 78]]<|/det|>
# 2024 年度采购合同

<|ref|>text<|/ref|><|det|>[[100, 120, 890, 300]]<|/det|>
甲方：北京某某科技有限公司
乙方：上海某某贸易有限公司

<|ref|>table<|/ref|><|det|>[[100, 320, 890, 520]]<|/det|>
<table><tr><td>项目</td><td>金额</td></tr><tr><td>服务费</td><td>120000</td></tr></table>

<|ref|>image<|/ref|><|det|>[[100, 540, 400, 700]]<|/det|>

<|ref|>formula<|/ref|><|det|>[[100, 720, 500, 760]]<|/det|>
$E = mc^2$"""


def test_dsocr2_full_page_maps_through_the_contract():
    """一页真实形状的 grounding 输出 -> 合规的 DDP-Layout。"""
    from ddp_gateway.services import dsocr2

    page = dsocr2.page_from_output(DSOCR2_PAGE, 0, 612, 792)
    built = layout.build_pages([page], engine="vlm-ocr")
    assert layout.validate(built) == []

    blocks = built["pdf_info"][0]["para_blocks"]
    # image 块没有题注 -> 被跳过（空块会白占一个 seq，而 seq 是出处的定位键）
    assert [b["type"] for b in blocks] == ["title", "text", "table", "equation"]
    # 标题的 markdown `#` 前缀要去掉：类型已经由 label 承载，留着会叠成 `## # 标题`
    assert layout.block_text(blocks[0]) == "2024 年度采购合同"
    assert "甲方" in layout.block_text(blocks[1])
    # formula -> equation：契约词汇表里公式块叫 equation
    assert layout.block_text(blocks[3]) == "$E = mc^2$"


def test_dsocr2_table_keeps_both_html_and_searchable_text():
    """表格结构进 html，同时**必须**留一份纯文本。

    只存 HTML 的话分块与检索读不到内容 —— "表里那个数"永远检索不到，
    全程无报错。这正是 layout.block_text 注释里记着的那个洞。
    """
    from ddp_gateway.services import dsocr2

    page = dsocr2.page_from_output(DSOCR2_PAGE, 0, 612, 792)
    table = [b for b in page["para_blocks"] if b["type"] == "table"][0]
    assert layout.table_html(table) == (
        "<table><tr><td>项目</td><td>金额</td></tr>"
        "<tr><td>服务费</td><td>120000</td></tr></table>")
    text = layout.block_text(table)
    assert "服务费" in text and "120000" in text
    assert "<td>" not in text


def test_dsocr2_coordinates_use_999_not_1000():
    """官方换算是 `x / 999 * width`。整幅框要正好落在整页上。"""
    from ddp_gateway.services import dsocr2

    assert dsocr2.to_bbox("[[0, 0, 999, 999]]", 612, 792) == [0.0, 0.0, 612.0, 792.0]
    assert dsocr2.to_bbox("[[100, 120, 890, 300]]", 612, 792) == pytest.approx(
        [61.26, 95.14, 545.23, 237.84], abs=0.01)


def test_dsocr2_multiple_boxes_become_their_union():
    """一个 ref 报多个框 = 这块内容确实横跨多个区域，取并集。

    只取第一个框会裁到半句话，出处指向一个不含证据的区域 ——
    那比框大一点恶劣得多。
    """
    from ddp_gateway.services import dsocr2

    assert dsocr2.to_bbox("[[100, 100, 200, 200], [300, 400, 500, 600]]",
                          999, 999) == [100.0, 100.0, 500.0, 600.0]


@pytest.mark.parametrize("det", [
    "",                                 # 空
    "[[]]",                             # 没有数字
    "[[100, 200]]",                     # 不足四个
    "[[100, 100, 100, 100]]",           # 零面积
    "[[900, 100, 100, 300]]",           # x1 <= x0
    "[[0, 0, 5000, 100]]",              # 越界
    "[[100, 100, 200, 200], [300]]",    # 数量不是 4 的倍数
])
def test_dsocr2_bad_det_becomes_none_not_a_guess(det):
    """**宁可 None 也不要凑合的框** —— 与 vlm_ocr.denormalize_bbox 同一条铁律。"""
    from ddp_gateway.services import dsocr2

    assert dsocr2.to_bbox(det, 612, 792) is None


def test_dsocr2_det_is_not_evaluated_as_python(tmp_path):
    """det 字符串来自模型输出，是不可信输入。

    官方脚本用的是 `eval()`；服务端照抄等于给模型开一个执行入口。
    这条钉住"解析而不是求值"：能算出数就算，算不出就 None，绝不执行。

    **三条断言都是判别性的** —— 换回 `eval()` 实现时必须有至少一条变红。
    （前一版只断言了 `to_bbox("[[__import__('sys')]]") is None`，
    而 eval 版对这个输入**也**返回 None：payload 求值成功、结果不是合法框。
    那条用例因此对 eval 与解析两种实现同样绿，是个假守卫。）
    """
    import inspect

    from ddp_gateway.services import dsocr2

    # ① 副作用：eval 会真的把文件写出来，解析路径不可能
    probe = tmp_path / "pwned.txt"
    payload = (f"[[1,2,3,4]] if __import__('pathlib')"
               f".Path({str(probe)!r}).write_text('x') else 0")
    dsocr2.to_bbox(payload, 612, 792)
    assert not probe.exists(), "det 字符串被求值了 —— 模型输出拿到了代码执行"

    # ② 判别性取值：eval 得到两个相同的框（并集是个合法 bbox），
    #    解析只数出 5 个数字（不是 4 的整数倍）-> None
    assert dsocr2.to_bbox("[[100,100,200,200]] * 2", 612, 792) is None

    # ③ 实现本身。①② 挡的是"眼下这个 eval 写法"，这条挡的是所有写法 ——
    #    守的正是"以后有人为了跟官方脚本对齐把它换回去"。
    #    走 AST 而不是查子串：模块注释里就写着"官方脚本用的是 eval()"，
    #    子串匹配会被这句话钉死；AST 只看**真的调用**。
    import ast

    called = {
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(dsocr2)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"eval", "exec"}, \
        "dsocr2 里出现了 eval/exec —— det/ref 全是模型输出，不许求值"


def test_dsocr2_block_without_any_tag_returns_none_not_empty():
    """没有 grounding 标签 -> None（"这页没走 grounding"），不是"解析出 0 个块"。

    两者必须分开：前者要退回整页纯文本，后者是真的空页。
    """
    from ddp_gateway.services import dsocr2

    assert dsocr2.page_from_output("就是一段普通的识别结果", 0, 612, 792) is None
    assert dsocr2.blocks_from_output("", 612, 792) is None


def test_dsocr2_strip_tags_leaves_clean_markdown():
    from ddp_gateway.services import dsocr2

    cleaned = dsocr2.strip_tags(DSOCR2_PAGE)
    assert "<|ref|>" not in cleaned and "<|det|>" not in cleaned
    assert "2024 年度采购合同" in cleaned


VQA = "http://vqa-dsocr:8000"
ONE_PAGE_PDF = (FIXTURES / "sample.pdf").read_bytes()


async def _recognize(content: str, options: dict):
    """打一次 vlm-ocr，返回 (layout_json, 发出去的请求体)。"""
    with respx.mock:
        route = respx.post(f"{VQA}/v1/chat/completions").mock(
            return_value=Response(200, json={
                "choices": [{"message": {"content": content}}]}))
        async with httpx.AsyncClient(trust_env=False) as http:
            built = await vlm_ocr_module().recognize(
                http, endpoint=VQA, model="deepseek-ocr-2",
                pdf_bytes=ONE_PAGE_PDF, options=options)
    return built, json.loads(route.calls[0].request.content)


def vlm_ocr_module():
    from ddp_gateway.services import vlm_ocr
    return vlm_ocr


async def test_dsocr2_request_carries_the_params_that_make_bbox_possible():
    """**这条守的是整个出处功能。**

    `skip_special_tokens` 在 OpenAI 接口里缺省是 true，而 `<|ref|>` / `<|det|>`
    正是特殊 token —— 不显式关掉的话，模型报出来的 bbox 会在返回前被剥光，
    我们只会看到"每个块 bbox 都是 null"，没有任何报错。
    `vllm_xargs` 里的 ngram 参数同理：vLLM 侧挂了 logits processor 还不够，
    每个请求得带 ngram_size 才会生效（没传就整个跳过）。
    """
    _, body = await _recognize(DSOCR2_PAGE, {"dialect": "deepseek-ocr2"})

    assert body["skip_special_tokens"] is False
    assert body["vllm_xargs"] == {
        "ngram_size": 20, "window_size": 50,
        "whitelist_token_ids": [128821, 128822],   # <td> / </td>
    }
    assert body["temperature"] == 0
    # 官方 prompt 逐字。`<image>` 不由我们写 —— vLLM 按 image_url 部件的位置插入，
    # 自己再写一个会变成两个占位符，直接对不上视觉 token 数
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image_url" and content[1]["type"] == "text"
    assert content[1]["text"] == "<|grounding|>Convert the document to markdown."


async def test_default_dialect_request_is_unchanged():
    """缺省方言（generic-json）**一个字段都不许多发**。

    老部署的注册表里没有 dialect，行为必须与 v1.1 逐字一致：
    多发 vLLM 私有字段会让严格的 OpenAI 代理（one-api / LiteLLM）直接 400。
    """
    _, body = await _recognize('{"blocks": [{"type": "text", "text": "甲"}]}', {})

    assert "vllm_xargs" not in body
    assert "skip_special_tokens" not in body
    assert set(body) == {"model", "messages", "stream"}


async def test_dsocr2_without_grounding_tags_leaves_a_visible_note():
    """标签被上游吃掉时**必须留痕**，不能只是安静地把 bbox 全填 null。

    模型返回了文字、却一个 grounding 标签都没有，几乎只可能是
    skip_special_tokens 没生效。这时每条出处都不能裁剪，
    而现有的每一条路径都不会报错 —— 正是这个项目最忌讳的静默降级。
    """
    built, _ = await _recognize("识别出来的正文，但没有任何标签",
                                {"dialect": "deepseek-ocr2"})

    notes = built.get("engine_notes") or []
    assert any("dsocr2_no_grounding" in n for n in notes), built
    assert built["pdf_info"][0]["para_blocks"][0]["bbox"] is None


async def test_dsocr2_with_grounding_leaves_no_note():
    """正常路径不该有噪音 —— 留痕机制只在真出事时说话。"""
    built, _ = await _recognize(DSOCR2_PAGE, {"dialect": "deepseek-ocr2"})

    assert "engine_notes" not in built
    assert built["pdf_info"][0]["para_blocks"][0]["bbox"] is not None


def test_dsocr2_strips_the_stop_string_from_the_last_block():
    """`include_stop_str_in_output: true` 会把结束符留在返回文本里。

    最后一个块的正文一路取到字符串结尾 —— 不剥掉的话
    `<｜end▁of▁sentence｜>` 就成了正文的一部分，跟着进检索索引和出处文本，
    而且**不会有任何报错**。官方脚本也是先 replace 掉再解析的。
    """
    from ddp_gateway.services import dsocr2

    raw = (DSOCR2_PAGE + "<｜end▁of▁sentence｜>")
    page = dsocr2.page_from_output(raw, 0, 612, 792)
    last = page["para_blocks"][-1]
    assert layout.block_text(last) == "$E = mc^2$"
    assert "end" not in layout.block_text(last)
    assert "end▁of▁sentence" not in dsocr2.strip_tags(raw)


def test_dsocr2_keeps_text_that_appears_before_the_first_tag():
    """第一个 grounding 标签之前的文字**不许丢**。

    正常输出里没有这一段，但模型偶尔会先说一句再开始报版面。
    从第一个标签开始遍历会把它悄悄丢掉 —— 丢的是文档内容本身，
    下游只会看到"这段原文检索不到"，全程无报错。
    位置确实不知道，所以 bbox 留 None（不编一个）。
    """
    from ddp_gateway.services import dsocr2

    raw = "这是模型多说的一句开场白\n" + DSOCR2_PAGE
    blocks = dsocr2.blocks_from_output(raw, 612, 792)
    assert layout.block_text(blocks[0]) == "这是模型多说的一句开场白"
    assert blocks[0]["bbox"] is None
    assert blocks[0]["type"] == "text"
    # 后面的块一个不少，顺序不变
    assert [b["type"] for b in blocks[1:]] == ["title", "text", "table", "equation"]


async def test_dsocr2_max_tokens_leaves_room_for_the_prompt():
    """`prompt_tokens + max_tokens` 必须放得进 8192 的上下文窗口。

    官方离线脚本写的是 max_tokens=8192，那是 LLM 类直连的用法。
    走 OpenAI 接口照抄的话，加上视觉 token（一页 256~1120 个）必然越界，
    **每个请求都 400** —— 而错误信息只说"上下文超了"，看不出是这里抄错了。
    """
    from ddp_gateway.services import vlm_ocr

    _, body = await _recognize(DSOCR2_PAGE, {"dialect": "deepseek-ocr2"})
    # 模型 config 的 max_position_embeddings = 8192；给视觉 token 留够余量
    assert body["max_tokens"] <= 8192 - 2048, body["max_tokens"]
    assert body["max_tokens"] == vlm_ocr._DSOCR2_MAX_TOKENS

    # 注册表可以调大，但调过头一样会 400 —— 这里只钉住缺省值是安全的
    _, custom = await _recognize(DSOCR2_PAGE,
                                 {"dialect": "deepseek-ocr2", "max_tokens": 2048})
    assert custom["max_tokens"] == 2048


def test_dsocr2_chat_template_emits_bos():
    """部署用的 chat template **必须**输出 BOS。

    这是本项目在真机上撞到过的最贵的一个坑（2026-08-25，4090D）：
    少了 BOS，服务健康、请求 200、token 数正常，但模型输出是彻底的垃圾
    （`Free OCR.` 吐 "PUBLIC DATA / ## 10 10 10 10…" 复读到 max_tokens），
    **没有任何报错**。原因是官方脚本 tokenize 时 `bos=True`，
    而 vLLM 渲染完模板是用 `add_special_tokens=False` 分词的。

    这条守的是"以后有人来精简这个模板"。理由写在模板自己的注释里。
    """
    tpl = REPO_ROOT / "infra" / "autodl" / "chat-template-deepseek-ocr2.jinja"
    assert tpl.exists(), f"部署模板不见了：{tpl}"
    body = tpl.read_text(encoding="utf-8")
    # 注释块里也会提到 BOS，所以要断言的是**真的输出语句**
    bos = "{{- '<｜begin▁of▁sentence｜>' -}}"
    assert bos in body, "模板没有输出 BOS —— 模型会吐垃圾且不报错，见模板注释"
    # **位置也要断言，不能只查存在。** 坏掉的方式不止"删掉它"：把这句挪到
    # 消息循环之后，渲染出的 prompt 就不再以 BOS 开头 —— 与彻底删掉是同一个
    # 故障（模型吐垃圾、零报错），而只查子串的断言对这种改法一片绿。
    # venv 里没有 jinja2，做不了真渲染；比较两者在模板里的先后是最省的等价判据。
    assert body.index(bos) < body.index("{%- for message in messages -%}"), \
        "BOS 必须在消息循环之前 —— 渲染出的 prompt 要以它开头，否则等于没有"
    # 反过来：模板绝不能自己写 <image>，那会变成两个占位符
    body_no_comment = body.split("-#}", 1)[-1]
    assert "<image>" not in body_no_comment, \
        "模板里不能自己写 <image> —— vLLM 会按 image_url 部件位置插入"


# ---- 用**真模型输出**回归（fixtures/dsocr2-real-output.json）----
# 上面那些用例喂的是手写样例，钉的是"我以为的格式"。这一条喂的是
# 2026-08-25 在 4090D 上 DeepSeek-OCR-2 对 tests/fixtures/contract.pdf
# 真吐出来的东西，钉的是"它实际的格式"。两者都要有：
# 手写样例覆盖边界，真实样本保证我们没有在对着幻想编程。

REAL_OUTPUT = FIXTURES / "dsocr2-real-output.json"


@pytest.mark.skipif(not REAL_OUTPUT.exists(), reason="缺真模型输出夹具")
def test_dsocr2_parses_real_model_output():
    """真实输出 -> 合规版面，且内容与 contract.truth.json 对得上。"""
    from ddp_gateway.services import dsocr2

    captured = json.loads(REAL_OUTPUT.read_text(encoding="utf-8"))
    truth = json.loads((FIXTURES / "contract.truth.json").read_text(encoding="utf-8"))

    # 生产里传的是 PDF 页尺寸（坐标是 0~999 归一化，与渲染倍率无关）
    pages = [dsocr2.page_from_output(captured[str(i)]["raw"], i, 612.0, 792.0)
             for i in range(len(captured))]
    assert all(p is not None for p in pages), "真实输出里没解析出 grounding 标签"

    built = layout.build_pages(pages, engine="vlm-ocr")
    assert layout.validate(built) == [], layout.validate(built)

    blocks = [b for p in built["pdf_info"] for b in p["para_blocks"]]
    # 真机实测 18 块；这里只钉"有内容且每块都有 bbox"，别把块数写死
    assert len(blocks) >= 10, len(blocks)
    assert all(b["bbox"] for b in blocks), "有块没有 bbox —— 真实输出里本该每块都有"
    for page in built["pdf_info"]:
        for b in page["para_blocks"]:
            x0, y0, x1, y1 = b["bbox"]
            assert 0 <= x0 < x1 <= 612.1 and 0 <= y0 < y1 <= 792.1, b["bbox"]

    text = " ".join(layout.block_text(b) for b in blocks)
    for fact in ("PURCHASE AGREEMENT", "NW-2026-0817",
                 "Northwind Trading Company Limited", "486,200.50"):
        assert fact in text, f"真实输出里没识别出 {fact!r}"


@pytest.mark.skipif(not REAL_OUTPUT.exists(), reason="缺真模型输出夹具")
def test_dsocr2_real_table_keeps_structure_and_text():
    """真实输出里的表格：HTML 结构与标准答案一致，同时留得下可检索文本。"""
    from ddp_gateway.services import dsocr2

    captured = json.loads(REAL_OUTPUT.read_text(encoding="utf-8"))
    truth = json.loads((FIXTURES / "contract.truth.json").read_text(encoding="utf-8"))
    expected_html = truth["pages"][1]["tables"][0]

    page = dsocr2.page_from_output(captured["1"]["raw"], 1, 612.0, 792.0)
    tables = [b for b in page["para_blocks"] if b["type"] == "table"]
    assert tables, "第 2 页应当有表格块"

    html = layout.table_html(tables[0])
    assert html == expected_html, f"表格 HTML 与标准答案不符：\n{html}"
    # **结构进 html，文本也要留一份** —— 只存 HTML 的话"表里那个数"检索不到
    cell_text = layout.block_text(tables[0])
    for cell in ("Industrial bearing 6204", "410.40", "Quantity"):
        assert cell in cell_text, f"表格文本里缺 {cell!r}"
    assert "<td>" not in cell_text


async def test_free_ocr_mode_does_not_cry_wolf():
    """**阻塞-7 的守卫（2026-08-26 验收）：没要过 grounding 就不许报"标签没来"。**

    `options.grounding: false` 是受支持的用法（走官方 `Free OCR.`，
    全页一个块、本来就没有 bbox）。旧实现只判方言不判 grounding，
    于是这条路**每次解析**都会往归档的版面里写一条"静默失效"告警。
    狼来了会毁掉这个信号本身 —— 而它是"任何降级都必须可见"在这条链路上的唯一落点。
    """
    from ddp_gateway.services import layout as layout_mod

    built, body = await _recognize(
        "整页纯文本，没有任何 grounding 标签",
        {"dialect": "deepseek-ocr2", "grounding": False})

    # 确实走了 Free OCR. 那条官方 prompt
    assert body["messages"][0]["content"][1]["text"] == "Free OCR."
    assert layout_mod.ENGINE_NOTES not in built, \
        f"没要过 grounding 却报了标签缺失：{built.get(layout_mod.ENGINE_NOTES)}"


async def test_skip_special_tokens_cannot_be_overridden_by_the_registry():
    """`skip_special_tokens` 不接受注册表覆盖 —— 模块注释称它是"最要命的一行"。

    它一旦变成 true，`<|ref|>`/`<|det|>` 会在返回前被剥光：
    bbox 全为 null 且**没有任何报错**，出处功能整体失效而无人察觉。
    能配的东西迟早会有人配错，所以这里根本不给配。
    """
    _, body = await _recognize(DSOCR2_PAGE, {
        "dialect": "deepseek-ocr2",
        "skip_special_tokens": True,        # 有人手贱配了
    })
    assert body["skip_special_tokens"] is False, \
        "注册表把 skip_special_tokens 顶成了 True —— 所有 bbox 会静默变 null"


async def test_max_tokens_cannot_exceed_the_context_window():
    """注册表把 max_tokens 配大了要被钳住，不能让它把每个请求送成 400。

    走 OpenAI 接口时 prompt_tokens + max_tokens 必须 <= max_model_len(8192)，
    而一页视觉 token 就有 256~1120 个。配成 8192 的话每个请求都 400，
    而 `_recognize_page` 会静默吞掉异常，最终抛的是
    "模型不可达，或返回全为空" —— **配置错误被报成网络错误**，排查方向全歪。
    """
    _, body = await _recognize(DSOCR2_PAGE,
                               {"dialect": "deepseek-ocr2", "max_tokens": 8192})
    assert body["max_tokens"] <= 4096, body["max_tokens"]


def test_truncated_output_does_not_leak_half_a_tag_into_a_block():
    """被 max_tokens 截断时尾部留下的半个标签，不许跟着块正文进索引。

    这不是臆想的输入：防复读处理器存在的理由就是 OCR 模型会复读到 max_tokens，
    而**截断处必然是半个标签**。`strip_tags` 的两遍剥离只覆盖兜底路径，
    正常 grounding 路径的块正文走的是另一条代码路。
    """
    from ddp_gateway.services import dsocr2

    raw = ("<|ref|>text<|/ref|><|det|>[[10,10,200,50]]<|/det|>\n第一段正文\n"
           "<|ref|>text<|/ref|><|det|>[[10,60,200,90]]<|/det|>\n尾段 <|ref|>text compa")
    blocks = dsocr2.blocks_from_output(raw, 612, 792)
    texts = [layout.block_text(b) for b in blocks]
    assert not any("<|ref|>" in x or "<|det|>" in x for x in texts), texts
    assert "尾段 text compa" in texts[-1] or "尾段" in texts[-1], texts


async def test_max_tokens_zero_or_negative_is_also_clamped():
    """下界同样要挡：0/负数一样让每个请求 400。

    而单页失败是静默吞的，最终抛的是"模型不可达，或返回全为空" ——
    配置错误伪装成网络错误，正是钳制这件事要防的那个后果。
    """
    _, body = await _recognize(DSOCR2_PAGE,
                               {"dialect": "deepseek-ocr2", "max_tokens": -1})
    assert body["max_tokens"] >= 1, body["max_tokens"]


def test_strip_tags_also_removes_half_a_tag():
    """半个标签也要剥掉 —— 兜底路径遇到的正是残缺输出。

    走到 strip_tags 这条兜底路径的输出，十有八九**就是标签残缺**的那一类：
    真机上少 BOS 时模型吐的就是 `<|ref|>text compared with in 45 c`
    （连 `<|/ref|>` 都配不齐）。只剥完整四元组的话这半个标签原样穿透，
    跟着进检索索引和出处文本，全程无报错。
    """
    from ddp_gateway.services import dsocr2

    raw = "<|ref|>text compared with in 45 c"
    assert dsocr2.strip_tags(raw) == "text compared with in 45 c"
    # 落单的 det 标签同理
    assert dsocr2.strip_tags("正文<|det|>[[1,2,3,4]]") == "正文[[1,2,3,4]]"

# ---- 随仓库发布的注册表：形状守卫 ----
# 起因（2026-08-26 验收）：`no_instruct` 与 `transcribe_prompt` 加在了
# models.yaml / models.cpu.yaml / models.autodl.yaml 上，**唯独漏了
# models.dev-host.yaml** —— 而漏标的两个后果都不报错：
#   抽值挑中 OCR 专用模型 -> 假的 not_found（系统能力缺失伪装成"文档里没有"）
#   核对拿中文指令去问它  -> 每条出处被误判 parse_mismatch
# 在这之前 conftest 只加载 models.yaml，另外三份**从没被任何测试碰过**，
# 连能不能 parse 都没人验过。这一组补上那道网。

REGISTRIES = sorted(REGISTRY.glob("models*.yaml"))

# OCR 专用模型：只会把图上的字抄出来，不会遵循指令。判据是名字里带 ocr。
# 认得出来就必须标 no_instruct + 给 transcribe_prompt。
_OCR_ONLY_HINT = "ocr"


@pytest.mark.parametrize("path", REGISTRIES, ids=lambda p: p.name)
def test_every_shipped_registry_parses(path):
    """随仓库发布的注册表都必须能被 load_registry 读出来。

    不是凑数：三份注册表此前从没被加载过，一个 YAML 手误就能让
    "换个部署档位"在真机上第一步就炸，而本机测试一片绿。
    """
    from ddp_gateway.config import load_registry

    registry = load_registry(path)
    assert registry.parse_engines, f"{path.name} 一个解析引擎都没注册"


@pytest.mark.parametrize("path", REGISTRIES, ids=lambda p: p.name)
def test_shipped_vqa_entries_declare_capabilities_explicitly(path):
    """随仓库发布的 `vqa_models` 条目**必须自己写 capabilities**，不许靠段名回填。

    段名缺省会把没写 capabilities 的 vqa 条目补成 `[vision]`
    （`config.SECTION_CAPABILITIES`）。这个缺省对"vqa 段里只住视觉模型"的
    旧世界是对的，但 `no_instruct` 之后这一段开始住纯文本模型了 ——
    于是"没写"就等于"被默默声明成看得见图"，而视觉核对会挑中它，
    **每条好出处都被判成 parse_mismatch**。
    这不是假想：`DeepDocParse-Web/deploy/docker.bash` 生成的注册表正好踩了它。

    强制显式声明，新条目就没机会被默默贴错标。

    **能挡到哪儿，说清楚**：这条挡的是"忘了写"。有人**明知故犯**地给纯文本
    条目写上 `[vision]`，从注册表本身是分辨不出来的（里面没有别的真值来源），
    任何只读注册表的守卫都到此为止。旁边那条按名字认 OCR 模型的守卫同理。
    """
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("vqa_models") or {}
    if not section:
        pytest.skip(f"{path.name} 没有 vqa 段（无 GPU 档）")
    for name, entry in section.items():
        assert (entry or {}).get("capabilities"), (
            f"{path.name} 的 {name} 没写 capabilities —— 会被段名默默补成 [vision]，"
            f"若它其实是纯文本模型，视觉核对会挑中它并把每条好出处判成 parse_mismatch")


@pytest.mark.parametrize("path", REGISTRIES, ids=lambda p: p.name)
def test_capability_labels_are_internally_consistent(path):
    """**与名字无关**的一致性不变式，补上名字判据的天花板。

    下面那条守卫按名字里有没有 "ocr" 认 OCR 专用模型 —— 注册表里没有别的
    真值来源，但它管不住改名字的：二次验收实测把条目改名 `dsv2-transcriber`
    并同时去掉两个标，12 条守卫一条不红，两个 bug 原样复活。

    这两条不依赖名字：
      不听指令的模型**按定义**就需要原生 prompt -> no_instruct ⇒ transcribe_prompt
      声明了怎么问它抄字的，**按定义**就得看得见图 -> transcribe_prompt ⇒ vision
    """
    from ddp_gateway.config import load_registry
    from ddp_gateway.services.extraction import NO_INSTRUCT, VISION

    registry = load_registry(path)
    for name, entry in registry.vqa_models.items():
        caps = entry.capabilities or []
        prompt = (entry.options or {}).get("transcribe_prompt")
        if NO_INSTRUCT in caps:
            assert prompt, (
                f"{path.name} 的 {name} 标了 {NO_INSTRUCT} 却没给 transcribe_prompt —— "
                f"不听指令的模型只认自己的原生 prompt，拿缺省中文指令去问它，"
                f"每条出处都会被误判 parse_mismatch")
        if prompt:
            assert VISION in caps, (
                f"{path.name} 的 {name} 给了 transcribe_prompt 却没标 {VISION} —— "
                f"抄写是看图的活，标不上视觉核对根本挑不到它")


@pytest.mark.parametrize("path", REGISTRIES, ids=lambda p: p.name)
def test_ocr_only_entries_are_labelled_in_every_registry(path):
    """OCR 专用模型在**每一份**注册表里都要标 no_instruct + transcribe_prompt。

    这条守的是"改了三份漏了第四份"。两个漏标后果都不报错，见本节开头。
    """
    from ddp_gateway.config import load_registry
    from ddp_gateway.services.extraction import NO_INSTRUCT

    registry = load_registry(path)
    if not registry.vqa_models:
        # 显式 skip 而不是让空循环变成绿的：一条恒真的守卫会让后人
        # 以为该性质被钉住了。"passed" 必须意味着"真的看过"
        pytest.skip(f"{path.name} 没有 vqa 段（无 GPU 档）")
    for name, entry in registry.vqa_models.items():
        if _OCR_ONLY_HINT not in name.lower():
            continue
        assert NO_INSTRUCT in entry.capabilities, (
            f"{path.name} 的 {name} 是 OCR 专用模型却没标 {NO_INSTRUCT} —— "
            f"/v1/extract 会拿它硬抽，抽出假的 not_found")
        assert (entry.options or {}).get("transcribe_prompt"), (
            f"{path.name} 的 {name} 没给 transcribe_prompt —— "
            f"视觉核对会拿中文指令去问它，每条出处都会被误判 parse_mismatch")


@pytest.mark.parametrize("path", REGISTRIES, ids=lambda p: p.name)
def test_vqa_section_can_always_do_visual_verification(path):
    """注册了 vqa 段的话，里面至少得有一个**看得见图**的条目。

    加了 no_instruct 之后 vqa_models 段第一次住进了纯文本模型；
    整段全是纯文本时视觉核对无从做起，而那条路只会静默地退成
    vision_unavailable —— 部署时就该发现，不该等到线上。
    """
    from ddp_gateway.config import load_registry
    from ddp_gateway.services.extraction import VISION

    registry = load_registry(path)
    if not registry.vqa_models:
        pytest.skip(f"{path.name} 没有 vqa 段（无 GPU 档）")
    assert any(VISION in entry.capabilities for entry in registry.vqa_models.values()), \
        f"{path.name} 的 vqa 段里没有任何看得见图的条目，视觉核对做不了"
