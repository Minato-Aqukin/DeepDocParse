# DDP-Layout v1 —— 版面中间表示

> **v1.2（2026-08-26）**：新增顶层可选承诺 `engine_notes`（引擎对这一趟解析的自述，
> 见下面「引擎自述」一节）。`layout_version` **不变**（仍是 `ddp-layout/1`）——
> 与 v1.1 升 `type` 同样是向后兼容的新增：老消费方不读它照跑。
> 动机是不变式「任何降级都必须可见」：有一类故障请求 200、文字也出来了，
> 只是 bbox 悄悄全没了，而抛异常太狠、只打日志又不会跟着归档走。

> **v1.1（2026-08-23）**：`para_blocks[].type` 从「不在承诺范围内」升进承诺字段，
> 并把表格 HTML（`spans[].html`，经 `layout.table_html()` 取）纳入可选承诺。`layout_version` **不变**（仍是 `ddp-layout/1`）——
> 这是向后兼容的新增：老消费方不读 type 照跑，新消费方多了一个可依赖的信号。
> 动机见下面「为什么 type 要进契约」。

`/v1/parse/{task_id}/result` 返回的 `layout_json` 用的就是这个格式。
在此之前它是一个**事实上的内部 schema**：四处消费它，却从未被承认为契约。
后果是注册表在传输层做到了引擎无关，数据格式层却写死了 mineru ——
「加引擎 = 加容器 + 一行配置」这个承诺只兑现了一半。

这份文档把它变成契约：**这里写了什么，消费方才能依赖什么。**

- 归一化实现：`gateway/app/services/layout.py`
- 生成方（normalizer，每个引擎一个）：`gateway/app/services/engines.py`
- 消费方：
  - `gateway/app/services/chunking.py`（索引链分块）
  - `mcp_server/server.py::_layout_blocks`（ask_document 检索与裁剪）
  - `DeepDocParse-Web/backend/app/chunking.py`（产品层分块，按铁律 1 各写一份）
  - `DeepDocParse/openapi.yaml` 的 `layout_json` 字段

## 结构

```jsonc
{
  "layout_version": "ddp-layout/1",   // 版本标记，加字段不改它，改语义/删字段才改
  "engine": "mineru",                 // 产出它的引擎名（排查用，不要拿来做分支逻辑）
  "pdf_info": [                       // 一页一个元素，顺序即页序
    {
      "page_idx": 0,                  // 0 基页码。**出处必须落到唯一页**，缺它整套定位失效
      "page_size": [612.0, 792.0],    // [宽, 高]，bbox 的坐标基准；含页面旋转
      "para_blocks": [                // 页内的块，顺序即阅读序
        {
          "type": "text",             // 归一化块类型，**v1.1 起在承诺范围内**（七值之一）
          "type_native": "plain text",// 引擎原生类型，归一化改写了才出现；排查用，不承诺
          "bbox": [72, 72, 540, 100], // [x0, y0, x1, y1]
          "lines": [                  // 块内的行
            { "spans": [ { "content": "第一行文字" } ] }
          ]
        }
      ]
    }
  ]
}
```

## 承诺字段（消费方只准依赖这些）

| 路径 | 类型 | 含义 |
|---|---|---|
| `pdf_info[]` | array | 按页序排列 |
| `pdf_info[].page_idx` | int | 0 基页码 |
| `pdf_info[].page_size` | [number, number] | 该页的 `[宽, 高]`，与 bbox 同一坐标系 |
| `pdf_info[].para_blocks[]` | array | 页内块，按阅读序 |
| `pdf_info[].para_blocks[].bbox` | [x0, y0, x1, y1] \| null | 块的外接矩形；可能缺失（缺了就不能裁剪，但块仍然有效）|
| `pdf_info[].para_blocks[].type` | string | **v1.1**：归一化块类型，取值只能是下表七个之一 |
| `pdf_info[].para_blocks[]…spans[].html` | string \| null | **v1.1，可选承诺**：表格块的 HTML。**不是块上的 `table_html` 字段**——它塞在 span 里，规范取法是 `layout.table_html(block)`（会下潜嵌套 blocks）。引擎没给就是 None，消费方必须能处理 |
| `pdf_info[].para_blocks[].lines[].spans[].content` | string | 文本片段；块文本 = 按序拼接（规范实现见 `layout.block_text`）|

**坐标系（最容易搞错的地方）**：原点在**左上角**，x 向右、y 向下，单位是 PDF 点（1/72 英寸），
与 `page_size` 同一基准，且**已经包含页面旋转**。
裁剪出处区域时按 `page_size` 换算成像素比例 —— 不要图省事用 PDF 库报的页尺寸：
遇到 CropBox 偏移或旋转页会裁到错误区域，产出"带着已验证标记的假出处"，
是这个项目最不能接受的一种错误。

### 引擎自述：`engine_notes`（v1.2，可选承诺）

| 路径 | 类型 | 含义 |
|---|---|---|
| `engine_notes` | string[] | **顶层**，可缺省。引擎对这一趟解析的自述：识别出来了、但有理由怀疑结果不对劲。每条以一个稳定的短标识开头（`<code>: <人话>`），消费方可以按标识分支 |

这一格存在的理由是**不变式「任何降级都必须可见」**：有一类故障不是"失败"——
请求 200、文字也识别出来了，只是某个我们依赖的东西悄悄没生效。
抛异常太狠（内容是真的），只打日志又等于没有（日志不会跟着归档走）。
`engine_notes` 跟着 `layout_json` 一起存进归档，排查时一眼可见。

**不是错误列表**：有 note 不代表这份版面不能用。它是"请人看一眼"，不是"作废"。

现有标识：

| 标识 | 什么时候出现 | 多半是什么原因 |
|---|---|---|
| `dsocr2_no_grounding` | 走 `deepseek-ocr2` 方言、**要过 grounding**、每页都识别出了文字、却一个 grounding 标签都没有 | 上游把 `skip_special_tokens: false` 吃掉了（中间隔了个 OpenAI 代理，或上游根本不是 vLLM）。后果是所有 bbox 全为 null —— 出处定位整体失效，而其余每一条路径都不会报错 |

> `options.grounding: false` 是受支持的用法（走官方 `Free OCR.`，全页一个块、本来就没有 bbox），
> 那一趟**不会**产出这条 note —— 没要过标签就不该报"标签没来"。

**不在承诺范围内的**：`type_native`、`index`、`angle` 以及引擎塞进来的其它字段。
归一化**不会删掉**它们（删了会让"下载版面 JSON"这类排查手段凭空变差），
但消费方依赖它们就要自担风险 —— 换个引擎它们可能根本不存在。

> 想让某个字段进契约（例如按 `type` 区分表格块，做块类型感知分块）：
> 先改这份文档 + `layout.PROMISED_*`，再改消费方。反过来做迟早会漂。

## 块类型词汇表（v1.1）

`type` 只能是这七个值之一。**归一化认不出来的一律归 `other`，不许丢块**——
丢块会让新引擎的内容凭空消失，而那正是这份契约要防的事。

| 值 | 含义 | 下游谁在用 |
|---|---|---|
| `text` | 正文段落 | 分块的主体 |
| `title` | 标题 | 作后续块的上下文前缀（不单独成块，太短检索不到） |
| `table` | 表格 | **独立成块，永不与正文合并**；抽取平面按它找记录数组 |
| `figure` | 图片 / 图注 | 独立成块；无文字层时只留 bbox |
| `equation` | 行间公式 | 独立成块 |
| `list` | 列表 / 目录 | 当正文处理，但不做标题前缀 |
| `other` | 认不出来的 | 当正文处理 |

映射表在 `layout.normalize_type()`（`_MINERU_TYPE_MAP`）。加引擎时先看能不能映射到已有七值，
**不要顺手加第八个值**：加值要同步改所有消费方的分支，是破坏性变更。

### 为什么 type 要进契约

v1 明确写着 type「不在承诺范围内」，于是四个消费方只能把所有块一视同仁当正文。
后果实测到两条：

1. **整张表格的文字在分块阶段被静默丢弃**。mineru 把表格内容放在 `blocks` 子结构里
   （`table_body` / `table_caption`），块自身没有 `lines`；而 `block_text` 只读 `lines`。
   表格解析出来了、索引里却没有——问表格里的数永远检索不到，**全程没有任何报错**。
   v1.1 的 `block_text` 因此会下潜嵌套 `blocks`。
2. **表格被揉进相邻段落**。合并循环只看字符数，一张表和它上下的正文会并成一个 chunk，
   出处 bbox 横跨整片版心，行列关系也早就拍平没了。

「结构化信息提取」这条线上，表格是最典型的载体。type 不进契约，
上面两条就没有任何地方可以修——这就是**把它升进契约的理由，而不是在消费方各自猜**。

### 表格结构：`table_html`

`block_text` 拼出来的是**拍平的单元格文字，行列关系已经没了**。
表格结构的唯一载体是 `table_html`（mineru 的表格模型产出，塞在 `table_body` 的 span 的 `html` 字段）。
规范取法：`layout.table_html(block)`。

它是**可选承诺**：born-digital 不做表格识别，给的是 `None`。
消费方必须能处理 `None`（退回按拍平文字处理，并让降级可见）。

## 现有 normalizer

| 引擎 | runtime | 归一化做什么 |
|---|---|---|
| MinerU | `mineru-api` | middle_json 本来就是这个格式的来源，归一化只做盖章与补齐（版本标记、page_idx 兜底、para_blocks 类型保证）|
| born-digital | `borndigital` | 从 pypdfium2 的字符/行矩形从零构造：行按 y 聚类、按行距合并成段，坐标从 PDF 空间（左下原点、不含旋转）翻到显示空间 |

### born-digital 的已知限制（写在这儿，免得被当成 bug 反复排查）

- **不做版面分析**。分栏靠水平投影自然分开（合并时拿块里最后一行比，不是并集 bbox），
  但**跨栏标题会被并进它下面那一栏的第一段**，该块的 bbox 因此横跨整个版心。
  文本顺序是对的，bbox 偏大。要精确的块类型与阅读序请用 mineru。
- **不抽图**，`images` 恒为空数组。
- **扫描件直接失败**而不是返回空版面 —— 空版面会让下游以为"解析成功了，只是没内容"。
- **整份 PDF 会读进 worker 内存**（有 `BORNDIGITAL_MAX_BYTES` 上限）。
  它跑在进程内，没有容器隔离：一份畸形的大文件影响的是 worker 本身，
  不像 mineru 那样被关在自己的容器里。这是"零依赖启动"换来的代价。

新增一个引擎要做的事：写一个 normalizer 产出上表的字段，跑 `layout.validate()` 确认没漏，
然后在 `models.yaml` 里加一行（`runtime` 指到新适配器）。**不需要改任何消费方**。

## 自检

```python
from app.services import layout
problems = layout.validate(layout_json)   # 返回问题清单，空 = 通过
```

`validate` **不在请求路径上强制**：一个字段缺失不该让整份解析结果作废。
但它必须能被发现 —— 所以它在测试里是硬断言（`tests/test_layout.py`）。
