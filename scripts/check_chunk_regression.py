"""分块回归：合并后的唯一实现，产出必须与**搬家前记录下来的**逐条相同。

    python scripts/check_chunk_regression.py     # 退出码 0 = 行为零变化

**阶段 2 搬 search/indexing/qa/extraction 时照样要跑这个。**
基线夹具 `tests/fixtures/chunk-regression-baseline.json` 是 2026-08-26
搬家前从两侧各录一份的产出，**不要因为"现在输出变了"就去刷新它** ——
那等于把回归改成同义反复。真要改基线，先说清为什么那次行为变化是对的。

搬家前两侧各有一份实现，`tests/fixtures/chunk-regression-baseline.json` 同时记下了两边的产出。
合并之后只剩一份，于是这里改成**拿今天的产出去对当年的两份记录**：

  - 对 Web 那份记录：必须**逐字段完全相同**（Web 的 9 键超集就是被选中的那份）
  - 对 service 那份记录：**前 5 个键必须完全相同**（service 只读这 5 个，
    多出来的四个是本轮有意新增的，见 ddp_core/chunking.py 的合并说明）

这就是「行为零变化」在这一步的可机械判定形式 —— **但它有盲区，别当成全覆盖**：

`RECORDED` 只有 5 个字段，因为搬家前那次记录只抓了这些（当年没记 `page_size`
与 `char_len`）。所以「拿今天对当年」这一半漏掉那两列；
不过「两侧互相对比」那一半是**全字段**的（只豁免 `text_tokenized`），
`page_size` / `char_len` 一旦在某一侧漂掉，那半边会红。
真正只靠单测兜着的，是"两侧同时漂成一样的错值"这种情形。
"""
import json
import os
import pathlib
import subprocess
import sys

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
BEFORE = json.loads((FIXTURES / "chunk-regression-baseline.json").read_text())
LAYOUT = json.loads((FIXTURES / "chunk-regression-layout.json").read_text())

SNIP = """
import json, sys
sys.path.insert(0, "{pkg}")
from ddp_core.chunking import layout_to_chunks
layout = json.load(sys.stdin)
print(json.dumps(layout_to_chunks(layout), ensure_ascii=False, sort_keys=True))
"""

# 工作区根：两个仓库的共同父目录。**不能写死本机路径** —— 这两把尺子
# plan.md 说了阶段 2 还要用，而它们要在 CI、GPU 机器、别人的机器上都能跑。
# 顺序：环境变量 > 从本文件位置上推两级（scripts/ -> DeepDocParse/ -> 工作区根）
WORKSPACE = pathlib.Path(
    os.environ.get("DDP_WORKSPACE") or pathlib.Path(__file__).resolve().parents[2]
)
SERVICE = str(WORKSPACE / "DeepDocParse")
WEB = str(WORKSPACE / "DeepDocParse-Web")

for _p in (SERVICE, WEB):
    if not pathlib.Path(_p).is_dir():
        raise SystemExit(
            f"找不到 {_p}。两个仓库必须是同级目录；"
            f"不是的话用 DDP_WORKSPACE=<共同父目录> 指过来")


# BEFORE 记录只抓了这几个字段（当年的探针没记 page_size），所以只能比这些。
# 它们正好覆盖出处定位的全部要素：哪一页、哪个框、什么类型、什么文字。
RECORDED = ("text", "page_idx", "bbox", "block_type", "table_html")

# 允许两侧不同的字段，**且只有这一个**。
# `text_tokenized` 取决于环境里有没有 jieba（gateway 没装 -> bigram，
# Web 装了 -> jieba），而这一列只有产品层的持久索引在用，gateway 不读。
# 见 ddp_core/chunking.py 的合并说明。
ENV_DEPENDENT = {"text_tokenized"}


def run(cwd, python, pkg):
    out = subprocess.run([python, "-c", SNIP.format(pkg=pkg)], cwd=cwd,
                         input=json.dumps(LAYOUT), capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        raise SystemExit(f"探针在 {cwd} 跑失败")
    return json.loads(out.stdout.strip().splitlines()[-1])


now_svc = run(SERVICE, f"{SERVICE}/.venv/bin/python", "gateway")
now_web = run(WEB, f"{WEB}/.venv/bin/python", "backend")

problems = []

# 1) 两侧现在必须产出一样的东西（同一份实现、同一份输入），
#    **除了明确允许的那一个环境相关字段**
if len(now_svc) != len(now_web):
    problems.append(f"两侧块数不同：{len(now_svc)} vs {len(now_web)}")
else:
    for i, (a, b) in enumerate(zip(now_svc, now_web)):
        for k in a:
            if k in ENV_DEPENDENT:
                continue
            if a[k] != b.get(k):
                problems.append(f"两侧第 {i} 块的 {k} 不同：{a[k]!r} vs {b.get(k)!r}")
    # 反过来钉住：允许清单里的字段**确实**只有那一个在飘
    drifting = {k for a, b in zip(now_svc, now_web) for k in a if a[k] != b.get(k)}
    if drifting - ENV_DEPENDENT:
        problems.append(f"出现了未预期的环境相关差异：{drifting - ENV_DEPENDENT}")

# 2) 对 Web 的历史记录：逐字段相同
web_before = BEFORE["web"]
if len(now_web) != len(web_before):
    problems.append(f"块数变了：{len(web_before)} -> {len(now_web)}")
else:
    for i, (a, b) in enumerate(zip(web_before, now_web)):
        for k in RECORDED + ("seq",):
            if a.get(k) != b.get(k):
                problems.append(f"第 {i} 块的 {k} 变了：{a.get(k)!r} -> {b.get(k)!r}")

# 3) 对 service 的历史记录：前 5 个键相同（seq 当年是 None，不比）
svc_before = BEFORE["service"]
if len(now_svc) != len(svc_before):
    problems.append(f"service 块数变了：{len(svc_before)} -> {len(now_svc)}")
else:
    for i, (a, b) in enumerate(zip(svc_before, now_svc)):
        for k in RECORDED:
            if a.get(k) != b.get(k):
                problems.append(f"service 第 {i} 块的 {k} 变了：{a.get(k)!r} -> {b.get(k)!r}")

print(json.dumps({"blocks": len(now_web),
                  "keys": sorted(now_web[0].keys()) if now_web else []},
                 ensure_ascii=False, indent=1))
if problems:
    print("\n".join(problems), file=sys.stderr)
    raise SystemExit(1)
print("行为零变化：合并后的产出与搬家前逐条一致 ✓", file=sys.stderr)
