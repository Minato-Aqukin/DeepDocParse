# 识别质量评测（OCR eval）

> 「多模态文字识别」是这个系统的标题词之一，而在 2026-08-23 之前，
> **它一个数字都没有**。这份评测补的就是这半边。

## 为什么需要它

产品层的 `../DeepDocParse-Web/docs/EVAL.md` 有四个指标，全部在量**出处**。
那份文档自己写明了边界：

> ground truth 的坐标终究来自解析器本身。所以 bbox 指标衡量的是「检索有没有指到
> 正确的块」，**不是**「解析器切得准不准」。……不要拿这个数字去论证解析质量。

于是换引擎、升级 mineru、上 vlm-ocr，全都只能靠"看着还行"。
`scripts/eval_ocr.py` 让这件事可度量。

## 方法：切片，不报综合分

与出处评测同一套（借自 OmniDocBench）。上一轮借了它的**方法**，
这一轮把它的**数据**也接上（`--dataset` 直接读官方标注）。

## 两层指标

本机轻量层用于 CI 与快速回归：

| 指标 | 定义 | 适用页 | 能否和官方榜单比较 |
|---|---|---|---|
| **文本准确率** | `1 - 编辑距离(识别文本, 真值) / len(真值)`，归一化后按字符算 | 有 `text` 真值的页 | ⚠️ 定义同源，但本表是 accuracy 方向 |
| **表格单元格 F1** | 表格拍成 `(行, 列, 单元格文本)` 三元组，求 P/R/F1 | 有 `tables` 真值的页 | ❌，只作回归 |
| **公式编辑准确率** | `1 - 编辑距离(识别 LaTeX, 真值) / len(真值)` | 有 `formulas` 真值的页 | ❌，只作回归 |

OmniDocBench v1.6 官方层由它自己的评测器计算：**Text Edit Distance / Table TEDS /
TEDS-S / Formula CDM / Reading-order Edit Distance**。本脚本只负责把 DDP-Layout 导出为
官方输入，并把官方 `metric_result.json` 合回报告；不会重新实现一个近似版冒充官方数字。

### 为什么表格这条不是 TEDS

TEDS 是这个领域的标准指标，但它是 HTML 表格树的树编辑距离。
**一份自己实现、没有对过官方结果的 TEDS，给出的数字看起来权威、实际不可比** ——
那比没有数字更糟（这个项目的立场一贯是：会高估或不可比的指标不能用来做决策）。

单元格 F1 定义清楚、能自己验证、跨引擎可比，够回答唯一真正要问的问题：
**换引擎之后表格识别是变好了还是变差了。**

现在用 `--export-official` 直接产出官方评测输入，见下方命令。

### 归一化只做一件事

比较前去掉空白。识别出来的空格/换行与真值几乎不可能逐字一致，
留着它们等于把排版噪声算成识别错误，反而让指标对真正的错字不敏感。
**标点不去掉** —— 中文标点错了就是错了，那是识别质量的一部分。

## 真值从哪来（**最要紧的一节**）

**真值必须来自生成 PDF 的源文本，不能来自解析器的输出。**

出处评测已经在这件事上留了一处循环性（bbox 真值来自解析器），
识别评测要是也拿解析器输出当真值，就是纯粹的自我印证——
无论引擎多差，分数都是 100%。

所以仓库自带的样本由 `scripts/make_fixtures.py` **生成**：
合同 PDF 的每一行文字都是脚本写进去的，源文本就是绝对真值
（`contract_truth()` 与 `build_contract_pdf()` 共用同一份行数据，不会漂）。

```bash
python scripts/make_fixtures.py     # 产出 contract.pdf + contract.truth.json
```

## 两种数据源

| 来源 | 怎么用 | 覆盖什么 |
|---|---|---|
| 仓库 fixture（默认） | `tests/fixtures/*.pdf` + 同名 `.truth.json` | 零外部依赖。英文合同 2 页 + **自建代码密集 24 页** |
| OmniDocBench v1.6 固定子集 | `scripts/prepare_eval_corpus.py` | 论文双栏 / 公式密集 / 图表引用 / 扫描版老手册，各 10 页 |

适配层是 `load_omnidocbench()`，读官方标注里的 `text` / `html` / `latex`，
**不依赖官方评测代码**。官方每个样本一页，`page_attribute` 直接拿来做切片。

固定清单在 `eval/omnidocbench-v1.6-slices.json`，钉了官方标注 SHA-256 与官方评测器
commit。真数据不进 git；准备脚本只下载入选的 40 张图，并同时生成单页 bitmap-only PDF
（OCR 官方逐页评测）与每域 10 页 PDF（Web 出处页码评测）：

```bash
python scripts/prepare_eval_corpus.py
```

公开基准的代码页太少，另由 `scripts/make_fixtures.py` 生成 24 页 Courier 代码小集，
覆盖 CamelCase / snake_case / dotted.name / namespace / path / `--flag`；前 10 页是核心切片。

## 用法

```bash
# 本地直跑（只支持 borndigital，绕开注册表与传输层，报表会标出来）
python scripts/eval_ocr.py --engine borndigital

# 量真实部署行为（推荐）：需要 gateway 在跑，且 fixture 要放在 gateway 取得到的地方
env EVAL_FILE_BASE=http://127.0.0.1:18081 SERVICE_TOKEN=xxx \
    python scripts/eval_ocr.py --engine mineru --gateway http://127.0.0.1:9000

# 跑固定的 OmniDocBench v1.6 子集，并导出官方输入
python scripts/eval_ocr.py \
    --dataset .eval-cache/omnidocbench-v1.6 \
    --manifest eval/omnidocbench-v1.6-slices.json \
    --engine vlm-ocr --gateway http://127.0.0.1:9000 \
    --export-official /tmp/ddp-omnidocbench

# 官方评测器必须用它验证过的 Python 3.10/3.11 + TeX/ImageMagick/Ghostscript 环境
(cd /tmp/ddp-omnidocbench && /path/to/omnidocbench-venv/bin/python \
    /path/to/OmniDocBench/pdf_validation.py --config omnidocbench.yaml)

# 把官方 TEDS / TEDS-S / CDM 合回 DDP 报告
python scripts/eval_ocr.py ... \
    --official-result /tmp/ddp-omnidocbench/result/predictions_quick_match_metric_result.json \
    --markdown docs/EVAL-ocr-report.md
```

清单同时钉住 v1.6 数据 revision、标注 SHA-256 与 v1.6 评测器 commit；OmniDocBench
数据受官方 Copyright Statement 的仅研究、非商业用途限制，不能沿用代码仓库的
Apache-2.0 许可。

合回报告时五项官方汇总值必须全部存在且为有限数；`metric_debug` 中任一 TEDS/CDM
超时、错误或异常计数也会令脚本非零退出。官方评测器会把部分逐样本异常记成 0 分，
若只看汇总值就会把评测环境故障误当成模型表现。

`--gateway` 走 HTTP 而不是进程内直调，是刻意的：**评测要量部署形态下的真实行为**，
进程内直调会绕开注册表与归一化层，量出来的是另一回事。

## 当前结论（2026-08-27，fixtures，本地直跑）

| 切片 | 页 | 文本准确率 | 表格单元格 F1 | 公式编辑准确率 |
|---|---|---|---|---|
| 代码密集 | 24 | 100.0% | — | — |
| 英文合同 | 2 | 99.9% | 0.0% | — |

两个数字都是**预期之内**，且都有用：

1. **代码文本 100% / 合同 99.9%**：born-digital 直接读 PDF 文字层，本来就该接近满分。
   它是这条指标的**上界基准** —— OCR 引擎在扫描件上达不到这个数，差距就是 OCR 的代价。
2. **表格 0%**：born-digital 不做表格识别（`docs/layout-format.md` 里写着）。
   **这正是要量的东西**：换成 mineru 或 vlm-ocr 之后这个数字应该起来，
   起不来就说明表格链路没真的通。

## 还没做的

- **真机数字全缺**：本机无 GPU，mineru 与 vlm-ocr 一次都没量过。
  拿到 GPU 机器后第一件事就是把这两列填上。
- **四个真实域的真机数字**：固定 40 页与下载/导出链已经就位，但本机无 GPU。
- **官方 TEDS/CDM 数字**：导出与回读已经就位；要在 §8 批次一的官方复现环境里跑。
