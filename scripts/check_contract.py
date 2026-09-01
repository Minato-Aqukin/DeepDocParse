#!/usr/bin/env python
"""契约守卫：model-gateway 实际暴露的端点必须与契约一字不差。

铁律 4（改 `/v1/*` 行为必须先改 openapi.yaml）此前只写在文档里，没有任何机械保障。
这个脚本把它变成可执行的检查，CI 每次都跑：

- 网关有、契约没有  -> 端点在契约外偷偷长出来了。消费方只认契约，
  它看不见的端点等于不存在；而契约已冻结 v1.0，新增必须先落到契约文件。
- 契约有、网关没有  -> 承诺了没实现。调用方按契约写代码会拿到 404。

只管路径与方法这一层。响应体的形状由 services/model-gateway/tests/test_contract.py
管，两者互补。

用法：
    python scripts/check_contract.py          # 退出码 0/1
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packages" / "contracts" / "openapi" / "corpus-v1.yaml"

# 契约只覆盖对外端点。这些是 FastAPI / instrumentator 自带的，不属于契约
IGNORED = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/metrics"}


def gateway_endpoints() -> set[tuple[str, str]]:
    """从 app 自己生成的 OpenAPI 取端点。

    不走 `app.routes`：FastAPI 0.141 起 include_router 不再把子路由摊平进去
    （留下的是 `_IncludedRouter`，没有 .path），照旧遍历会得到一个空集合，
    于是"契约声明了但没实现"全部误报——这种**假绿/假红**比不检查更糟。
    """
    from ddp_gateway.main import app          # noqa: PLC0415

    return {(path, method.lower())
            for path, item in (app.openapi().get("paths") or {}).items()
            if path not in IGNORED
            for method in item
            if method.lower() in ("get", "post", "put", "patch", "delete")}


def contract_endpoints(spec_path: Path) -> set[tuple[str, str]]:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    return {(path, method.lower())
            for path, item in (spec.get("paths") or {}).items()
            for method in item
            if method.lower() in ("get", "post", "put", "patch", "delete")}


def main() -> int:
    declared = contract_endpoints(SPEC)
    implemented = gateway_endpoints()

    undeclared = sorted(implemented - declared)
    unimplemented = sorted(declared - implemented)

    for path, method in undeclared:
        print(f"::error::gateway 暴露了契约里没有的端点：{method.upper()} {path}\n"
              f"  铁律 4：改 /v1/* 行为必须先改 {SPEC.relative_to(ROOT)}（契约已冻结 v1.0，"
              f"只许向后兼容的新增）")
    for path, method in unimplemented:
        print(f"::error::契约声明了网关没实现的端点："
              f"{method.upper()} {path}\n"
              f"  调用方按契约写代码会拿到 404")

    if undeclared or unimplemented:
        return 1
    print(f"契约守卫通过：{len(declared)} 个端点，契约与网关一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
