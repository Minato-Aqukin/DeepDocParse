#!/usr/bin/env python
"""联邦任务路由守卫 —— federation-tasks-v1.yaml ←→ corpus-api 实际端点必须一致。

    python scripts/check_federation_routes.py              # 门禁用，退出码 0/1
    python scripts/check_federation_routes.py --self-test  # 只验比较逻辑本身

## 它守的是什么

P5 契约 `packages/contracts/openapi/federation-tasks-v1.yaml` 冻结了 21 个
端点（节点/执行者 9 个含证据集 + 协调者 10 个含本人任务列表 + 交付读取与确认 2 个；
数目以脚本输出为准）。这个脚本把它变成可执行的检查，
与 `scripts/check_contract.py` 同一套路，对象换成 `services/corpus-api`：

- 契约有、app 没有 -> 承诺了没实现。调用方按契约写代码会拿到 404。
- app 有、契约没有 -> 端点在契约外偷偷长出来了（只查下面四组前缀；
  corpus-api 还有 documents / search / resources 等大量不归这份契约管的端点）。

路径参数名按结构归一化（`{root_task_id}` 与 `{task_id}` 是同一段路由）——
比对的是路由形状，不是参数拼写。请求/响应体的形状由契约文件本身与各路由
测试管，不在这里。

## 为什么用 app.openapi() 而不是遍历 app.routes

FastAPI 0.141 起 `include_router` 不再把子路由摊平进 `app.routes`
（留下的是没有 `.path` 的 `_IncludedRouter`）。照旧遍历会得到空集合，
于是"契约声明的全部没实现"这种**假红**会永远挂着，而它和真的缺失长得
一模一样。`check_contract.py` 已经踩过并把结论写在注释里，这里沿用同一条
已验证路径。`openapi()` 只做静态生成，不跑 lifespan、不连数据库。

## 反向哨兵

`app.openapi()` 哪天退化或被换掉，上面取到的会是空集 —— 与"实现还没写"
无法区分。所以先断言一个不可能消失的端点（GET /healthz）在枚举结果里；
它不在就直接报"枚举本身失效"，而不是报一堆缺路由。

## 变异确认

    python scripts/check_federation_routes.py --self-test

对比较逻辑做三组变异：抽掉一条实现必须只报它 missing、塞一条实现必须只报
它 extra、参数名不同必须视为同一条；任何一组不符就红。比较函数坏掉时主
检查会沉默地全绿 —— 那正是这个项目反复抓到的假守卫。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packages" / "contracts" / "openapi" / "federation-tasks-v1.yaml"

#: 受这份契约约束的前缀（计划 §9.5 / P5-INTERFACES-v3 §2–§3，交付确认见 §9.4）。
GUARDED_PREFIXES = (
    "/api/v1/federation",
    "/api/v1/task-intents",
    "/api/v1/task-plans",
    "/api/v1/tasks",
    "/api/v1/deliveries",
)

#: 枚举 app 端点的存在性探针。不属于任何业务前缀，也不该被任何契约删除。
CANARY = ("/healthz", "get")

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def normalize(path: str) -> str:
    """把路径参数名抹平：`{root_task_id}` 与 `{task_id}` 是同一段路由。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def guarded(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/")
               for prefix in GUARDED_PREFIXES)


def contract_endpoints() -> set[tuple[str, str]]:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    return {(normalize(path), method.lower())
            for path, item in (spec.get("paths") or {}).items()
            for method in item
            if method.lower() in _HTTP_METHODS}


def app_endpoints() -> set[tuple[str, str]]:
    """corpus-api 声明的全部 (路径, 方法)。

    在 import 之前把服务凭据放进环境：settings 是模块级单例，import 时就读
    env；main 的 lifespan 还会拒绝占位密钥。给一个非占位值、**不设**
    ALLOW_INSECURE_DEFAULTS，让守卫跑在"检查是打开的"那个形态上。这里
    import 不触发 lifespan，所以不会连 PG / MinIO。
    """
    os.environ["SERVICE_TOKEN"] = "federation-routes-guard"
    from ddp_corpus.main import app  # noqa: PLC0415

    return {(normalize(path), method.lower())
            for path, item in (app.openapi().get("paths") or {}).items()
            for method in item
            if method.lower() in _HTTP_METHODS}


def diff(declared: set[tuple[str, str]],
         implemented: set[tuple[str, str]]) -> tuple[list, list]:
    return sorted(declared - implemented), sorted(implemented - declared)


def self_test() -> int:
    declared = contract_endpoints()
    if not declared:
        print("::error::自检失败：契约里一条端点都没有", file=sys.stderr)
        return 1

    # 变异 1：实现少一条 -> 必须且只能报这一条 missing
    victim = sorted(declared)[0]
    missing, extra = diff(declared, declared - {victim})
    if missing != [victim] or extra:
        print(f"::error::自检失败：抽掉实现里的 {victim} 后 "
              f"missing={missing} extra={extra}", file=sys.stderr)
        return 1

    # 变异 2：实现多一条受看守前缀内的路由 -> 必须且只能报这一条 extra
    ghost = ("/api/v1/federation/_self_test_ghost", "post")
    if not guarded(ghost[0]):
        print("::error::自检失败：鬼路由不在受看守前缀内", file=sys.stderr)
        return 1
    missing, extra = diff(declared, declared | {ghost})
    if missing or extra != [ghost]:
        print(f"::error::自检失败：塞入 {ghost} 后 "
              f"missing={missing} extra={extra}", file=sys.stderr)
        return 1

    # 变异 3：参数名不同、结构相同 -> 必须视为同一条路由
    missing, extra = diff({(normalize("/api/v1/tasks/{root_task_id}"), "get")},
                          {(normalize("/api/v1/tasks/{task_id}"), "get")})
    if missing or extra:
        print(f"::error::自检失败：路径参数归一化失效 "
              f"missing={missing} extra={extra}", file=sys.stderr)
        return 1

    print("路由守卫自检通过：missing / extra / 路径参数归一化三组变异都有效")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="只验比较逻辑，不需要 P5 实现落地")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    declared = contract_endpoints()
    if not declared:
        print(f"::error::{SPEC.relative_to(ROOT)} 里一条端点都没有 —— "
              f"守卫本身就失去了意义")
        return 1

    outside = sorted(ep for ep in declared if not guarded(ep[0]))
    if outside:
        print(f"::error::契约出现了受看守前缀之外的端点：{outside}\n"
              f"  它们不会被「多出来的实现」那个方向覆盖："
              f"要么改路径，要么把前缀加进 GUARDED_PREFIXES")
        return 1

    implemented = app_endpoints()
    if CANARY not in implemented:
        print(f"::error::corpus-api 端点枚举失效：连 "
              f"{CANARY[1].upper()} {CANARY[0]} 都取不到 —— "
              f"这不是「实现缺失」。守卫的红没有信息量，先修枚举本身")
        return 1

    implemented_guarded = {ep for ep in implemented if guarded(ep[0])}
    missing, extra = diff(declared, implemented_guarded)

    for path, method in missing:
        print(f"::error::契约声明了 corpus-api 没实现的联邦端点："
              f"{method.upper()} {path}\n"
              f"  调用方按契约写代码会拿到 404")
    for path, method in extra:
        print(f"::error::corpus-api 暴露了联邦契约里没有的端点："
              f"{method.upper()} {path}\n"
              f"  契约先于实现：新增端点先改 {SPEC.relative_to(ROOT)}")

    if missing or extra:
        print(f"\n联邦任务路由：契约 {len(declared)} 条，"
              f"corpus-api 缺失 {len(missing)} 条，契约外 {len(extra)} 条")
        return 1
    print(f"联邦任务路由守卫通过：{len(declared)} 个端点，契约与 corpus-api 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
