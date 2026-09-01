#!/usr/bin/env python
"""把人工驳回标注导出成固定 JSONL 评测集；重复执行结果幂等。"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.db import get_sessionmaker  # noqa: E402
from app.review_export import export_reviews  # noqa: E402

DEFAULT_OUTPUT = ROOT / "eval" / "reviewed-knowledge.jsonl"


async def run(output: Path) -> int:
    async with get_sessionmaker()() as session:
        count, revision = await export_reviews(session, output)
    print(f"exported={count} revision={revision} output={output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return asyncio.run(run(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
