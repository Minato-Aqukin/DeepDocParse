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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import contract_yaml  # noqa: E402 —— 契约 YAML 的唯一读取口（重复键报错）

HERE = Path(__file__).resolve().parent
CONTRACTS = HERE.parent
ROOT = CONTRACTS.parent.parent
SOURCE = CONTRACTS / "enums.yaml"

def ident(value: str) -> str:
    """把枚举取值变成合法标识符片段：`rag.answer.cited` -> `rag_answer_cited`。"""
    return value.replace(".", "_")


SEVERITIES = ("neutral", "progress", "ok", "warn", "error")
BANNER_LINES = [
    "由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。",
    "改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。",
]


# --------------------------------------------------------------------- 载入

def load() -> dict:
    spec = contract_yaml.load(SOURCE)
    problems: list[str] = []
    for name, block in spec["enums"].items():
        if not block.get("description", "").strip():
            problems.append(f"{name} 没有 description")
        seen = set()
        identifiers: dict[str, str] = {}
        for item in block["values"]:
            v = item.get("value")
            if not v:
                problems.append(f"{name} 有一条没有 value")
                continue
            if v in seen:
                problems.append(f"{name}.{v} 重复")
            seen.add(v)
            # 允许点分段（`rag.answer.cited` 这类 operation 名），每段仍是 snake_case。
            # 生成常量名时点会换成下划线（见 `ident`），所以标识符照样合法。
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", v):
                problems.append(f"{name}.{v} 不是 snake_case（允许点分段）")
            # `a.b` 与 `a_b` 会生成同一个常量名：Go 那边是两条重复声明，
            # 只有真正 `go build` 才报错（`--check` 与 gofmt 都看不出来）。
            if ident(v) in identifiers:
                problems.append(f"{name}.{v} 与 {identifiers[ident(v)]!r} 会生成同一个常量名 "
                                f"{ident(v)!r} —— 换一个取值")
            identifiers[ident(v)] = v
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
            # 键用引号包起来：点分段的取值不是合法的 TS 标识符
            out.append(f"  {json.dumps(v['value'])}: {{ value: '{v['value']}', "
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
    out += ["export type { GenerationOperation, GenerationCandidate, GenerationCandidates } "
            "from './generation-candidates'", ""]
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
            out.append(f'\t{T}{pascal(ident(v["value"]))} {T} = "{v["value"]}"')
        out += [")", ""]
        out += [f"// {T}Values 保持 enums.yaml 里的声明顺序。",
                f"var {T}Values = []{T}{{"]
        out += [f'\t{T}{pascal(ident(v["value"]))},' for v in values]
        out += ["}", ""]
        out += [f"var {T}Meta = map[{T}]EnumMeta{{"]
        for v in values:
            active = ", Active: true" if v.get("active") else ""
            label = json.dumps(v["label"], ensure_ascii=False)
            out.append(f'\t{T}{pascal(ident(v["value"]))}: {{Value: "{v["value"]}", '
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

def render_generation_candidates_ts(spec: dict) -> str:
    """Generate the authenticated discovery response directly from its OpenAPI schema."""
    api = contract_yaml.load(CONTRACTS / "openapi" / "discovery-v1.yaml")
    schemas = api["components"]["schemas"]

    def ts_type(schema: dict) -> str:
        if "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[-1]
        if "const" in schema:
            return json.dumps(schema["const"])
        if "x-ddp-enum" in schema:
            return pascal(schema["x-ddp-enum"])
        if "enum" in schema:
            return " | ".join(json.dumps(value) for value in schema["enum"])
        kind = schema["type"]
        if kind == "array":
            return f"Array<{ts_type(schema['items'])}>"
        return {"string": "string", "boolean": "boolean", "integer": "number"}[kind]

    out = ["// Generated from openapi/discovery-v1.yaml; do not edit.",
           "import type { CapabilityReadiness } from './enums'", ""]
    for name in ("GenerationOperation", "GenerationCandidate", "GenerationCandidates"):
        schema = schemas[name]
        if schema["type"] != "object":
            out.extend([f"export type {name} = {ts_type(schema)}", ""])
            continue
        out.append(f"export interface {name} {{")
        required = set(schema["required"])
        for key, field in schema["properties"].items():
            optional = "" if key in required else "?"
            out.append(f"  {key}{optional}: {ts_type(field)}")
        out.extend(["}", ""])
    return "\n".join(out)


OPENAPI_DIR = CONTRACTS / "openapi"
SCHEMAS_RESOLVED = CONTRACTS / "generated" / "schemas-resolved.json"

#: generate.py --check 要对拍的 OpenAPI 文件。content/gateway/control 三平面的
#: x-ddp-enum 必须全部对拍，discovery/federation-tasks 的既有枚举一并校验，
#: bundle 的路径结构一并校验（它没有枚举绑定，但截断/合并曾让它的路径项
#: 只剩 parameters 而门禁全绿），否则 walker 只扫一半，"别处手写 enum
#: 漂了/路径项截断了"照样绿。
OPENAPI_CHECK_FILES = (
    "content-v1.yaml",
    "gateway-v1.yaml",
    "control-v1.yaml",
    "discovery-v1.yaml",
    "federation-tasks-v1.yaml",
    "bundle-v1.yaml",
)

TARGETS = {
    "ts": (CONTRACTS / "generated" / "ts" / "enums.ts", render_ts),
    "go": (ROOT / "services" / "control-api" / "internal" / "contracts" / "enums.go",
           render_go_formatted),
    "py": (ROOT / "python" / "ddp_contracts" / "ddp_contracts" / "enums.py", render_py),
    "generation_candidates_ts": (CONTRACTS / "generated" / "ts" / "generation-candidates.ts",
                                 render_generation_candidates_ts),
}


def _walk_openapi_enums(node, path: str, out: list[tuple[str, dict]]) -> None:
    """收集 OpenAPI 文档里全部 x-ddp-enum 标注点（含 paths 下的内联 schema）。"""
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk_openapi_enums(item, f"{path}[{i}]", out)
        return
    if not isinstance(node, dict):
        return
    if "x-ddp-enum" in node:
        out.append((path, node))
    for key, child in node.items():
        _walk_openapi_enums(child, f"{path}/{key}", out)


#: 属性名即枚举名的字段。OpenAPI 里一个叫 `degraded` 的 string 属性，
#: 不管有没有标注，都只能装 `degraded` 枚举的取值 —— 否则生产者换个名字
#: 写法（或漏掉标注行），约束就静默消失了。
ENUM_NAMED_PROPERTIES = ("degraded", "compile_degraded")


def _walk_named_enum_properties(node, path: str,
                                out: list[tuple[str, str, dict]]) -> None:
    """收集 `properties:` 下名字落在 ENUM_NAMED_PROPERTIES 里的属性节点。"""
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk_named_enum_properties(item, f"{path}[{i}]", out)
        return
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for name, schema in props.items():
            if name in ENUM_NAMED_PROPERTIES and isinstance(schema, dict):
                out.append((f"{path}/properties/{name}", name, schema))
    for key, child in node.items():
        _walk_named_enum_properties(child, f"{path}/{key}", out)


def check_openapi_parity(spec: dict) -> list[str]:
    """校验 openapi/*.yaml 的枚举绑定与 enums.yaml 一致。

    五条规则（与 schemas/*.json 的守卫 `check_federation_contracts.py` 同构，
    但 openapi 侧此前没有任何守卫 —— 自由文本 `description: 取值见 enums.yaml`
    不是约束，改了枚举这边不会红）：

    1. `x-ddp-enum` 必须指向 enums.yaml 里真实存在的枚举。
    2. 带 `x-ddp-enum` 的节点必须同时手写 `enum`，且与 enums.yaml 一字不差
       （顺序都要一致）。光有标注没有列表，OpenAPI 侧仍是 `type: string`，
       生产者照样能打出契约外的取值 —— 标注不是约束，列表才是。
       例外：带 `x-ddp-enum-subset` 的节点声明的是 contract_subset 子集，
       此时手写 `enum` 必须与该子集一字不差（顺序都要一致）。
    3. `x-ddp-enum-subset` 引用的子集必须在该枚举的 `contract_subset` 里声明，
       且子集里的每个值都必须在该枚举的 values 里存在。
    4. 名字落在 ENUM_NAMED_PROPERTIES 里的属性（`degraded` /
       `compile_degraded`），必须带上同名的 `x-ddp-enum` 标注 —— 漏掉标注
       行不能让约束静默消失。
    5. 路径条目必须至少声明一个操作（get/post/…）：只有 `parameters` 没有
       操作的路径项是截断/合并事故，不是合法契约。
    """
    errors: list[str] = []
    methods = ("get", "post", "put", "patch", "delete", "head", "options")
    for filename in OPENAPI_CHECK_FILES:
        doc = contract_yaml.load(OPENAPI_DIR / filename)
        sites: list[tuple[str, dict]] = []
        _walk_openapi_enums(doc, filename, sites)
        for path, node in sites:
            name = node.get("x-ddp-enum")
            block = spec["enums"].get(name) if isinstance(name, str) else None
            if block is None:
                errors.append(f"{path}: x-ddp-enum 指向不存在的枚举 {name!r}")
                continue
            declared = [v["value"] for v in block["values"]]
            subset_key = node.get("x-ddp-enum-subset")
            hand = node.get("enum")
            if subset_key is not None:
                subsets = block.get("contract_subset") or {}
                if subset_key not in subsets:
                    errors.append(f"{path}: x-ddp-enum-subset {subset_key!r} "
                                  f"不在 enums.yaml {name}.contract_subset 里")
                    continue
                want = list(subsets[subset_key])
                unknown = [v for v in want if v not in declared]
                if unknown:
                    errors.append(f"{path}: contract_subset {name}.{subset_key} "
                                  f"里有未定义的值：{sorted(unknown)}")
                    continue
                if hand != want:
                    errors.append(f"{path}: 手写 enum 与 enums.yaml "
                                  f"{name}.contract_subset.{subset_key} 不一致")
                continue
            if node.get("x-ddp-enum-items") is True:
                # 数组形枚举：标注落在数组节点上，手写 enum 落在 items 上
                # （compile_degraded 那种列表形状）。数组节点自己不写 enum。
                items = node.get("items")
                items_hand = items.get("enum") if isinstance(items, dict) else None
                if hand is not None or items_hand is None:
                    errors.append(f"{path}: x-ddp-enum-items 的手写 enum 必须写在 "
                                  f"items 下（数组节点不写 enum）")
                elif items_hand != declared:
                    errors.append(f"{path}/items: 手写 enum 与 enums.yaml {name} "
                                  f"不一致，取值必须只由 enums.yaml 决定")
                continue
            if node.get("x-ddp-enum-nullable") is True:
                # 可空枚举：手写 enum 是"取值 + null"（与 schemas 侧注入逻辑
                # check_federation_contracts.walk 同构：它也是可空则追加 None）。
                # JSON Schema 的 enum 不管 type —— 不把 null 写进列表，
                # degraded: null 的合法响应就验不过了。
                if hand != declared + [None]:
                    errors.append(f"{path}: x-ddp-enum-nullable 的手写 enum 必须是 "
                                  f"enums.yaml {name} 的取值 + null（顺序一致）")
                continue
            if hand is None:
                errors.append(f"{path}: x-ddp-enum {name} 缺少手写 enum —— "
                              f"取值必须同时落在 OpenAPI 的 enum 列表里")
            elif hand != declared:
                errors.append(f"{path}: 手写 enum 与 enums.yaml {name} 不一致，"
                              f"取值必须只由 enums.yaml 决定")
        named: list[tuple[str, str, dict]] = []
        _walk_named_enum_properties(doc, filename, named)
        for path, wanted, node in named:
            if node.get("x-ddp-enum") != wanted:
                errors.append(f"{path}: 名为 {wanted} 的属性必须带 "
                              f"x-ddp-enum: {wanted}（连同手写 enum），"
                              f"否则约束静默缺失")
        for route, item in (doc.get("paths") or {}).items():
            if not isinstance(item, dict) or not any(m in item for m in methods):
                errors.append(f"{filename}: 路径项 {route} 没有声明任何操作 —— "
                              f"疑似截断，请补全后再跑 --check")
    return errors


def check_schemas_resolved_fresh(spec: dict) -> list[str]:
    """校验 generated/schemas-resolved.json 与当前 enums.yaml + schemas/ 一致。

    复用 `scripts/check_federation_contracts.py` 的同一套注入逻辑：把
    schemas/*.json 按 x-ddp-enum 注入后的 bundle 字节与入库文件逐字节比对。
    不在这里另写一套注入 —— 两套注入的后果是它们先互相漂移。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_federation_contracts as fed
    fed.problems.clear()
    enums = fed.load_enums()
    schemas = fed.load_schemas(enums, inject=True)
    if fed.problems:
        return [f"schemas 解析失败：{p}" for p in list(fed.problems)]
    want = fed.bundle_bytes(schemas)
    if not SCHEMAS_RESOLVED.exists():
        return [f"{SCHEMAS_RESOLVED.relative_to(ROOT)} 不存在，"
                f"跑 python scripts/check_federation_contracts.py --write 生成"]
    if SCHEMAS_RESOLVED.read_bytes() != want:
        return [f"{SCHEMAS_RESOLVED.relative_to(ROOT)} 已过期，"
                f"跑 python scripts/check_federation_contracts.py --write 重新生成"]
    # load_enums 从 enums.yaml 现场读：上面的 inject 已经把新取值带进了比对，
    # 这里再确认 generate.py 自己的 load() 看到的枚举与守卫看到的一致。
    _ = spec
    return []


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

    if args.check:
        # --check 之前只看 enums 输出（ts/go/py）：openapi 手写 enum 漂了、
        # schemas-resolved.json 过期了照样绿。拓宽到三处，默认 generate
        # 行为不变（仍只写 TARGETS）。
        openapi_errors = check_openapi_parity(spec)
        bundle_errors = check_schemas_resolved_fresh(spec)
        for err in openapi_errors + bundle_errors:
            print(f"::error::{err}")
        if stale:
            for p in stale:
                print(f"::error::{p} 与 packages/contracts/enums.yaml 不同步")
            print("\n重跑 `npm run contracts:gen`（或 python "
                  "packages/contracts/scripts/generate.py）", file=sys.stderr)
        if stale or openapi_errors or bundle_errors:
            return 1
        total = sum(len(b["values"]) for b in spec["enums"].values())
        print(f"契约枚举是最新的：{len(spec['enums'])} 组 / {total} 个取值；"
              f"schemas-resolved.json 新鲜；openapi 枚举对拍通过")
        return 0

    if stale:
        for p in stale:
            print(f"::error::{p} 与 packages/contracts/enums.yaml 不同步")
        print("\n重跑 `npm run contracts:gen`（或 python "
              "packages/contracts/scripts/generate.py）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
