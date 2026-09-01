#!/usr/bin/env python
"""control 迁移文件的两处副本必须一致。

    python scripts/check_control_migrations.py

## 为什么会有两份

Go 的 `//go:embed` **不能跨越模块根目录**向上取文件，而迁移的权威位置
应该与 corpus 侧并列在 `database/` 下（两套迁移各管各的 schema，
是"一个数据对象只能有一个写入所有者"的组织形式）。

所以 `services/control-api/internal/migrate/sql/` 是一份复制品。
复制品靠人同步迟早会漂，而漂开的表现是**改了 database/control 但服务跑的是旧 DDL**
—— 代码读起来是新的，库里是旧的。这把尺子就是那个同步器。
"""
import hashlib
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "database" / "control"
MIRROR = ROOT / "services" / "control-api" / "internal" / "migrate" / "sql"


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    fix = "--fix" in sys.argv
    source = {p.name: p for p in sorted(SOURCE.glob("*.sql"))}
    mirror = {p.name: p for p in sorted(MIRROR.glob("*.sql"))}

    if not source:
        print("::error::database/control 下一个 .sql 都没有 —— 这把尺子等于没有")
        return 1

    problems = []
    for name, src in source.items():
        dst = mirror.get(name)
        if dst is None or digest(dst) != digest(src):
            if fix:
                shutil.copyfile(src, MIRROR / name)
                print(f"已同步 {name}")
            else:
                problems.append(f"{name} 在 internal/migrate/sql 里缺失或内容不同")
    for name in mirror.keys() - source.keys():
        if fix:
            (MIRROR / name).unlink()
            print(f"已删除多余的 {name}")
        else:
            problems.append(f"internal/migrate/sql/{name} 在 database/control 里不存在")

    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n跑 `python scripts/check_control_migrations.py --fix` 同步。",
              file=sys.stderr)
        return 1
    print(f"control 迁移同步：{len(source)} 个文件一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
