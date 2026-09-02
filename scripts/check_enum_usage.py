#!/usr/bin/env python
"""枚举使用守卫：Python 里出现的降级/状态取值必须都在契约里声明过。

    python scripts/check_enum_usage.py          # 退出码 0 = 通过

## 它守的是什么

契约生成解决的是"三处手写会漂"，但它挡不住**第四种写法**：
有人在 `qa.py` 里直接写 `degraded = "vision_timeout"`，一个契约里没有的新值。
后果不报错 —— 后端如实落库、API 如实返回，而前端的文案表里没有它，
于是界面上显示"已降级（vision_timeout）"；更糟的情况是某些视图按枚举
分支渲染，那条降级干脆**不显示**，而"降级必须可见"是第二条不变式。

所以这把尺子反过来扫：从**用法**回推声明。

## 判据

用 AST 找这几种形状里的字符串字面量，逐个比对契约：

    degraded = "xxx"                    赋值
    x.degraded = "xxx"                  属性赋值
    degraded, ok = "xxx", False         元组解包赋值
    f(degraded="xxx")                   关键字参数
    {"degraded": "xxx"}                 字典字面量
    degraded = ["xxx"] / {"xxx"}        容器字面量（元素逐个算）
    degraded.add("xxx") / .append(…)    往集合/列表里塞
    return None, "xxx"                  return 里的字面量（**按位置**认）
    compile_degraded / index_status / compile_status / status(受限) 同理

**这七种形状不是一次写全的。** 头一版只认单目标赋值、关键字参数与字典，
而代码里 `degraded` 的真实写点几乎全在另外三种形状里
（`conversations.py` 的元组解包 4 处、`compilation.py` 的 `.add()` 6 处、
`indexing.py` 的列表字面量 1 处）—— 也就是说**这把尺子当时一处真正的
写点都没量到**，报的"38 处用法"全是别的枚举。
守卫报绿而完全没覆盖目标，比没有守卫更危险。加形状时请连变异确认一起做。

补完那三种之后**还剩一种**，独立验收数出来的：`return` 表达式里的字面量
（`qa.py` / `compilation.py` / `ddp_mcp/corpus.py` / `ddp_core/rerank.py` 共 18 处）。
后果是 21 个 `degraded` 取值里有 6 个、8 个 `compile_degraded` 里有 1 个
**一处写点都没被量到**。`return` 这一类只能按**位置**认：函数签名声明
返回 `tuple[str | None, str]` 时，第几个位置是 degraded 是函数自己的约定 ——
所以下面用一张显式的函数表，而不是猜。

**不扫 `status`**：这个名字在代码里被复用得太厉害（HTTP 状态、任务状态、
字段状态各一套），扫它只会得到一堆误报，而误报多的守卫最后会被人加白名单
加到失效。要扫的是那些名字唯一、语义唯一的。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 变量/键名 -> 契约里的枚举名
TRACKED = {
    "degraded": "degraded",
    "compile_degraded": "compile_degraded",
    "index_status": "index_status",
    "compile_status": "compile_status",
    "field_status": "field_status",
    "source_type": "source_type",
    "block_type": "block_type",
    "task_status": "task_status",
    "upload_status": "upload_status",
    "actor_kind": "actor_kind",
}

#: 扫哪些树
SCAN_ROOTS = [
    ROOT / "python" / "ddp_core" / "ddp_core",
    ROOT / "services" / "corpus-api" / "ddp_corpus",
    ROOT / "services" / "model-gateway" / "ddp_gateway",
    ROOT / "services" / "mcp" / "ddp_mcp",
    ROOT / "services" / "corpus-worker",
]

#: 局部名撞了、语义没撞的地方：`compile_document` 里的局部变量叫 `degraded`，
#: 装的却是**编译期**降级（最后落到 `compile_degraded` 字段）。
#:
#: **不要改成"两个降级枚举取并集"糊过去** —— 那样一来"把编译期的值
#: 写进问答降级"就再也没人拦得住了，而那正是这把尺子要守的漂移。
#: 逐个文件写清楚，加一条就要说清它为什么是另一个枚举。
NAME_OVERRIDES = {
    ("services/corpus-api/ddp_corpus/compilation.py", "degraded"): "compile_degraded",
}

#: `return` 里哪个位置是哪个枚举。**必须显式列**：返回元组的第几个位置
#: 装什么是函数自己的约定，猜不出来。键是"文件路径::函数名"，
#: 值是 {位置: 枚举名}；位置 -1 表示"返回值本身就是那个枚举"。
#:
#: 加一个返回降级的函数却忘了在这里登记 = 它的取值永远不被检查。
#: `test_every_degraded_value_is_reachable`（见下）会算出"哪些取值一处
#: 写点都没有"，那正是提醒你回来加一行的地方。
RETURN_POSITIONS = {
    "python/ddp_core/ddp_core/rerank.py::rerank_hits": {1: "degraded"},
    "services/corpus-api/ddp_corpus/compilation.py::_understand": {1: "compile_degraded"},
    "services/corpus-api/ddp_corpus/compilation.py::one": {2: "compile_degraded"},
    "services/mcp/ddp_mcp/corpus.py::_embedding": {1: "degraded"},
    "services/model-gateway/ddp_gateway/services/extraction.py::extract_records":
        {1: "degraded"},
}

#: 契约声明了、但**确实没有任何代码把它写进这个字段**的取值。
#: 每一条都要说清为什么，否则它就是"契约里有一个没人用的词"。
KNOWN_UNPRODUCED = {
    "degraded": {
        # 它是 SSE error 帧的 **code**，不是 degraded 字段的取值：
        # 中途断流时 `error.code = upstream_interrupted` 而
        # `degraded = upstream_error`（粗类别）。两者刻意分开，
        # `test_ask_survives_midstream_upstream_failure` 同时钉着这两个值。
        # 它留在 degraded 枚举里是为了有一份用户可见文案（"回答生成中途断流"）。
        "upstream_interrupted",
    },
    "compile_degraded": set(),
}

#: 哪些枚举要做逐取值覆盖检查。只列不变式 2 的那两个 ——
#: 别的枚举（block_type / source_type 之类）有大量取值本来就只在
#: 契约与前端出现，逐取值要求会变成噪音。
REQUIRED_COVERAGE = {name: KNOWN_UNPRODUCED[name] for name in KNOWN_UNPRODUCED}

#: 这些取值出现在被扫的位置上，但**不是**枚举值 —— 逐条写清理由，不许无脑加。
ALLOWED_NON_ENUM = {
    # `degraded` 有时被赋成空串表示"没有降级"（None 与 "" 都出现过）
    ("degraded", ""),
    ("compile_degraded", ""),
    # block_type 归一化的入参是**引擎原生类型**，不是契约词汇表
    ("block_type", "table_caption"),
    ("block_type", "image_caption"),
}


def contract_values() -> dict[str, set[str]]:
    sys.path.insert(0, str(ROOT / "python" / "ddp_contracts"))
    from ddp_contracts import enums

    return {
        name: set(getattr(enums, f"{name.upper()}_VALUES"))
        for name in set(TRACKED.values())
    }


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _str_values(node: ast.AST | None) -> list[str]:
    """一个节点里的字符串字面量：本身是字符串，或是容器字面量的元素。

    `degraded = "a"` 与 `degraded = ["a", "b"]` 语义上是同一件事
    （这个字段两种形状都在用），所以两种都要拆开逐个比对。
    """
    if node is None:
        return []
    if (direct := _const_str(node)) is not None:
        return [direct]
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return [v for elt in node.elts if (v := _const_str(elt)) is not None]
    # `degraded or "no_hits"` / `x if cond else "no_hits"` —— 兜底值那一支
    # 是真正的写点，而它在 qa.py 里正是最常见的写法
    if isinstance(node, ast.BoolOp):
        return [v for value in node.values for v in _str_values(value)]
    if isinstance(node, ast.IfExp):
        return _str_values(node.body) + _str_values(node.orelse)
    return []


def _tracked_name(node: ast.AST, tracked: dict[str, str]) -> str | None:
    """节点指向的被跟踪名字：`degraded` / `self.degraded` 都算。"""
    if isinstance(node, ast.Name) and node.id in tracked:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in tracked:
        return node.attr
    return None


def _returns(tree: ast.AST, rel: str) -> list[tuple[str, str, int]]:
    """按 RETURN_POSITIONS 抽 `return` 里的枚举字面量。"""
    found: list[tuple[str, str, int]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positions = RETURN_POSITIONS.get(f"{rel}::{func.name}")
        if not positions:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            # `return (a, None) if cond else (None, "x")` —— 两支都是返回值，
            # 都要按位置拆。不展开的话这一整支静默漏掉
            branches = ([node.value.body, node.value.orelse]
                        if isinstance(node.value, ast.IfExp) else [node.value])
            for branch in branches:
                for index, enum_name in positions.items():
                    if index == -1:
                        values = _str_values(branch)
                    elif isinstance(branch, ast.Tuple) and index < len(branch.elts):
                        values = _str_values(branch.elts[index])
                    else:
                        values = []
                    found += [(enum_name, v, node.lineno) for v in values]
    return found


def scan(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """返回 [(枚举名, 取值, 行号)]。"""
    found: list[tuple[str, str, int]] = []
    rel = path.relative_to(ROOT).as_posix()
    tracked = dict(TRACKED)
    for (override_path, name), enum_name in NAME_OVERRIDES.items():
        if rel == override_path:
            tracked[name] = enum_name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                # `a, b = x, y` —— 按位置配对，不然这一整类写法全漏
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    for elt, val in zip(target.elts, node.value.elts):
                        if (name := _tracked_name(elt, tracked)) is not None:
                            found += [(tracked[name], v, node.lineno)
                                      for v in _str_values(val)]
                    continue
                if (name := _tracked_name(target, tracked)) is not None:
                    found += [(tracked[name], v, node.lineno)
                              for v in _str_values(node.value)]
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in tracked:
                    found += [(tracked[kw.arg], v, node.lineno)
                              for v in _str_values(kw.value)]
            # `degraded.add("x")` / `parts.append("x")` —— 集合与列表的写点
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr in ("add", "append")
                    and (name := _tracked_name(func.value, tracked)) is not None):
                for arg in node.args:
                    found += [(tracked[name], v, node.lineno) for v in _str_values(arg)]
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                key = _const_str(k) if k is not None else None
                if key in tracked:
                    found += [(tracked[key], val, node.lineno) for val in _str_values(v)]
    return found + _returns(tree, rel)


def main() -> int:
    declared = contract_values()
    problems: list[str] = []
    per_enum: dict[str, int] = {}
    covered: dict[str, set[str]] = {}
    total = 0
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            for enum_name, value, line in scan(path):
                key = (enum_name, value)
                if key in ALLOWED_NON_ENUM:
                    continue
                total += 1
                per_enum[enum_name] = per_enum.get(enum_name, 0) + 1
                covered.setdefault(enum_name, set()).add(value)
                if value not in declared[enum_name]:
                    rel = path.relative_to(ROOT)
                    problems.append(
                        f"{rel}:{line} 用了契约里没有的 {enum_name} 取值 {value!r}")

    # 反哨兵。走过三版，每一版都是被上一版放过去的东西逼出来的：
    #
    #   v1 总数阈值      -> 报着"38 处"而一处 degraded 写点都没量到（全是别的枚举）
    #   v2 逐枚举非零    -> 拆掉两段 scan 后 52->40、compile_degraded 7->2，照样绿
    #   v3 **逐取值覆盖**  <- 现在这版
    #
    # 判据：契约里声明的每一个降级取值，都必须**至少有一处写点被扫到**。
    # 扫描退化时，掉出来的是具体哪几个取值，而不是一个变小的总数。
    for enum_name, expected in REQUIRED_COVERAGE.items():
        seen = covered.get(enum_name, set())
        missing = sorted((declared[enum_name] - seen) - expected)
        if missing:
            problems.append(
                f"{enum_name} 的这些取值一处写点都没扫到：{missing}。"
                f"要么 scan() 漏了某种写法（补形状），"
                f"要么它们真的没人产生（那就从 enums.yaml 里删掉，"
                f"或者写进 KNOWN_UNPRODUCED 并说明理由）")
        stale = sorted(expected & seen)
        if stale:
            problems.append(
                f"{enum_name} 的 {stale} 已经登记在 KNOWN_UNPRODUCED 里，"
                f"但现在扫得到了 —— 把它从那张表里删掉")

    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n新的取值要先加进 packages/contracts/enums.yaml（连同用户可见文案），"
              "再重跑 npm run contracts:gen。", file=sys.stderr)
        return 1
    detail = "，".join(f"{k} {v}" for k, v in sorted(per_enum.items()))
    print(f"枚举使用守卫通过：{total} 处用法全部在契约内（{detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
