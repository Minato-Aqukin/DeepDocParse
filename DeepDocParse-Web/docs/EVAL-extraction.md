# 抽取评测（extraction eval）

> 抽取的输出是要**被当数据用**的。一个抽错的字段不像一个答错的问题——
> 它会被复制进表格、汇总进报表，没有人会再去核对。
> 所以这条线上的评测比问答那条更要紧。

方法论沿用 [EVAL.md](EVAL.md) 一个字不改：**按属性切片，从不报综合分**。

## 四个指标

| 指标 | 定义 | 适用字段 |
|---|---|---|
| **字段准确率** | 抽出来的值与期望值一致（按类型归一化后比较） | 期望 `status=found` 的字段 |
| **字段出处命中率** | 该字段的出处落在期望页码上。默认只看 **top-1 出处**；`--any-citation` 放宽 | 同上，且样本标了 `page` |
| **空值正确率** | 文档里确实没有的字段，是否真的报了 `not_found` | 期望 `status=not_found` 的字段 |
| **schema 合规率** | 结果是否符合 DDP-Extract v1。**负样本反着算**：标了 `expect.schema_valid=false` 的样本，被守卫拦下才算通过 | 每个样本一次 |

### 空值正确率是核心，不是凑数的第四个

抽取里最危险的输出是**看起来像结论的空值**。
把"我们的检索挂了"报成"文档里没有"，用户会直接拿去用——
而"这份合同没写违约金"和"我们没能抽出来"在一张表格里长得一模一样。

所以判定是严格的：**`error` 不算对**。只有真的报了 `not_found` 才算。
这一条对应 [EVAL.md](EVAL.md) 的「拒答正确率」，方法论上是同一件事。

### 值比较只做最小归一化

数字去掉千分位与空白（`"USD 486,200.50"` → `486200.5`），
字符串去掉空白与常见标点。**不做同义词、不做模糊匹配** ——
那会把"差不多对"算成对，而这个指标存在的全部意义就是把"差不多"和"对"分开。

## 两种模式

| 模式 | 依赖 | 量的是什么 |
|---|---|---|
| `offline` | 只要本地版面样本 + 本层分块与 schema 代码 | **schema 层与定位链路**：schema 校验是否严格、字段的期望页进不进得了关键词候选。**不调模型**，所以「字段准确率」与「空值正确率」两列必然为空 |
| `live` | backend + PG + MinIO + embedding + chat 全在跑 | 四个指标全量，就是用户真实拿到的东西 |

offline 判定「可定位性」的口径值得说清楚：它是**字段准确率的上界**——
检索都到不了那一页，模型再强也抽不出来。
反过来 offline 通过不代表 live 会通过（模型可能从对的块里抽出错的值）。

**offline 的数字不能代表产品表现，别混着引用。**

## 用法

```bash
python scripts/eval_extraction.py --mode offline
python scripts/eval_extraction.py --mode live --web http://127.0.0.1:8080
python scripts/eval_extraction.py --mode offline --markdown docs/EVAL-extraction-report.md
```

## 数据集格式

`eval/extractions.json`：

```jsonc
{
  "samples": [
    {
      "id": "contract/header-fields",
      "source": "../DeepDocParse/tests/fixtures/contract.pdf",   // live 模式上传它
      "layout": "backend/tests/fixtures/layout-contract.json",   // offline 模式读它
      "schema": { /* 受限 JSON Schema，见 ../DeepDocParse/docs/extract-format.md */ },
      "expect": {
        "schema_valid": true,        // 省略即 true；false = 负样本，期望被守卫拦下
        "fields": {
          "buyer_name": { "status": "found", "value": "Northwind Trading Company Limited", "page": 0 },
          "penalty_rate": { "status": "not_found" }    // 空值样本
        }
      },
      "attributes": ["英文单栏", "born-digital", "封面字段"]
    }
  ]
}
```

**真值来自 `../DeepDocParse/scripts/make_fixtures.py` 的 `CONTRACT_FIELDS`。**
合同 PDF 由那个脚本生成，字段值与表格行都是写进去的，所以是绝对真值。
改了那里的合同内容就要同步改这里——不同步的表现是"抽取准确率突然掉了"，
而实际上是评测集过期了。

## 当前结论（2026-08-23，offline，6 条种子样本）

| 切片 | 样本 | 字段准确率 | 出处命中率 | 空值正确率 | schema 合规率 |
|---|---|---|---|---|---|
| 全部 | 6 | — | 88.9% (8/9) | — | 100.0% (6/6) |
| 封面字段 | 1 | — | 75.0% (3/4) | — | 100.0% |
| 数值字段 | 1 | — | 100.0% (3/3) | — | 100.0% |
| 空值样本 | 1 | — | — | — | 100.0% |

> 「空值正确率」这一列在 offline 下是空的，**这是刻意的**。
> 早先的实现拿"检索零命中"当空值正确，但那个判定取值只可能是"对"或"不适用"，
> **永远不会红** —— 一个不会红的指标不能用来做决策。
> 验收抓到了这一条（EVAL.md 里「bbox 指标恒等于页码指标」那个先例的重演）。

**已经抓到一个真问题**（这正是验收标准要的：指标必须问得出问题）：

> `contract/header-fields` 的 `signed_at` 字段，关键词路候选页是空的。
> 原因是这个字段的检索 query 是中文（"signed_at 合同签订日期"），
> 而文档是英文——jieba 切出来的中文词与英文文本零交集。
>
> **这不是分词器的 bug，是混合检索的一个真实边界**：关键词路跨不了语种。
> 向量路（bge-m3 是多语言模型）能跨，所以这条在 live 模式下大概率不会红。
> 但它量化了一件以前只是"知道"的事：`degraded=embedding_unavailable` 时，
> **跨语种的字段会直接抽不到**，而不是"质量下降一点"。

## 还没做的

- **live 模式的真实数字**：需要 embedding + chat 运行时，本机无 GPU 跑不了。
  「字段准确率」这一列到现在还是空的——**这条线最重要的那个数字一次都没量过**。
- **样本量**：种子只有 6 条，目标 30~50 条。缺的是标注工作量不是工具。
- **属性覆盖**：`中文` / `扫描件` / `真表格` 三个切片一条样本都没有。
  尤其是**真表格**：born-digital 不做表格识别，`contract/schedule-rows` 那条
  实际是在拿"拍平成文本的表格"当表格测——换 mineru 之后才是真的在测表格抽取。
- **多记录（array）的判定**：目前只判了 schema 合规，没逐行比对记录。
  逐行比对需要处理"顺序不同""行数不同"，那是另一套判定逻辑，等有真表格样本再做。
