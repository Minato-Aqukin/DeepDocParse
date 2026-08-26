"""块类型判据对拍：两侧对同一输入必须给出同一个块类型。

    python scripts/check_blocktype_parity.py     # 退出码 0 = 两侧一致

**它比"两侧 import 同一个模块所以恒等"更有用。** 比的是两条真实路径：
service 走 `app.services.layout.normalize_type`（normalizer 层），
Web 走 `ddp_core.blocks.normalize_type`（分块层）。
阶段 1 里 `layout.py` 已改成从 `ddp_core.blocks` 再导出，所以现在理应恒等 ——
**而这正是它要盯的事**：一旦有人在任何一侧重新实现了一份块类型判据
（历史上真发生过，`block_text` 那个循环被抄过四遍），这里立刻会红。

阶段 2 往 core 里搬 search/indexing 时继续用它。

plan.md §7 阶段 1 要求「合并前先跑一遍两侧的块类型判据对拍（20 个取值），
合并后再跑一遍，结果必须一致」。这份脚本就是那把尺子 —— 搬家前后各跑一次。

判据不一致的后果写在两侧注释里：同一份版面会切出不同的块，
而出处的稳定定位键 seq 按块序算 —— **历史出处会指到错误的块**。
"""
import json
import os
import pathlib
import subprocess
import sys

# 20 个取值：覆盖两种"不认识"、大小写、空白、None、非字符串、以及各映射分支
CASES = [
    None, "", "   ", "text", "TEXT", " Text ", "plain text", "paragraph",
    "title", "header", "sub_title", "table", "table_body", "table_caption",
    "image", "image_caption", "interline_equation", "isolate_formula",
    "list", "index", "other", "Table", "未知类型", 123, True,
]

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


SERVICE_SNIP = """
import json, sys
sys.path.insert(0, "gateway")
from app.services.layout import normalize_type
cases = json.load(sys.stdin)
print(json.dumps([normalize_type(c) for c in cases]))
"""

WEB_SNIP = """
import json, sys
sys.path.insert(0, "backend")
from ddp_core.blocks import normalize_type
cases = json.load(sys.stdin)
print(json.dumps([normalize_type(c) for c in cases]))
"""


def run(cwd: str, python: str, snippet: str) -> list:
    out = subprocess.run([python, "-c", snippet], cwd=cwd, input=json.dumps(CASES),
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        raise SystemExit(f"探针在 {cwd} 跑失败")
    return json.loads(out.stdout.strip().splitlines()[-1])


svc = run(SERVICE, f"{SERVICE}/.venv/bin/python", SERVICE_SNIP)
web = run(WEB, f"{WEB}/.venv/bin/python", WEB_SNIP)

rows, bad = [], 0
for case, s, w in zip(CASES, svc, web):
    same = s == w
    bad += 0 if same else 1
    rows.append({"input": case, "service": s, "web": w, "same": same})

print(json.dumps(rows, ensure_ascii=False, indent=1))
print(f"\n共 {len(CASES)} 个取值，不一致 {bad} 个", file=sys.stderr)
raise SystemExit(1 if bad else 0)
