#!/usr/bin/env bash
# 「基于大语言模型」那三条线的真机 e2e。**这是本仓库第一次真的在 GPU 上跑通它们。**
#
#   bash deploy/autodl/e2e-llm.bash
#
# 前置：bootstrap.bash 装完、ocr.bash / chat.bash 起好、verify.bash 全绿。
#
# 它验的是 mock 单测**验不了**的那部分：单测能钉住"我们发的请求形状对不对"、
# "拿到 grounding 标签后解析得对不对"，但钉不住"真模型在这个请求形状下到底
# 会不会吐标签、吐出来的框指得准不准"。所以这里全部对着真模型跑：
#
#   1. /v1/parse engine=vlm-ocr  -> DDP-Layout，**断言 bbox 不是清一色 null**
#   2. layout_json 里不许有 engine_notes（有就说明 grounding 静默失效了）
#   3. /v1/extract               -> 字段值 + 出处，断言不是假的 not_found
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=env.bash
source "$HERE/env.bash"

PY="$VENV_DIR/bin/python"
TOKEN="${SERVICE_TOKEN:-e2e-local-token-not-a-placeholder}"
GATEWAY_PORT="${GATEWAY_PORT:-9000}"
FIXTURE_PORT="${FIXTURE_PORT:-18081}"
fails=0

pass()    { printf '\033[32m  PASS\033[0m %s\n' "$*"; }
fail()    { printf '\033[31m  FAIL\033[0m %s\n' "$*"; fails=$((fails + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

cleanup() {
  for f in "$LOG_DIR"/gateway.pid "$LOG_DIR"/worker.pid "$LOG_DIR"/fixtures.pid; do
    [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
  done
}
trap cleanup EXIT

# ---------------------------------------------------------------- 0. 依赖
section "0. 起依赖（redis / gateway / worker / fixtures）"

# Redis：任务状态与结果暂存。focal 自带的是 5.0.7，**没有 RediSearch** ——
# 解析与抽取用不到它，向量检索才要（这套部署本来也没注册 embedding，见 models.autodl.yaml）
if ! redis-cli ping >/dev/null 2>&1; then
  # **`apt-get update` 不能省**：AutoDL 基础镜像里的 apt 索引是空的，
  # 直接 install 会静默失败（装完 redis-cli 还是 command not found）
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq redis-server >/dev/null 2>&1
  redis-server --daemonize yes --port 6379 --save '' >/dev/null 2>&1
  sleep 2
fi
redis-cli ping >/dev/null 2>&1 && pass "redis" || { fail "redis 起不来"; exit 1; }

# gateway 的依赖装进同一个 venv（vLLM 已经在里面，省一份 torch）
if ! "$PY" -c "import app.main" 2>/dev/null; then
  ( cd "$REPO/gateway" && uv pip install --python "$PY" -q --index-url "$PIP_INDEX_URL" -e '.[dev]' )
fi

export SERVICE_TOKEN="$TOKEN"
export REDIS_URL="redis://localhost:6379/0"
export MODELS_CONFIG="$REPO/models.autodl.yaml"
mkdir -p "$LOG_DIR"

# fixtures：把 PDF 用 HTTP 供出来，gateway 按 file_url 去下
( cd "$REPO/tests/fixtures" && nohup "$PY" -m http.server "$FIXTURE_PORT" --bind 127.0.0.1 \
    >"$LOG_DIR/fixtures.log" 2>&1 & echo $! > "$LOG_DIR/fixtures.pid" )

( cd "$REPO/gateway" && nohup "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$GATEWAY_PORT" \
    >"$LOG_DIR/gateway.log" 2>&1 & echo $! > "$LOG_DIR/gateway.pid" )
( cd "$REPO/gateway" && nohup "$VENV_DIR/bin/arq" app.worker.tasks.WorkerSettings \
    >"$LOG_DIR/worker.log" 2>&1 & echo $! > "$LOG_DIR/worker.pid" )

for _ in $(seq 1 40); do
  curl -sf --max-time 3 "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null && break
  sleep 2
done
if curl -sf --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null; then
  pass "gateway :$GATEWAY_PORT"
else
  fail "gateway 起不来，看 $LOG_DIR/gateway.log"; tail -20 "$LOG_DIR/gateway.log"; exit 1
fi

ready=$(curl -s --max-time 20 "http://127.0.0.1:$GATEWAY_PORT/readyz" || echo '{}')
echo "    /readyz: $(printf '%s' "$ready" | head -c 300)"

# ---------------------------------------------------------------- 1~3. 主体
section "1-3. 解析 / 出处 / 抽取"
"$PY" - "$GATEWAY_PORT" "$FIXTURE_PORT" "$TOKEN" <<'PYEOF'
import json, sys, time, urllib.error, urllib.request

port, fixture_port, token = sys.argv[1], sys.argv[2], sys.argv[3]
BASE = f"http://127.0.0.1:{port}"
FILE_URL = f"http://127.0.0.1:{fixture_port}/contract.pdf"
HDR = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
fails = []


def call(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def check(ok, label, detail=""):
    print(("  PASS " if ok else "  FAIL ") + label + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(label)


# ---- 1. 解析：走 vlm-ocr + deepseek-ocr2 方言 ----
print("\n[1] POST /v1/parse engine=vlm-ocr")
status, body = call("POST", "/v1/parse", {"file_url": FILE_URL, "engine": "vlm-ocr"})
check(status == 202, "受理 202", f"实际 {status}")
task_id = body["task_id"]

deadline = time.time() + 900
state = {}
while time.time() < deadline:
    _, state = call("GET", f"/v1/parse/{task_id}")
    if state["status"] in ("succeeded", "failed"):
        break
    time.sleep(5)
check(state.get("status") == "succeeded", "解析成功",
      f"状态 {state.get('status')} error={state.get('error')}")
if state.get("status") != "succeeded":
    raise SystemExit(1 if fails else 0)

_, result = call("GET", f"/v1/parse/{task_id}/result", timeout=180)
layout = result["layout_json"]
pages = layout.get("pdf_info") or []
blocks = [b for p in pages for b in (p.get("para_blocks") or [])]
print(f"    {len(pages)} 页 / {len(blocks)} 块 / engine={layout.get('engine')}")

# ---- 2. 出处：bbox 必须是真的 ----
# **这是整轮验证的核心断言。** 请求形状错一点（skip_special_tokens 没生效、
# chat template 挂错、占位符数量不对），结果都是"解析成功但 bbox 全 null"，
# 而这条路径上没有任何一处会报错。
print("\n[2] 出处 bbox")
with_bbox = [b for b in blocks if b.get("bbox")]
check(bool(blocks), "识别出了块", f"{len(blocks)} 块")
check(len(with_bbox) > 0, "bbox 不是清一色 null",
      f"{len(with_bbox)}/{len(blocks)} 个块有 bbox")
notes = layout.get("engine_notes") or []
check(not notes, "没有 engine_notes（grounding 没有静默失效）", "; ".join(notes))

if with_bbox:
    page_sizes = {p["page_idx"]: p["page_size"] for p in pages}
    bad = []
    for p in pages:
        w, h = p["page_size"]
        for b in (p.get("para_blocks") or []):
            bb = b.get("bbox")
            if not bb:
                continue
            if not (0 <= bb[0] < bb[2] <= w + 1 and 0 <= bb[1] < bb[3] <= h + 1):
                bad.append((p["page_idx"], bb, [w, h]))
    check(not bad, "bbox 都落在页面内", f"越界 {len(bad)} 个：{bad[:3]}")
    sample = with_bbox[0]
    print(f"    样例块 type={sample['type']} bbox={sample['bbox']}")

types = sorted({b.get("type") for b in blocks})
print(f"    块类型: {types}")
check(all(t in ("text", "title", "table", "figure", "equation", "list", "other")
          for t in types), "块类型都在契约词汇表里", str(types))

md = result.get("markdown") or ""
check(len(md) > 50, "markdown 有内容", f"{len(md)} 字")
check("<|ref|>" not in md and "<|det|>" not in md, "markdown 里没有 grounding 标签残渣")
check("end▁of▁sentence" not in md, "markdown 里没有结束符残渣")

# ---- 3. 抽取 ----
print("\n[3] POST /v1/extract")
doc_hash = state.get("doc_hash")
# fixture 是英文合同（tests/fixtures/contract.truth.json 是它的标准答案）。
# description 同时是检索 query，所以写成能命中英文原文的说法。
schema = {"type": "object", "properties": {
    "buyer": {"type": "string",
              "description": "Buyer company full name（买方单位全称）"},
    "total_price": {"type": "string",
                    "description": "Total contract price（合同总价，含币种）"},
}}
# 比对前把分隔符去掉：模型很可能把 "USD 486,200.50" 规范成 "486200.50"，
# 那是**更好的抽取结果**，不该判成错。断言的是"值对不对"，不是"格式一模一样"。
EXPECTED = {"buyer": "Northwind", "total_price": "486200.50"}


def norm(v):
    return "".join(ch for ch in str(v or "") if ch.isalnum() or ch == ".")
try:
    # **开着 verify 跑**：这条路会裁出块图、让 OCR 模型原样抄一遍、与块文本比一致度。
    # 它用的 prompt 由注册表的 options.transcribe_prompt 决定 —— 拿一句中文指令
    # 去问 OCR 专用模型的话，它会回应那句指令而不是抄写，于是每条出处
    # 都被误判成 parse_mismatch。这里就是在真机上钉住那个修复。
    # **doc_hash 和 file_url 都给**：doc_hash 让它直接用已有的解析缓存（不重新解析），
    # file_url 是裁图用的 —— 只给 doc_hash 的话 _load_pdf 拿不到文件，
    # 核对会安静地跳过（crop 为空、verified=False），那条路就没验到。
    status, body = call("POST", "/v1/extract",
                        {"doc_hash": doc_hash, "file_url": FILE_URL, "schema": schema,
                         "options": {"verify": True}})
except urllib.error.HTTPError as exc:
    check(False, "抽取受理", f"HTTP {exc.code}: {exc.read()[:200]!r}")
    raise SystemExit(1)
check(status == 202, "受理 202", f"实际 {status}")
xid = body["task_id"]

deadline = time.time() + 600
xstate = {}
while time.time() < deadline:
    _, xstate = call("GET", f"/v1/extract/{xid}")
    if xstate["status"] in ("succeeded", "failed"):
        break
    time.sleep(5)
print(f"    任务状态: {xstate.get('status')}")

if xstate.get("status") == "succeeded":
    _, xr = call("GET", f"/v1/extract/{xid}/result", timeout=120)
    for name, expected in EXPECTED.items():
        field = (xr.get("fields") or {}).get(name) or {}
        print(f"    {name}: status={field.get('status')} value={field.get('value')!r} "
              f"degraded={field.get('degraded')}")
        # **最要紧的一条**：不许是 no_instruct_model —— 那说明指令模型没接上，
        # 而抽取平面在那种情况下等于没有
        check(field.get("degraded") != "no_instruct_model",
              f"{name}: 指令模型接上了（不是 no_instruct_model）")
        check(field.get("status") in ("found", "not_found"),
              f"{name}: 三态落在 found/not_found", str(field.get("status")))
        # 只断言"抽到了"是不够的 —— 抽错值同样会是 found。
        # 标准答案在 tests/fixtures/contract.truth.json
        check(field.get("status") == "found"
              and norm(expected) in norm(field.get("value")),
              f"{name}: 值正确（应含 {expected!r}，比对时忽略分隔符）",
              repr(field.get("value")))
        if field.get("status") == "found":
            cits = field.get("citations") or []
            check(bool(cits), f"{name}: 抽到值必须带出处")
            if cits:
                print(f"      出处: seq={cits[0].get('seq')} page={cits[0].get('page_idx')} "
                      f"bbox={cits[0].get('bbox')} crop={'有' if cits[0].get('crop_url') else '无'}")
            # 视觉核对：不该判成 parse_mismatch。真判了就说明抄写路径又坏了
            # （最常见的原因是 transcribe_prompt 没按模型的原生说法给）
            check(field.get("degraded") != "parse_mismatch",
                  f"{name}: 视觉核对没误报 parse_mismatch",
                  f"degraded={field.get('degraded')}")
            # 核对**真的跑了**才算数：crop_url 为空说明它安静地跳过了（多半是没给 file_url）
            check(bool(cits and cits[0].get("crop_url")),
                  f"{name}: 视觉核对真的跑了（裁出了区域图）")
            # verified 只作为信息打印，**不当断言**：它是质量指标不是链路指标。
            # vlm-ocr 的块是合并过的大块，抄写比值天然低于 borndigital 的小块，
            # 会贴着阈值上下浮动（真机实测同一个块两次跑出过 True 和 False）。
            # 链路对不对由"跑了 + 没误报 parse_mismatch"这两条钉住。
            print(f"      verified={field.get('verified')}（信息项，非断言）")
else:
    check(False, "抽取任务成功", f"状态 {xstate.get('status')} error={xstate.get('error')}")

print("\n" + ("全部通过" if not fails else f"{len(fails)} 项未通过: {fails}"))
raise SystemExit(0 if not fails else 1)
PYEOF
# shellcheck disable=SC2181
[ $? -eq 0 ] || fails=$((fails + 1))

section "汇总"
if [ "$fails" -eq 0 ]; then
  printf '\033[32me2e 全部通过。\033[0m\n'
  exit 0
fi
printf '\033[31m有 %d 组未通过。\033[0m 日志：%s/{gateway,worker}.log\n' "$fails" "$LOG_DIR"
exit 1
