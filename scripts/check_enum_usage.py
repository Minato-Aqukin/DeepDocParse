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
    f(degraded="xxx")                   关键字参数
    {"degraded": "xxx"}                 字典字面量
    compile_degraded / index_status / compile_status / status(受限) 同理

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


def scan(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """返回 [(枚举名, 取值, 行号)]。"""
    found: list[tuple[str, str, int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            names += [t.attr for t in targets if isinstance(t, ast.Attribute)]
            value = _const_str(node.value) if node.value is not None else None
            for n in names:
                if n in TRACKED and value is not None:
                    found.append((TRACKED[n], value, node.lineno))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                v = _const_str(kw.value)
                if kw.arg in TRACKED and v is not None:
                    found.append((TRACKED[kw.arg], v, node.lineno))
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                key = _const_str(k) if k is not None else None
                val = _const_str(v)
                if key in TRACKED and val is not None:
                    found.append((TRACKED[key], val, node.lineno))
    return found


def main() -> int:
    declared = contract_values()
    problems: list[str] = []
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
                if value not in declared[enum_name]:
                    rel = path.relative_to(ROOT)
                    problems.append(
                        f"{rel}:{line} 用了契约里没有的 {enum_name} 取值 {value!r}")

    # 反哨兵：一个用法都没扫到时上面必然全绿，而那说明扫描逻辑坏了
    if total < 20:
        problems.append(
            f"只扫到 {total} 处枚举用法，扫描逻辑可能坏了（正常应有几十处）")

    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n新的取值要先加进 packages/contracts/enums.yaml（连同用户可见文案），"
              "再重跑 npm run contracts:gen。", file=sys.stderr)
        return 1
    print(f"枚举使用守卫通过：{total} 处用法全部在契约内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
