#!/usr/bin/env python
"""分块回归守卫：分块规则变了就红。

    python scripts/check_chunk_regression.py     # 退出码 0 = 通过

## 它守的是什么

出处的稳定定位键 `seq` **按块序算**。分块规则一变，同一份版面切出的块
就换了编号 —— 历史 citations 会指到错误的块。而这件事**不报错**：
界面照常显示"已验证"，指的却是别处。

合仓前这把尺子跨两个仓库对拍（两侧各起一个子进程算同一份版面再比结果），
因为当年 `layout_to_chunks` 有两份复制品。合仓后只剩一份，跨仓对拍没有意义，
但**对冻结基线的回归**一件没少。

⚠️ **这把尺子从 2026-08-28 起一直是红的，没人看见** —— 那天
`eae7c3d` 有意让没有 caption 的 figure 也产出原子（VLM 理解需要它），
块数 9 -> 10，而基线没跟着更新，且这个脚本**当时不在任何 CI 里**。
合仓时补记了基线（`chunk-regression-baseline.json` 的 `history` 字段有全过程），
并把它挂进 CI。守卫不进 CI 等于没有守卫。

允许改分块规则，但必须：
1. 明确知道老文档的 citations 会失效（`attach_resolution` 比对内容，对不上标失效）；
2. 同一次改动里更新基线，让它出现在 diff 里被人看见。
"""
import json
import pathlib
import sys

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
BEFORE = json.loads((FIXTURES / "chunk-regression-baseline.json").read_text())
LAYOUT = json.loads((FIXTURES / "chunk-regression-layout.json").read_text())

# 基线抓的字段。它们正好覆盖出处定位的全部要素：
# 第几块（seq）、哪一页、哪个框、什么类型、什么文字。
RECORDED = ("seq", "text", "page_idx", "bbox", "block_type", "table_html")

# `text_tokenized` **不比**：它取决于环境里有没有 jieba，而基线是在
# 装了 jieba 的机器上记的。换 tokenizer 会静默毁掉关键词路，
# 但那件事由 `ddp_core.tokenize.backend()` 进 model_meta 来守，不在这里。
ENV_DEPENDENT = {"text_tokenized"}


def current_chunks() -> list[dict]:
    from ddp_core.chunking import layout_to_chunks

    return layout_to_chunks(LAYOUT)


def main() -> int:
    now = current_chunks()
    before = BEFORE["chunks"]
    problems: list[str] = []

    if len(now) != len(before):
        problems.append(f"块数变了：{len(before)} -> {len(now)}")
    else:
        for i, (a, b) in enumerate(zip(before, now)):
            for k in RECORDED:
                if a.get(k) != b.get(k):
                    problems.append(f"第 {i} 块的 {k} 变了：{a.get(k)!r} -> {b.get(k)!r}")

    # 反哨兵：基线是空的 / 夹具读错了，上面会一片绿
    if not now:
        problems.append("当前分块结果是空的 —— 夹具或分块实现坏了")
    if not before:
        problems.append("基线是空的，守卫等于没有")

    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n分块规则变了。老文档的 citations 会失效 —— 想清楚再改，"
              "并在同一次改动里更新 tests/fixtures/chunk-regression-baseline.json。",
              file=sys.stderr)
        return 1

    print(json.dumps({"blocks": len(now),
                      "compared_fields": list(RECORDED) + ["seq"],
                      "ignored_fields": sorted(ENV_DEPENDENT)},
                     ensure_ascii=False, indent=1))
    print(f"\n分块回归守卫通过：{len(now)} 块与两份历史基线逐字段一致", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
