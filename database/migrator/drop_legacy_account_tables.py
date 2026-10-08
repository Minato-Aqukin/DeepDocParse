#!/usr/bin/env python
"""删掉搬走之后剩下的四张旧账号表。

    TARGET_DATABASE_URL=<dsn> python database/migrator/drop_legacy_account_tables.py  # 只检查
    TARGET_DATABASE_URL=<dsn> python database/migrator/drop_legacy_account_tables.py --apply \
        --approval out/drop-approval.json --reconcile-report out/migration-report.json  # 真删

## 为什么这不是一个 alembic 迁移

放进迁移链的话，**任何一次 `alembic upgrade head` 都会执行它** ——
包括在对账通过之前、包括在一台刚从生产快照恢复出来的库上。
而这是本仓库唯一一处不可逆地丢数据的地方（`gc.py` 至少还有宽限期）。

所以它是一个**要人手动跑的脚本**，而且默认只检查不动手。
`MIGRATION-DRILLS.md` 里说的"搬完并对账通过之后要另起一个"，就是这个。

## 它删什么

    users            -> control.users
    api_keys         -> control.api_keys
    usage_records    -> control.usage_ledger
    file_tokens      -> control.file_grants

四张都由 `migrate.py` 搬过，而**迁移器有意不删** ——
搬完立刻删的话，一旦对账发现问题就没有回头路了。

## 它在删之前查什么

1. 四张表在**代码里已经没有任何引用**（静态判据在 tests/，这里只查库）
2. **每一行都能在新表里按主键找到**（不是比行数 —— 新表还有迁移之后新建的
   用户与 key，行数只会多不会少，一次漏搬照样能"通过"）
3. `control` schema 存在且四张新表都在
4. **`CASCADE` 不会连带删掉这四张表之外的东西**

第 4 条是验收在真库上照出来的：`DROP TABLE users CASCADE` 会顺手删掉
`conversations` / `extraction_templates` / `extraction_runs` 三张**活语料表**
上的外键约束，而脚本只会打印"四张旧账号表已删除"。
用一次不可逆的 DROP 顺手改掉别的表的 schema，还不告诉任何人 ——
这正是这个脚本最不该做的事。

（那三条外键本身是迁移 0013 的缺陷，已由 `0014_drop_legacy_user_fkeys.py`
按 `pg_constraint` 查名删掉。这里的检查是防**下一次**。）

任何一条不满足就拒绝，并说清是哪一条。**不提供 --force。**
真要跳过检查，说明前提没成立，那时候要做的是查清楚而不是绕过。

## --apply 还要两样东西：审批文件与对账报告

`--apply` 除了上面的行级检查，还要求两个参数：

    --approval out/drop-approval.json --reconcile-report out/migration-report.json

审批文件是 JSON，恰好三个键，一个不能多一个不能少：

    {"compat_window_expired_on": "2026-10-01",
     "approved_by": "alice（第二审批人）",
     "reconcile_report_sha256": "<migration-report.json 的 sha256>"}

- `compat_window_expired_on`：兼容窗口过期日（YYYY-MM-DD），必须是今天或过去 ——
  窗口没过就删表，等于把回滚路提前烧掉；
- `approved_by`：第二位人类审批人签字，必须是非空字符串 ——
  动手的人不能给自己批，这里的"第二"靠换人执行保证，脚本只强制非空；
- `reconcile_report_sha256`：`--reconcile-report` 指向的那份报告文件的 sha256 ——
  审批必须绑定某一次具体的对账，换一份报告就要重新批；
  那份报告的 `ok` 必须为 true（migrate.py 报告的形状），没通过的对账不能删表。

为什么：这是本仓库唯一一处不可逆丢数据的地方。行数对上只能证明"搬对了"，
证明不了"现在可以删" —— 删表还需要两个独立证据：兼容窗口已过（回滚期结束），
以及一位没动手的人看过对账报告并签字。而 sha 绑定保证他签字的就是你指的那份报告。

"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime

import asyncpg

#: 旧表 -> (新表, 主键列, 说明)。
#:
#: **判据是"每一行都搬过去了"，不是"新表行数更多"。** 后者证明不了任何事：
#: 新表还有迁移之后新建的用户与 key，行数只会多不会少，
#: 一次漏搬照样能"通过"（这个库上四张旧表都是 0 行，比行数等于什么都没验）。
#:
#: 迁移器保留了主键（`users`/`api_keys`/`usage_ledger` 用源 `id`，
#: `file_grants` 用源 `token`），所以逐行核对是现成的。
PAIRS = {
    "users": ("control.users", "id", "id", "用户"),
    "api_keys": ("control.api_keys", "id", "id", "API key"),
    "usage_records": ("control.usage_ledger", "id", "id", "计量流水"),
    "file_tokens": ("control.file_grants", "token", "token", "文件凭证"),
}

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


#: 审批文件的三个键，一个不能多一个不能少。
APPROVAL_KEYS = frozenset({
    "compat_window_expired_on", "approved_by", "reconcile_report_sha256"})


def _check_approval(approval_path: str, report_path: str) -> str | None:
    """审掉 --apply 的第二道闸：返回 None 表示放行，否则返回拒绝理由。"""
    try:
        with open(approval_path, encoding="utf-8") as fh:
            approval = json.load(fh)
    except FileNotFoundError:
        return f"审批文件不存在：{approval_path}"
    except (OSError, ValueError) as exc:
        return f"审批文件读不出来：{approval_path}（{exc}）"
    if not isinstance(approval, dict) or set(approval) != APPROVAL_KEYS:
        return ("审批文件必须是恰好这三个键的 JSON："
                "compat_window_expired_on / approved_by / reconcile_report_sha256")
    try:
        expired_on = datetime.strptime(
            approval["compat_window_expired_on"], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "compat_window_expired_on 必须是 YYYY-MM-DD 日期"
    if expired_on > date.today():
        return (f"兼容窗口还没过（compat_window_expired_on="
                f"{approval['compat_window_expired_on']}）—— "
                "窗口期内删表等于提前烧掉回滚路")
    approved_by = approval["approved_by"]
    if not isinstance(approved_by, str) or not approved_by.strip():
        return "approved_by 必须是非空字符串 —— 动手的人不能给自己批，要第二位人类审批人签字"
    try:
        with open(report_path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError as exc:
        return f"对账报告读不出来：{report_path}（{exc}）"
    want = approval["reconcile_report_sha256"]
    if not isinstance(want, str) or digest != want.strip().lower():
        return ("审批绑定的对账报告对不上（reconcile_report_sha256 不一致）—— "
                "换一份报告就要重新批")
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"对账报告不是合法 JSON：{report_path}（{exc}）"
    if not isinstance(report, dict) or report.get("ok") is not True:
        return "对账报告的 ok 不是 true —— 没通过的对账不能删表"
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=False,
                        default=os.environ.get("TARGET_DATABASE_URL", ""),
                        help="被迁移过的库连接串。优先用环境变量 TARGET_DATABASE_URL —— "
                             "argv 在 ps 与 CI 日志里可见，连接串里有口令")
    parser.add_argument("--apply", action="store_true",
                        help="真的 DROP。**不给这个参数就只检查**")
    parser.add_argument("--approval", default="",
                        help="审批文件路径（JSON）。--apply 时必填")
    parser.add_argument("--reconcile-report", default="",
                        help="migrate.py 生成的对账报告路径。--apply 时必填，审批绑定到这份报告")
    args = parser.parse_args()
    if not args.database:
        parser.error("缺 --database：请 export TARGET_DATABASE_URL 再跑"
                     "（连接串里有口令，不要写进命令行或 CI 日志）")

    conn = await asyncpg.connect(args.database)
    try:
        blockers: list[str] = []

        has_control = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'control'")
        if not has_control:
            print(f"{RED}没有 control schema —— 这个库根本没迁过{RESET}", file=sys.stderr)
            return 2

        present = []
        for old, (new, old_key, new_key, label) in PAIRS.items():
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = $1", old)
            if not exists:
                print(f"  {YELLOW}跳过{RESET} {old}：已经不在了")
                continue
            present.append(old)

            new_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_schema = 'control' AND table_name = $1", new.split(".", 1)[1])
            if not new_exists:
                blockers.append(f"{old} 还在，而 {new} 不存在 —— 没搬过")
                continue

            old_count = await conn.fetchval(f'SELECT count(*) FROM public."{old}"')
            # **逐行核对，不比行数。** 旧表里的每一个主键都要能在新表里找到
            missing = await conn.fetchval(
                f'SELECT count(*) FROM public."{old}" o'
                f" WHERE NOT EXISTS (SELECT 1 FROM {new} n"
                f'                   WHERE n.{new_key} = o."{old_key}")')
            if missing:
                samples = await conn.fetch(
                    f'SELECT o."{old_key}" AS k FROM public."{old}" o'
                    f" WHERE NOT EXISTS (SELECT 1 FROM {new} n"
                    f'                   WHERE n.{new_key} = o."{old_key}") LIMIT 5')
                blockers.append(
                    f"{label}：旧表 {old_count} 行里有 {missing} 行在 {new} 里找不到 —— "
                    f"搬漏了。样例：{[r['k'] for r in samples]}")
            else:
                print(f"  {GREEN}OK{RESET} {old} {old_count} 行，每一行都能在 {new} 里找到")

        if not present:
            print("四张旧表都已经不在了，没什么可做的。")
            return 0

        # **CASCADE 会顺手删掉什么，必须先说清楚。**
        # 验收实测：`DROP TABLE users CASCADE` 会连带删掉三张活语料表上的
        # 外键约束，而脚本只会打印"已删除" —— 用一次不可逆的 DROP
        # 改掉别的表的 schema，还不告诉任何人。
        outside = await conn.fetch(
            """
            SELECT c.conname, c.conrelid::regclass::text AS tbl,
                   c.confrelid::regclass::text AS refs
            FROM pg_constraint c
            WHERE c.contype = 'f'
              AND c.confrelid::regclass::text = ANY($1::text[])
              AND c.conrelid::regclass::text <> ALL($1::text[])
            ORDER BY 1
            """, present)
        if outside:
            for row in outside:
                blockers.append(
                    f"CASCADE 会连带删掉 {row['tbl']} 上的 {row['conname']}"
                    f"（它指向 {row['refs']}）—— 那张表不在要删的四张里")

        if blockers:
            for line in blockers:
                print(f"{RED}拒绝删除{RESET}：{line}", file=sys.stderr)
            print("\n先把上面的问题查清楚。**这个脚本没有 --force** —— "
                  "前提不成立的时候要做的是查清楚，不是绕过。", file=sys.stderr)
            return 1

        if not args.apply:
            print(f"\n检查通过。可以删的表：{present}")
            print("加 --apply 真正执行。**这一步不可逆**，"
                  "执行前请确认已有当天的 pg_dump。")
            return 0

        # --apply 的第二道闸：审批 + 对账报告绑定。放在任何 DROP 之前 ——
        # 上面的行级检查证明"搬对了"，这里证明"现在可以删"。
        if not args.approval or not args.reconcile_report:
            print("::error::--apply 必须同时给 --approval 与 --reconcile-report —— "
                  "不可逆的 DROP 需要窗口过期 + 独立审批证据，并绑定一次通过的对账报告",
                  file=sys.stderr)
            return 2
        refusal = _check_approval(args.approval, args.reconcile_report)
        if refusal:
            print(f"{RED}拒绝删除{RESET}：{refusal}", file=sys.stderr)
            print("先把上面的问题查清楚。**这个脚本没有 --force** —— "
                  "前提不成立的时候要做的是查清楚，不是绕过。", file=sys.stderr)
            return 1

        # 走到这里说明上面那条依赖检查已经确认：CASCADE 只会碰这四张表
        # 彼此之间的外键（`api_keys.user_id -> users.id`、
        # `usage_records.user_id -> users.id`），不会碰别人。
        # 逐张单独 DROP 而不是一条语句 —— 出错时能看出是哪一张
        async with conn.transaction():
            for old in present:
                await conn.execute(f'DROP TABLE public."{old}" CASCADE')
                print(f"  {GREEN}已删除{RESET} {old}")
        print("\n四张旧账号表已删除。")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
