"""DeepDocParse 契约的 Python 生成物。

**不要手改这个包里的任何东西** —— 改枚举请改 `packages/contracts/enums.yaml`，
然后重跑 `python packages/contracts/scripts/generate.py`（或 `npm run contracts:gen`）。
CI 有 `--check` 盯着生成物有没有过期。

这个包**必须保持零依赖**：它是依赖图最底下那一层，ddp_core 与四个服务都依赖它。
"""
from ddp_contracts.enums import *  # noqa: F401,F403
