#!/usr/bin/env python
"""删掉搬走之后剩下的四张旧账号表。

    python database/migrator/drop_legacy_account_tables.py --database <dsn>          # 只检查
    python database/migrator/drop_legacy_account_tables.py --database <dsn> --apply  # 真删

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
2. 每一张的行数都 <= 对应新表的行数（搬漏了就不许删）
3. `control` schema 存在且四张新表都在

任何一条不满足就拒绝，并说清是哪一条。**不提供 --force。**
真要跳过检查，说明前提没成立，那时候要做的是查清楚而不是绕过。
"""
import argparse
import asyncio
import sys

import asyncpg

#: 旧表 -> (新表, 说明)。**新表的行数只会多不会少**：
#: control 侧还会有迁移之后新建的用户/key/用量。
PAIRS = {
    "users": ("control.users", "用户"),
    "api_keys": ("control.api_keys", "API key"),
    "usage_records": ("control.usage_ledger", "计量流水"),
    "file_tokens": ("control.file_grants", "文件凭证"),
}

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, help="目标库连接串")
    parser.add_argument("--apply", action="store_true",
                        help="真的 DROP。**不给这个参数就只检查**")
    args = parser.parse_args()

    conn = await asyncpg.connect(args.database)
    try:
        blockers: list[str] = []

        has_control = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'control'")
        if not has_control:
            print(f"{RED}没有 control schema —— 这个库根本没迁过{RESET}", file=sys.stderr)
            return 2

        present = []
        for old, (new, label) in PAIRS.items():
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
            new_count = await conn.fetchval(f"SELECT count(*) FROM {new}")
            if new_count < old_count:
                blockers.append(
                    f"{label}：旧表 {old_count} 行，新表 {new} 只有 {new_count} 行 —— 搬漏了")
            else:
                print(f"  {GREEN}OK{RESET} {old} {old_count} 行 -> {new} {new_count} 行")

        if not present:
            print("四张旧表都已经不在了，没什么可做的。")
            return 0

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

        # CASCADE 是必要的：旧表之间有外键（api_keys.user_id -> users.id）。
        # 但**逐张单独 DROP** 而不是一条语句 —— 出错时能看出是哪一张
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
