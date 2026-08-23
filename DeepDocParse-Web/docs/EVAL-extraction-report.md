# 抽取评测报表（mode=offline）

样本 6 条。指标定义见 docs/EVAL-extraction.md。
**从不报综合分**：下面每一行都是一个切片。

> offline 模式不调模型，量的是 schema 层与关键词路的可定位性。
> 「字段准确率」这一列必然为空 —— 它需要 live。

| 切片 | 样本 | 字段准确率 | 出处命中率 | 空值正确率 | schema 合规率 |
|---|---|---|---|---|---|
| 全部 | 6 | — |  88.9% (8/9) | — | 100.0% (6/6) |
| born-digital | 5 | — |  88.9% (8/9) | — | 100.0% (5/5) |
| 多记录 | 1 | — | — | — | 100.0% (1/1) |
| 契约守卫 | 1 | — | — | — | 100.0% (1/1) |
| 封面字段 | 1 | — |  75.0% (3/4) | — | 100.0% (1/1) |
| 数值字段 | 1 | — | 100.0% (3/3) | — | 100.0% (1/1) |
| 短事实 | 2 | — |  83.3% (5/6) | — | 100.0% (2/2) |
| 空值样本 | 1 | — | — | — | 100.0% (1/1) |
| 英文单栏 | 5 | — |  88.9% (8/9) | — | 100.0% (5/5) |
| 表格 | 1 | — | — | — | 100.0% (1/1) |
| 长文档 | 1 | — | 100.0% (2/2) | — | 100.0% (1/1) |

## 逐字段

| 样本 | 字段 | 值 | 出处 | 空值 | 备注 |
|---|---|---|---|---|---|
| `contract/header-fields` | buyer_name | — | ✅ | — |  |
| `contract/header-fields` | seller_name | — | ✅ | — |  |
| `contract/header-fields` | contract_no | — | ✅ | — |  |
| `contract/header-fields` | signed_at | — | ❌ | — | 关键词路候选页 [] 不含期望页 0 |
| `contract/numeric-fields` | total_amount | — | ✅ | — |  |
| `contract/numeric-fields` | currency | — | ✅ | — |  |
| `contract/numeric-fields` | payment_days | — | ✅ | — |  |
| `contract/absent-clause` | penalty_rate | — | — | — | 检索零命中（下游必然 not_found） |
| `contract/absent-clause` | warranty_months | — | — | — | 检索零命中（下游必然 not_found） |
| `contract/absent-clause` | governing_law | — | — | — | 检索零命中（下游必然 not_found） |
| `contract/bad-schema-no-description` | — | — | — | — | 按预期被拦下：properties.amt 缺 description。它就是这个字段的检索 query —— 没有它只能拿字段名去检索，'amt' 这种名字必然打偏，而失败会表现成「抽不到」，看起来像模型不行 |
| `long-doc/embedded-facts` | launch_code | — | ✅ | — |  |
| `long-doc/embedded-facts` | annual_revenue | — | ✅ | — |  |
