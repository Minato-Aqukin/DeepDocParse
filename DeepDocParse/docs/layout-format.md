# DDP-Layout v1 —— 版面中间表示

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
          "type": "text",             // 引擎给的块类型，**当前不在承诺范围内**（见下）
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
| `pdf_info[].para_blocks[].lines[].spans[].content` | string | 文本片段；块文本 = 按序拼接（规范实现见 `layout.block_text`）|

**坐标系（最容易搞错的地方）**：原点在**左上角**，x 向右、y 向下，单位是 PDF 点（1/72 英寸），
与 `page_size` 同一基准，且**已经包含页面旋转**。
裁剪出处区域时按 `page_size` 换算成像素比例 —— 不要图省事用 PDF 库报的页尺寸：
遇到 CropBox 偏移或旋转页会裁到错误区域，产出"带着已验证标记的假出处"，
是这个项目最不能接受的一种错误。

**不在承诺范围内的**：`type`、`index`、`angle` 以及引擎塞进来的其它字段。
归一化**不会删掉**它们（删了会让"下载版面 JSON"这类排查手段凭空变差），
但消费方依赖它们就要自担风险 —— 换个引擎它们可能根本不存在。

> 想让某个字段进契约（例如按 `type` 区分表格块，做块类型感知分块）：
> 先改这份文档 + `layout.PROMISED_*`，再改消费方。反过来做迟早会漂。

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
