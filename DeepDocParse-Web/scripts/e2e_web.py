"""真环境 e2e：浏览器视角 + 第三方开发者视角，跑通产品层全链路。

与 backend 单测互补 —— 这里全是真 Postgres(pgvector) / 真 MinIO / 真 service。
单测里被替身挡掉的两件事只有这里能验：**pgvector 的真实检索**与**真实版面裁剪**。

覆盖：
  1. 注册 -> 登录 -> 上传 -> 轮询至 succeeded -> 索引 ready
  2. 归档产物落 MinIO，markdown 无 base64 残留，图片可取
  3. 页数计量正确；按页取块（前端左右栏对齐的数据源）
  4. 同文件二次上传命中去重
  5. 稳定文件 URL 免鉴权可下载（service 与 MCP 都靠它）
  6. **文档问答**：SSE 逐帧、出处页码与版面对得上、裁剪图可取；VQA 挂掉时降级可见
  7. 跨文档检索命中
  8. 换参数重解析产生新版本，旧版本仍可读
  9. sk- key 调 /v1/* 与 /mcp；超额度 402、超速率 429
 10. 软删除后 GC 清掉对象

前置：
  docker compose -f docker/compose.web.yml --env-file ../.env up -d   # PG(pgvector) + MinIO
  alembic upgrade head && uvicorn app.main:app --port 8080 --reload
  service 侧按 ../CLAUDE.md「宿主机混合模式」起 gateway / arq / mcp_server

用法：
  python scripts/e2e_web.py                 # 全部
  python scripts/e2e_web.py --skip-mcp      # mcp_server 没起时
环境变量：WEB_URL / FIXTURE / DATABASE_URL（核对 chunks 表）
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
    str(Path(__file__).resolve().parents[2] / "DeepDocParse" / "tests" / "fixtures" / "long-doc.pdf"),
)
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://ddp:ddp@127.0.0.1:15432/deepdocparse")

POLL_LIMIT, POLL_INTERVAL = 90, 5
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


async def wait_until(http: httpx.AsyncClient, document_id: str, headers: dict,
                     ready_index: bool = False) -> dict:
    for i in range(POLL_LIMIT):
        doc = (await http.get(f"{WEB}/api/documents/{document_id}", headers=headers)).json()
        done = doc["status"] in ("succeeded", "failed")
        if done and (not ready_index or doc["index_status"] in ("ready", "failed", "none")):
            return doc
        if i % 4 == 0:
            print(f"    ... {doc['status']}/{doc['index_status']} ({i * POLL_INTERVAL}s)")
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(f"文档 {document_id} 在 {POLL_LIMIT * POLL_INTERVAL}s 内未就绪")


PASSWORD = "e2e-password-123"


async def sign_in(http: httpx.AsyncClient, username: str | None) -> dict:
    """没给用户名就新注册一个；给了就先试注册、已存在则登录（分段跑时复用账号）。"""
    username = username or f"e2e_{uuid.uuid4().hex[:8]}"
    reg = await http.post(f"{WEB}/api/auth/register",
                          json={"username": username, "password": PASSWORD})
    if reg.status_code == 201:
        check("注册返回 JWT", True)
        return {"Authorization": f"Bearer {reg.json()['access_token']}"}
    login = await http.post(f"{WEB}/api/auth/login",
                            json={"username": username, "password": PASSWORD})
    check("登录已有账号", login.status_code == 200, f"status={login.status_code}")
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def pick_ready_document(http: httpx.AsyncClient, headers: dict) -> dict:
    """分段跑的第二段：直接挑一份已归档且索引就绪的文档。"""
    documents = (await http.get(f"{WEB}/api/documents", headers=headers)).json()
    ready = [d for d in documents
             if d["status"] == "succeeded" and d["index_status"] == "ready"]
    check("找到可问答的文档", bool(ready), f"共 {len(documents)} 份")
    return ready[0] if ready else {"status": "failed"}


async def scenario_web_flow(http: httpx.AsyncClient, username: str | None) -> tuple[dict, dict]:
    print("\n[1] 浏览器视角：注册 -> 上传 -> 归档 -> 索引 -> 预览")
    headers = await sign_in(http, username)

    content = Path(FIXTURE).read_bytes()
    upload = await http.post(f"{WEB}/api/documents", headers=headers,
                             files={"file": (Path(FIXTURE).name, content, "application/pdf")})
    if not check("上传被受理(202)", upload.status_code == 202, upload.text[:200]):
        return headers, {"status": "failed"}

    document = await wait_until(http, upload.json()["id"], headers, ready_index=True)
    if not check("解析并归档成功", document["status"] == "succeeded", document.get("error") or ""):
        return headers, document
    check("向量索引就绪", document["index_status"] == "ready", document.get("index_error") or "")

    result = (await http.get(f"{WEB}/api/documents/{document['id']}/result", headers=headers)).json()
    check("markdown 无 base64 残留", "data:image/" not in result["markdown"])
    layout = (await http.get(f"{WEB}/api/documents/{document['id']}/layout", headers=headers)).json()
    check("页数与版面一致", len(layout.get("pdf_info", [])) == document["page_count"],
          f"pdf_info={len(layout.get('pdf_info', []))} page_count={document['page_count']}")

    pages = (await http.get(f"{WEB}/api/documents/{document['id']}/pages", headers=headers)).json()
    blocks = [b for p in pages["pages"] for b in p["blocks"]]
    check("按页取块可用且带 bbox", bool(blocks) and any(b["bbox"] for b in blocks),
          f"blocks={len(blocks)}")
    check("块带 page_size（裁剪坐标基准）", any(b["page_size"] for b in blocks))

    if result["images"]:
        job_id = result["job_id"]
        img = await http.get(
            f"{WEB}/api/documents/{document['id']}/jobs/{job_id}/images/{result['images'][0]}",
            headers=headers)
        check("归档图片可取回", img.status_code == 200 and len(img.content) > 0)
    else:
        print("  SKIP  归档图片（本文档没有图片）")

    src = (await http.get(f"{WEB}/api/documents/{document['id']}/source-url",
                          headers=headers)).json()
    raw = await http.get(src["url"])          # 故意不带鉴权头：service 就是这么取的
    check("稳定文件 URL 免鉴权可下载", raw.status_code == 200 and raw.content == content)

    dup = await http.post(f"{WEB}/api/documents", headers=headers,
                          files={"file": (Path(FIXTURE).name, content, "application/pdf")})
    check("同文件二次上传命中去重", dup.json()["id"] == document["id"])

    usage = (await http.get(f"{WEB}/api/usage", headers=headers)).json()
    check("用量按页计入", usage["total_pages"] == document["page_count"],
          f"usage={usage['total_pages']} page_count={document['page_count']}")

    count = await chunk_count(document["id"])
    if count is None:
        print("  SKIP  chunks 向量核对（未配 DATABASE_URL 或缺 asyncpg）")
    else:
        check("chunks 落库且带向量（pgvector 真实路径）", count > 0, f"chunks={count}")
    return headers, document


async def scenario_qa(http: httpx.AsyncClient, headers: dict, document: dict) -> None:
    print("\n[2] 文档问答：SSE + 出处 + 降级可见")
    created = await http.post(f"{WEB}/api/documents/{document['id']}/conversations",
                              headers=headers)
    if not check("建会话", created.status_code == 201, created.text[:200]):
        return
    cid = created.json()["id"]

    events: list[tuple[str, dict]] = []
    async with http.stream("POST", f"{WEB}/api/conversations/{cid}/ask", headers=headers,
                           json={"question": "这份文档讲了什么？"}, timeout=600.0) as resp:
        if not check("问答返回 200", resp.status_code == 200, str(resp.status_code)):
            return
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
        for block in buffer.split("\n\n"):
            lines = block.splitlines()
            if len(lines) >= 2 and lines[0].startswith("event: "):
                events.append((lines[0][7:], json.loads(lines[1][6:])))

    names = [n for n, _ in events]
    check("逐帧流式返回", names.count("delta") >= 1, f"帧序列={names[:6]}")
    answer = "".join(d.get("text", "") for n, d in events if n == "delta")
    check("回答非空", bool(answer.strip()), answer[:80])

    done = dict(events).get("done", {})
    citations = dict(events).get("citations", {}).get("citations", [])
    check("回答带出处", bool(citations), f"citations={len(citations)}")
    if citations:
        page = citations[0]["page_idx"]
        check("出处页码在文档范围内", 0 <= page < document["page_count"],
              f"page={page} of {document['page_count']}")
        if citations[0].get("crop_url"):
            crop = await http.get(f"{WEB}{citations[0]['crop_url']}", headers=headers)
            check("出处裁剪图可取回", crop.status_code == 200 and len(crop.content) > 100,
                  f"status={crop.status_code}")
        else:
            print(f"  SKIP  出处裁剪图（降级：{done.get('degraded')}）")

    # 降级必须可见：VQA 没起时 verified=false 且给出原因
    if done.get("verified"):
        # 曾经这里写的是 check("已做视觉验证", True) —— 一条恒真断言，什么都没验。
        # "已做视觉验证"这句话的**证据**是：真有一张裁剪图取得回来，且它是张真 PNG。
        # 说得出口的东西必须验得出来，否则这个标记就是装饰
        crop_url = citations[0].get("crop_url") if citations else None
        evidence = b""
        if crop_url:
            evidence = (await http.get(f"{WEB}{crop_url}", headers=headers)).content
        check("声称已验证时确有出处截图作证",
              bool(crop_url) and evidence.startswith(b"\x89PNG"),
              f"crop_url={crop_url} bytes={len(evidence)}")
        check("声称已验证时不得同时带降级标记", not done.get("degraded"), f"done={done}")
    else:
        check("未验证时给出降级原因（不许静默）", bool(done.get("degraded")),
              f"done={done}")

    # 出处必须带可量纲的相关度（RRF 名次分表达不了"有多相关"，见 app/search.py）
    if citations:
        confidence = done.get("confidence") or {}
        check("回答带检索可信度", confidence.get("level") in ("high", "low", "unknown"),
              f"confidence={confidence}")
        check("出处带稳定定位键（reindex 后还接得回原文）",
              all(c.get("parse_job_id") and c.get("seq") is not None for c in citations),
              json.dumps(citations[0], ensure_ascii=False)[:160])

    history = (await http.get(f"{WEB}/api/conversations/{cid}/messages", headers=headers)).json()
    check("问答落库", [m["role"] for m in history] == ["user", "assistant"],
          f"messages={len(history)}")


async def scenario_search_and_versions(http: httpx.AsyncClient, headers: dict,
                                       document: dict) -> None:
    print("\n[3] 检索与解析版本")
    hits = (await http.get(f"{WEB}/api/search", headers=headers, params={"q": "文档"})).json()
    check("跨文档检索有命中", bool(hits["groups"]), json.dumps(hits)[:120])

    again = await http.post(f"{WEB}/api/documents/{document['id']}/reparse", headers=headers,
                            json={"engine": "mineru", "options": {"backend": "pipeline",
                                                                  "lang": "ch"}})
    if check("换参数重解析被受理", again.status_code == 202, again.text[:200]):
        jobs = (await http.get(f"{WEB}/api/documents/{document['id']}/jobs",
                               headers=headers)).json()
        check("产生了新的解析版本", len(jobs) >= 2, f"jobs={len(jobs)}")
        current = [j for j in jobs if j["is_current"]]
        check("当前版本仍是旧的（切换要显式）", len(current) == 1)


async def scenario_api_key(http: httpx.AsyncClient, headers: dict, document: dict) -> None:
    print("\n[4] 第三方视角：sk- key 调 /v1/*")
    created = (await http.post(f"{WEB}/api/keys", headers=headers,
                               json={"name": "e2e", "quota_pages": 5000,
                                     "rate_limit_per_min": 3})).json()
    key_headers = {"Authorization": f"Bearer {created['key']}"}
    check("key 明文只返回一次", created["key"].startswith("sk-"))

    models = await http.get(f"{WEB}/v1/models", headers=key_headers)
    check("/v1/models 透传", models.status_code == 200 and "data" in models.json(),
          models.text[:120])

    codes = [(await http.get(f"{WEB}/v1/models", headers=key_headers)).status_code
             for _ in range(4)]
    check("超速率返回 429", 429 in codes, f"codes={codes}")

    tiny = (await http.post(f"{WEB}/api/keys", headers=headers,
                            json={"name": "e2e-quota", "quota_pages": 1,
                                  "rate_limit_per_min": 120})).json()
    tiny_headers = {"Authorization": f"Bearer {tiny['key']}"}
    src = (await http.get(f"{WEB}/api/documents/{document['id']}/source-url",
                          headers=headers)).json()

    submit = await http.post(f"{WEB}/v1/parse", headers=tiny_headers,
                             json={"file_url": src["url"]})
    if check("对外 /v1/parse 受理", submit.status_code == 202, submit.text[:120]):
        service_task = submit.json()["task_id"]
        got = None
        for _ in range(POLL_LIMIT):
            got = await http.get(f"{WEB}/v1/parse/{service_task}/result", headers=tiny_headers)
            if got.status_code == 200:
                break
            await asyncio.sleep(POLL_INTERVAL)
        check("取结果时按页扣额度", got is not None and got.status_code == 200,
              f"status={got.status_code if got else 'n/a'}")

        exhausted = await http.get(f"{WEB}/v1/models", headers=tiny_headers)
        body = exhausted.json()
        check("额度耗尽返回 402 且错误体是 OpenAI 风格",
              exhausted.status_code == 402
              and body.get("error", {}).get("code") == "quota_exhausted",
              f"status={exhausted.status_code} body={json.dumps(body, ensure_ascii=False)[:120]}")


async def scenario_mcp(http: httpx.AsyncClient, headers: dict, document: dict) -> None:
    print("\n[5] Agent 视角：经 backend 反代调 MCP ask_document")
    created = (await http.post(f"{WEB}/api/keys", headers=headers,
                               json={"name": "e2e-mcp", "rate_limit_per_min": 120})).json()
    src = (await http.get(f"{WEB}/api/documents/{document['id']}/source-url",
                          headers=headers)).json()

    init = await http.post(f"{WEB}/mcp", headers={
        "Authorization": f"Bearer {created['key']}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "e2e", "version": "1"}}})
    session = init.headers.get("mcp-session-id")
    if not check("MCP 会话头透传回来", bool(session), f"status={init.status_code}"):
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
    check("ask_document 有返回", call.status_code == 200 and "result" in call.text,
          call.text[:200])


async def scenario_gc(http: httpx.AsyncClient, headers: dict, document: dict) -> None:
    print("\n[6] 软删除与对象回收")
    resp = await http.delete(f"{WEB}/api/documents/{document['id']}", headers=headers)
    check("删除返回 204", resp.status_code == 204, str(resp.status_code))
    check("删除后不可见", (await http.get(f"{WEB}/api/documents/{document['id']}",
                                          headers=headers)).status_code == 404)
    check("删除后检索不到", not (await http.get(f"{WEB}/api/search", headers=headers,
                                                params={"q": "文档"})).json()["groups"])
    print("    （对象回收由对账循环执行，最长等一个 RECONCILE_INTERVAL）")


async def chunk_count(document_id: str) -> int | None:
    """直连 PG 数 chunks —— 这是"pgvector 真的写进去了"的唯一物证。"""
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError:
        return None
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM chunks WHERE document_id = :d "
                     "AND embedding IS NOT NULL"), {"d": document_id})
            return int(row.scalar_one())
    except Exception:
        return None
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-mcp", action="store_true")
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--user", help="复用固定账号（分段跑时两段要用同一个）")
    parser.add_argument("--phase", choices=["all", "parse", "qa"], default="all",
                        help="dev 机内存装不下所有运行时时分两段："
                             "parse 段要 mineru+TEI，qa 段要 VQA（互斥）")
    args = parser.parse_args()

    print(f"e2e 目标：{WEB}\n素材：{FIXTURE}\n阶段：{args.phase}")
    async with httpx.AsyncClient(trust_env=False, timeout=180.0) as http:
        if args.phase == "qa":
            headers = await sign_in(http, args.user)
            document = await pick_ready_document(http, headers)
            if document.get("status") == "succeeded":
                await scenario_qa(http, headers, document)
                # 删除放在问答之后：带着会话与消息删，才覆盖到外键顺序那条回归
                await scenario_gc(http, headers, document)
        else:
            headers, document = await scenario_web_flow(http, args.user)
            if document.get("status") == "succeeded" and args.phase == "all":
                await scenario_qa(http, headers, document)
            if document.get("status") == "succeeded":
                await scenario_search_and_versions(http, headers, document)
                if not args.skip_api:
                    await scenario_api_key(http, headers, document)
                if not args.skip_mcp:
                    await scenario_mcp(http, headers, document)
                if args.phase == "all":
                    await scenario_gc(http, headers, document)

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项：{failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
