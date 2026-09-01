"""契约守卫本身的测试（C4）。

铁律 4「改 `/v1/*` 行为必须先改 openapi.yaml」此前只写在文档里。
scripts/check_contract.py 把它变成机械检查，这里保证那个检查是真的在工作 ——
一个恒真的守卫比没有守卫更危险（它会让人以为已经保护过了）。
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

from ddp_paths import CONTRACTS, REPO_ROOT

ROOT = REPO_ROOT
OPENAPI = CONTRACTS / "openapi" / "gateway-v1.yaml"


def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "check_contract", ROOT / "scripts" / "check_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def test_openapi_yaml_is_parseable():
    """契约文件必须是合法 YAML。

    看着显然，实际上直到这条测试写出来之前它都不是：`data[].embedding` 写在
    flow mapping 里没加引号，`[` 被当成流式序列的开头 —— 任何按 OpenAPI 读它的
    工具（代码生成、mock server、校验器）都会在这里炸。
    """
    spec = yaml.safe_load((OPENAPI).read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.")
    assert spec["paths"], "契约里一个端点都没有"


def test_gateway_endpoints_match_contract():
    declared = guard.contract_endpoints(OPENAPI)
    implemented = guard.gateway_endpoints()
    assert implemented - declared == set(), "gateway 暴露了契约外的端点（铁律 4）"
    assert declared - implemented == set(), "契约声明了 gateway 没实现的端点"


def test_guard_is_not_vacuous(tmp_path):
    """守卫必须真的会红。

    上一版遍历 `app.routes` 取端点，而 FastAPI 0.141 起 include_router 不再摊平子路由，
    取到的是空集合 —— 那一版守卫任何时候都在报"契约声明了但没实现"。
    这条用例把"守卫能分辨对错"钉住。
    """
    spec = yaml.safe_load((OPENAPI).read_text(encoding="utf-8"))
    spec["paths"]["/v1/not-implemented"] = {"get": {"responses": {"200": {"description": "x"}}}}
    forged = tmp_path / "openapi.yaml"
    forged.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")

    declared = guard.contract_endpoints(forged)
    assert ("/v1/not-implemented", "get") in declared - guard.gateway_endpoints()


@pytest.mark.parametrize("path", ["/v1/parse", "/v1/chat/completions", "/v1/embeddings"])
def test_frozen_v1_endpoints_are_still_declared(path):
    """契约冻结 v1.0 之后，这三个端点只许向后兼容地新增，不许消失。"""
    declared = {p for p, _ in guard.contract_endpoints(OPENAPI)}
    assert path in declared
