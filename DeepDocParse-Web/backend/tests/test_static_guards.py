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
    """检索调用必须显式传 `min_similarity=settings.qa_min_similarity`。

    **这条守的是不变式 1：出处必须是真出处。** 下限的作用是让"向量路判定全都
    不相关"的问题不要靠共现词捞出 chunk 来充当出处 —— 而 `verified` 只看有没有
    裁剪图，于是假出处还会被打上"已做视觉验证"，比不给出处更糟。

    问答路有行为断言钉着（test_qa.py），但抽取路与跨文档检索路没有：
    阶段 2a 验收把这两处的 `settings.qa_min_similarity` 换成 `0.0`，**152 例全绿**。
    行为断言给这两条路补起来代价不小（要造"词面命中但语义不相关"的语料），
    而形状守卫在这里够用：少传或写死字面量都会红。
    """
    violations = []
    for path, call in _search_calls():
        where = f"{path.relative_to(BACKEND)}:{call.lineno}"
        kw = next((k for k in call.keywords if k.arg == "min_similarity"), None)
        if kw is None:
            violations.append(f"{where} 没传 min_similarity")
        elif not (isinstance(kw.value, ast.Attribute) and kw.value.attr == FLOOR):
            violations.append(f"{where} 的 min_similarity 是 {ast.unparse(kw.value)}，"
                              f"不是 settings.{FLOOR}")
    assert not violations, (
        "检索少了相似度下限 —— 低于下限的词面命中会成为出处，且被打上 verified：\n  "
        + "\n  ".join(violations))


def test_the_search_scan_actually_finds_the_call_sites():
    """防止上面那条因为匹配不到调用而恒真（改个变量名就能让它静默失效）。"""
    calls = _search_calls()
    files = {p.relative_to(BACKEND).as_posix() for p, _ in calls}
    assert len(calls) >= 3, f"只扫到 {len(calls)} 处 index.search，匹配逻辑可能坏了"
    # 三条检索路各一处：问答、抽取、跨文档检索。少了任何一条都说明扫漏了
    assert files >= {"app/qa.py", "app/extraction.py", "app/routers/search.py"}, \
        f"三条检索路没扫全，实际扫到 {sorted(files)}"
