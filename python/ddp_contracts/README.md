# ddp-contracts

`packages/contracts/enums.yaml` 的 Python 生成物。**不要手改这里的任何文件**
—— 改枚举请改 `packages/contracts/enums.yaml`，然后重跑：

```bash
python packages/contracts/scripts/generate.py      # 或 npm run contracts:gen
```

CI 用 `--check` 盯着它有没有过期。

这个包**必须保持零依赖**：它是依赖图最底下那一层，`ddp_core` 与四个服务
都依赖它，往这里加一个依赖等于给全仓加一个依赖。
