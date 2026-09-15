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
    f("xxx") / f(reason="xxx")          登记过的工厂函数的参数（CALL_POSITIONS，按位置与参数名认）
    str(x or "xxx") / f"xxx:{detail}"   兜底值与 f-string 固定前缀（带 `:细节` 的只认登记过的取值）
    "xxx:" + y / "xxx:%s" % y / .format 拼接与模板的固定前缀，规则同 f-string
    result["degraded"] = "xxx"          下标赋值

    **仍然看不见的**（靠 `federation.unavailable_answer` 的运行时检查或验收）：变量中转
    （`reason = "x"; f(reason)`）、函数别名、以占位符开头的 f-string / `%` / `.format` / 拼接、
    条件表达式参与的拼接（`("a" if c else "b") + ":" + x`）、`+=`、`setdefault`、
    字典里值是变量的 `{"answer_reason": var}`。
    compile_degraded / index_status / compile_status / status(受限) 同理

**这些形状不是一次写全的。** 头一版只认单目标赋值、关键字参数与字典，
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
    "answer_reason": "federated_answer_reason",
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
    "services/model-gateway/ddp_gateway/services/extraction.py::extract_records":
        {1: "degraded"},
    # 返回值本身就是答案原因，调用方原样交给 `_delegated_failure` / `unavailable_answer`
    "services/corpus-api/ddp_corpus/federation.py::excerpt_reason": {-1: "federated_answer_reason"},
    "services/corpus-api/ddp_corpus/federation_tasks.py::_receipt_binding_error":
        {-1: "federated_answer_reason"},
    "services/corpus-api/ddp_corpus/federation_tasks.py::_remote_answer_reason":
        {-1: "federated_answer_reason"},
}

#: 调用处**第几个位置参数**是哪个枚举。按函数名认（`federation.unavailable_answer`
#: 与本地的 `unavailable_answer` 是同一个写点）。联邦答案原因几乎全靠这三个工厂
#: 函数写出去 —— 不登记它们，这个枚举就一处写点都量不到。
CALL_POSITIONS = {
    "unavailable_answer": {0: "federated_answer_reason"},
    "_unavailable_answer": {0: "federated_answer_reason"},
    "_delegated_failure": {0: "federated_answer_reason"},
}
#: 同一批函数用关键字传参时的参数名（`_delegated_failure(reason="x")` 也是写点）。
CALL_KEYWORDS = {
    "unavailable_answer": {"reason": "federated_answer_reason"},
    "_unavailable_answer": {"reason": "federated_answer_reason"},
    "_delegated_failure": {"reason": "federated_answer_reason"},
}

#: 允许带 `:细节` 后缀的**具体取值**（契约里写明了这种形状，如
#: `receipt_binding_mismatch:plan_digest`）。只比对冒号前的部分；不在这里的取值带后缀即红。
#: 与 `ddp_corpus.federation.ANSWER_REASONS_WITH_DETAIL` 必须一致（corpus-api 有用例钉着）。
SUFFIXED_VALUES = {
    "federated_answer_reason": {
        "receipt_binding_mismatch", "delegated_execution_failed",
        "delegated_admission_not_accepted", "delegated_answer_rejected", "peer_unavailable",
    },
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
    # 联邦答案原因是给用户看的"为什么没有答案"：声明了却没人写出去，就是一句
    # 永远不会出现的文案；写出去了却没声明，界面上就是一串原始代码。
    "federated_answer_reason": set(),
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
    # `str(receipt.get("state") or "not_accepted")` —— 包一层 str() 的兜底值
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"
            and len(node.args) == 1 and not node.keywords):
        return _str_values(node.args[0])
    # f-string：`f"receipt_binding_mismatch:{field}"` 的取值是冒号前的固定前缀（后缀是细节）；
    # 全是常量的 f-string 就是那个常量；以常量开头却没有冒号（`f"code_{x}"`）的取值
    # 算不出来 —— 报成带 `…` 的取值让它红，而不是静默放过。以占位符开头的看不出代码，跳过。
    if isinstance(node, ast.JoinedStr) and node.values:
        if all(_const_str(part) is not None for part in node.values):
            return ["".join(part.value for part in node.values)]
        return _dynamic_head(_const_str(node.values[0]))
    # `"peer_unavailable:" + detail` —— 字符串拼接，左边是常量时同 f-string 处理
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _str_values(node.left), _const_str(node.right)
        if len(left) == 1 and left[0].endswith("…"):
            return left   # `"code:" + x + "!"`：左结合，代码已在左侧认出来，后面都是细节
        if len(left) == 1 and right is not None:
            return [left[0] + right]
        return _dynamic_head(left[0] if len(left) == 1 else None)
    # `"code:%s" % detail` / `"code:{}".format(detail)` —— 模板里占位符之前是固定部分
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _const_str(node.left)
        return _dynamic_head(template.split("%", 1)[0] if template is not None else None)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"):
        template = _const_str(node.func.value)
        return _dynamic_head(template.split("{", 1)[0] if template is not None else None)
    return []


def _dynamic_head(head: str | None) -> list[str]:
    """取值带运行时拼接部分时，能认出来的只有开头的常量。

    有冒号 → 代码是冒号前那段，后缀是细节（报成 `代码:…`）；没有冒号 → 整个取值算不出来，
    报成 `常量…` 让它红（例如 `f"receipt_binding_mismatch_{x}"`）；开头不是常量 → 看不出
    代码，跳过（靠 `federation.unavailable_answer` 的运行时检查兜住）。
    """
    if not head:   # None 或空串：以占位符开头，与 f"{x}" 一样看不出代码
        return []
    return [head.split(":", 1)[0] + ":…"] if ":" in head else [head + "…"]


def _tracked_name(node: ast.AST, tracked: dict[str, str]) -> str | None:
    """节点指向的被跟踪名字：`degraded` / `self.degraded` / `result["degraded"]` 都算。"""
    if isinstance(node, ast.Name) and node.id in tracked:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in tracked:
        return node.attr
    if isinstance(node, ast.Subscript) and (key := _const_str(node.slice)) in tracked:
        return key
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
            callee_name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            for kw in node.keywords:
                enum_name = CALL_KEYWORDS.get(callee_name, {}).get(kw.arg)
                if enum_name:
                    found += [(enum_name, v, node.lineno) for v in _str_values(kw.value)]
            callee = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            for index, enum_name in CALL_POSITIONS.get(callee, {}).items():
                if index < len(node.args):
                    found += [(enum_name, v, node.lineno) for v in _str_values(node.args[index])]
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
                if ":" in value:
                    head, _, detail = value.partition(":")
                    if head not in SUFFIXED_VALUES.get(enum_name, set()):
                        problems.append(f"{path.relative_to(ROOT)}:{line} {enum_name} 取值 "
                                        f"{head!r} 不允许带 `:细节` 后缀")
                    elif not detail:
                        problems.append(f"{path.relative_to(ROOT)}:{line} {enum_name} 取值 "
                                        f"{value!r} 的细节是空的")
                    value = head
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
