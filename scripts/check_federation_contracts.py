#!/usr/bin/env python
"""联邦协议契约守卫 —— schema、枚举与夹具三方对齐。

    python scripts/check_federation_contracts.py           # 校验（门禁用）
    python scripts/check_federation_contracts.py --write   # 重写已解析 bundle

## 它守的是什么

`packages/contracts/schemas/` 下的五份 schema 是「桌面与可验证联邦路由升级
计划 v3」§9.3 要求的契约。它们**目前没有实现**（见 docs/refactor/BASELINE-v3.md），
所以这把尺子量的不是运行时行为，而是三件在实现开始前就能量、而且一旦漂掉
就很难再查的事：

1. **schema 里的枚举不许与 enums.yaml 漂开。** schema 不直接写 enum 数组，
   只写 `"x-ddp-enum": "coverage_target_state"`；真正的取值由本脚本从
   enums.yaml 注入。这样「加了一个降级值但 schema 的 enum 数组里没有」
   这件事在结构上不可能发生 —— 那正是本项目合仓前 degraded 各处手抄的病根。

2. **每个不变量都有一条夹具钉着。** 计划里最重要的几条规则
   （fast 不许声明 complete、local_only 不许带 allowed_payload、
   accepted 必须有已校验输入摘要、generated 证据必须指回原文）
   都是 schema 里的 if/then。**if/then 写错了不会报错，只会永远通过** ——
   所以每条都配一个"本该被拒绝"的反例夹具。

3. **反例必须因为正确的理由被拒绝。** 这是第 2 条的补丁，也是本项目
   反复吃亏的地方（`docs/refactor/FINDINGS.md` 里的假守卫）：一个反例夹具
   如果因为拼错字段名而被拒绝，它看起来是绿的，但它量的不是目标规则。
   所以 manifest 里每个反例都要声明 `violates`（出错的实例路径），
   本脚本断言**实际错误就出在那个路径上**。

## 已解析 bundle

`packages/contracts/generated/schemas-resolved.json` 是注入枚举之后的成品，
**入库**（与 generated/ts/enums.ts 同一个理由：diff 里看得见契约的变化，
且不装 PyYAML 的消费方也能直接用）。过期即红。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    import jsonschema
except ModuleNotFoundError:                                   # pragma: no cover
    # **显式报缺，不静默跳过** —— 静默跳过的绿与真的绿长得一模一样。
    # jsonschema 目前是 services/mcp[dev] 的传递依赖，门禁 CI 装了它；
    # 这条分支是为了让"哪天 MCP SDK 不再依赖它"变成一句看得懂的话，
    # 而不是一条消失的检查。
    print("::error::缺 jsonschema。门禁需要它：pip install jsonschema", file=sys.stderr)
    raise SystemExit(1) from None

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contract_yaml  # noqa: E402 —— 同目录模块

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "packages" / "contracts"
SCHEMA_DIR = CONTRACTS / "schemas"
FIXTURES = CONTRACTS / "fixtures"
MANIFEST = FIXTURES / "manifest.yaml"
BUNDLE = CONTRACTS / "generated" / "schemas-resolved.json"

#: schema 里允许出现的自定义关键字。写错一个字（x-ddp-enums）的后果是
#: 那条枚举**悄悄不再被注入**，于是该字段退化成任意字符串 —— 所以未知的
#: x-ddp-* 关键字一律报错，不放过。
KNOWN_X_KEYWORDS = {
    "x-ddp-contract",        # 契约名，如 ddp-scope-coverage/1
    "x-ddp-doc",             # 给人读的「不能从这里推断什么」
    "x-ddp-enum",            # 指向 enums.yaml 的某个枚举
    "x-ddp-enum-nullable",   # 该字段允许 null（枚举值之外）
    "x-ddp-local-mapping",   # 契约字段 -> 当前源码列的映射
}

problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


# ------------------------------------------------------------------ 载入枚举

def load_enums() -> dict[str, list[str]]:
    spec = contract_yaml.load(CONTRACTS / "enums.yaml")
    return {name: [v["value"] for v in block["values"]]
            for name, block in spec["enums"].items()}


# --------------------------------------------------------------- 注入与体检

def walk(node, path: str, enums: dict[str, list[str]], *, inject: bool):
    """递归处理 schema：注入 x-ddp-enum，同时体检自定义关键字。"""
    if isinstance(node, list):
        for i, item in enumerate(node):
            walk(item, f"{path}[{i}]", enums, inject=inject)
        return
    if not isinstance(node, dict):
        return

    for key in node:
        if key.startswith("x-") and key not in KNOWN_X_KEYWORDS:
            fail(f"{path}: 未知的自定义关键字 {key!r}（写错的 x-ddp-* 等于该约束消失）")

    name = node.get("x-ddp-enum")
    if name is not None:
        if name not in enums:
            fail(f"{path}: x-ddp-enum 指向不存在的枚举 {name!r}")
        else:
            if "enum" in node:
                # 手写的 enum 数组就是漂移本身 —— 唯一真相只能有一份。
                fail(f"{path}: 同时写了 x-ddp-enum 与 enum，取值必须只由 enums.yaml 决定")
            if node.get("type") not in ("string", ["string", "null"], None):
                fail(f"{path}: x-ddp-enum 字段的 type 必须是 string（或 [string,null]），"
                     f"现在是 {node.get('type')!r}")
            if inject:
                values = list(enums[name])
                if node.get("x-ddp-enum-nullable") or node.get("type") == ["string", "null"]:
                    values.append(None)
                node["enum"] = values

    for key, child in node.items():
        if key in ("x-ddp-local-mapping",):     # 纯文档，不是 schema
            continue
        walk(child, f"{path}/{key}", enums, inject=inject)


def load_schemas(enums: dict[str, list[str]], *, inject: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(SCHEMA_DIR.rglob("v1.json")):
        rel = path.relative_to(SCHEMA_DIR).as_posix()
        schema = json.loads(path.read_text(encoding="utf-8"))
        walk(schema, rel, enums, inject=inject)
        # $defs 里每个对象都该有 description —— 没有描述的契约对象等于没有契约
        for def_name, body in schema.get("$defs", {}).items():
            if isinstance(body, dict) and body.get("type") in ("object", ["object", "null"]):
                if not str(body.get("description", "")).strip():
                    fail(f"{rel}#{def_name} 没有 description")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            fail(f"{rel} 不是合法的 JSON Schema：{exc.message}")
        out[rel] = schema
    if not out:
        fail(f"{SCHEMA_DIR} 下一份 schema 都没找到")
    return out


# ------------------------------------------------------------------ 夹具校验

def subschema(schema: dict, rel: str, def_name: str) -> dict | None:
    defs = schema.get("$defs", {})
    if def_name not in defs:
        fail(f"manifest 引用了不存在的 {rel}#/$defs/{def_name}")
        return None
    # $ref 是相对 root 的，所以把 $defs 整体带上，root 换成目标 $def
    return {**defs[def_name], "$defs": defs,
            "$schema": "https://json-schema.org/draft/2020-12/schema"}


def check_fixtures(schemas: dict[str, dict]) -> int:
    entries = contract_yaml.load(MANIFEST)
    seen_files: set[str] = set()
    checked = 0

    for entry in entries:
        fx = entry["fixture"]
        seen_files.add(fx)
        rel, def_name, expect = entry["schema"], entry["def"], entry["expect"]
        path = FIXTURES / fx
        if not path.exists():
            fail(f"manifest 里的夹具不存在：{fx}")
            continue
        if rel not in schemas:
            fail(f"manifest 引用了不存在的 schema：{rel}")
            continue
        sub = subschema(schemas[rel], rel, def_name)
        if sub is None:
            continue

        instance = json.loads(path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(sub)
        errors = sorted(validator.iter_errors(instance), key=lambda e: e.json_path)
        checked += 1

        if expect == "valid":
            if errors:
                detail = "; ".join(f"{e.json_path}: {e.message}" for e in errors[:4])
                fail(f"{fx} 本该通过 {rel}#{def_name}，却报错：{detail}")
            continue

        # expect == "invalid"
        if not errors:
            fail(f"{fx} 本该被 {rel}#{def_name} 拒绝，却通过了 —— "
                 f"要么夹具写得不够坏，要么 schema 的 if/then 是个假约束")
            continue
        want = entry.get("violates")
        if not want:
            fail(f"{fx} 是反例却没声明 violates。"
                 f"不声明出错位置的反例可能因为拼错字段名而'通过'，量不到目标规则")
            continue
        got = {e.json_path for e in errors}
        if want not in got:
            fail(f"{fx} 本该在 {want} 出错，实际出错位置是 {sorted(got)} —— "
                 f"它验的不是 {entry.get('because', '声明的那条规则')}")
            continue

        # 缺必填字段时 jsonschema 报的路径是**对象本身**（`$`），粒度不够 ——
        # 「少了 exclusion_basis」与「少了别的字段」在 `$` 上长得一样。
        # 所以路径为 `$` 的反例必须再声明 violates_message，把是哪个字段钉死。
        at_want = [e for e in errors if e.json_path == want]
        if want == "$" and not entry.get("violates_message"):
            fail(f"{fx} 的 violates 是 $（缺必填字段那类），必须再声明 "
                 f"violates_message 指明是哪个字段，否则换个字段缺失它照样'通过'")
            continue
        needle = entry.get("violates_message")
        if needle and not any(needle in e.message for e in at_want):
            fail(f"{fx} 的报错信息里没有 {needle!r}。"
                 f"实际：{[e.message[:70] for e in at_want]}")
            continue
        keyword = entry.get("violates_keyword")
        if keyword and not any(e.validator == keyword for e in at_want):
            fail(f"{fx} 本该因 {keyword} 被拒，实际是 "
                 f"{sorted({e.validator for e in at_want})}")

    # manifest 没覆盖到的夹具文件 = 写了但没人跑的夹具
    on_disk = {p.relative_to(FIXTURES).as_posix()
               for p in FIXTURES.rglob("*.json")}
    for orphan in sorted(on_disk - seen_files):
        fail(f"夹具 {orphan} 不在 manifest 里，没有任何检查会读它")
    return checked


# -------------------------------------------------------------------- bundle

def bundle_bytes(schemas: dict[str, dict]) -> bytes:
    payload = {
        "_comment": "由 scripts/check_federation_contracts.py --write 生成 —— 不要手改。"
                    "改 schema 请改 packages/contracts/schemas/，改枚举请改 enums.yaml。",
        "schemas": schemas,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="重写已解析 bundle")
    args = ap.parse_args()

    enums = load_enums()
    # 体检用未注入的副本（这样"手写 enum 数组"能被抓到），注入版用于校验夹具
    load_schemas(enums, inject=False)
    schemas = load_schemas(enums, inject=True)
    checked = check_fixtures(schemas)

    want = bundle_bytes(schemas)
    if args.write:
        BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        BUNDLE.write_bytes(want)
        print(f"已写入 {BUNDLE.relative_to(ROOT)}")
    elif not BUNDLE.exists():
        fail(f"{BUNDLE.relative_to(ROOT)} 不存在，跑 --write 生成")
    elif BUNDLE.read_bytes() != want:
        fail(f"{BUNDLE.relative_to(ROOT)} 已过期，跑 --write 重新生成")

    if problems:
        for p in problems:
            print(f"::error::联邦契约: {p}", file=sys.stderr)
        return 1
    print(f"联邦契约 OK：{len(schemas)} 份 schema，{checked} 个夹具，"
          f"{sum(len(s.get('$defs', {})) for s in schemas.values())} 个定义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
