"""事务/回滚/并发/双跑/退出码 的后端测试（独立临时库，不污染业务库）。"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PANTRY_FAULT_POINT", raising=False)
    monkeypatch.delenv("PANTRY_SWEEP_HOLD_SECONDS", raising=False)
    from app import seed
    seed.init_db()
    return tmp_path


def _ids(rows):
    return sorted(r["id"] for r in rows)


def test_sweep_is_all_or_nothing_on_fault(tmp_data):
    from app.engines.fault import FaultInjected
    from app.ops import lot_status_snapshot, run_expire_sweep

    before = lot_status_snapshot()
    os.environ["PANTRY_FAULT_POINT"] = "expire_sweep:before_commit"
    with pytest.raises(FaultInjected):
        run_expire_sweep("2026-10-06")
    after = lot_status_snapshot()
    # 整场回滚：没有任何批状态发生变化（禁止顶条已无、全层仍在的半提交）
    assert before == after
    assert "PANTRY_FAULT_POINT" not in os.environ  # 一次性自毁


def test_double_sweep_second_is_empty_and_idempotent(tmp_data):
    from app.db import read_conn
    from app.ops import run_expire_sweep

    r1 = run_expire_sweep("2026-10-06")
    # 种子候选：1(2026-10-01)、2(2026-09-28)、4(2025-01-01)；
    # 3 于 2026-11-01 到期不在列，5 为负余量脏数据被 qty_remain>0 挡掉。
    assert sorted(r1["expired_ids"]) == [1, 2, 4]
    assert r1["touched_rows"] == 3
    with read_conn() as c:
        expired_rows_1 = {r["id"]: dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE status='expired'")}

    r2 = run_expire_sweep("2026-10-06")
    assert r2["expired_ids"] == []
    assert r2["touched_rows"] == 0
    with read_conn() as c:
        expired_rows_2 = {r["id"]: dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE status='expired'")}
    assert expired_rows_1 == expired_rows_2  # 已 expired 行未被二次改动


def test_concurrent_consume_waits_and_never_hits_half_expired(tmp_data):
    from app.engines.fault import FaultInjected
    from app.ops import run_consume, run_expire_sweep

    os.environ["PANTRY_FAULT_POINT"] = "expire_sweep:before_commit"
    os.environ["PANTRY_SWEEP_HOLD_SECONDS"] = "2"
    err = []

    def sweep():
        try:
            run_expire_sweep("2026-10-06")
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=sweep)
    t.start()
    threading.Event().wait(0.6)

    # 下架写锁在握时消费只能阻塞等待；回滚释放锁后，1 号批仍是在架的，
    # 消费成功扣的是“在架批”，而不是半 expired 批。
    result = run_consume(1, 0.5, note="concurrent")
    t.join(timeout=15)
    assert len(err) == 1 and isinstance(err[0], FaultInjected)
    assert result["ok"]
    assert {d["lot_id"] for d in result["deductions"]} <= {1, 2}
    # 回滚后到期批（1/2/4）仍是 on_shelf，没有半 expired
    from app.db import read_conn
    with read_conn() as c:
        st = {r["id"]: r["status"] for r in c.execute("SELECT id,status FROM lots")}
    assert st[1] == "on_shelf" and st[2] == "on_shelf" and st[4] == "on_shelf"


def test_consume_cannot_touch_expired_lot(tmp_data):
    from app.ops import run_consume, run_expire_sweep
    # 先成功下架：1/2/4 全部 expired
    run_expire_sweep("2026-10-06")
    # 再消费 item 1：在架上无正余量批，必须 short，绝不扣 expired 的 1/2
    with pytest.raises(Exception) as ei:
        run_consume(1, 1)
    assert ei.value.result["reason"] == "short"
    from app.db import read_conn
    with read_conn() as c:
        qtys = {r["id"]: r["qty_remain"] for r in c.execute(
            "SELECT id,qty_remain FROM lots WHERE id IN (1,2)")}
    assert qtys == {1: 2, 2: 1}


def test_cli_exit_codes_separated(tmp_data):
    env = os.environ.copy()

    def cli(*args, extra=None):
        e = env.copy()
        e.update(extra or {})
        p = subprocess.run([sys.executable, *args], cwd=BACKEND, env=e,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    # 成功 0；第二次成功也 0
    code1, out1, _ = cli("run_sweep.py")
    code2, out2, _ = cli("run_sweep.py")
    assert code1 == 0 and code2 == 0
    assert json.loads(out2)["expired_ids"] == []

    # 注入失败 3（新临时库）
    fd = tmp_data / "faultcase"
    code3, out3, _ = cli("run_sweep.py",
                         extra={"DATA_DIR": str(fd),
                                "PANTRY_FAULT_POINT": "expire_sweep:before_commit"})
    assert code3 == 3
    assert json.loads(out3)["rolled_back"] is True

    # 对账成功 0
    code4, _, _ = cli("reconcile.py", "--today", "2026-10-06")
    assert code4 == 0

    # 对账失败 2（成功下架后却要求已离场候选批仍在架）
    nd = tmp_data / "neg"
    cli("run_sweep.py", extra={"DATA_DIR": str(nd)})
    code5, out5, _ = cli("reconcile.py", "--today", "2026-10-06",
                         "--require-on-shelf", "1,2,4",
                         extra={"DATA_DIR": str(nd)})
    assert code5 == 2
    assert json.loads(out5)["violations"]


def test_restore_prunes_shelf_without_deleting_rows(tmp_data):
    from app.ops import (lot_status_snapshot, on_shelf_snapshot,
                         restore_lots_to_snapshot, run_expire_sweep)
    before = lot_status_snapshot()
    before_shelf = on_shelf_snapshot()
    run_expire_sweep("2026-10-06")
    info = restore_lots_to_snapshot(before)
    assert info["on_shelf_after"] == before_shelf
    from app.db import read_conn
    with read_conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM lots").fetchone()["n"]
    assert n == len(before)  # 一行没删
