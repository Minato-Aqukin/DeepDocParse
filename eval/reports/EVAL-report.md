# 出处评测报表（mode=offline）

样本 16 条。指标定义见 docs/EVAL.md。

| 切片 | 样本 | 页码命中率 | bbox 包含率 | 拒答正确率 | 降级标记准确率 |
|---|---|---|---|---|---|
| 全部 | 16 |  92.9% (13/14) |  92.9% (13/14) |  50.0% (1/2) | — |
| born-digital | 16 |  92.9% (13/14) |  92.9% (13/14) |  50.0% (1/2) | — |
| 中文 | 1 | — | — | 100.0% (1/1) | — |
| 代码密集 | 10 | 100.0% (10/10) | 100.0% (10/10) | — | — |
| 拒答 | 2 | — | — |  50.0% (1/2) | — |
| 标识符精确查询 | 10 | 100.0% (10/10) | 100.0% (10/10) | — | — |
| 标题 | 2 |  50.0% (1/2) |  50.0% (1/2) | — | — |
| 短事实 | 2 | 100.0% (2/2) | 100.0% (2/2) | — | — |
| 英文单栏 | 5 |  75.0% (3/4) |  75.0% (3/4) |   0.0% (0/1) | — |

## 逐条

| 样本 | 页码 | bbox | 拒答 | 降级 | 备注 |
|---|---|---|---|---|---|
| `long-doc/zephyr-code` | ✅ | ✅ | — | — | want p3, got [3, 5] |
| `long-doc/acme-revenue` | ✅ | ✅ | — | — | want p5, got [5, 3] |
| `long-doc/heading-page-1` | ✅ | ✅ | — | — | want p1, got [1, 2, 3, 4] |
| `long-doc/heading-page-4` | ❌ | ❌ | — | — | want p4, got [1, 2, 3, 4] |
| `long-doc/unanswerable-quantum` | — | — | ✅ | no_hits | citations=0 |
| `long-doc/unanswerable-ceo` | — | — | ❌ | — | citations=2 |
| `code/http-request-parser` | ✅ | ✅ | — | — | want p1, got [1, 2, 3, 4] |
| `code/parse-job-id` | ✅ | ✅ | — | — | want p2, got [2, 8, 1, 3] |
| `code/registry-default-of` | ✅ | ✅ | — | — | want p3, got [3, 1, 2, 4] |
| `code/ddp-core-path` | ✅ | ✅ | — | — | want p4, got [4] |
| `code/std-vector-result` | ✅ | ✅ | — | — | want p5, got [5] |
| `code/java-indexer` | ✅ | ✅ | — | — | want p6, got [6] |
| `code/skip-special-tokens` | ✅ | ✅ | — | — | want p7, got [7] |
| `code/qa-mismatch-threshold` | ✅ | ✅ | — | — | want p8, got [8, 2] |
| `code/load-citation-targets` | ✅ | ✅ | — | — | want p9, got [9, 1, 2, 3] |
| `code/evidence-content-digest` | ✅ | ✅ | — | — | want p10, got [10, 20, 23, 1] |
