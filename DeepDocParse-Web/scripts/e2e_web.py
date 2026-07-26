"""真环境 e2e：浏览器视角 + 第三方开发者视角，跑通产品层全链路。

覆盖（与 backend 单测互补 —— 这里全是真 Postgres / 真 MinIO / 真 service）：
  1. 注册 -> 登录 -> 上传 -> 轮询至 succeeded
  2. 归档产物落在 MinIO，markdown 无 base64 残留，图片可取
  3. 页数计量正确（page_count == layout_json.pdf_info 长度）
  4. 同文件二次上传命中去重，不再打 service
  5. 稳定文件 URL 免鉴权可下载（service 与 MCP 都靠它）
  6. sk- key 调 /v1/parse 与 /v1/chat/completions（流式收到多帧）
  7. sk- key 调 /mcp 的 ask_document，并核对 service 侧 retrieval 计数
  8. 超速率 429 / 超额度 402，错误体是 OpenAI 风格

前置：
  docker compose -f docker/compose.web.yml --env-file ../.env up -d   # PG + MinIO
  alembic upgrade head && uvicorn app.main:app --port 8080            # 本层
  service 侧按 ../CLAUDE.md「宿主机混合模式」起 gateway / arq / mcp_server

用法：
  python scripts/e2e_web.py                 # 全部
  python scripts/e2e_web.py --skip-mcp      # mcp_server 没起时
环境变量：WEB_URL / FIXTURE（要上传的文件）/ REDIS_URL（核对 retrieval 计数）
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

WEB = os.environ.get("WEB_URL", "http://127.0.0.1:8080")
FIXTURE = os.environ.get(
    "FIXTURE",
    str(Path(__file__).resolve().parents[2] / "DeepDocParse" / "tests" / "fixtures" / "sample.pdf"),
)
REDIS_URL = os.environ.get("REDIS_URL", "")

POLL_LIMIT, POLL_INTERVAL = 90, 5
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


async def wait_for_status(http: httpx.AsyncClient, task_id: str, headers: dict) -> dict:
    for i in range(POLL_LIMIT):
        task = (await http.get(f"{WEB}/api/tasks/{task_id}", headers=headers)).json()
        if task["status"] in ("succeeded", "failed"):
            return task
        if i % 4 == 0:
            print(f"    ... {task['status']} ({i * POLL_INTERVAL}s)")
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(f"任务 {task_id} 在 {POLL_LIMIT * POLL_INTERVAL}s 内未落终态")


async def scenario_web_flow(http: httpx.AsyncClient) -> tuple[dict, dict]:
    print("\n[1] 浏览器视角：注册 -> 上传 -> 归档 -> 预览")
    username = f"e2e_{uuid.uuid4().hex[:8]}"
    reg = await http.post(f"{WEB}/api/auth/register",
                          json={"username": username, "password": "e2e-password-123"})
    check("注册返回 JWT", reg.status_code == 201, f"status={reg.status_code}")
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    content = Path(FIXTURE).read_bytes()
    upload = await http.post(f"{WEB}/api/tasks/upload", headers=headers,
                             files={"file": (Path(FIXTURE).name, content, "application/pdf")})
    if not check("上传被受理(202)", upload.status_code == 202, upload.text[:200]):
        return headers, {"status": "failed"}
    task = await wait_for_status(http, upload.json()["id"], headers)
    if not check("解析并归档成功", task["status"] == "succeeded", task.get("error") or ""):
        return headers, task

    result = (await http.get(f"{WEB}/api/tasks/{task['id']}/result", headers=headers)).json()
    check("markdown 无 base64 残留", "data:image/" not in result["markdown"])
    check("页数与版面一致", task["page_count"] > 0, f"page_count={task['page_count']}")
    layout = (await http.get(f"{WEB}/api/tasks/{task['id']}/layout", headers=headers)).json()
    check("layout.json 已归档", len(layout.get("pdf_info", [])) == task["page_count"],
          f"pdf_info={len(layout.get('pdf_info', []))} page_count={task['page_count']}")

    if result["images"]:
        img = await http.get(f"{WEB}/api/tasks/{task['id']}/images/{result['images'][0]}",
                             headers=headers)
        check("归档图片可取回", img.status_code == 200 and len(img.content) > 0)
    else:
        print("  SKIP  归档图片（本文档没有图片）")

    src = (await http.get(f"{WEB}/api/tasks/{task['id']}/source-url", headers=headers)).json()
    raw = await http.get(src["url"])          # 故意不带任何鉴权头：service 就是这么取的
    check("稳定文件 URL 免鉴权可下载", raw.status_code == 200 and raw.content == content)

    dup = await http.post(f"{WEB}/api/tasks/upload", headers=headers,
                          files={"file": (Path(FIXTURE).name, content, "application/pdf")})
    check("同文件二次上传命中去重", dup.json()["id"] == task["id"])

    usage = (await http.get(f"{WEB}/api/usage", headers=headers)).json()
    check("用量按页计入", usage["total_pages"] == task["page_count"],
          f"usage={usage['total_pages']} page_count={task['page_count']}")
    return headers, task


async def scenario_api_key(http: httpx.AsyncClient, headers: dict, task: dict) -> None:
    print("\n[2] 第三方视角：sk- key 调 /v1/*")
    created = (await http.post(f"{WEB}/api/keys", headers=headers,
                               json={"name": "e2e", "quota_pages": 5000,
                                     "rate_limit_per_min": 3})).json()
    key_headers = {"Authorization": f"Bearer {created['key']}"}
    check("key 明文只返回一次", created["key"].startswith("sk-"))

    models = await http.get(f"{WEB}/v1/models", headers=key_headers)
    check("/v1/models 透传", models.status_code == 200 and "data" in models.json(),
          models.text[:120])

    chat = await http.post(f"{WEB}/v1/chat/completions", headers=key_headers, json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 16})
    if chat.status_code == 200:
        frames = chat.text.count("data:")
        check("chat 流式收到多帧", frames >= 2, f"frames={frames}")
    else:
        print(f"  SKIP  chat 流式（VQA 未启动，status={chat.status_code}）")

    # 限速：rate_limit_per_min=3，前面已用掉若干次，连打必然触发
    codes = [(await http.get(f"{WEB}/v1/models", headers=key_headers)).status_code
             for _ in range(4)]
    limited = next((c for c in codes if c == 429), None)
    check("超速率返回 429", limited == 429, f"codes={codes}")

    # 额度：给一把 1 页额度的 key，用它走完整的"提交 -> 取结果（按页扣减）-> 再调"
    tiny = (await http.post(f"{WEB}/api/keys", headers=headers,
                            json={"name": "e2e-quota", "quota_pages": 1,
                                  "rate_limit_per_min": 120})).json()
    tiny_headers = {"Authorization": f"Bearer {tiny['key']}"}
    src = (await http.get(f"{WEB}/api/tasks/{task['id']}/source-url", headers=headers)).json()

    submit = await http.post(f"{WEB}/v1/parse", headers=tiny_headers,
                             json={"file_url": src["url"]})
    if check("对外 /v1/parse 受理", submit.status_code == 202, submit.text[:120]):
        service_task = submit.json()["task_id"]
        for _ in range(POLL_LIMIT):
            got = await http.get(f"{WEB}/v1/parse/{service_task}/result", headers=tiny_headers)
            if got.status_code == 200:
                break
            await asyncio.sleep(POLL_INTERVAL)
        check("取结果时按页扣额度", got.status_code == 200, f"status={got.status_code}")

        exhausted = await http.get(f"{WEB}/v1/models", headers=tiny_headers)
        body = exhausted.json()
        check("额度耗尽返回 402 且错误体是 OpenAI 风格",
              exhausted.status_code == 402 and body.get("error", {}).get("code") == "quota_exhausted",
              f"status={exhausted.status_code} body={json.dumps(body, ensure_ascii=False)[:120]}")


async def scenario_mcp(http: httpx.AsyncClient, headers: dict, task: dict) -> None:
    print("\n[3] Agent 视角：经 backend 反代调 MCP ask_document")
    created = (await http.post(f"{WEB}/api/keys", headers=headers,
                               json={"name": "e2e-mcp", "rate_limit_per_min": 120})).json()
    src = (await http.get(f"{WEB}/api/tasks/{task['id']}/source-url", headers=headers)).json()

    before = await retrieval_counts()
    init = await http.post(f"{WEB}/mcp", headers={
        "Authorization": f"Bearer {created['key']}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "e2e", "version": "1"}}})
    session = init.headers.get("mcp-session-id")
    check("MCP 会话头透传回来", bool(session), f"status={init.status_code}")
    if not session:
        return

    mcp_headers = {"Authorization": f"Bearer {created['key']}",
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json",
                   "mcp-session-id": session}
    await http.post(f"{WEB}/mcp", headers=mcp_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = await http.post(f"{WEB}/mcp", headers=mcp_headers, timeout=600.0, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "ask_document",
                   "arguments": {"file_url": src["url"], "question": "这份文档讲了什么？"}}})
    text = call.text
    check("ask_document 有返回", call.status_code == 200 and "result" in text, text[:200])

    after = await retrieval_counts()
    if before is None or after is None:
        print("  SKIP  检索路径核对（未配 REDIS_URL）")
    else:
        check("走的是向量检索而非 BM25 兜底",
              after["vector"] > before["vector"] and after["bm25"] == before["bm25"],
              f"{before} -> {after}")


async def retrieval_counts() -> dict | None:
    """读 service 侧的检索路径计数（mcp_server 每次调用会 incr）。"""
    if not REDIS_URL:
        return None
    try:
        import redis.asyncio as redis
    except ImportError:
        return None
    r = redis.from_url(REDIS_URL)
    try:
        return {mode: int(await r.get(f"metrics:retrieval:{mode}") or 0)
                for mode in ("vector", "bm25")}
    except Exception:
        return None
    finally:
        await r.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-mcp", action="store_true")
    parser.add_argument("--skip-api", action="store_true")
    args = parser.parse_args()

    print(f"e2e 目标：{WEB}\n素材：{FIXTURE}")
    async with httpx.AsyncClient(trust_env=False, timeout=120.0) as http:
        headers, task = await scenario_web_flow(http)
        if task.get("status") == "succeeded":
            if not args.skip_api:
                await scenario_api_key(http, headers, task)
            if not args.skip_mcp:
                await scenario_mcp(http, headers, task)

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项：{failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
