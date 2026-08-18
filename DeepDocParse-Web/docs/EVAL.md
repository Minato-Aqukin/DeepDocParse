# 出处评测（citation eval）

> 这个类别里**没有任何公认基准在度量 bbox 级出处的正确性**。所有项目都宣称支持
> grounded citation，没有一个能证明。这份评测就是为了让本项目能证明。

## 为什么需要它

现有验收很厚（单测 + e2e 约 40 条断言），但几乎全部在问「跑通没有」：

| 原来的断言 | 实际检查的 | 漏掉的 |
|---|---|---|
| 回答非空 | `bool(answer.strip())` | 是否正确 |
| 回答带出处 | `bool(citations)` | 出处是否正确 |
| 出处页码在范围内 | `0 <= page < page_count` | 50 页文档答案在 37 页，给 0 页也通过 |
| 出处裁剪图可取回 | `len(crop) > 100` | 图里有没有答案 |

有个规律值得记下来：**降级路径测得极扎实，成功路径几乎没测**——
这是「降级必须可见」那条铁律的副作用。它保证了出错时不会骗人，
但没有保证不出错时是对的。

`scripts/eval_citations.py` 补的就是后半句。

## 方法：切片，不报综合分

借自 OmniDocBench（借方法，不是借基准）：**从不报一个综合分**，按属性分别给分。
综合分只告诉你「变好了 3%」，切片才告诉你「双栏页的出处命中率只有 40%」——
前者没法指导任何决策，后者直接指向下一步该修哪儿。

当前属性标签：`中文` / `英文单栏` / `中文双栏` / `含表格` / `扫描件` / `旋转页` /
`短事实` / `标题` / `拒答` / `born-digital`。加标签不用改代码，写进样本的 `attributes` 即可。

## 四个指标

| 指标 | 定义 | 适用样本 |
|---|---|---|
| **出处页码命中率** | 期望页码是否出现在返回的出处里。默认只看 **top-1**（用户第一眼看到的那条）；`--any-citation` 放宽到任意一条 | `answerable=true` 且标了 `page_idx` 或 `text_anchor` |
| **bbox 包含率** | 出处 bbox **在同一页上**盖住了期望区域的 ≥50%（交集面积 / 期望区域面积）。同页是硬条件——不同页的两个块坐标当然可能重叠（版式一样）。判定用「覆盖比例」而不是「相交」：相交的门槛低到几乎恒真，那样这一列只会是页码列的影子 | 同上，且能定出期望 bbox |
| **拒答正确率** | 文档里没有答案时，是否真的拒答了（零出处 / `degraded=no_hits` / 回答里明说未找到）。**给了出处又说没找到不算对**：出处是断言，不是装饰 | `answerable=false` |
| **降级标记准确率** | 实际 `degraded` 是否与样本期望一致 | 样本写了 `expect.degraded` |

## 两种模式

| 模式 | 依赖 | 量的是什么 |
|---|---|---|
| `offline` | 只要本地版面样本 + 本层分块代码 | **定位链路本身**：分块粒度、页码归属、bbox 精度。检索是纯词面打分（无向量），所以数字是「关键词路单打独斗」的**下界**，不是产品实际表现 |
| `live` | backend + PG + MinIO + embedding + chat 全在跑 | 四个指标全量，就是用户真实拿到的东西 |

```bash
python scripts/eval_citations.py --mode offline
python scripts/eval_citations.py --mode live --web http://127.0.0.1:8080
python scripts/eval_citations.py --mode offline --markdown docs/EVAL-report.md
```

没有 GPU 的机器上只跑得动 `offline`——而它恰好覆盖的是差异化所在的那一半（定位），
不是模型能力那一半。

## 数据集格式

`eval/citations.json`：

```jsonc
{
  "samples": [
    {
      "id": "long-doc/zephyr-code",
      "source": {"kind": "local", "path": "../DeepDocParse/tests/fixtures/long-doc.pdf"},
      // 外部样本用 {"kind": "url", "url": "https://arxiv.org/pdf/xxxx.pdf"}
      "question": "What is the launch code of project Zephyr?",
      "expect": {
        "answerable": true,
        "page_idx": 2,                 // 0 基页码
        "text_anchor": "launch code",  // 答案原文片段；bbox 由它在版面里定位得出
        "bbox": null,                  // 也可以直接写死 [x0,y0,x1,y1]
        "answer_contains": ["8712"],
        "degraded": null               // 期望的降级标记，不写则不计入该指标
      },
      "attributes": ["英文单栏", "born-digital", "短事实"]
    }
  ]
}
```

**样本必须可公开**：仓库自带的 fixture，或 arXiv（CC-BY）、政府公开文件这类
可自由引用的来源。外部样本**只记 URL，PDF 不进仓库**——再分发别人的文件是另一回事。

### 半自动标注

`text_anchor` 是把标注成本压下来的关键：人只需要抄一句**答案原文长什么样**，
页码与 bbox 由版面自己算出来（`_anchor_bbox`），不用逐条量坐标。
30~50 条标注因此是一下午而不是一整周的事。

`text_anchor` 定位的是版面里含这段文字的**原始 `para_block`**，不是分块后的 chunk。
这一点很要紧：早先的实现用 `layout_to_chunks(max_chars=100000)` 取 ground truth，
那个上限会把整页所有块并成一个 chunk，bbox 就是整片版心 —— 于是同页的任何出处
都必然「覆盖」它，**bbox 指标恒等于页码指标，永远不会独立变红**（2026-08-18 验收抓到）。
回归用例在 `backend/tests/test_eval_metrics.py`。

**还剩一处循环性，用之前必须知道**：ground truth 的坐标终究来自解析器本身。
所以 bbox 指标衡量的是「检索有没有指到正确的块」，**不是**「解析器切得准不准」。
后者要人工核对原件。不要拿这个数字去论证解析质量。

## 当前结论（2026-08-18，offline 模式，6 条种子样本）

| 切片 | 页码命中率 | bbox 包含率 | 拒答正确率 |
|---|---|---|---|
| 全部 | 25.0% (1/4) | 25.0% (1/4) | 50.0% (1/2) |
| 标题 | 0.0% (0/2) | 0.0% (0/2) | — |
| 短事实 | 50.0% (1/2) | 50.0% (1/2) | — |

**已经暴露出两个可改进点**（这正是验收标准要的：指标必须问得出问题）：

1. **关键词路单独工作时定位很差**（页码命中率 25%）。种子文档每页共享大量填充词，
   纯词面打分区分不开页。这量化了一件以前只是"知道"的事：
   `degraded=embedding_unavailable` 时出处的可信度会掉到什么程度。
   → 直接支持 D2（中文分词）与 D1（rerank）的优先级判断，**但先要 live 模式的数字**
   才能知道向量路补回了多少。
2. **拒答正确率 50%**：`unanswerable-ceo` 这条靠共现词捞出了两条"出处"。
   相似度下限（`qa_min_similarity`）正是为了拦住它，而 offline 模式没有向量、
   下限无从生效 —— 这条失败**恰好证明了那道下限不是可选项**。

> 报表产物在 `docs/EVAL-report.md`（可重跑覆盖）。

## 前置条件

- `--mode offline` 只读 `backend/tests/fixtures/layout-*.json`，**没有任何外部依赖**。
- `--mode live` 会按样本的 `source` 取原件。种子样本里 `kind=local` 的路径指向
  `../DeepDocParse/tests/fixtures/long-doc.pdf`，**要求两个仓库并排 clone**；
  单独 clone 本仓库时那几条会 FileNotFoundError。外部样本（`kind=url`）当场下载，
  PDF 不进仓库 —— 再分发别人的文件是另一回事。

## 还没做的

- **样本量**：种子只有 6 条，目标 30~50 条。缺的是标注工作量，不是工具。
- **属性覆盖**：`中文双栏` / `含表格` / `扫描件` / `旋转页` 四个切片目前**一条样本都没有**——
  而它们恰恰是最可能出问题的那几类。
- **live 模式的真实数字**：需要 embedding + chat 运行时，本机无 GPU 跑不了。
  offline 的数字**不能**代表产品表现，别混着引用。
- **版面样本只有 born-digital 产出的一份**。mineru 的格式漂移仍然没有测试盯着
  （那需要一份真实 mineru middle_json，要 GPU）。见 `backend/tests/test_chunking.py` 的说明。
