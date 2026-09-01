#!/usr/bin/env python
"""块类型判据守卫：全仓只准有一份实现，且它的映射表不许悄悄变。

    python scripts/check_blocktype_parity.py     # 退出码 0 = 通过

## 它守的是什么

块类型归一化决定了同一份版面被切成哪些块，而出处的稳定定位键 `seq`
**按块序算** —— 判据变一点点，历史出处就指到错误的块。这是这个项目
定义的最恶劣错误的一种：不报错、界面照常显示"已验证"、指的却是别处。

`block_text` 那个循环历史上被抄过四遍。合仓前这把尺子的做法是
**跨仓库对拍**：起两个子进程，分别在两个仓库里算同一批取值再比结果。
合仓之后跨仓对拍已经没有意义（只剩一个仓库），但要守的事一件没少，
只是判据换成了更强的两条：

1. **恒等**：所有对外暴露 `normalize_type` 的地方必须是**同一个函数对象**
   （`is`，不是 `==`）。有人重新实现一份、行为暂时也对，这条照样红 ——
   而"行为暂时也对"正是四次抄写每一次的起点。
2. **冻结映射**：25 个取值的归一化结果与基线逐条比对。
   改判据是允许的，但必须**同时**更新基线，让它出现在 diff 里被人看见。
"""
import json
import sys

# 25 个取值：覆盖两种"不认识"、大小写、空白、None、非字符串、以及各映射分支
CASES = [
    None, "", "   ", "text", "TEXT", " Text ", "plain text", "paragraph",
    "title", "header", "sub_title", "table", "table_body", "table_caption",
    "image", "image_caption", "interline_equation", "isolate_formula",
    "list", "index", "other", "Table", "未知类型", 123, True,
]

# 冻结基线。**改这张表 = 改出处的分块边界**，必须与迁移/回填方案一起想清楚：
# 老文档的 citations 是按旧判据切出来的块序存的。
EXPECTED = {
    'None': 'text', '': 'text', '   ': 'text',
    'text': 'text', 'TEXT': 'text', ' Text ': 'text',
    'plain text': 'text', 'paragraph': 'text', 'title': 'title',
    'header': 'title', 'sub_title': 'title', 'table': 'table',
    'table_body': 'table', 'table_caption': 'table', 'image': 'figure',
    'image_caption': 'figure', 'interline_equation': 'equation', 'isolate_formula': 'equation',
    'list': 'list', 'index': 'list', 'other': 'other',
    'Table': 'table', '未知类型': 'other', '123': 'other',
    'True': 'other',
}

#: 每一个把 normalize_type 摆到自己命名空间里的模块。加一处就往这里加一行 ——
#: 漏加的表现是那一处可以偷偷换实现而守卫不知道。
REEXPORTS = [
    ("ddp_core.blocks", "normalize_type"),                   # 规范实现
    ("ddp_gateway.services.layout", "normalize_type"),       # 网关的归一化层
]


def _canonical():
    from ddp_core.blocks import normalize_type

    return normalize_type


def check_single_implementation() -> list[str]:
    """条件 1：所有再导出点都必须是同一个函数对象。"""
    canonical = _canonical()
    problems = []
    for module_name, attr in REEXPORTS:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError as exc:
            problems.append(f"{module_name} 装不上：{exc}")
            continue
        fn = getattr(module, attr, None)
        if fn is None:
            problems.append(f"{module_name}.{attr} 不见了 —— 再导出被删了？")
        elif fn is not canonical:
            problems.append(
                f"{module_name}.{attr} 不是 ddp_core.blocks.normalize_type 本尊 —— "
                f"有人重新实现了一份块类型判据")
    return problems


def check_frozen_mapping() -> list[str]:
    """条件 2：25 个取值的归一化结果与冻结基线一致。"""
    canonical = _canonical()
    problems = []
    for case in CASES:
        key = str(case)
        got = canonical(case)
        want = EXPECTED[key]
        if got != want:
            problems.append(f"normalize_type({case!r}) = {got!r}，基线是 {want!r}")
    return problems


def main() -> int:
    problems = check_single_implementation() + check_frozen_mapping()
    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n块类型判据变了。想清楚再改：同一份版面会切出不同的块，"
              "而出处的定位键 seq 按块序算 —— 历史出处会指到错误的块。",
              file=sys.stderr)
        return 1
    canonical = _canonical()
    print(json.dumps([{"input": c, "type": canonical(c)} for c in CASES],
                     ensure_ascii=False, indent=1))
    print(f"\n块类型守卫通过：{len(REEXPORTS)} 个再导出点恒等，"
          f"{len(CASES)} 个取值与基线一致", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
