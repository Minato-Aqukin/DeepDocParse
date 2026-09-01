#!/usr/bin/env python
"""从 `packages/contracts/enums.yaml` 生成 Go / TypeScript / Python 三侧的枚举。

    python packages/contracts/scripts/generate.py            # 写文件
    python packages/contracts/scripts/generate.py --check    # 只校验（CI 用）

## 为什么要生成

合仓前 `degraded` / `status` / `source_type` 这些枚举**各写三份**：
Python 的字符串字面量、TS 的文案表、openapi.yaml 的 enum 列表。
三处漂开的表现不是报错，而是：后端打了一个新的降级值，前端的表里没有它，
于是那条降级在 UI 上**等于不存在** —— 而"降级必须可见"是第二条不变式。

生成的东西包含**用户可见文案**（`label`），这是刻意的：加一个降级值时
如果只加值不加文案，生成器会当场报错，而不是让它悄悄漏到界面上。

## 生成到哪里

生成物**直接写进消费方的目录**，不走中转的 `generated/` 再软链 ——
软链在某些 checkout 上会变成普通文件，那时它会安静地停止更新：

    packages/contracts/generated/ts/enums.ts        -> @deepdocparse/contracts（前端 import）
    services/control-api/internal/contracts/enums.go -> Go 控制面
    python/ddp_contracts/ddp_contracts/enums.py      -> Python 侧的 ddp-contracts 包

**生成物入库**（不进 .gitignore）：diff 里看得见枚举的变化，是评审时最需要
看见的东西之一；同时让 `--check` 能在不装任何工具链的机器上跑。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CONTRACTS = HERE.parent
ROOT = CONTRACTS.parent.parent
SOURCE = CONTRACTS / "enums.yaml"

SEVERITIES = ("neutral", "progress", "ok", "warn", "error")
BANNER_LINES = [
    "由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。",
    "改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。",
]


# --------------------------------------------------------------------- 载入

def load() -> dict:
    spec = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    problems: list[str] = []
    for name, block in spec["enums"].items():
        if not block.get("description", "").strip():
            problems.append(f"{name} 没有 description")
        seen = set()
        for item in block["values"]:
            v = item.get("value")
            if not v:
                problems.append(f"{name} 有一条没有 value")
                continue
            if v in seen:
                problems.append(f"{name}.{v} 重复")
            seen.add(v)
            if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
                problems.append(f"{name}.{v} 不是 snake_case")
            # 缺 label 的枚举值 = 用户看不懂的枚举值。这是硬错误，不是警告
            for field in ("summary", "label", "severity"):
                if not str(item.get(field, "")).strip():
                    problems.append(f"{name}.{v} 缺 {field}")
            if item.get("severity") not in SEVERITIES:
                problems.append(f"{name}.{v} 的 severity 必须是 {SEVERITIES} 之一")
        subset = block.get("contract_subset") or {}
        for key, values in subset.items():
            unknown = set(values) - seen
            if unknown:
                problems.append(f"{name}.contract_subset.{key} 里有未定义的值：{sorted(unknown)}")
    if problems:
        for p in problems:
            print(f"::error::enums.yaml: {p}")
        raise SystemExit(1)
    return spec


def pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def wrap_comment(text: str, prefix: str) -> list[str]:
    out = []
    for line in (text or "").strip().splitlines():
        out.append(f"{prefix} {line}".rstrip())
    return out


# ------------------------------------------------------------------ TypeScript

def render_ts(spec: dict) -> str:
    out = ["/*"]
    out += [f" * {line}" for line in BANNER_LINES]
    out += [" */", "",
            "export type Severity = " + " | ".join(f"'{s}'" for s in SEVERITIES), "",
            "export interface EnumMeta {",
            "  /** 枚举值本身 */", "  value: string",
            "  /** 给用户看的中文文案 */", "  label: string",
            "  /** UI 据此选标签颜色，不要在前端另立一套 */", "  severity: Severity",
            "  /** 是否属于「还在动」的状态 —— 列表页据此决定要不要继续轮询 */",
            "  active?: boolean", "}", ""]
    for name, block in spec["enums"].items():
        T = pascal(name)
        values = block["values"]
        out += wrap_comment(block["description"], "//")
        union = " | ".join(f"'{v['value']}'" for v in values)
        out += [f"export type {T} = {union}", ""]
        out += [f"export const {name.upper()}_VALUES: readonly {T}[] = ["]
        out += [f"  '{v['value']}'," for v in values]
        out += ["] as const", ""]
        out += [f"export const {name.upper()}_META: Record<{T}, EnumMeta> = {{"]
        for v in values:
            out += wrap_comment(v["summary"], "  //")
            active = ", active: true" if v.get("active") else ""
            # 用 json.dumps 转义，不要 repr —— 文案里出现引号时 repr 会给出
            # Python 语法的字符串，塞进 TS 里就是语法错误
            label = json.dumps(v["label"], ensure_ascii=False)
            out.append(f"  {v['value']}: {{ value: '{v['value']}', "
                       f"label: {label}, severity: '{v['severity']}'{active} }},")
        out += ["}", ""]
        subset = block.get("contract_subset") or {}
        for key, vals in subset.items():
            const = f"{name.upper()}_{key.upper()}"
            out += [f"/** 契约 {key} 只承诺这几个值 */",
                    f"export const {const}: readonly {T}[] = ["
                    + ", ".join(f"'{x}'" for x in vals) + "]", ""]
        out += [f"export function {camel(name)}LabelOf(value: string | null | undefined)"
                ": string | null {",
                "  if (!value) return null",
                f"  return {name.upper()}_META[value as {T}]?.label"
                f" ?? `未知取值（${{value}}）`", "}", ""]
    return "\n".join(out)


# ------------------------------------------------------------------------ Go

def render_go(spec: dict) -> str:
    out = []
    out += [f"// {line}" for line in BANNER_LINES]
    out += ["", "package contracts", "",
            "// Severity 是语义色，不是 UI 框架的颜色名 —— 映射在前端一处完成。",
            "type Severity string", "",
            "const ("]
    out += [f'\t{"Severity" + pascal(s):<18}Severity = "{s}"' for s in SEVERITIES]
    out += [")", "",
            "// EnumMeta 是一个枚举取值的全部对外信息。",
            "type EnumMeta struct {",
            '\tValue    string   `json:"value"`',
            '\tLabel    string   `json:"label"`',
            '\tSeverity Severity `json:"severity"`',
            '\tActive   bool     `json:"active,omitempty"`',
            "}", ""]
    for name, block in spec["enums"].items():
        T = pascal(name)
        values = block["values"]
        out += wrap_comment(block["description"], "//")
        out += [f"type {T} string", "", "const ("]
        for v in values:
            out += wrap_comment(v["summary"], "\t//")
            out.append(f'\t{T}{pascal(v["value"])} {T} = "{v["value"]}"')
        out += [")", ""]
        out += [f"// {T}Values 保持 enums.yaml 里的声明顺序。",
                f"var {T}Values = []{T}{{"]
        out += [f'\t{T}{pascal(v["value"])},' for v in values]
        out += ["}", ""]
        out += [f"var {T}Meta = map[{T}]EnumMeta{{"]
        for v in values:
            active = ", Active: true" if v.get("active") else ""
            label = json.dumps(v["label"], ensure_ascii=False)
            out.append(f'\t{T}{pascal(v["value"])}: {{Value: "{v["value"]}", '
                       f'Label: {label}, Severity: Severity{pascal(v["severity"])}{active}}},')
        out += ["}", ""]
        out += [f"// Valid 报告 s 是不是一个已知的 {name} 取值。",
                f"func (s {T}) Valid() bool {{",
                f"\t_, ok := {T}Meta[s]",
                "\treturn ok", "}", ""]
    return "\n".join(out)


# -------------------------------------------------------------------- Python

def render_py(spec: dict) -> str:
    out = ['"""' + BANNER_LINES[0], BANNER_LINES[1], '"""',
           "from __future__ import annotations", "",
           "from typing import Final, Literal, TypedDict", "", "",
           "class EnumMeta(TypedDict, total=False):",
           '    """一个枚举取值的全部对外信息。"""',
           "    value: str",
           "    label: str",
           "    severity: Literal[" + ", ".join(f'"{x}"' for x in SEVERITIES) + "]",
           "    active: bool", "", ""]
    for name, block in spec["enums"].items():
        values = block["values"]
        U = name.upper()
        out += wrap_comment(block["description"], "#")
        literal = ", ".join(f'"{v["value"]}"' for v in values)
        out += [f"{pascal(name)} = Literal[{literal}]", ""]
        out += [f"{U}_VALUES: Final[tuple[str, ...]] = ("]
        out += [f'    "{v["value"]}",' for v in values]
        out += [")", ""]
        out += [f"{U}_META: Final[dict[str, EnumMeta]] = {{"]
        for v in values:
            out += wrap_comment(v["summary"], "    #")
            active = ', "active": True' if v.get("active") else ""
            label = json.dumps(v["label"], ensure_ascii=False)
            out.append(f'    "{v["value"]}": {{"value": "{v["value"]}", '
                       f'"label": {label}, "severity": "{v["severity"]}"{active}}},')
        out += ["}", ""]
        subset = block.get("contract_subset") or {}
        for key, vals in subset.items():
            out += [f"# 契约 {key} 只承诺这几个值",
                    f"{U}_{key.upper()}: Final[tuple[str, ...]] = ("
                    + "".join(f'"{x}", ' for x in vals).rstrip(", ") + ",)", ""]
        out += ["", f"def {name}_label(value: str | None) -> str | None:",
                f'    """{name} 的用户文案。未知取值也要给出可读文字，',
                '    不能把原始枚举丢给用户。"""',
                "    if not value:", "        return None",
                f'    meta = {U}_META.get(value)',
                '    return meta["label"] if meta else f"未知取值（{value}）"', "", ""]
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ 后处理

def gofmt(source: str) -> str | None:
    """把 Go 生成物过一遍 gofmt。

    **为什么不自己对齐**：gofmt 的 const/map 块对齐走的是 tabwriter，
    中日文字符的显示宽度规则很难在 Python 里复现一致。复现得"差不多"
    比不复现更糟 —— `gofmt -l` 会永远报这个文件，然后所有人学会忽略它。

    没装 Go 时返回 None，调用方**显式报 SKIP**（不是安静跳过）。
    """
    tool = shutil.which("gofmt")
    if tool is None:
        return None
    done = subprocess.run([tool], input=source, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"gofmt 拒绝了生成的代码，说明生成器写出了语法错误：\n{done.stderr}")
    return done.stdout


def render_go_formatted(spec: dict) -> str:
    raw = render_go(spec)
    formatted = gofmt(raw)
    if formatted is None:
        print("::warning::没找到 gofmt，Go 生成物未格式化 —— "
              "CI 的 `gofmt -l` 会因此报红。装 Go 后重跑。", file=sys.stderr)
        return raw
    return formatted


# ---------------------------------------------------------------------- 主流程

TARGETS = {
    "ts": (CONTRACTS / "generated" / "ts" / "enums.ts", render_ts),
    "go": (ROOT / "services" / "control-api" / "internal" / "contracts" / "enums.go",
           render_go_formatted),
    "py": (ROOT / "python" / "ddp_contracts" / "ddp_contracts" / "enums.py", render_py),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验是否最新")
    args = parser.parse_args()

    spec = load()
    stale = []
    for key, (path, render) in TARGETS.items():
        content = render(spec)
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != content:
                stale.append(path.relative_to(ROOT))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"已写入 {path.relative_to(ROOT)}")

    if stale:
        for p in stale:
            print(f"::error::{p} 与 packages/contracts/enums.yaml 不同步")
        print("\n重跑 `npm run contracts:gen`（或 python "
              "packages/contracts/scripts/generate.py）", file=sys.stderr)
        return 1
    if args.check:
        total = sum(len(b["values"]) for b in spec["enums"].values())
        print(f"契约枚举是最新的：{len(spec['enums'])} 组 / {total} 个取值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
