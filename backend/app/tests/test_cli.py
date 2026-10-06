"""CLI tests: exit codes, twice-idempotency, snapshot prune-back."""
import json

import pytest

from app import cli
from app.tests.conftest import ids_on_shelf, statuses

pytestmark = pytest.mark.usefixtures("fresh_db")

TODAY_EXPIRED = [1, 2, 4]


def _run(capsys, argv):
    code = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(out)


def test_sweep_success_exit_0(capsys):
    code, body = _run(capsys, ["sweep"])
    assert code == cli.EXIT_OK == 0
    assert body["committed"] is True
    assert body["expired_ids"] == TODAY_EXPIRED


def test_injected_failure_exit_nonzero(capsys, monkeypatch):
    monkeypatch.setenv("SWEEP_FAULT", "before_commit")
    code, body = _run(capsys, ["sweep"])
    assert code == cli.EXIT_ROLLED_BACK and code != 0
    assert body["committed"] is False
    assert body["report"]["rolled_back_clean"] is True


def test_twice_success_both_runs_exit_0(capsys):
    # 连跑两次成功路径退出码都为 0, second list empty
    code, body = _run(capsys, ["twice"])
    assert code == cli.EXIT_OK == 0
    assert body["run1"]["expired_ids"] == TODAY_EXPIRED
    assert body["run2"]["expired_ids"] == []
    assert body["run2"]["report"]["changed_ids"] == []
    assert set(TODAY_EXPIRED) <= set(body["run2"]["report"]["already_expired_untouched"])


def test_twice_with_fault_first_run_nonzero(capsys, monkeypatch):
    monkeypatch.setenv("SWEEP_FAULT", "before_commit")
    code, body = _run(capsys, ["twice"])
    assert code == cli.EXIT_ROLLED_BACK
    assert body["run1"]["committed"] is False
    assert ids_on_shelf() == [1, 2, 3, 4]


def test_snapshot_restore_prunes_back_to_start_list(tmp_path, capsys):
    snap = tmp_path / "s.json"
    # start-of-scene snapshot
    code, body0 = _run(capsys, ["snapshot", str(snap)])
    assert code == 0
    start_ids = body0["on_shelf_ids"]
    assert start_ids == [1, 2, 3, 4]

    # scene happens: inbound two new lots (one expiring) and sweep them
    from app.db import connect
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (2,5,5,'2020-01-01','on_shelf','clean')")
    new1 = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (2,7,7,'2030-01-01','on_shelf','clean')")
    new2 = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.commit(); c.close()

    code, body = _run(capsys, ["sweep"])
    assert code == 0 and new1 in body["expired_ids"]

    # restore: on-shelf set trimmed back EXACTLY to the scene-start list
    code, rep = _run(capsys, ["restore", str(snap)])
    assert code == 0
    assert rep["after_ids"] == start_ids
    # new2 was still on-shelf -> pruned; new1 was already swept -> stays expired
    assert new2 in rep["pruned"]
    assert {1, 2, 4} <= set(rep["restored"])

    # business table NOT wiped: all original lots still exist, and the
    # scene-start lots are back to their start state
    st = statuses()
    for i in start_ids:
        assert i in st
    assert st[1] == "on_shelf" and st[3] == "on_shelf"
    assert st[new1] == "expired" and st[new2] == "expired"
    assert ids_on_shelf() == start_ids


def test_restore_runs_again_is_stable(tmp_path, capsys):
    snap = tmp_path / "s.json"
    _run(capsys, ["snapshot", str(snap)])
    code1, rep1 = _run(capsys, ["restore", str(snap)])
    code2, rep2 = _run(capsys, ["restore", str(snap)])
    assert code1 == 0 and code2 == 0
    assert rep1["after_ids"] == rep2["after_ids"]
    assert rep2["pruned"] == []


def test_usage_exit_2(capsys):
    assert cli.main([]) == cli.EXIT_USAGE == 2
    assert cli.main(["bogus"]) == 2
