#!/usr/bin/env python
"""动作引用与门禁 pip 工具的 pin 守卫。

    python scripts/check_action_pins.py

`uses: actions/checkout@v5` 这种浮动 tag 引用会在上游重打 tag 时静默改变
CI 行为 —— 发版链（desktop-windows → release）尤其承受不起。所以所有
第三方动作必须按完整 commit SHA 引用，tag 只许出现在行尾注释里备查。
门禁 pip 工具（jsonschema / ruff / httpx）同理必须 `==` 锁死版本，
否则某天上游发新版，绿了半年的 CI 会在无人改代码的情况下变红。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# `uses: foo/bar@<40 位小写 hex>  # vN` —— 注释里的 tag 是给人看的，
# 给机器看的只有 SHA。
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>\S+)\s*(?:#\s*(?P<comment>v\d+\S*)\s*)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# 只查门禁工具：测试依赖（pytest 等）不在此列。
PINNED_TOOLS = ("jsonschema", "ruff", "httpx")


def check_uses(path: pathlib.Path, problems: list) -> int:
    count = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if "uses:" not in stripped:
            continue
        match = USES_RE.match(raw)
        if match is None:
            continue  # 不是动作引用行（如注释里提到 uses:）
        count += 1
        ref = match.group("ref")
        if "@" not in ref:
            problems.append(f"{path.name}:{lineno}: uses 缺少 @ 引用：{stripped}")
            continue
        _repo, _at, pinned = ref.partition("@")
        if not SHA_RE.fullmatch(pinned):
            problems.append(
                f"{path.name}:{lineno}: 动作未按 SHA pin（应为 @<40 位 hex>  # vN）：{stripped}"
            )
            continue
        if not match.group("comment"):
            problems.append(f"{path.name}:{lineno}: 缺少 `# vN` 注释（SHA 对应哪个 tag 看不出来）：{stripped}")
    return count


def check_pip(path: pathlib.Path, problems: list) -> None:
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if "pip install" not in stripped or stripped.startswith("#"):
            continue
        for tool in PINNED_TOOLS:
            # `pip install httpx` / `pip install "httpx==x"` / 同一行多个包都要看。
            if re.search(rf"(?<![\w\-.]){tool}(?![\w\-.])", stripped) and "==" not in stripped:
                problems.append(
                    f"{path.name}:{lineno}: {tool} 未 pin 版本（应为 \"{tool}==X.Y.Z\"）：{stripped}"
                )


def main() -> int:
    files = sorted(WORKFLOWS.glob("*.yml"))
    if not files:
        print(f"::error::{WORKFLOWS} 下一个 workflow 都没有 —— 扫描路径坏了")
        return 1
    problems: list = []
    total_uses = 0
    for path in files:
        total_uses += check_uses(path, problems)
        check_pip(path, problems)
    # 反哨兵：uses 行数为零说明解析坏了，此时通过等于没查。
    if total_uses == 0:
        print("::error::一个 uses 都没扫到 —— 解析坏了，拒绝通过")
        return 1
    for line in problems:
        print(f"::error::{line}")
    if problems:
        return 1
    print(f"动作引用 pin 检查通过：{len(files)} 个 workflow、{total_uses} 个动作引用全是 SHA pin，门禁 pip 工具全已锁版本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
