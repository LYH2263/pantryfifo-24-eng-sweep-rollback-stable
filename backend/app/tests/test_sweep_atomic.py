"""Engine-level tests: atomicity, rollback, idempotency, reconciliation."""
import pytest

from app.engines import reconcile
from app.engines.expire_sweep import run_expire_sweep
from app.engines.faults import make_injector
from app.tests.conftest import ids_on_shelf, statuses

pytestmark = pytest.mark.usefixtures("fresh_db")

TODAY = "2026-10-06"
EXPIRED_SEED = [1, 2, 4]   # 2026-10-01 / 2026-09-28 / 2025-01-01 (qty>0)
KEEP_SEED = [3]            # 2026-11-01, still good (lot 5 has negative qty)


def _row(lot_id):
    c = reconcile.connect()
    r = dict(c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close()
    return r


def test_success_flips_exact_candidate_set():
    before = ids_on_shelf()
    res = run_expire_sweep(today=TODAY)
    assert res["committed"] is True
    assert res["expired_ids"] == EXPIRED_SEED
    assert ids_on_shelf() == KEEP_SEED
    rep = res["report"]
    assert rep["ok"] is True and rep["mismatches"] == []
    assert rep["before_ids"] == before
    assert rep["after_ids"] == KEEP_SEED
    assert rep["changed_ids"] == EXPIRED_SEED
    assert statuses()[1] == "expired" and statuses()[4] == "expired"


def test_second_run_empty_and_untouched():
    r1 = run_expire_sweep(today=TODAY)
    assert r1["expired_ids"] == EXPIRED_SEED
    snap = {i: _row(i) for i in EXPIRED_SEED}

    r2 = run_expire_sweep(today=TODAY)
    assert r2["committed"] is True
    assert r2["expired_ids"] == []                       # 2nd list empty
    assert r2["report"]["candidate_ids"] == []
    assert r2["report"]["changed_ids"] == []
    assert r2["report"]["ok"] is True
    for i in EXPIRED_SEED:                               # expired rows untouched
        assert _row(i) == snap[i], f"expired row {i} modified by 2nd sweep"
    # and the report proves it
    assert set(EXPIRED_SEED) <= set(r2["report"]["already_expired_untouched"])


def test_inject_at_begin_rolls_back_whole_run():
    before = ids_on_shelf()
    res = run_expire_sweep(today=TODAY, injector=make_injector("begin"))
    assert res["committed"] is False
    assert "InjectedFault" in res["error"]
    assert res["expired_ids"] == []
    assert res["report"]["rolled_back_clean"] is True
    assert ids_on_shelf() == before                      # 全层 back pre-submit
    assert statuses() == {1: "on_shelf", 2: "on_shelf", 3: "on_shelf",
                          4: "on_shelf", 5: "on_shelf"}


def test_inject_before_commit_rolls_back_all_updates():
    """The dangerous point: UPDATEs already issued — none may survive."""
    before = ids_on_shelf()
    res = run_expire_sweep(today=TODAY, injector=make_injector("before_commit"))
    assert res["committed"] is False
    assert res["report"]["rolled_back_clean"] is True
    assert ids_on_shelf() == before
    assert all(v == "on_shelf" for v in statuses().values())


def test_reconcile_detects_view_divergence():
    # candidates 1,2 removed — but id 2 leaks back onto the shelf / bar
    rep = reconcile.build_report([1, 2, 3], [1, 2], [2, 3], TODAY)
    assert rep["ok"] is False
    assert any(m["type"] in ("removed_set_mismatch", "expired_still_on_shelf")
               for m in rep["mismatches"])


def test_reconcile_detects_expired_row_tampering():
    # Use the real on-shelf universe so view checks stay neutral; the
    # synthetic expired lot 901 only exercises row-content comparison.
    base = ids_on_shelf()
    fields = (10, 2, 2, "2026-10-01", "expired", "clean")
    clean = reconcile.build_report(base, [], base, TODAY,
                                   expired_before={901: fields},
                                   expired_after={901: fields})
    assert clean["ok"] is True
    tampered = (10, 2, 1, "2026-10-01", "expired", "clean")
    dirty = reconcile.build_report(base, [], base, TODAY,
                                   expired_before={901: fields},
                                   expired_after={901: tampered})
    assert any(m["type"] == "expired_row_modified" for m in dirty["mismatches"])


def test_invalid_fault_point_rejected():
    with pytest.raises(ValueError):
        make_injector("midway")
