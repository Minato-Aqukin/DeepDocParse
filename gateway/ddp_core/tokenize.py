"""中文分词 —— 关键词检索路的地基。**两侧共用的唯一一份。**

这里住着**两套规则**，它们看起来像复制品但不是（详见 `whole_text_bigrams`）：

    tokens / tokenized / query_string   产品层持久索引用，优先 jieba，按 CJK 段切
    whole_text_bigrams                  gateway 检索路用，不引 jieba，二元组跨段

搬进 ddp_core 时**两套都原样保留** —— 阶段 1 的标准是「行为零变化」。

## 为什么需要它

PostgreSQL 的 `to_tsvector('simple', ...)` 按空白切词，**中文整段会变成一个 token**：
一句 40 字的中文＝一个词，`websearch_to_tsquery` 几乎永远匹配不上。
于是"混合检索"在中文文档上实际只有向量一条腿 —— 而 A1 评测量到的
关键词路单打独斗时页码命中率 25%，正是这条腿瘸着的样子。

## 为什么是应用层分词，不是 PG 扩展

实测过三个 PG 扩展（见 plan.md D2）：`pg_jieba` 停滞近 3 年、`yanyiwu/pg_jieba` 是空壳、
只有 `zhparser` 还在维护。但两条路都要**自建 PG 镜像**（现在是 stock `pgvector/pgvector:pg16`），
对一个主打自部署的项目是实打实的分发成本。

应用层分词换来三件事：不动 PG 镜像 · 单测照样在 SQLite 里跑（MemoryIndex 用同一个
tokenizer）· 索引时切一次存一列，查询时切一次，`to_tsvector('simple', ...)` 直接可用。

## jieba 是软依赖，但降级必须可见

装不上 jieba（离线机器、受限网络）时退回 CJK 二元组 —— 能用，但召回明显差。
**这个差别必须说得出来**：`backend()` 返回当前实现，它会进 `model_meta`
和启动日志。静默用一个更差的分词器，然后让人对着"检索怎么变差了"排查半天，
正是这个项目吃过大亏的那类事。
"""
import re

try:                                # noqa: SIM105
    import jieba

    # 关掉 jieba 自己的日志：它在首次切词时会往 stderr 打一段构建前缀词典的话，
    # 混在 uvicorn 日志里像报错
    jieba.setLogLevel(60)
    _JIEBA = True
except ImportError:                 # pragma: no cover - 取决于部署环境
    jieba = None
    _JIEBA = False

# 英文/数字按词。中文交给 jieba（或二元组兜底）
_ASCII_WORD = re.compile(r"[a-zA-Z0-9]+")
_CJK = re.compile(r"[一-鿿]+")
_CJK_CHAR = re.compile(r"[一-鿿]")
# 单字词几乎不携带区分度（"的""在""了"），但**不做停用词表**：
# 停用词表要维护、跨领域会误杀（"钱"在财务文档里是实词）。只滤掉长度 1 的纯中文词，
# 代价可控且不需要任何词典
_MIN_CJK_LEN = 2


def backend() -> str:
    """当前用的分词实现：jieba | bigram。会进 model_meta 与启动日志。"""
    return "jieba" if _JIEBA else "bigram"


def _bigrams(text: str) -> list[str]:
    """CJK 二元组兜底。**按传进来的这一段切**，调用方负责先分出 CJK 连续段。"""
    chars = _CJK_CHAR.findall(text)
    return ["".join(p) for p in zip(chars, chars[1:])] or chars[:1]


def whole_text_bigrams(text: str) -> list[str]:
    """中英混排的轻量切分：英文/数字按词，CJK 按二元组 —— **gateway 检索路专用**。

    **它和下面的 `tokens()` 不是同一套规则，别当成复制品合掉。** 两处差异：

    1. **不用 jieba。** gateway 是无状态薄适配层，多一个 7MB 词典依赖不划算；
       真正需要中文分词质量的是产品层的持久索引，那里才值得付这个成本。
    2. **二元组跨段。** 它把全文的 CJK 字符**先拼在一起**再切二元组，
       而 `tokens()` 是按 CJK 连续段分别切。于是
       `"中文 abc 日本"` 在这里会多出一个跨过空隙的 `文日`，那边不会。

    第 2 条是个**已知的粗糙处，不是有意设计** —— 但阶段 1 的标准是「行为零变化」，
    所以原样搬过来，没有顺手"修正"。要动它得单独立项并重跑检索评测
    （`gateway/app/services/retrieval.keyword_rank` 的打分会跟着变）。
    """
    out = _ASCII_WORD.findall(text.lower())
    chars = _CJK_CHAR.findall(text)
    out += ["".join(p) for p in zip(chars, chars[1:])] or chars[:1]
    return out


def tokens(text: str) -> list[str]:
    """切词，返回 token 列表（未去重，词频信息留给 ts_rank）。"""
    if not text:
        return []
    out = _ASCII_WORD.findall(text.lower())
    for run in _CJK.findall(text):
        if _JIEBA:
            out.extend(w for w in jieba.cut(run, cut_all=False)
                       if len(w) >= _MIN_CJK_LEN)
        else:
            out.extend(_bigrams(run))
    return out


def tokenized(text: str) -> str:
    """切好并用空格连起来 —— 直接喂给 `to_tsvector('simple', ...)` 的形态。

    存成一列（`chunks.text_tokenized`）而不是查询时现切：
    现切要在 SQL 里调 Python 函数，做不到；而且索引时切一次比每次查询切一遍便宜得多。
    """
    return " ".join(tokens(text))


def query_string(text: str) -> str:
    """查询侧切词。**必须与索引侧用同一个 tokenizer**，否则两边切法不同 = 永远不匹配。

    这是这类方案最经典的翻车点：索引用 jieba、查询用空白切，
    表现是"关键词检索一条都命中不了"，而没有任何报错。
    """
    return tokenized(text)
