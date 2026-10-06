#!/usr/bin/env python3
"""对账报告入口（与下架入口、故障注入辅助分开落地）。

    python reconcile.py [--today YYYY-MM-DD]
                        [--require-on-shelf 1,2,3]
                        [--require-expired 4,5]
                        [--report-dir DIR]

报告会打印到 stdout，并落一份 JSON 到 DATA_DIR/reports/（可用 --report-dir 覆盖）。

退出码纪律（与成功 0 不得混用）：
    0  全部对账项一致
    2  对账失败（视图与库状态不一致 / 不满足 --require-* 终态）
    4  自身执行异常（非对账失败，别和 2 混）
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.db import db_path
from app.reconcile import reconcile_report
from app.seed import init_db

EXIT_OK = 0
EXIT_MISMATCH = 2
EXIT_ERROR = 4


def _parse_ids(s: str | None) -> list[int] | None:
    if s is None or s == "":
        return None
    return [int(x) for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today")
    ap.add_argument("--require-on-shelf", default=None)
    ap.add_argument("--require-expired", default=None)
    ap.add_argument("--report-dir", default=None)
    args = ap.parse_args(argv)

    init_db()
    try:
        report = reconcile_report(
            today=args.today,
            require_on_shelf=_parse_ids(args.require_on_shelf),
            require_expired=_parse_ids(args.require_expired),
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        return EXIT_ERROR

    report_dir = Path(args.report_dir) if args.report_dir \
        else db_path().parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    out = report_dir / f"reconcile-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK if report["ok"] else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
