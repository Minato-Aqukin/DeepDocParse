"""全仓共享的路径定位器。

冻结夹具住在仓库根的 `tests/fixtures/`，而消费方分散在
`services/*/tests`、`python/*/tests`、`eval/tests` 里 —— 它们的 rootdir 各不相同。
各包的 pyproject 用 pytest 的 `pythonpath` 把本目录挂上，测试统一
`from ddp_paths import FIXTURES`，不要再各写一份 `parents[N]` 的数数游戏
（数错了表现是 skip 而不是 fail，夹具悄悄不参与测试）。
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REGISTRY = REPO_ROOT / "infra" / "registry"
CONTRACTS = REPO_ROOT / "packages" / "contracts"


def fixture(name: str) -> Path:
    """定位一个冻结夹具；不存在就当场报错，不要静默 skip。"""
    p = FIXTURES / name
    if not p.exists():
        raise FileNotFoundError(f"缺少冻结夹具 {p} —— 跑 scripts/make_fixtures.py 重建")
    return p
