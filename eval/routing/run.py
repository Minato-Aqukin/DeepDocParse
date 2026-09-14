#!/usr/bin/env python
"""P6 路由/覆盖评测入口。

    cd eval && ../.venv/bin/python -m routing.run            # 跑冻结夹具并写报告
    cd eval && ../.venv/bin/python -m routing.run --rebuild  # 先按构造器重建夹具

诚实性违规时退出码非 0（同时不写出"看起来正常"的报告）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import dataset as dataset_module
from . import harness, report as report_module
from .dataset import FIXTURE_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P6 routing/coverage eval harness")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--report", type=Path, default=None,
                        help="report path (default: eval/reports/routing-<digest>.json)")
    parser.add_argument("--rebuild", action="store_true",
                        help="rebuild the frozen fixture from dataset.build_dataset()")
    parser.add_argument("--no-write", action="store_true",
                        help="print the summary but do not write a report file")
    args = parser.parse_args(argv)

    if args.rebuild:
        dataset_module.freeze(args.fixture)
    dataset = dataset_module.load_frozen(args.fixture)
    try:
        runs = [harness.run_question(dataset, question, mode)
                for question in dataset["questions"]
                for mode in ("fast", "exhaustive_scope")]
    except harness.CoverageHonestyError as exc:
        print(f"coverage honesty violation, refusing to report: {exc}", file=sys.stderr)
        return 2
    report = report_module.evaluate(dataset, runs)
    print(report["summary_markdown"])
    if not args.no_write:
        path = args.report or report_module.default_report_path(dataset)
        report_module.write_report(report, path)
        print(f"report written to {path}")
    return 1 if report["honesty"]["violation_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
