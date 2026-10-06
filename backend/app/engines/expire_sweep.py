"""Atomic expiry sweep.

One transaction flips the WHOLE candidate set on_shelf -> expired:

    BEGIN IMMEDIATE
      read on-shelf snapshot + candidate ids
      UPDATE ... (whole set)
      [injection point before_commit]
    COMMIT

There is deliberately no per-batch loop outside the transaction: a
failure anywhere rolls back every UPDATE in this run, so the three
views (全层 / 层页 / 顶条) can never diverge — readers on other
connections only ever see the state before BEGIN or after COMMIT.

BEGIN IMMEDIATE takes SQLite's write lock up front, serialising the
sweep against /consume (which does the same): a concurrent consume
waits for the sweep to commit or roll back and can never deduct from a
half-expired batch.
"""
from datetime import date

from app.db import connect
from app.engines.fefo import expire_lots
from app.engines.reconcile import build_report, expired_rows_snapshot


def _on_shelf_rows(c) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE status='on_shelf' AND qty_remain>0")]


def _ids(rows) -> list[int]:
    return sorted(r["id"] for r in rows)


def run_expire_sweep(today: str | None = None, injector=None) -> dict:
    """Run one sweep. Returns a result envelope, never raises on faults.

    Result:
      committed   : bool
      expired_ids : candidate ids (empty when rolled back)
      report      : reconciliation report dict
      error       : present only when committed is False
    """
    today = today or date.today().isoformat()
    c = connect()
    before_ids: list[int] = []
    expired_before: dict = {}
    candidate_ids: list[int] = []
    committed = False
    error = None

    try:
        # Take the lock first, then snapshot the pre-submit set INSIDE the
        # transaction so rollback reconciliation has the true baseline.
        c.execute("BEGIN IMMEDIATE")
        rows = _on_shelf_rows(c)
        before_ids = _ids(rows)
        expired_before = expired_rows_snapshot(c)

        if injector is not None:
            injector("begin")

        candidate_ids = expire_lots(rows, today)

        if candidate_ids:
            c.executemany(
                "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'",
                [(i,) for i in candidate_ids])

        if injector is not None:
            # Last chance to fail: lock held, all updates done, nothing committed.
            injector("before_commit")

        c.commit()
        committed = True
    except Exception as e:  # injection OR a real DB failure: same rollback path
        c.rollback()
        error = f"{type(e).__name__}: {e}"
        candidate_ids = []  # nothing left the shelf
    finally:
        c.close()

    # Verify from a SEPARATE connection: exactly what a fresh API request sees.
    r2 = connect()
    after_ids = _ids(_on_shelf_rows(r2))
    expired_after = expired_rows_snapshot(r2)
    r2.close()

    if committed:
        report = build_report(
            before_ids, candidate_ids, after_ids, today,
            expired_before=expired_before, expired_after=expired_after)
    else:
        report = build_report(before_ids, [], after_ids, today,
                              expired_before=expired_before,
                              expired_after=expired_after,
                              ok=False, error=error)
        # Rollback cleanliness is itself a reconciled invariant: after must
        # equal before exactly (全层/层页/顶条 all back to pre-submit state).
        report["rolled_back_clean"] = after_ids == before_ids
        if not report["rolled_back_clean"]:
            report["ok"] = False
            report["mismatches"].append({
                "type": "rollback_dirty",
                "before_ids": before_ids,
                "after_ids": after_ids,
            })

    return {
        "committed": committed,
        "expired_ids": candidate_ids if committed else [],
        "report": report,
        **({"error": error} if error else {}),
    }
