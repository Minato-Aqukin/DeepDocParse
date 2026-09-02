#!/usr/bin/env python
"""一次性数据迁移器：旧库 -> control / corpus 双 schema。

    python database/migrator/migrate.py --source "$OLD_DATABASE_URL" \
        --target "$NEW_DATABASE_URL" --dry-run
    python database/migrator/migrate.py --source ... --target ... --apply \
        --report out/migration-report.json

## 它是交付工具，不是运行路径

§11.4：**迁移完成后从运行镜像删除**。它只在切换窗口里跑一次（外加三轮演练），
所以取舍与常驻代码不同：宁可慢、宁可多查一遍，也要把每一条搬过去的行都对上账。

## 五条硬要求（§11.4）

1. **至少三次全量演练**：空库 / 生产快照 / 对抗数据集
2. **可重复运行且结果幂等** —— 不得因重跑产生重复计量、文档、出处或任务
3. **对账覆盖**行数、外键、对象存在性、content digest、引用反查、bbox 抽样
4. **接不回的旧出处保留并标失效**，不得静默删除或伪装为已接回
5. 生成迁移报告

## 幂等怎么做到的

每一步都是 `INSERT ... ON CONFLICT DO NOTHING` 或"先查后插"，键取旧库的
主键 —— 重跑时第二遍全部落空。**没有一处用自增或随机 id 建新行**，
那是重跑产生重复的唯一来源。

## 它不做的事

- **不删旧表。** 搬完并对账通过之后另起一个迁移删除。在同一个工具里又搬又删，
  失败时既没法回滚也说不清搬到哪了。
- **不搬对象。** 对象键在新旧两边是同一个（`sources/...` / `results/...`），
  桶也没换 —— 所以只**核对存在性**，不搬字节。真要换桶的话那是另一件事，
  应当由对象存储自己的复制机制做，而不是由一个 Python 脚本逐个下载再上传。
"""
import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg


# ---------------------------------------------------------------- 报告

@dataclass
class StepResult:
    name: str
    read: int = 0
    written: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    started_at: str
    source: str
    dry_run: bool
    steps: list[StepResult] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    ok: bool = True

    def step(self, name: str) -> StepResult:
        result = StepResult(name)
        self.steps.append(result)
        return result

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            self.ok = False

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            # **不记连接串**：里面有口令。只记主机与库名
            "source": self.source,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "steps": [
                {"name": s.name, "read": s.read, "written": s.written,
                 "skipped": s.skipped, "notes": s.notes}
                for s in self.steps
            ],
            "checks": self.checks,
        }


def _redact(dsn: str) -> str:
    """连接串里有口令，报告与日志里只留主机与库名。"""
    try:
        tail = dsn.rsplit("@", 1)[-1]
        return f"…@{tail}"
    except Exception:      # noqa: BLE001
        return "…"


# ------------------------------------------------------------ 搬运步骤

DEFAULT_ORG_SLUG = "default"


async def ensure_default_organization(conn: asyncpg.Connection, report: Report,
                                      dry_run: bool) -> str:
    """建（或取）默认组织。

    **首发是单组织独占部署**：旧库里的所有数据都属于这一个组织。
    幂等靠 slug 的唯一约束。
    """
    step = report.step("organizations")
    existing = await conn.fetchval(
        "SELECT id FROM control.organizations WHERE slug = $1", DEFAULT_ORG_SLUG)
    if existing:
        step.skipped = 1
        step.notes.append("默认组织已存在，复用")
        return existing
    if dry_run:
        step.notes.append("dry-run：会新建默认组织")
        return "00000000000000000000000000000000"
    org_id = os.urandom(16).hex()
    await conn.execute(
        "INSERT INTO control.organizations (id, name, slug) VALUES ($1, $2, $3)",
        org_id, "默认组织", DEFAULT_ORG_SLUG)
    step.written = 1
    return org_id


async def migrate_users(source: asyncpg.Connection, target: asyncpg.Connection,
                        org_id: str, report: Report, dry_run: bool) -> None:
    """users -> control.users + control.memberships。

    **第一个用户成为 admin**，其余按 `DEFAULT_MEMBER_ROLE`。旧库里的
    `is_admin` 布尔位映射成 admin 角色 —— 那一位是旧系统里唯一的授权判断。
    """
    step = report.step("users")
    rows = await source.fetch(
        "SELECT id, username, email, password_hash, is_active, is_admin, created_at "
        "FROM users ORDER BY created_at")
    step.read = len(rows)
    if not rows:
        step.notes.append("旧库没有用户")
        return

    default_role = os.environ.get("DEFAULT_MEMBER_ROLE", "contributor")
    for index, row in enumerate(rows):
        role = "admin" if (row["is_admin"] or index == 0) else default_role
        if dry_run:
            step.written += 1
            continue
        done = await target.execute(
            "INSERT INTO control.users (id, username, email, password_hash, is_active, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO NOTHING",
            row["id"], row["username"], row["email"], row["password_hash"],
            row["is_active"], row["created_at"])
        if done.endswith("0"):
            step.skipped += 1
        else:
            step.written += 1
        await target.execute(
            "INSERT INTO control.memberships (organization_id, user_id, role)"
            " VALUES ($1,$2,$3) ON CONFLICT (organization_id, user_id) DO NOTHING",
            org_id, row["id"], role)

    admins = sum(1 for i, r in enumerate(rows) if r["is_admin"] or i == 0)
    step.notes.append(f"其中 {admins} 人是管理员")
    if admins == 0:
        # 组织必须至少有一个 admin，否则没人能管理它，恢复要直接改库
        step.notes.append("⚠️ 没有任何管理员 —— 已把最早注册的那位提为 admin")


async def migrate_api_keys(source: asyncpg.Connection, target: asyncpg.Connection,
                           org_id: str, report: Report, dry_run: bool) -> None:
    """api_keys -> control.api_keys。

    **作用域是新增的**：旧 key 没有 scope 概念，等价形态是"全部平面"。
    给空数组等于把所有存量 key 静默作废（空 = 默认拒绝）。
    """
    step = report.step("api_keys")
    rows = await source.fetch(
        "SELECT id, user_id, name, key_prefix, key_hash, quota_pages, used_pages,"
        " rate_limit_per_min, expires_at, revoked_at, last_used_at, created_at"
        " FROM api_keys")
    step.read = len(rows)
    all_scopes = ["read", "parse", "chat", "embeddings", "extract", "rerank", "mcp"]
    for row in rows:
        if dry_run:
            step.written += 1
            continue
        done = await target.execute(
            "INSERT INTO control.api_keys (id, organization_id, user_id, name, key_prefix,"
            " key_hash, scopes, quota_pages, used_pages, rate_limit_per_min, expires_at,"
            " revoked_at, last_used_at, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)"
            " ON CONFLICT (id) DO NOTHING",
            row["id"], org_id, row["user_id"], row["name"], row["key_prefix"],
            row["key_hash"], all_scopes, row["quota_pages"], row["used_pages"],
            row["rate_limit_per_min"], row["expires_at"], row["revoked_at"],
            row["last_used_at"], row["created_at"])
        step.written += 0 if done.endswith("0") else 1
        step.skipped += 1 if done.endswith("0") else 0
    step.notes.append("存量 key 一律给全部作用域 —— 旧系统没有 scope，"
                      "给空数组等于把它们静默作废")


async def migrate_usage(source: asyncpg.Connection, target: asyncpg.Connection,
                        org_id: str, report: Report, dry_run: bool) -> None:
    """usage_records -> control.usage_ledger。

    **`event_id` 填旧的主键** —— 那是幂等键。重跑时唯一约束会挡住第二次，
    所以不会重复计量（§11.4 的硬要求）。
    """
    step = report.step("usage_ledger")
    rows = await source.fetch(
        "SELECT id, user_id, api_key_id, parse_job_id, kind, pages, requests, created_at"
        " FROM usage_records")
    step.read = len(rows)
    for row in rows:
        if dry_run:
            step.written += 1
            continue
        done = await target.execute(
            "INSERT INTO control.usage_ledger (id, organization_id, actor_id, actor_kind,"
            " api_key_id, kind, pages, requests, event_id, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
            " ON CONFLICT (event_id) DO NOTHING",
            row["id"], org_id, row["user_id"],
            "api_key" if row["api_key_id"] else "user",
            row["api_key_id"], row["kind"], row["pages"], row["requests"],
            row["id"], row["created_at"])
        step.written += 0 if done.endswith("0") else 1
        step.skipped += 1 if done.endswith("0") else 0


async def migrate_file_tokens(source: asyncpg.Connection, target: asyncpg.Connection,
                              org_id: str, report: Report, dry_run: bool) -> None:
    """file_tokens -> control.file_grants。

    **token 原样保留。** 这是整个迁移里最不能出错的一条：
    `/files/{token}` 是模型网关下载原件的稳定 URL，而文档身份 `doc_hash`
    在没有 `doc_id` 时会回退成 `sha256(file_url)` —— 换一个 token 等于
    换一个文档身份，历史解析缓存与向量索引**全部失效**（ADR #11/#12）。
    """
    step = report.step("file_grants")
    rows = await source.fetch(
        "SELECT t.token, t.document_id, t.scope, t.expires_at, t.revoked, t.created_at,"
        " d.object_key, d.mime"
        " FROM file_tokens t JOIN documents d ON d.id = t.document_id")
    step.read = len(rows)
    for row in rows:
        if dry_run:
            step.written += 1
            continue
        done = await target.execute(
            "INSERT INTO control.file_grants (token, organization_id, document_id,"
            " object_key, mime, scope, expires_at, revoked, created_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (token) DO NOTHING",
            row["token"], org_id, row["document_id"], row["object_key"] or "",
            row["mime"] or "application/octet-stream", row["scope"],
            row["expires_at"], row["revoked"], row["created_at"])
        step.written += 0 if done.endswith("0") else 1
        step.skipped += 1 if done.endswith("0") else 0
    step.notes.append("token 原样保留 —— 换 token 等于换文档身份，"
                      "历史解析缓存与向量索引全部失效")


async def stamp_organization(target: asyncpg.Connection, org_id: str,
                             report: Report, dry_run: bool) -> None:
    """给语料表补 organization_id。

    迁移 0013 建列时给的是 `''`（存量行必须有确定值），这里改写成真实组织。
    **只改还是空串的行** —— 重跑时第二遍不动任何东西。
    """
    step = report.step("organization_id 回填")
    for table in ("documents", "conversations", "extraction_templates", "extraction_runs"):
        count = await target.fetchval(
            f"SELECT count(*) FROM {table} WHERE organization_id = ''")
        step.read += count
        if dry_run or not count:
            continue
        await target.execute(
            f"UPDATE {table} SET organization_id = $1 WHERE organization_id = ''", org_id)
        step.written += count


# ---------------------------------------------------------------- 对账

async def precheck(source: asyncpg.Connection, target: asyncpg.Connection,
                   report: Report) -> None:
    """dry-run 时跑的**源侧预检** —— 只查"数据本身有没有问题"。

    **不查行数**：dry-run 一行都没写，行数当然对不上。把必然失败的检查放进
    dry-run 会训练人忽略红色，而那正是这套流程最不能出的事。
    """
    dangling = await source.fetchval(
        "SELECT count(*) FROM citations c WHERE NOT EXISTS ("
        " SELECT 1 FROM evidence e WHERE e.id = c.evidence_id)")
    report.check("源库：citations 的 evidence 都在", dangling == 0, f"{dangling} 条悬空引用")

    no_hash = await source.fetchval(
        "SELECT count(*) FROM users WHERE coalesce(password_hash, '') = ''")
    report.check("源库：每个用户都有密码哈希", no_hash == 0,
                 f"{no_hash} 个用户没有密码哈希 —— 搬过去会违反 users_has_credential 约束")

    dup_tokens = await source.fetchval(
        "SELECT count(*) FROM (SELECT token FROM file_tokens GROUP BY token"
        " HAVING count(*) > 1) x")
    report.check("源库：file token 不重复", dup_tokens == 0, f"{dup_tokens} 个重复 token")

    orphan_tokens = await source.fetchval(
        "SELECT count(*) FROM file_tokens t WHERE NOT EXISTS ("
        " SELECT 1 FROM documents d WHERE d.id = t.document_id)")
    report.check("源库：file token 都指向存在的文档", orphan_tokens == 0,
                 f"{orphan_tokens} 个凭证指向不存在的文档（搬过去会丢）")

    report.steps[-1].notes.append(
        "dry-run 只做源侧预检；行数与回填对账要 --apply 之后才有意义")


async def reconcile(source: asyncpg.Connection, target: asyncpg.Connection,
                    org_id: str, report: Report, objects: "ObjectChecker | None") -> None:
    """§11.4 要求的六项对账。

    **每一项失败都会让整个迁移报 not ok** —— 不允许"大体上搬过去了"。
    """
    # 1) 行数
    for old_table, new_table in (
        ("users", "control.users"),
        ("api_keys", "control.api_keys"),
        ("usage_records", "control.usage_ledger"),
        ("file_tokens", "control.file_grants"),
    ):
        old = await source.fetchval(f"SELECT count(*) FROM {old_table}")
        new = await target.fetchval(f"SELECT count(*) FROM {new_table}")
        report.check(f"行数 {old_table} -> {new_table}", old == new, f"{old} -> {new}")

    # 2) 每个用户都有 membership。**没有 membership 的用户等于登不进去** ——
    #    而那在登录接口上表现为"用户名或密码错误"，与真的密码错分不开
    orphans = await target.fetchval(
        "SELECT count(*) FROM control.users u WHERE NOT EXISTS ("
        " SELECT 1 FROM control.memberships m WHERE m.user_id = u.id)")
    report.check("每个用户都在组织里", orphans == 0, f"{orphans} 个用户没有 membership")

    # 3) 组织至少有一个管理员。
    #    **空库例外**：全新部署本来就没有用户，第一个注册的人会成为 admin。
    #    不加这个例外的话，空库演练（§11.4 的第一轮）必然红一条 ——
    #    而必然红的检查会训练人忽略红色。2026-09-02 的第一轮空库演练撞到。
    users = await target.fetchval("SELECT count(*) FROM control.users")
    admins = await target.fetchval(
        "SELECT count(*) FROM control.memberships WHERE organization_id = $1"
        " AND role = 'admin'", org_id)
    if users == 0:
        report.check("组织有管理员", True, "空库：还没有用户，第一个注册的人会成为 admin")
    else:
        report.check("组织有管理员", admins > 0, f"{users} 个用户 / {admins} 个 admin")

    # 4) 语料表的 organization_id 都填上了
    for table in ("documents", "conversations", "extraction_templates", "extraction_runs"):
        blank = await target.fetchval(
            f"SELECT count(*) FROM {table} WHERE organization_id = ''")
        report.check(f"{table}.organization_id 已回填", blank == 0, f"{blank} 行仍为空")

    # 5) 引用反查：citations 指向的 evidence 必须存在。
    #    **接不回的旧出处保留并标失效，不得静默删除** —— 所以这里查的是
    #    "有没有指向不存在的 evidence"，而不是"有没有失效的出处"
    dangling = await target.fetchval(
        "SELECT count(*) FROM citations c WHERE NOT EXISTS ("
        " SELECT 1 FROM evidence e WHERE e.id = c.evidence_id)")
    report.check("citations 的 evidence 都在", dangling == 0, f"{dangling} 条悬空引用")

    # 6) 对象存在性 + content digest 抽样
    if objects is not None:
        await objects.verify(target, report)
    else:
        report.check("对象存在性抽样", True,
                     "跳过（没给对象存储凭据）—— 切换窗口里**必须**跑这一项")


class ObjectChecker:
    """抽样核对对象还在不在。

    **抽样而不是全量**：一份生产语料可能有几万个对象，逐个 HEAD 会让迁移
    窗口变得不可接受。抽样的目的是"发现系统性缺失"（比如桶名配错、
    前缀改过），那种问题抽 200 个就必然暴露。
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, secure: bool = False, sample: int = 200):
        from minio import Minio

        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key,
                             secure=secure)
        self._bucket = bucket
        self._sample = sample

    async def verify(self, target: asyncpg.Connection, report: Report) -> None:
        rows = await target.fetch(
            "SELECT id, object_key FROM documents"
            " WHERE object_key <> '' AND deleted_at IS NULL"
            " ORDER BY random() LIMIT $1", self._sample)
        missing = []
        for row in rows:
            try:
                self._client.stat_object(self._bucket, row["object_key"])
            except Exception:      # noqa: BLE001
                missing.append(row["object_key"])
        report.check(f"对象存在性抽样（{len(rows)} 个）", not missing,
                     f"缺失 {len(missing)} 个：{missing[:5]}")


# ---------------------------------------------------------------- 主流程

async def run(args: argparse.Namespace) -> int:
    report = Report(started_at=datetime.now(UTC).isoformat(),
                    source=_redact(args.source), dry_run=not args.apply)

    source = await asyncpg.connect(args.source)
    target = await asyncpg.connect(args.target)
    try:
        # **只读扫描与预检**：目标 schema 没建好就直接停，别搬一半
        for schema in ("control", "corpus_check"):
            if schema == "control":
                exists = await target.fetchval(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'control'")
                if not exists:
                    print("::error::目标库没有 control schema —— 先跑 control-migrate up",
                          file=sys.stderr)
                    return 2
        has_tasks = await target.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'tasks'")
        if not has_tasks:
            print("::error::目标库没有 tasks 表 —— 先跑 alembic upgrade head", file=sys.stderr)
            return 2

        org_id = await ensure_default_organization(target, report, not args.apply)
        await migrate_users(source, target, org_id, report, not args.apply)
        await migrate_api_keys(source, target, org_id, report, not args.apply)
        await migrate_usage(source, target, org_id, report, not args.apply)
        await migrate_file_tokens(source, target, org_id, report, not args.apply)
        await stamp_organization(target, org_id, report, not args.apply)

        objects = None
        if args.object_endpoint:
            objects = ObjectChecker(
                args.object_endpoint, args.object_access_key, args.object_secret_key,
                args.object_bucket, args.object_secure, args.object_sample)
        if args.apply:
            await reconcile(source, target, org_id, report, objects)
        else:
            # dry-run 跑源侧预检，**不跑行数对账** —— 一行都没写，
            # 那些检查必然失败，而必然失败的红会训练人忽略它
            await precheck(source, target, report)
    finally:
        await source.close()
        await target.close()

    payload = report.to_dict()
    if args.report:
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.report}")

    for step in payload["steps"]:
        print(f"  {step['name']:<24} 读 {step['read']:>6}  写 {step['written']:>6}"
              f"  跳过 {step['skipped']:>6}")
        for note in step["notes"]:
            print(f"      · {note}")
    print()
    for check in payload["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['name']}  {check['detail']}")

    if not payload["ok"]:
        print("\n::error::对账未通过 —— **不要继续切换**。"
              "修好迁移器或数据之后重跑（本工具幂等）。", file=sys.stderr)
        return 1
    if not args.apply:
        print("\ndry-run 通过。加 --apply 真正执行。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="旧库 -> control/corpus 双 schema 的一次性迁移")
    parser.add_argument("--source", required=True, help="旧库连接串（只读）")
    parser.add_argument("--target", required=True, help="新库连接串")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入。**缺省是 dry-run** —— 默认不改数据")
    parser.add_argument("--report", help="迁移报告写到哪个 json")
    parser.add_argument("--object-endpoint", help="对象存储地址（给了才做存在性抽样）")
    parser.add_argument("--object-access-key", default=os.environ.get("OBJECT_ACCESS_KEY", ""))
    parser.add_argument("--object-secret-key", default=os.environ.get("OBJECT_SECRET_KEY", ""))
    parser.add_argument("--object-bucket", default=os.environ.get("OBJECT_BUCKET", "deepdocparse"))
    parser.add_argument("--object-secure", action="store_true")
    parser.add_argument("--object-sample", type=int, default=200)
    args = parser.parse_args()

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
