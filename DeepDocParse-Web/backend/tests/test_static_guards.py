"""**静态守卫**：不连库、不 import 被测模块，只解析源码就能查出来的错。

住在这里的都有一个共同点 —— **常规单测结构上覆盖不到，漏改不会红**：
迁移文件从不被 import；脚本也不被 import；而 `index.search()` 少传一个参数，
跑起来照样是一份"看着挺像样"的答案。

§1 迁移与脚本里 import 的模块必须真的存在
§2 每一次检索都必须带相似度下限

---

§1 **为什么需要这条**：阶段 1 把 `app/tokenize.py` 等模块搬进了 `ddp_core`，
但 `alembic/versions/0005_*.py:128` 里那句 `from app.tokenize import tokenized`
没跟着改。已有库早就过了 0005，所以升级路径上看不见 —— 而**任何全新部署
都会死在那里并停在 0004**（`quickstart.sh`、CI 接真库、灾备重建）。
阶段 1 的验收放过了它，阶段 2a 的验收才抓到。

两套 pytest 都覆盖不到这类问题：迁移文件从不被 import，脚本也不被 import。
所以单独用一条**静态**守卫扫它们 —— 不需要数据库，不需要跑迁移。

§2 见文件下半部分 `test_every_retrieval_passes_the_similarity_floor` 的说明。
"""
import ast
import importlib
import importlib.util
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
REPO = BACKEND.parent

# 扫这些地方的 import：它们都不会被单测 import 到，因此漏改不会红
SCAN_DIRS = [BACKEND / "alembic" / "versions", REPO / "scripts"]

# 只查这两个顶层包 —— 第三方依赖由安装环节保证，不该在这里重复校验
OWNED_PREFIXES = ("app.", "ddp_core.")


def _local_imports(path: pathlib.Path) -> set[str]:
    """文件里所有指向本项目两个顶层包的 import（含函数体内的惰性 import）。"""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(OWNED_PREFIXES):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(OWNED_PREFIXES):
                    found.add(alias.name)
    return found


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            out.extend(sorted(p for p in d.glob("*.py") if p.name != "__init__.py"))
    return out


@pytest.mark.parametrize("path", _files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_migrations_and_scripts_import_modules_that_exist(path):
    """迁移与脚本引用的每一个 app.* / ddp_core.* 模块都要能 import。

    **只解析不执行** —— 迁移里的 import 大多在函数体内，真跑要连数据库。
    这里用 importlib 查模块存不存在就够了：漏改的表现正是"模块没了"。
    """
    missing = []
    for module in sorted(_local_imports(path)):
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            missing.append(module)
    assert not missing, (
        f"{path.relative_to(REPO)} 引用了不存在的模块：{missing}。"
        f"搬家之后忘了改这里 —— 迁移与脚本都不被单测 import，漏改不会红，"
        f"但全新部署会死在这一步"
    )


def test_the_scan_actually_covers_something():
    """防止上面那条因为扫不到文件而恒真（空 parametrize 会静默通过）。"""
    files = _files()
    assert len(files) >= 6, f"扫到的文件太少（{len(files)}），SCAN_DIRS 是不是写错了"
    total = sum(len(_local_imports(f)) for f in files)
    assert total >= 5, f"一条本项目的 import 都没扫到（{total}），解析逻辑可能坏了"


def test_quickstart_passes_corpus_credentials_to_the_mcp_service():
    """干净部署的随机 PG/MinIO 密钥必须同步给 service 侧 MCP。

    只写 Web `.env` 时所有容器都会健康，直到第一次 MCP 调用才报数据库认证失败；
    这是部署脚本特有的静默错位，常规 API 单测与 compose config 都抓不到。
    """
    text = (REPO / "quickstart.sh").read_text(encoding="utf-8")
    start = text.index("write_service_env()")
    end = text.index("write_frontend_env()", start)
    body = text[start:end]
    required = {
        "CORPUS_DATABASE_URL", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY", "MINIO_BUCKET", "MCP_PUBLIC_BASE_URL",
    }
    missing = sorted(key for key in required
                     if f'set_env "$SERVICE_ENV" {key}' not in body)
    assert not missing, f"quickstart 没把这些语料 MCP 配置写进 service .env：{missing}"
    assert "host.docker.internal:15432" in body
    assert "host.docker.internal:19000" in body


# ---------------------------------------------------------------------------
# §2 每一次检索都必须带相似度下限
# ---------------------------------------------------------------------------

APP = BACKEND / "app"

# 下限的唯一来源。写死成字面量（哪怕数值一样）也算违规：
# 阈值要能被一处配置改掉，散落的字面量会让"调阈值"变成一次全仓库搜索
FLOOR = "qa_min_similarity"


def _search_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    """`app/` 下所有 `<...>index.search(...)` 调用。"""
    out: list[tuple[pathlib.Path, ast.Call]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "search"
                    and ast.unparse(node.func.value).endswith("index")):
                out.append((path, node))
    return out


def test_every_retrieval_passes_the_similarity_floor():
    """检索调用必须在 SearchIndex 或紧随其后的可审计门控应用统一下限。

    **这条守的是不变式 1：出处必须是真出处。** 下限的作用是让"向量路判定全都
    不相关"的问题不要靠共现词捞出 chunk 来充当出处 —— 而 `verified` 只看有没有
    裁剪图，于是假出处还会被打上"已做视觉验证"，比不给出处更糟。

    问答路有行为断言钉着（test_qa.py），但抽取路与跨文档检索路没有：
    阶段 2a 验收把这两处的 `settings.qa_min_similarity` 换成 `0.0`，**152 例全绿**。
    行为断言给这两条路补起来代价不小（要造"词面命中但语义不相关"的语料），
    阶段 6 的问答路是唯一例外：它要保留低分候选和拒绝原因，所以 SearchIndex
    不得先丢数据，必须取全后立刻交给 `gate_candidates`。其余检索路仍在 search
    调用处应用下限。两种形状都在这里钉死。
    """
    violations = []
    for path, call in _search_calls():
        where = f"{path.relative_to(BACKEND)}:{call.lineno}"
        kw = next((k for k in call.keywords if k.arg == "min_similarity"), None)
        if kw is None:
            violations.append(f"{where} 没传 min_similarity")
        elif path.name == "qa.py" and ast.unparse(kw.value) == "-1.01":
            continue
        elif not (isinstance(kw.value, ast.Attribute) and kw.value.attr == FLOOR):
            violations.append(f"{where} 的 min_similarity 是 {ast.unparse(kw.value)}，"
                              f"不是 settings.{FLOOR}")
    assert not violations, (
        "检索少了相似度下限 —— 低于下限的词面命中会成为出处，且被打上 verified：\n  "
        + "\n  ".join(violations))


def test_qa_deferred_gate_uses_the_configured_floor():
    """问答取全候选后必须用同一配置门控；否则假出处会从后门进入回答。"""
    path = APP / "qa.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func).endswith("gate_candidates")]
    assert len(calls) == 1, f"预期唯一 gate_candidates 调用，实际 {len(calls)}"
    kw = next((item for item in calls[0].keywords if item.arg == "min_similarity"), None)
    assert kw is not None
    assert isinstance(kw.value, ast.Attribute) and kw.value.attr == FLOOR, \
        f"问答门控阈值不是 settings.{FLOOR}: {ast.unparse(kw.value)}"


def test_the_search_scan_actually_finds_the_call_sites():
    """防止上面那条因为匹配不到调用而恒真（改个变量名就能让它静默失效）。"""
    calls = _search_calls()
    files = {p.relative_to(BACKEND).as_posix() for p, _ in calls}
    assert len(calls) >= 3, f"只扫到 {len(calls)} 处 index.search，匹配逻辑可能坏了"
    # 三条检索路各一处：问答、抽取、跨文档检索。少了任何一条都说明扫漏了
    assert files >= {"app/qa.py", "app/extraction.py", "app/routers/search.py"}, \
        f"三条检索路没扫全，实际扫到 {sorted(files)}"


# ---------------------------------------------------------------------------
# §3 出处锚定判据只许有一份
# ---------------------------------------------------------------------------


def test_every_anchor_judgement_is_the_same_function():
    """读路径、历史回填、老的接回逻辑，用的必须是**同一个函数对象**。

    判据回答的是同一个问题："这条出处指的，还是当初那段原文吗？"
    三处一旦有差异，回填就会把一批本来对得上的标成失效（可惜，但安全），
    或者把对不上的标成有效 —— **那就是带着已验证标记的假出处**
    （plan.md §9 不变式 1）。

    这条守的是"同一个对象"而不是"行为一致"：行为一致要靠穷举输入才能证明，
    而同一个对象是结构性的，改不掉。
    """
    from ddp_core import anchor
    import app.backfill as backfill
    import app.evidence as evidence

    assert evidence.same_content is anchor.same_content
    assert backfill.same_content is anchor.same_content
    # 阶段 4 删掉了 app.qa 里那份老的接回逻辑（连同 messages.citations 列）。
    # **判据的实现只许有一处** —— 全仓库扫一遍，除 anchor.py 外不许再出现
    import pathlib

    reimplemented = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if 'rstrip("…")' in text:
            reimplemented.append(path.name)
    assert not reimplemented, \
        f"{reimplemented} 又自己实现了一遍锚定判据 —— 判据一漂就会产出假出处"


def test_the_two_anchor_paths_really_differ():
    """严格路与宽松路**不能是同一件事** —— 否则 content_digest 白加了。

    反哨兵：如果 `same_content` 退化成只看 snippet，指纹就形同虚设，
    而"块尾被改掉"这类改动正好是 snippet 判据抓不到、指纹能抓到的。
    """
    from ddp_core.anchor import digest_of, same_content

    original = "开头这段话会被存进 snippet，后面还有很长一截内容。"
    tampered = original + "（后来被追加的一段，snippet 里看不见）"
    snippet = original[:12]

    # 宽松路：snippet 还在里面 -> 放行（这正是老判据的盲区）
    assert same_content(snippet=snippet, chunk_text=tampered, digest="") is True
    # 严格路：指纹对不上 -> 拦住
    assert same_content(snippet=snippet, chunk_text=tampered,
                        digest=digest_of(original)) is False


def test_citation_shape_has_exactly_one_implementation():
    """出处的对外形状只许有一处实现（阶段 4 合并的）。

    问答与抽取必须给出完全一样的形状 —— 前端的 CitationChip 两边共用一个组件。
    此前是各写一份，靠抽取那份 docstring 里"形状必须与 conversations 一致"
    这句话维持，而**靠注释维持的一致性迟早会破**：阶段 3 就抓到过
    两份接回逻辑走岔的后果（抽取的出处被无条件标成有效）。
    """
    import ast

    hits = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.endswith("citation_out"):
                hits.append(f"{path.relative_to(BACKEND)}::{node.name}")
    assert hits == ["app/evidence.py::citation_out"], \
        f"出处形状又出现了第二份实现：{hits}"


def test_fresh_is_an_assertion_not_a_default():
    """`resolved` 不许有默认值 —— 忘了算就必须缺，不能静默变成"有效"。

    写成 `setdefault("resolved", True)` 的话，任何漏算 resolved 的路径都会被
    判成有效出处。阶段 3 抓到的抽取平面 bug 就是这么来的。
    """
    from app.evidence import citation_out

    plain = citation_out("d1", {"seq": 0, "crop_key": None})
    assert "resolved" not in plain, "没人给 resolved，却被兜底成了有效"
    assert citation_out("d1", {"seq": 0, "crop_key": None}, fresh=True)["resolved"] is True


# ---------------------------------------------------------------------------
# §3 知识层可以整体关掉 —— 既有问答/检索链路不许 import 它
# ---------------------------------------------------------------------------

# 阶段 7 之前就存在、且被 `eval_agent` 那张表衡量的生产路径
QA_PATH = (
    BACKEND / "app" / "qa.py",
    BACKEND / "app" / "extraction.py",
    BACKEND / "app" / "indexing.py",
    BACKEND / "app" / "routers" / "conversations.py",
    BACKEND / "app" / "routers" / "search.py",
)
KNOWLEDGE_MODULES = ("app.knowledge", "app.review_export", "app.routers.knowledge",
                     "ddp_core.knowledge")


def test_the_existing_qa_path_does_not_import_the_knowledge_layer():
    """plan.md 阶段 7 验收的「图关掉后既有指标不变」，结构上的那一半。

    数值那一半在 `scripts/eval_graph.py`：对阶段 6 报表记录的基线逐字段比。
    **但只比数字挡不住"知识层被悄悄挂进问答链"** —— 那种改动会让基线一起漂，
    而人很容易顺手把基线也改了（`agent_baseline` 就在仓库里）。
    这条守卫改成机械可判的：这几个文件里出现任何一个知识层模块就红。

    反过来说，`app/routers/knowledge.py` 依赖问答侧的东西（evidence / metering）
    是允许的 —— 依赖方向只许从新到旧，关掉新的那头不影响旧的。
    """
    offenders = []
    for path in QA_PATH:
        assert path.is_file(), f"{path} 不在了 —— 这条守卫的扫描面得跟着改"
        for module in sorted(_local_imports(path)):
            if any(module == name or module.startswith(name + ".")
                   for name in KNOWLEDGE_MODULES):
                offenders.append(f"{path.relative_to(BACKEND)} -> {module}")
    assert not offenders, (
        f"既有问答链引用了知识层：{offenders}。"
        f"知识层必须能被 knowledge_enabled=False 整体关掉而不动既有指标；"
        f"真要挂进去，先改 plan.md 的验收标准并重跑 eval_agent 定新基线")
