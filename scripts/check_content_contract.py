"""内容契约路由守卫 —— content-v1.yaml 声明的端点必须真有人实现。

与 `scripts/check_federation_routes.py` 同一套路，但只做**契约→实现**方向：

- content-v1 只收 Web 实际调用的读子集（+ 点名的写），不收 corpus-api 的全部
  端点（reparse/reindex/bundle/versions/publish…都不在契约里，这是故意的：
  桌面宿主对中心源只放行 GET/HEAD，写一律走联邦任务）。
  所以"实现多出来"那个方向不查 —— 查了等于逼契约把全量语料面抄一遍。
- 契约横跨两个服务：5 条 control 自有（auth/me、uploads×3、download-url）对
  control-api 的 mux 注册查；其余对 corpus-api 的 app.openapi() 查；
  `GET /api/documents/{id}/source` 是本机同源形态，中心不实现，由 ddp_local
  的镜像测试钉住（`LOCAL_ONLY_PATHS`）。

响应体的形状由 `services/corpus-api/tests/test_content_contract.py` 管，两者互补。

用法：
    python scripts/check_content_contract.py              # 门禁用，退出码 0/1
    python scripts/check_content_contract.py --self-test  # 只验比较逻辑
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract_yaml  # noqa: E402 —— 先把 scripts/ 放进 sys.path 才能导入同目录模块

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packages" / "contracts" / "openapi" / "content-v1.yaml"
CONTROL_SERVER = ROOT / "services" / "control-api" / "internal" / "api" / "server.go"

#: 受这份契约约束的前缀。
GUARDED_PREFIXES = (
    "/api/auth/me",
    "/api/resources",
    "/api/uploads",
    "/api/documents",
    "/api/conversations",
    "/api/search",
    "/api/evidence",
    "/api/wikis",
)

#: 契约声明、但由 control-api（不是 corpus-api）实现的端点。(方法, 归一化路径)
CONTROL_OWNED = {
    ("get", "/api/auth/me"),
    ("post", "/api/uploads"),
    ("get", "/api/uploads/{}"),
    ("post", "/api/uploads/{}/finalize"),
    ("get", "/api/documents/{}/download-url"),
}

#: 契约声明、但中心语料侧故意不实现、由本机运行时实现的端点。
LOCAL_ONLY_PATHS = {
    ("get", "/api/documents/{}/source"),
}

#: 存在性探针。不属于任何业务前缀，也不该被任何契约删除。
CANARY = ("/healthz", "get")

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def normalize(path: str) -> str:
    """把路径参数名抹平：`{document_id}` 与 `{cid}` 是同一段路由。

    Go 侧的 `{rest...}` 通配也抹成同一占位 —— 这里只比"有没有这条路由"，
    不比参数名。
    """
    return re.sub(r"\{[^}]+\}", "{}", path)


def guarded(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/")
               for prefix in GUARDED_PREFIXES)


def contract_endpoints() -> set[tuple[str, str]]:
    spec = contract_yaml.load(SPEC)
    return {(normalize(path), method.lower())
            for path, item in (spec.get("paths") or {}).items()
            for method in item
            if method.lower() in _HTTP_METHODS}


def corpus_endpoints() -> set[tuple[str, str]]:
    """corpus-api 声明的全部 (路径, 方法)。

    在 import 之前把服务凭据放进环境：settings 是模块级单例，import 时就读
    env；main 的 lifespan 还会拒绝占位密钥。给一个非占位值、**不设**
    ALLOW_INSECURE_DEFAULTS，让守卫跑在"检查是打开的"那个形态上。这里
    import 不触发 lifespan，所以不会连 PG / MinIO。
    """
    os.environ["SERVICE_TOKEN"] = "content-contract-guard"
    from ddp_corpus.main import app  # noqa: PLC0415

    return {(normalize(path), method.lower())
            for path, item in (app.openapi().get("paths") or {}).items()
            for method in item
            if method.lower() in _HTTP_METHODS}


def control_endpoints() -> set[tuple[str, str]]:
    """control-api mux 注册的 (归一化路径, 方法)。

    只解析 `mux.Handle("METHOD /path…")` 这种显式注册；`corpusPrefixes` 转发
    是整段前缀（方法不限），语料面的端点不靠它证明 control 自有。
    """
    out: set[tuple[str, str]] = set()
    text = CONTROL_SERVER.read_text(encoding="utf-8")
    for method, pattern in re.findall(r'mux\.Handle\("([A-Z]+)\s+([^"]+)"', text):
        out.add((normalize(pattern), method.lower()))
    return out


def self_test() -> int:
    declared = contract_endpoints()
    assert declared, "content-v1.yaml 里一条端点都没有 —— 守卫本身就失去了意义"
    problems = [ep for ep in declared if not guarded(re.sub(r"\{\}", "{x}", ep[0]))]
    assert not problems, f"契约出现了受看守前缀之外的端点：{problems}"
    for must in [("/api/search", "get"), ("/api/evidence/{}/backlinks", "get"),
                 ("/api/documents/{}/crops/{}/{}", "get"),
                 ("/api/conversations/{}/ask", "post"), ("/api/wikis", "get")]:
        assert must in declared, f"{must[1].upper()} {must[0]} 必须在契约里"
    assert CONTROL_OWNED, "control 自有集合空了 —— 方向划分逻辑可能坏了"
    assert LOCAL_ONLY_PATHS, "本机专有集合空了 —— 豁免逻辑可能坏了"
    print(f"内容契约自检通过：{len(declared)} 条声明端点，前缀与关键端点都在")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="只验比较逻辑，不需要实现落地")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    declared = contract_endpoints()
    if not declared:
        print(f"::error::{SPEC.relative_to(ROOT)} 里一条端点都没有 —— "
              f"守卫本身就失去了意义")
        return 1

    outside = sorted(ep for ep in declared if not guarded(re.sub(r"\{\}", "{x}", ep[0])))
    if outside:
        print(f"::error::契约出现了受看守前缀之外的端点：{outside}\n"
              f"  要么改路径，要么把前缀加进 GUARDED_PREFIXES")
        return 1

    corpus = corpus_endpoints()
    if CANARY not in corpus:
        print(f"::error::corpus-api 端点枚举失效：连 "
              f"{CANARY[1].upper()} {CANARY[0]} 都取不到 —— "
              f"这不是「实现缺失」。守卫的红没有信息量，先修枚举本身")
        return 1
    control = control_endpoints()
    if not control:
        print(f"::error::control-api 路由解析失效：{CONTROL_SERVER.relative_to(ROOT)} 里"
              f"一条显式注册都没扫到 —— 守卫的红没有信息量，先修解析本身")
        return 1

    failures = 0
    for path, method in sorted(declared):
        if (method, path) in LOCAL_ONLY_PATHS:
            continue
        if (method, path) in CONTROL_OWNED:
            if (path, method) not in control:
                print(f"::error::契约声明了 control-api 没实现的端点："
                      f"{method.upper()} {path}\n"
                      f"  调用方按契约写代码会拿到 404")
                failures += 1
        elif (path, method) not in corpus:
            print(f"::error::契约声明了 corpus-api 没实现的中心内容端点："
                  f"{method.upper()} {path}\n"
                  f"  调用方按契约写代码会拿到 404")
            failures += 1

    if failures:
        print(f"\n内容契约路由：契约 {len(declared)} 条，缺失 {failures} 条")
        return 1
    print(f"内容契约路由守卫通过：{len(declared)} 个端点，契约与实现一致 "
          f"（control 自有 {len(CONTROL_OWNED)} 条，本机专有 {len(LOCAL_ONLY_PATHS)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
