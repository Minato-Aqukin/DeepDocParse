"""出处锚定判据 —— **两个仓库、运行时与迁移，共用的唯一一份**。

## 为什么必须只有一份

判据回答的是同一个问题：**"这条出处指的，还是当初那段原文吗？"**
运行时读出处时问一次，历史回填时问一次。两处判据一旦有差异，
回填就会把一批"其实对得上"的出处标成失效（可惜，但安全），
或者把"其实对不上"的标成有效 —— **那就是带着已验证标记的假出处**，
本项目定义的最恶劣错误（plan.md §9 不变式 1）。

alembic 迁移通常应当自包含（冻结当时的逻辑），这里**故意不那样做**：
判据漂移的代价远大于迁移与代码耦合的代价，而 `ddp_core` 本来就是
两侧共用的叶子模块，没有循环依赖的风险。有守卫钉着两边是同一个函数对象。

## 两条路，不是一条

    有 content_digest（阶段 2b 之后写的）  -> 比指纹，**严格**
    没有（阶段 2b 之前的老记录）           -> 比 snippet 包含，**宽松**

老记录当年只存了截断过的 snippet，指纹无从补算 —— 硬要给它算一个
"当前块的指纹"就等于宣布"它一直指着这里"，那是凭空造证。
所以老记录只能继续走宽松判据。**别以为 digest 是全覆盖的。**
"""
import hashlib


def normalize(text: str) -> str:
    """比对前的归一化：只压空白。

    **不动标点、不改大小写** —— 这里要回答的是"内容变没变"，
    不是"读起来像不像"。分块规则变化会改换行与前缀（标题现在会并进块首），
    所以空白必须压掉；除此之外的任何一点差异都是真的内容差异。
    """
    return " ".join((text or "").split())


def digest_of(text: str) -> str:
    """整块文本的指纹。**对整块算，不对 snippet 算。**

    snippet 是截断过的（160 字 + 省略号），拿它算指纹的话，
    块尾被改掉不会被发现 —— 而"内容变了却判成没变"正好产出假出处。
    """
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def same_content(*, snippet: str, chunk_text: str, digest: str = "") -> bool:
    """这条出处，还指着同一段原文吗？

    `digest` 非空 -> 走严格路：指纹一致才算数。
    `digest` 为空 -> 走宽松路：当年只存了 snippet，用**包含**判断
                     （snippet 截断过，不能用相等）。

    snippet 也为空时返回 True：0003 之前的老记录连 snippet 都没有，
    **无从判断就不冤枉它** —— 这是既有行为，切换到新表时必须原样保留，
    否则一批老回答会突然集体显示"出处已失效"。
    """
    if digest:
        return digest == digest_of(chunk_text)
    want = normalize(snippet).rstrip("…")
    if not want:
        return True
    return want in normalize(chunk_text)
