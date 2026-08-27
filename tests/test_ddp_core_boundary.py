"""铁律 7 的机械保障：`ddp_core` 是叶子，且分成两层。

两条都是**静态**检查（AST），不需要装任何东西 —— 正因为它们要防的事
恰恰是"某个环境里装不上"。

## 为什么需要

`ddp_core` 住在本仓库，却被 DeepDocParse-Web 安装并 import。两个仓库
**各有一个叫 `app` 的顶层包**，所以 `ddp_core` 里任何一句 `import app.*`
都是错的：在本仓库解析成 gateway 的应用层，在 Web 那边解析成 Web 的，
而两者都不是作者想要的那个。

第二层是依赖切分：`ddp_core` 里碰数据库的那几个模块要 SQLAlchemy，
在 `[corpus]` extra 里；**gateway 自己一行 ORM 都不 import，venv 里压根没装**。
所以 gateway / mcp_server 一旦 import 到那几个模块，容器起来就是
`ModuleNotFoundError` —— 而单测在装了 sqlalchemy 的开发机上照样绿。
（阶段 2a 搬模型时就是被这个 ModuleNotFoundError 撞出来的。）
"""
import ast
import pathlib

CORE = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "ddp_core"
GATEWAY = pathlib.Path(__file__).resolve().parent.parent / "gateway" / "app"
MCP = pathlib.Path(__file__).resolve().parent.parent / "mcp_server"

# 装在 [corpus] extra 里、gateway 的 venv 没有的东西
CORPUS_DEPS = ("sqlalchemy", "pgvector")


def _imports(path: pathlib.Path, *, top_level_only: bool) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = tree.body if top_level_only else ast.walk(tree)
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
    return found


def _core_modules() -> list[pathlib.Path]:
    return sorted(CORE.glob("*.py"))


def test_ddp_core_never_imports_app():
    """`ddp_core` 不得 import 任何 `app.*` —— 它是叶子。

    反向依赖会把 gateway 的应用层（config / task_store / FastAPI）
    整个拖进 Web 的进程，而 Web 那边的 `app` 是另一个包。
    """
    offenders = []
    for path in _core_modules():
        # 连函数体内的惰性 import 一起查：藏进函数里不会让它变得正确，
        # 只会让它在第一次调用时才崩
        for module in _imports(path, top_level_only=False):
            if module == "app" or module.startswith("app."):
                offenders.append(f"{path.name} -> {module}")
    assert not offenders, (
        "ddp_core 反向依赖了 app：" + "; ".join(offenders)
        + "。两个仓库各有一个 app 顶层包，import 谁都是错的")


def test_ddp_core_init_stays_import_free():
    """`__init__.py` 必须零 import。

    有一句 `from ddp_core.models import Base` 就够了：gateway 那边
    `import ddp_core.chunking` 会连带执行 `__init__`，于是缺 sqlalchemy 直接崩。
    这个包的"最小集 / [corpus]"切分全靠它是空的。
    """
    init = CORE / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    bad = [ast.unparse(n) for n in ast.walk(tree)
           if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not bad, f"ddp_core/__init__.py 有 import：{bad} —— 会让最小集装不起来"


def _corpus_modules() -> set[str]:
    """顶层就 import 了 SQLAlchemy/pgvector 的 core 模块 —— 即 [corpus] 那一层。

    **实测而不是写死名单**：写死的话，哪天有人往 `chunking.py` 里加一句
    顶层 `from sqlalchemy import ...`，名单不变、守卫照绿，而 gateway 容器起不来。
    """
    out = set()
    for path in _core_modules():
        if any(m.split(".")[0] in CORPUS_DEPS
               for m in _imports(path, top_level_only=True)):
            out.add(path.stem)
    return out


def test_gateway_does_not_reach_into_the_corpus_layer():
    """gateway / mcp_server 不得 import 需要 SQLAlchemy 的 core 模块。

    它们的 venv 里没有 sqlalchemy（无状态适配层，plan.md §2 的边界）。
    违反的表现是**容器起不来**，而开发机上单测全绿。
    """
    corpus = _corpus_modules()
    assert corpus, "一个 corpus 模块都没识别出来，探测逻辑坏了"

    offenders = []
    for root in (GATEWAY, MCP):
        for path in sorted(root.rglob("*.py")):
            for module in _imports(path, top_level_only=False):
                head = module.split(".")
                if head[0] == "ddp_core" and len(head) > 1 and head[1] in corpus:
                    offenders.append(f"{path.relative_to(root.parent)} -> {module}")
    assert not offenders, (
        f"gateway/mcp_server 碰到了 [corpus] 层（{sorted(corpus)}）：" + "; ".join(offenders)
        + "。它们的 venv 没有 sqlalchemy，容器会起不来")


def test_the_boundary_scan_actually_covers_something():
    """防止上面三条因为扫不到文件而恒真。"""
    files = _core_modules()
    assert len(files) >= 8, f"只扫到 {len(files)} 个 core 模块，路径可能写错了"
    assert _corpus_modules() >= {"models", "search", "types"}, \
        f"corpus 层识别结果不对：{_corpus_modules()}"
    assert any(GATEWAY.rglob("*.py")) and any(MCP.rglob("*.py"))
