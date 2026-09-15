#!/usr/bin/env python
"""验收台账守卫 —— `docs/refactor/ACCEPTANCE-MATRIX-v3.md` 与仓库对得上。

    python scripts/check_acceptance_matrix.py            # 校验（门禁用）
    python scripts/check_acceptance_matrix.py --write    # 按逐行状态重写汇总表

## 它守的是什么

台账是"对照计划 §13 逐条记证据"的地方。它一旦和仓库漂开，就会变成一份
看起来很权威、实际上指向不存在的测试的文档 —— 这比没有台账更糟：后人会据它
以为某条判据已经被钉住。所以这几件事必须机械校对：

1. **T01–T88 一个不少、不重复。** 漏一行就等于悄悄删掉一条验收判据。
2. **汇总表等于逐行状态的计数。** 手改汇总表而不改行（或反过来）会让
   "完成多少"这个数字失真。
3. **每个 ✅ 行的证据栏至少有一个会被校验的测试引用。** 台账口径是"没有测试名
   只有文档的最多 🟡"；这条不机器执行，把一行 ✅ 的证据删成散文照样绿。只数
   证据栏：写在缺口栏的测试名是"只测了一半"的说明，不算证据。
4. **引用的测试真实存在，而且指得准。**
   - `文件.py::test_x` / `文件_test.go::TestX`：文件必须唯一定位（同名文件在
     两个包里各有一份时要写上区分用的目录），用例必须在**那个文件**里；
   - `test_x*` / `` `test_x…` ``：前缀引用，至少命中一个用例，且前缀本身要比
     `test_` 多出至少 3 个字符（`test_*` 什么都没指）；
   - 裸 `` `test_x` `` 与 Go `TestX`：仓库里要有这个用例；
   - 「JS/Playwright 用例标题」：必须**恰好等于**某个会真跑的 `test(...)`/`it(...)`
     的标题（skip/todo/fixme 不算）；结尾 `…`（或 `...`）表示前缀，前缀至少 8 个字符
     且**恰好**命中一个标题；
   - 只写文件名的引用（`xxx.md`、`artifacts/*.json`、`xxx_test.go`）：文件存在；
   - 无条件跳过的用例（`@pytest.mark.skip`、模块级 `pytestmark = pytest.mark.skip`、
     node:test 的 `{ skip: true }`、`test.skip`/`it.todo`）不算定义过。运行时才决定的
     `skipif` 与 `describe.skip(...)` 里的内层用例判不了，靠验收。
5. **✅ 行的缺口栏只能是 `—` 或以"（判据外）"开头。** 缺口栏自己承认缺了判据的
   一半而状态是 ✅，是自相矛盾（形式约定；语义仍靠验收）。

它**不**判断状态是否正确 —— 那要读判据和测试，是提交验收的事。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/refactor/ACCEPTANCE-MATRIX-v3.md"
STATUSES = {"✅": "✅ 已验证", "🟡": "🟡 部分", "🔴": "🔴 未验证", "⛔": "⛔ 需外部条件"}
ROW = re.compile(r"^\|\s*(T\d{2})\b[^|]*\|\s*[^|]*\|\s*(✅|🟡|🔴|⛔)\s*\|(.*)$")
#: `路径/文件.ext::用例`（py 与 go）；用例名结尾 `*` 表示前缀。
FILE_TEST_REF = re.compile(
    r"((?:[\w.-]+/)*[\w.-]+\.(?:py|go))::((?:test_|Test)[A-Za-z0-9_]*\*?)")
#: 反引号里的裸 Python 用例；结尾 `…` 表示前缀。
BARE_PY_TEST = re.compile(r"`(test_[A-Za-z0-9_]*)(…|\.\.\.)?`")
#: 省略号：`…` 与 ASCII `...` 一视同仁（后者以前会被静默跳过）。
ELLIPSIS = re.compile(r"(…|\.\.\.)$")
GO_REF = re.compile(r"(?<![\w:])(Test[A-Z][A-Za-z0-9_]+)\b")
#: 反引号里的文件引用（可带 `::用例`，也可带 `*` 通配）。
FILE_REF = re.compile(r"`((?:[\w.*-]+/)*[\w.*-]+\.(?:py|go|mjs|ts|md|json|yaml|yml|sh))(?:::[^`]*)?`")
JS_REF = re.compile(r"「([^」]+)」")
#: 只认会真跑的用例：`test.skip(` / `it.todo(` / `test.fixme(` 的标题不是证据。
JS_TITLE = re.compile(r"\b(?:test|it)(?:\.(?:only|serial))?\(\s*(['\"`])(.+?)(?<!\\)\1")
JS_SKIP_OPTION = re.compile(r"\s*,\s*\{[^}]*\b(?:skip|todo)\s*:\s*(?:true|['\"`])")
COUNTS = re.compile(r"(<!-- counts:begin -->\n)(.*?)(\n<!-- counts:end -->)", re.S)
MIN_PREFIX_TAIL = 3
#: JS 标题前缀至少这么多字符：一两个字的前缀即使碰巧只命中一个标题，也没指明是哪条判据的证据。
MIN_TITLE_PREFIX = 8


def fail(message: str) -> None:
    print(f"::error::验收台账: {message}")
    sys.exit(1)


def tracked(*patterns: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.split()
    return out


def counts_table(counts: dict[str, int]) -> str:
    lines = ["| 状态 | 条数 |", "|---|---|"]
    lines += [f"| {label} | {counts[key]} |" for key, label in STATUSES.items()]
    lines.append(f"| **合计** | **{sum(counts.values())}** |")
    return "\n".join(lines)


def locate(ref: str, files: list[str]) -> list[str]:
    """按路径后缀定位跟踪文件；`*` 通配只用于文件存在性检查。"""
    if "*" in ref:
        pattern = re.compile(r"(^|.*/)" + re.escape(ref).replace(r"\*", r"[^/]*") + r"$")
        return [path for path in files if pattern.match(path)]
    return [path for path in files if path == ref or path.endswith("/" + ref)]


#: 无条件跳过的 Python 用例不是证据（`skipif` 要到运行时才知道，不在这里判）。
PY_UNCONDITIONAL_SKIP = re.compile(r"^\s*@pytest\.mark\.skip\b(?!if)")
PY_MODULE_SKIP = re.compile(r"^pytestmark\s*=\s*.*\bpytest\.mark\.skip\b(?!if)", re.M)


def defines(source: str, name: str, *, go: bool) -> bool:
    """文件里定义了这个（会真跑的）用例。前缀引用任一命中即可。"""
    prefix = name.endswith("*")
    stem = re.escape(name.rstrip("*"))
    head = r"^func " if go else r"^\s*(async )?def "
    tail = r"\w*\(" if prefix else r"\("
    if go:
        return re.search(head + stem + tail, source, re.M) is not None
    if PY_MODULE_SKIP.search(source):
        return False
    lines = source.splitlines()
    pattern = re.compile(head + stem + tail)
    for index, line in enumerate(lines):
        if not pattern.match(line):
            continue
        cursor = index - 1
        skipped = False
        # 装饰器（含多行 parametrize 的续行）一直连到 def；向上扫到空行或上一个
        # def/class 为止 —— 不越过上一个函数，免得把它的 skip 算到这里。
        while cursor >= 0 and lines[cursor].strip() and not re.match(
                r"\s*(async\s+def|def|class)\s", lines[cursor]):
            if PY_UNCONDITIONAL_SKIP.match(lines[cursor]):
                skipped = True
            cursor -= 1
        if not skipped:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="按逐行状态重写汇总表")
    args = parser.parse_args()
    text = MATRIX.read_text(encoding="utf-8")

    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        match = ROW.match(line)
        if not match:
            continue
        test_id, status, rest = match.groups()
        if test_id in rows:
            fail(f"{test_id} 出现了两次")
        rows[test_id] = (status, rest)
    expected = {f"T{number:02d}" for number in range(1, 89)}
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        fail(f"T01–T88 不完整：缺 {missing}，多 {extra}")

    counts = {key: 0 for key in STATUSES}
    for status, _ in rows.values():
        counts[status] += 1
    table = counts_table(counts)
    block = COUNTS.search(text)
    if block is None:
        fail("找不到 <!-- counts:begin --> / <!-- counts:end --> 汇总表标记")
    if args.write:
        MATRIX.write_text(text[:block.start(2)] + table + text[block.end(2):], encoding="utf-8")
        print(f"已按逐行状态重写汇总表：{counts}")
        return
    if block.group(2).strip() != table:
        fail(f"汇总表与逐行状态不一致（逐行计数 {counts}），跑 --write 重写")

    files = tracked()
    source_cache: dict[str, str] = {}

    def source(path: str) -> str:
        if path not in source_cache:
            source_cache[path] = (ROOT / path).read_text(encoding="utf-8", errors="ignore")
        return source_cache[path]

    py_files = [path for path in files if path.endswith(".py")]
    go_files = [path for path in files if path.endswith("_test.go")]
    js_files = [path for path in files if path.endswith((".test.mjs", ".spec.ts", ".test.ts"))]
    # node:test 的 `test(title, { skip: true }, …)` 同样不跑，不算证据。
    js_titles = [match.group(2) for path in js_files for match in JS_TITLE.finditer(source(path))
                 if not JS_SKIP_OPTION.match(source(path), match.end())]

    problems: list[str] = []

    def short_prefix(name: str) -> bool:
        stem = ELLIPSIS.sub("", name.rstrip("*"))
        head = "test_" if stem.startswith("test_") else "Test"
        return len(stem) - len(head) < MIN_PREFIX_TAIL

    def check_refs(test_id: str, text: str) -> int:
        """校验一段文字里的全部引用，返回其中成立的测试引用个数。"""
        verified = 0
        for path_ref in FILE_REF.findall(text):
            candidates = locate(path_ref, files)
            if not candidates:
                problems.append(f"{test_id}: 找不到文件 {path_ref}")
            elif len(candidates) > 1 and "*" not in path_ref:
                problems.append(f"{test_id}: {path_ref} 有歧义（{', '.join(candidates)}），"
                                "写上区分用的目录")
        for path_ref, name in FILE_TEST_REF.findall(text):
            go = path_ref.endswith(".go")
            candidates = locate(path_ref, files)
            # 不带反引号的 `路径::用例` 不经过上面的文件检查，缺失/歧义在这里也要报
            # （重复的报错在最后去重）。
            if not candidates:
                problems.append(f"{test_id}: 找不到文件 {path_ref}")
                continue
            if len(candidates) > 1:
                problems.append(f"{test_id}: {path_ref} 有歧义（{', '.join(candidates)}），"
                                "写上区分用的目录")
                continue
            if name.endswith("*") and short_prefix(name):
                problems.append(f"{test_id}: 前缀 {name} 太宽，什么都没指")
                continue
            if defines(source(candidates[0]), name, go=go):
                verified += 1
            else:
                problems.append(f"{test_id}: {candidates[0]} 里没有 {name}")
        # 裸引用的正则只认紧跟反引号的 `test_`，`文件::test_x` 不会被当成裸引用；
        # 不要用 `"::" + name in text` 去跳过 —— 那是子串判断，`test_` 会被任何
        # `::test_xxx` 顶掉（变异确认过）。
        for name, ellipsis in BARE_PY_TEST.findall(text):
            prefix = name + "*" if ellipsis else name
            if ellipsis and short_prefix(prefix):
                problems.append(f"{test_id}: 前缀 {name}{ellipsis} 太宽，什么都没指")
                continue
            if any(defines(source(path), prefix, go=False) for path in py_files):
                verified += 1
            else:
                problems.append(f"{test_id}: 仓库里没有 Python 用例 {prefix}")
        for name in GO_REF.findall(text):   # 前瞻已排除 `文件::TestX`
            if any(defines(source(path), name, go=True) for path in go_files):
                verified += 1
            else:
                problems.append(f"{test_id}: 仓库里没有 Go 用例 {name}")
        for title in JS_REF.findall(text):
            stem = ELLIPSIS.sub("", title).rstrip()
            if stem != title:
                if len(stem) < MIN_TITLE_PREFIX:
                    problems.append(f"{test_id}: 标题前缀「{title}」太短（至少 {MIN_TITLE_PREFIX} 个字符）")
                    continue
                hits = [candidate for candidate in js_titles if candidate.startswith(stem)]
                if len(hits) != 1:
                    problems.append(f"{test_id}: 标题前缀「{title}」命中 {len(hits)} 个用例，"
                                    "必须恰好一个")
                    continue
            elif title not in js_titles:
                # 不带省略号就必须是完整标题：子串匹配会让「owner」这种词冒充证据。
                problems.append(f"{test_id}: 没有标题恰为「{title}」的 JS/Playwright 用例"
                                "（只写前缀要以 … 结尾）")
                continue
            verified += 1
        return verified

    for test_id, (status, rest) in sorted(rows.items()):
        # 列：证据 | 缺口。**只数证据栏里的引用**：写在缺口栏的测试名是"它只测了一半"
        # 的说明，不是这一行的证据（第六次验收的变异：证据栏写散文、缺口栏写测试名，
        # 整行一起扫就会让 ✅ 蒙混过关）。缺口栏的引用照样校验存在性。
        columns = rest.split("|")
        evidence_column = columns[0]
        gap_column = "|".join(columns[1:])
        verified = check_refs(test_id, evidence_column)
        check_refs(test_id, gap_column)
        if status == "✅" and verified == 0:
            problems.append(f"{test_id}: 标 ✅ 却没有任何可校验的测试引用（只有文档的最多 🟡）")
        gap = gap_column.strip().rstrip("|").strip()
        if status == "✅" and gap != "—" and not gap.startswith("（判据外）"):
            # ✅ 的定义是"判据里每个动作都被覆盖"；缺口栏还在写缺什么就不是 ✅。
            problems.append(f"{test_id}: 标 ✅ 但缺口栏不是 — 也没标（判据外）：{gap[:40]}")
    problems = list(dict.fromkeys(problems))
    if problems:
        fail("引用不成立：\n  " + "\n  ".join(problems))
    print(f"验收台账 OK：88 条（{', '.join(f'{STATUSES[k]} {v}' for k, v in counts.items())}），"
          "每个 ✅ 行都有可校验的测试引用，引用的文件与用例全部存在且指得准")


if __name__ == "__main__":
    main()
