#!/usr/bin/env python
"""真实用户路径 e2e —— 对着**真起来的全栈**跑，不 mock 任何东西。

    scripts/dev.sh up
    python scripts/e2e_stack.py                     # 无 GPU：到解析归档为止
    python scripts/e2e_stack.py --with-embeddings   # 有 embedding 端点时再往后跑

## 它与单测的关系

单测全是进程内的（SQLite + respx + 内存对象存储），跑得快、覆盖细，
但它**验不到部署形态**：预签名 URL 签在哪个 host、内部头是不是真的被剥掉、
两套迁移是不是真的能在同一个库上共存、worker 是不是真的领得到任务。

这些恰恰是"本机全绿、上线就炸"的那一类。所以这个脚本只做一件事：
**把一条真实用户路径从头走到尾，每一步都断言得具体。**

## 无 GPU 能走到哪

    注册 -> 拿预签名 -> 直传对象存储 -> finalize -> 服务端校验摘要
      -> 文档入库 -> borndigital 解析 -> 归档 -> 出处 bbox 可取

再往后（分块索引 -> 问答 -> 视觉核对）要 embedding 与 chat 端点，
本机没有。**那一段会显式 SKIP 并说明原因** —— 不是静默跳过。
"""
import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from urllib.parse import quote
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "long-doc.pdf"

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


class Runner:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(name)
        print(f"  {GREEN}PASS{RESET} {name}  {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        print(f"  {RED}FAIL{RESET} {name}  {detail}")

    def skip(self, name: str, why: str) -> None:
        # **显式 skip 不是恒真的绿。** 每一条都要说清为什么跳过
        self.skipped.append((name, why))
        print(f"  {YELLOW}SKIP{RESET} {name}  {why}")

    def summary(self) -> int:
        print(f"\n通过 {len(self.passed)} · 失败 {len(self.failed)} · 跳过 {len(self.skipped)}")
        for name, why in self.skipped:
            print(f"  {YELLOW}跳过{RESET} {name}：{why}")
        for name, detail in self.failed:
            print(f"  {RED}失败{RESET} {name}：{detail}")
        return 1 if self.failed else 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="统一入口")
    parser.add_argument("--user", default="", help="用哪个用户名（缺省随机）")
    parser.add_argument("--password", default="e2e-correct-horse-battery")
    parser.add_argument("--with-embeddings", action="store_true",
                        help="部署里有可用的 embedding 端点，索引与问答那一段也跑")
    parser.add_argument("--timeout", type=float, default=180.0, help="等解析的上限（秒）")
    args = parser.parse_args()

    if not FIXTURE.exists():
        print(f"::error::缺少夹具 {FIXTURE}", file=sys.stderr)
        return 2

    run = Runner()
    nonce = secrets.token_hex(8)
    username = args.user or f"e2e-{nonce[:8]}"

    # **每次跑都要是一份新文档。** 文档身份是内容的 sha256，用同一份夹具的话
    # 第二次起就命中幂等复用 —— 解析不会真的跑、用量不会真的产生，
    # 而断言全部"通过"。那是一条测不到东西的绿。
    #
    # PDF 允许 %%EOF 之后有尾随字节，加一行注释就换了内容摘要，
    # 解析结果不变（页数、块数、bbox 都还是原来那些）
    content = FIXTURE.read_bytes() + f"\n% ddp-e2e {nonce}\n".encode()
    # **名字里带中文**：这个库面向中文技术手册，非 ASCII 文件名是常态。
    # 裸 UTF-8 塞进 Content-Disposition 各家浏览器解读不一，
    # 而这条路径此前一次都没被取过（见下面那组断言）
    filename = f"长文档-{nonce[:6]}.pdf"
    digest = hashlib.sha256(content).hexdigest()

    # trust_env=False：带代理变量的机器会把 127.0.0.1 也塞进代理，
    # 表现是卡住而不是报错
    async with httpx.AsyncClient(base_url=args.base, timeout=60.0, trust_env=False) as http:
        # ---------------------------------------------------------- 0 健康
        print("\n[0] 入口健康")
        try:
            health = await http.get("/healthz")
            run.ok("healthz", f"{health.status_code}")
        except httpx.HTTPError as exc:
            run.fail("healthz", f"入口不可达：{exc}（先跑 scripts/dev.sh up）")
            return run.summary()

        ready = await http.get("/readyz")
        body = ready.json()
        if ready.status_code == 200:
            run.ok("readyz", json.dumps(body.get("checks", {}), ensure_ascii=False))
        else:
            run.fail("readyz", json.dumps(body, ensure_ascii=False))

        # ---------------------------------------------------------- 1 注册
        print("\n[1] 注册与角色")
        resp = await http.post("/api/auth/register",
                               json={"username": username, "password": args.password})
        if resp.status_code != 201:
            run.fail("注册", f"{resp.status_code} {resp.text[:200]}")
            return run.summary()
        token = resp.json()["access_token"]
        user = resp.json()["user"]
        http.headers["Authorization"] = f"Bearer {token}"
        run.ok("注册", f"{username} role={user['role']}")

        me = (await http.get("/api/auth/me")).json()
        # 角色是每次回查的，不是从 token 里解的
        if me.get("role") == user["role"] and me.get("organization_id"):
            run.ok("会话与组织上下文", f"org={me['organization_id'][:8]}")
        else:
            run.fail("会话与组织上下文", json.dumps(me, ensure_ascii=False)[:200])

        # ---------------------------------------------------- 2 直传上传
        print("\n[2] 直传上传（字节流不经过应用进程）")
        created = await http.post("/api/uploads", json={
            "filename": filename, "size": len(content),
            "mime": "application/pdf", "sha256": digest,
        })
        if created.status_code != 201:
            run.fail("创建上传会话", f"{created.status_code} {created.text[:200]}")
            return run.summary()
        session = created.json()
        parts = session.get("parts") or []
        run.ok("创建上传会话", f"{len(parts)} 个分片，part_size={session['part_size']}")

        # 预签名 URL 必须是**浏览器可达**的地址，不是容器内 DNS 名。
        # 用容器名的话，签出来的 URL 只有容器里能访问 —— 而这只在真部署里才暴露
        if not parts:
            run.fail("创建上传会话返回了分片",
                     "parts 是空的 —— 后面那两条（预签名 URL 的 host、分片直传）"
                     "会各白给一个 PASS")
            return run.summary()
        if "minio:9000" in parts[0]["url"]:
            run.fail("预签名 URL 的 host",
                     "签成了容器内地址 minio:9000，浏览器访问不了"
                     "（OBJECT_PUBLIC_ENDPOINT 没配对）")
        else:
            run.ok("预签名 URL 的 host", parts[0]["url"].split("?")[0] if parts else "—")

        # 直传：**不带 Authorization** —— 预签名已经把凭证放在查询串里
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as raw:
            part_size = session["part_size"]
            uploaded = []
            for part in parts:
                start = (part["part_number"] - 1) * part_size
                chunk = content[start:start + part_size]
                put = await raw.put(part["url"], content=chunk)
                if put.status_code not in (200, 204):
                    run.fail("分片直传", f"分片 {part['part_number']} -> {put.status_code}")
                    return run.summary()
                etag = put.headers.get("ETag")
                if etag:
                    uploaded.append({"part_number": part["number"] if "number" in part
                                     else part["part_number"], "etag": etag})
        run.ok("分片直传", f"{len(parts)} 片，共 {len(content)} 字节")

        finalized = await http.post(f"/api/uploads/{session['id']}/finalize",
                                    json={"parts": uploaded or None},
                                    headers={"Idempotency-Key": session["id"]})
        if finalized.status_code != 202:
            run.fail("finalize", f"{finalized.status_code} {finalized.text[:300]}")
            return run.summary()
        state = finalized.json()["status"]
        if state == "verifying":
            run.ok("finalize", "状态是 verifying —— 摘要还没校验完，不该已经是 ready")
        else:
            run.fail("finalize", f"状态是 {state}，应当是 verifying")

        # ------------------------------------------------- 3 服务端校验摘要
        print("\n[3] 服务端流式校验摘要")
        settled = await _poll(http, f"/api/uploads/{session['id']}",
                              lambda d: d["status"] not in ("created", "uploading", "verifying"),
                              timeout=120)
        if settled and settled["status"] == "ready":
            run.ok("摘要校验", "ready")
        else:
            run.fail("摘要校验", json.dumps(settled, ensure_ascii=False)[:300])
            return run.summary()

        # -------------------------------------------------- 4 文档入库与解析
        print("\n[4] 事件消费 -> 文档入库 -> 解析")
        # **按 doc_id 找，不要拿列表第 0 个。** 列表里还有历次跑留下的文档，
        # 拿第一个的话，后面每一条断言验的都是别人那次跑的结果 ——
        # 而它们大多会"通过"
        document = await _poll(
            http, "/api/documents", lambda d: bool(d), timeout=60,
            pick=lambda docs: next((d for d in docs if d.get("doc_id") == digest), None))
        if not document:
            run.fail("文档入库", "上传通过校验之后 60 秒内没有出现文档"
                                 "（control 的 outbox 没投出去？看 corpus-api 日志）")
            return run.summary()
        run.ok("文档入库", f"doc_id={document['doc_id'][:12]}… 由 DocumentSubmitted 事件建的")

        # 文档是按 doc_id 找出来的，所以再断一次 `doc_id == digest` 是**恒真**的。
        # 真正要证的是"服务端自己算过摘要"，而不是照抄了客户端声称的那个 ——
        # 上传会话里的 verified_sha256 是服务端流式算出来的，比它
        settled_upload = (await http.get(f"/api/uploads/{session['id']}")).json()
        verified = settled_upload.get("verified_sha256")
        if verified == digest == document["doc_id"]:
            run.ok("doc_id = 服务端**自己算过**的 sha256", digest[:12] + "…")
        else:
            run.fail("doc_id = 服务端自己算过的 sha256",
                     f"verified={str(verified)[:12]}… declared={digest[:12]}… "
                     f"doc_id={document['doc_id'][:12]}… —— 三者必须一致，"
                     f"缺 verified 说明服务端根本没算")

        detail = await _poll(http, f"/api/documents/{document['id']}",
                             lambda d: d["status"] in ("succeeded", "failed"),
                             timeout=args.timeout)
        if not detail:
            run.fail("解析", f"{args.timeout:.0f} 秒内没有落终态")
            return run.summary()
        if detail["status"] != "succeeded":
            run.fail("解析", f"status={detail['status']} error={detail.get('error')}")
            return run.summary()
        # 引擎名在 **job** 上，不在 document 上（document 只有"生效状态"）。
        # 注册表驱动的部署靠它判"这份文档到底走了哪条路" ——
        # 无 GPU 档位只注册 borndigital，跑出别的名字就说明档位没切对
        jobs = (await http.get(f"/api/documents/{document['id']}/jobs")).json()
        current = next((j for j in jobs if j.get("is_current")), jobs[0] if jobs else None)
        pages = detail.get("page_count")
        if current and current.get("engine"):
            run.ok(f"解析（{current['engine']}）",
                   f"页数={pages} job={current['id'][:8]}…")
        else:
            run.fail("解析结果带引擎名",
                     f"status=succeeded 但 job 上没有 engine：{current}")

        # ---------------------------------------------------- 5 出处三件套
        print("\n[5] 版面与出处")
        pages = (await http.get(f"/api/documents/{document['id']}/pages")).json()
        blocks = [b for page in pages.get("pages", []) for b in page.get("blocks", [])]
        with_bbox = [b for b in blocks if b.get("bbox")]
        # **每一个**，不是"至少一个"。写成 `if with_bbox:` 的话
        # 10 个块里 1 个有 bbox 也会 PASS —— 而不变式 1 说的是每一条结论
        # 字段名是 `page_idx`（不是 page / page_no）。写错名字的话
        # 这条会永远报"0 个有页码"，看着像后端坏了 —— 而后端好好的
        with_page = [b for b in blocks if b.get("page_idx") is not None]
        if blocks and len(with_bbox) == len(blocks) and len(with_page) == len(blocks):
            run.ok("每个块都有页码与 bbox", f"{len(blocks)}/{len(blocks)}")
        else:
            run.fail("每个块都有页码与 bbox",
                     f"{len(blocks)} 个块里 {len(with_bbox)} 个有 bbox、"
                     f"{len(with_page)} 个有页码")

        source = (await http.get(f"/api/documents/{document['id']}/source-url")).json()
        if "/files/" in source.get("url", ""):
            run.ok("稳定文件 URL", source["url"])
        else:
            run.fail("稳定文件 URL", json.dumps(source, ensure_ascii=False)[:200])

        # 浏览器用的短期 URL 与稳定 URL **必须不同**
        short = (await http.get(f"/api/documents/{document['id']}/download-url",
                                params={"disposition": "attachment"})).json()
        if short.get("url") and short["url"] != source.get("url"):
            run.ok("短期签名 URL 与稳定 URL 分开",
                   f"supports_range={short.get('supports_range')}")
        else:
            run.fail("短期签名 URL 与稳定 URL 分开",
                     "两者相同 —— 短期 URL 会破坏 doc_hash 幂等")

        # **真的把签名 URL 取一次。** 在此之前整条 e2e 一次都没 fetch 过
        # 签名 URL —— 于是"签出来的东西到底能不能用、文件名对不对"
        # 完全没人验，而那正是直传改造最容易坏的地方。
        # 独立验收就是靠手工 curl 才发现文件名被写成了 document id。
        if short.get("url"):
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as raw:
                got = await raw.get(short["url"])
            if got.status_code == 200 and got.content == content:
                run.ok("签名 URL 取回的就是原件", f"{len(got.content)} 字节，逐字节相同")
            else:
                run.fail("签名 URL 取回的就是原件",
                         f"{got.status_code}，{len(got.content)} 字节"
                         f"（应为 {len(content)}）")

            # 文件名必须是**用户传的那个**。这条 URL 跨源，浏览器忽略
            # `<a download>` 的提示 —— 服务端签的这个说了算。
            # 签成 document id 的话，用户下到的是 `46250ceb…`（还没有扩展名）
            disposition = got.headers.get("content-disposition", "")
            if filename in disposition or quote(filename, safe="") in disposition:
                run.ok("下载的文件名是原始文件名", disposition[:80])
            else:
                run.fail("下载的文件名是原始文件名",
                         f"Content-Disposition = {disposition!r}，"
                         f"应当带上 {filename!r}")

            # Range：PDF 阅读器靠它按页取，不支持就得整份下完才能翻第一页
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as raw:
                part = await raw.get(short["url"], headers={"Range": "bytes=0-99"})
            if part.status_code == 206 and len(part.content) == 100:
                run.ok("签名 URL 支持 Range", "bytes=0-99 -> 206")
            else:
                run.fail("签名 URL 支持 Range",
                         f"{part.status_code}，{len(part.content)} 字节")

        # ------------------------------------------------------ 6 索引与问答
        print("\n[6] 索引与问答")
        if not args.with_embeddings:
            run.skip("分块索引与问答",
                     "本机没有 embedding 端点（无 GPU）。索引会显式标 "
                     "index_status=failed + index_error，问答会返回 "
                     "degraded=embedding_unavailable —— 那是**可见降级**，不是静默失败。"
                     "有 GPU 的机器上加 --with-embeddings 再跑")
            # **要等它落终态。** `indexing` 只是"还在跑"，直接判会得到一个
            # 取决于时序的结论 —— 而那种断言迟早会变成"偶尔红一下"，
            # 然后被人当成 flaky 关掉
            settled = await _poll(
                http, f"/api/documents/{document['id']}",
                lambda d: d.get("index_status") not in ("indexing", "queued"),
                timeout=args.timeout)
            status = (settled or {}).get("index_status")
            if status == "failed":
                run.ok("索引失败可见", f"index_status=failed "
                                        f"index_error={(settled.get('index_error') or '')[:60]}")
            elif status == "ready":
                run.fail("索引失败可见",
                         "没有 embedding 端点却报 ready —— 要么静默退回了别的实现，"
                         "要么这个部署其实有 embedding（那就加 --with-embeddings 跑）")
            else:
                run.fail("索引失败可见",
                         f"index_status={status} 不是终态（等了 {args.timeout:.0f} 秒）")
        else:
            indexed = await _poll(http, f"/api/documents/{document['id']}",
                                  lambda d: d["index_status"] in ("ready", "failed"),
                                  timeout=args.timeout)
            if indexed and indexed["index_status"] == "ready":
                run.ok("分块索引", "ready")
            else:
                run.fail("分块索引",
                         f"index_status={indexed and indexed['index_status']} "
                         f"error={indexed and indexed.get('index_error')}")

        # ------------------------------------------------------ 7 计量与审计
        print("\n[7] 计量与审计")
        # 缺省只看**调用者自己**的用量，所以上面那份文档必须是这次跑新传的
        usage = (await http.get("/api/usage", params={"days": 1})).json()
        points = usage.get("points") or []
        kinds = sorted({p["kind"] for p in points})
        if "parse" in kinds:
            pages = sum(p["pages"] for p in points if p["kind"] == "parse")
            run.ok("计量流水", f"{len(points)} 条，种类={kinds}，parse {pages} 页")
        else:
            run.fail("计量流水",
                     f"解析完成后没有 parse 用量（拿到 {kinds}）—— "
                     f"corpus 的 outbox 没投给 control？看 corpus_outbox 的 last_error")

        audit = await http.get("/api/audit", params={"limit": 20})
        if me.get("role") != "admin":
            # 只有 admin 能读审计。**403 在这里是正确答案** ——
            # 把它当失败会训练人忽略红色，而这条恰恰是权限矩阵在生效的证据
            if audit.status_code == 403:
                run.ok("审计日志按角色隔离", f"{me.get('role')} 读审计得到 403，符合预期")
            else:
                run.fail("审计日志按角色隔离",
                         f"{me.get('role')} 居然读到了审计：{audit.status_code}")
        elif audit.status_code == 200 and audit.json():
            actions = sorted({e["action"] for e in audit.json()})
            run.ok("审计日志", f"{len(audit.json())} 条：{actions}")
        else:
            run.fail("审计日志", f"{audit.status_code} {audit.text[:200]}")

        # ---------------------------------------------- 8 内部头不可伪造
        print("\n[8] 客户端不能伪造身份")
        forged = await http.get("/api/auth/me", headers={
            "X-DDP-Role": "admin", "X-DDP-Actor": "someone-else",
            "X-DDP-Organization": "other-org",
        })
        if forged.status_code == 200 and forged.json()["id"] == me["id"]:
            run.ok("内部头被剥掉", "伪造的 X-DDP-* 没有生效")
        else:
            run.fail("内部头被剥掉",
                     f"{forged.status_code} {forged.text[:200]} —— 客户端可能能伪造身份")

    return run.summary()


async def _poll(http: httpx.AsyncClient, path: str, done, *, timeout: float,
                interval: float = 2.0, pick=None):
    """轮询到 `done(payload)` 为真；超时返回最后一次的值（可能是 None）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        resp = await http.get(path)
        if resp.status_code == 200:
            payload = resp.json()
            last = pick(payload) if pick else payload
            if last is not None and done(last):
                return last
        await asyncio.sleep(interval)
    return last


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
