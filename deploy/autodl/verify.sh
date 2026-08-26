#!/usr/bin/env bash
# vLLM 起来之后跑这个。**别跳过。**
#
#   bash deploy/autodl/verify.sh
#
# 它花不到两分钟，但专门抓这条链路上**四处不会报错的失效**：
#
#   1. chat template 缺失 —— 服务起得来，每个 chat 请求 400
#   2. **模板漏了 BOS** —— 服务健康、请求 200、token 数正常，输出却是彻底的垃圾
#      （2026-08-25 实测撞到：`Free OCR.` 吐 "PUBLIC DATA / ## 10 10 10 10…" 复读到底）
#   3. 图片没进 prompt —— 模型在盲猜
#   4. 特殊 token 被剥 —— 模型明明报了 bbox，返回里一个标签都没有，
#      于是每个块 bbox 全是 null，**全程零报错**
#
# 第 2、4 条是最贵的那种 bug：不验的话，要等跑完整份 e2e、看到"出处都不能裁剪"
# 或者"识别结果是乱码"才会发现，而那时 GPU 已经烧了几十分钟。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

BASE="http://$VLLM_HOST:$VLLM_PORT"
PY="$VENV_DIR/bin/python"
fails=0

pass() { printf '\033[32m  PASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31m  FAIL\033[0m %s\n' "$*"; fails=$((fails + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 1. 活着吗
section "1. 服务可达"
if curl -sf --max-time 10 "$BASE/health" >/dev/null; then
  pass "$BASE/health"
else
  fail "$BASE/health 不通 —— 服务还没起完？看 $LOG_DIR/vllm.log"
  echo "（后面的检查都依赖它，先解决这个）"
  exit 1
fi

# ---------------------------------------------------------------- 2. 模型名
section "2. 对外模型名"
models=$(curl -sf --max-time 10 "$BASE/v1/models" || echo '{}')
if echo "$models" | grep -q "\"$SERVED_NAME\""; then
  pass "$SERVED_NAME"
else
  fail "/v1/models 里没有 $SERVED_NAME —— gateway 发过来的 model 字段会对不上（404）"
  echo "    实际返回：$models"
fi

# ---------------------------------------------------------------- 3. prompt 渲染
# 这是最容易错又最难发现的一处。用 /tokenize + /detokenize 把 vLLM **真正**
# 渲染出来的 prompt 打回原形，逐字对官方格式。
section "3. chat template 渲染出的 prompt"
rendered=$("$PY" - "$BASE" "$SERVED_NAME" <<'PYEOF'
import json, sys, urllib.request

base, model = sys.argv[1], sys.argv[2]
# 1x1 的透明 PNG，够触发占位符插入，又不用真去识别
tiny = ("data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

def post(path, payload):
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

try:
    tok = post("/tokenize", {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": tiny}},
            {"type": "text", "text": "<|grounding|>Convert the document to markdown."},
        ]}],
    })
except Exception as exc:                      # noqa: BLE001
    print("ERROR " + str(exc))
    raise SystemExit(0)

ids = tok.get("tokens") or []
try:
    back = post("/detokenize", {"tokens": ids})
    print("OK " + json.dumps(back.get("prompt", "")))
except Exception as exc:                      # noqa: BLE001
    print("ERROR detokenize: " + str(exc))
PYEOF
)
# 注意：**这一项是诊断，不是判据。**
# /tokenize + /detokenize 的响应结构随 vLLM 版本变过，取不到时说明不了任何问题。
# 真正的判据是下面第 4 项（真的识别一张图）—— chat template 没挂上的话那条必红。
# 所以这里取不到就只打 NOTE，不计入失败，免得诊断工具自身的脆弱造成假红。
prompt=""
case "$rendered" in
  OK*) prompt=$(printf '%s' "${rendered#OK }" \
        | "$PY" -c 'import json,sys; print(json.load(sys.stdin))' 2>/dev/null) ;;
esac
if [ -z "$prompt" ]; then
  printf '  \033[33mNOTE\033[0m 取不到渲染后的 prompt（/tokenize 结构随版本变过）。\n'
  printf '       这不说明有问题 —— 判据看第 4 项。%s\n' "${rendered#ERROR }"
else
  printf '    渲染结果: %s\n' "$prompt"
  # **不要断言占位符只有 1 个。** /detokenize 打回来的是**展开之后**的 prompt：
  # vLLM 已经把那一个 `<image>` 替换成了 N 个图像 token（模型卡：(0-6)×144 + 256）。
  # 实测 1x1 图 257 个、整页 1120 个 —— 都是正确的。
  n_img=$(printf '%s' "$prompt" | grep -o '<image>' | wc -l)
  if [ "$n_img" -ge 1 ]; then
    pass "图像占位符已展开（$n_img 个视觉 token，(0-6)×144+256 的范围内）"
  else
    fail "prompt 里一个图像占位符都没有 —— 图片没进 prompt"
  fi
  case "$prompt" in
    "<｜begin▁of▁sentence｜>"*) pass "BOS 在（少了它模型输出全是垃圾，见模板注释）" ;;
    *) fail "prompt 开头没有 BOS —— 模板漏了，模型会吐垃圾且不报错" ;;
  esac
  case "$prompt" in
    *"<|grounding|>Convert the document to markdown."*)
      pass "grounding prompt 逐字正确" ;;
    *) fail "prompt 里没有官方那句 grounding 指令" ;;
  esac
fi

# ---------------------------------------------------------------- 4. 真识别一张图
# **这条是整个脚本存在的理由。**造一张有字的图，走与 gateway 完全相同的请求形状
# （含 skip_special_tokens=false 与 vllm_xargs），看返回里有没有 grounding 标签。
section "4. 端到端识别（与 gateway 同样的请求形状）"
"$PY" - "$BASE" "$SERVED_NAME" <<'PYEOF'
import base64, io, json, sys, urllib.request

base, model = sys.argv[1], sys.argv[2]

from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (900, 400), "white")
draw = ImageDraw.Draw(img)
# 字号要够大。PIL 的默认位图字体只有 11px，模型认不出来会让这条检查
# **假红**——然后有人去排查一个根本不存在的链路问题，纯烧 GPU 时间。
try:
    font = ImageFont.load_default(size=34)       # Pillow >= 10.1
except TypeError:
    font = ImageFont.load_default()
# 只写英文数字 —— 这里验的是链路通不通，不是中文识别质量
draw.text((60, 60), "PURCHASE ORDER 2026", fill="black", font=font)
draw.text((60, 150), "Invoice Number: DDP-8712", fill="black", font=font)
draw.text((60, 240), "Total Amount: 120000 CNY", fill="black", font=font)
buf = io.BytesIO(); img.save(buf, format="PNG")
uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

payload = {
    "model": model,
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "text", "text": "<|grounding|>Convert the document to markdown."},
    ]}],
    "stream": False,
    "temperature": 0,
    "max_tokens": 2048,
    # 不传这个的话，<|ref|>/<|det|> 会在返回前被剥光 —— 本脚本的头号目标
    "skip_special_tokens": False,
    "include_stop_str_in_output": True,
    "vllm_xargs": {"ngram_size": 20, "window_size": 50,
                   "whitelist_token_ids": [128821, 128822]},
}
req = urllib.request.Request(base + "/v1/chat/completions", method="POST",
                             data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)
except Exception as exc:                      # noqa: BLE001
    print("FAIL 请求失败: %s" % exc); raise SystemExit(1)

text = (out["choices"][0]["message"]["content"] or "")
print("    模型返回前 300 字：")
print("    " + text[:300].replace("\n", "\n    "))

ok = True
if "<|ref|>" in text and "<|det|>" in text:
    print("PASS grounding 标签在（bbox 拿得到）")
else:
    print("FAIL 返回里没有 <|ref|>/<|det|> —— 特殊 token 被剥了，"
          "所有 bbox 都会是 null 且不会报错")
    ok = False

if "8712" in text or "DDP" in text:
    print("PASS 识别出了图上的文字")
else:
    print("FAIL 没识别出图上文字（识别质量问题，不是链路问题）")
    ok = False
raise SystemExit(0 if ok else 1)
PYEOF
# shellcheck disable=SC2181
if [ $? -eq 0 ]; then pass "端到端识别通过"; else fail "端到端识别没通过（细节见上）"; fi

# ---------------------------------------------------------------- 5. FlashAttention
# 不装 flash-attn 这个 pip 包是**故意的**（见 bootstrap.sh 的长注释）：
# vLLM 自带 vllm-flash-attn，FlashAttention 照样在用。这里打印证据，
# 免得将来有人看见"没装 flash-attn"就以为没用上，又去编译一个小时。
section "5. FlashAttention 用上了吗"
if "$PY" -c "import vllm_flash_attn" 2>/dev/null; then
  pass "vllm_flash_attn 可导入（vLLM 自带的 FlashAttention，无需另装 flash-attn）"
else
  printf '  \033[33mNOTE\033[0m vllm_flash_attn 导不进来；看下面日志里实际选了哪个后端\n'
fi
if [ -f "$LOG_DIR/vllm.log" ]; then
  backend=$(grep -i -m1 "attention backend\|Using .* backend" "$LOG_DIR/vllm.log" || true)
  [ -n "$backend" ] && printf '    日志: %s\n' "$backend"
fi

# ---------------------------------------------------------------- 6. 抽取平面的指令模型
# 不起它 /v1/extract 会一律报 no_instruct_model —— 那是**如实报错**（比硬抽出
# 一堆假的 not_found 好得多），但抽取平面等于没有。所以这里也验一下。
section "6. 抽取平面的指令模型"
if [ "$ENABLE_CHAT" != "1" ]; then
  printf '  \033[33mSKIP\033[0m ENABLE_CHAT=0；/v1/extract 会一律报 no_instruct_model\n'
elif curl -sf --max-time 10 "http://$VLLM_HOST:$CHAT_PORT/health" >/dev/null; then
  pass "http://$VLLM_HOST:$CHAT_PORT 可达"
  # 抽值要的是"能按指令吐 JSON"。这里就用一个最小的真实任务验它，
  # 而不是只看端口通不通 —— 端口通但模型不听指令，正是我们要防的那件事
  "$PY" - "http://$VLLM_HOST:$CHAT_PORT" "$CHAT_SERVED_NAME" <<'PYEOF'
import json, sys, urllib.request
base, model = sys.argv[1], sys.argv[2]
payload = {"model": model, "temperature": 0, "max_tokens": 200, "stream": False,
           "messages": [
               {"role": "system", "content": "只输出 JSON，不要任何解释文字。"},
               {"role": "user", "content":
                '【资料】买方：北极星科技有限公司。\n'
                '抽取字段 buyer（买方单位全称），'
                '按 {"found": bool, "value": str|null} 输出。'}]}
req = urllib.request.Request(base + "/v1/chat/completions", method="POST",
                             data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        text = json.load(r)["choices"][0]["message"]["content"] or ""
except Exception as exc:                      # noqa: BLE001
    print("FAIL 请求失败: %s" % exc); raise SystemExit(1)

print("    返回: " + text[:200].replace("\n", " "))
start, end = text.find("{"), text.rfind("}")
try:
    obj = json.loads(text[start:end + 1])
except Exception:                             # noqa: BLE001
    print("FAIL 输出里取不出 JSON —— 这个模型不适合当抽取模型"); raise SystemExit(1)
if obj.get("found") and "北极星" in str(obj.get("value") or ""):
    print("PASS 按指令吐出了正确的 JSON")
    raise SystemExit(0)
print("FAIL JSON 出来了但内容不对：%r" % obj); raise SystemExit(1)
PYEOF
  # shellcheck disable=SC2181
  if [ $? -eq 0 ]; then pass "指令跟随 + JSON 输出"; else fail "指令模型没按要求输出"; fi
else
  fail "http://$VLLM_HOST:$CHAT_PORT 不通 —— 先跑 serve-chat.sh（看 $LOG_DIR/chat.log）"
fi

# ---------------------------------------------------------------- 汇总
section "汇总"
if [ "$fails" -eq 0 ]; then
  printf '\033[32m全部通过。\033[0m 可以接 gateway 了：MODELS_CONFIG=models.autodl.yaml\n'
  exit 0
fi
printf '\033[31m%d 项未通过。\033[0m 修完再往下走 —— 带病跑 e2e 只会烧掉更多 GPU 时间。\n' "$fails"
exit 1
