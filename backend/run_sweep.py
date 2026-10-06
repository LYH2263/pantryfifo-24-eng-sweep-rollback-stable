#!/usr/bin/env python3
"""下架入口（与故障注入辅助、对账报告分开落地）。

    python run_sweep.py                # 跑一次过期下架
    PANTRY_FAULT_POINT=expire_sweep:before_commit python run_sweep.py

退出码纪律（不得与成功 0 混用）：
    0  成功提交（含第二次空跑：名单为空、不改任何已 expired 行）
    3  注入失败，事务已整场回滚（在架集合与开跑前完全一致）
    4  其它未预期错误
"""
import json
import sys

from app.engines.fault import FaultInjected
from app.ops import run_expire_sweep
from app.seed import init_db

EXIT_OK = 0
EXIT_FAULT_ROLLED_BACK = 3
EXIT_UNEXPECTED = 4


def main() -> int:
    init_db()
    try:
        result = run_expire_sweep()
    except FaultInjected as e:
        print(json.dumps({
            "ok": False,
            "rolled_back": True,
            "exit_code": EXIT_FAULT_ROLLED_BACK,
            "fault": str(e),
        }, ensure_ascii=False, indent=2))
        return EXIT_FAULT_ROLLED_BACK
    except Exception as e:  # 未预期错误：不冒充成功
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        return EXIT_UNEXPECTED
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
