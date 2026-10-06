"""Reconciliation: verify on-shelf lot-id sets across a sweep.

This module is read-only against business data and produces a
plain-dict report. It does NOT mutate lots and does NOT decide exit
codes — those entrypoint concerns live in main.py / cli.py.

Consistency rule (the feature in one sentence): after a sweep, the
three views 全层(/fridge) / 层页(/fridge?layer=) / 顶条(/alerts) must
all be views onto the SAME on-shelf id set — the set read inside the
sweep transaction just before commit.
"""
from app.db import connect

# Fields compared when proving a second sweep never touches expired rows.
_ROW_FIELDS = ("item_id", "qty_in", "qty_remain", "expiry", "status", "data_quality")


def on_shelf_ids(layer: str | None = None) -> list[int]:
    c = connect()
    if layer is None:
        rows = c.execute(
            "SELECT lots.id FROM lots JOIN items ON items.id=lots.item_id "
            "WHERE lots.status='on_shelf' AND lots.qty_remain>0",
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT lots.id FROM lots JOIN items ON items.id=lots.item_id "
            "WHERE lots.status='on_shelf' AND lots.qty_remain>0 AND items.layer=?",
            (layer,),
        ).fetchall()
    c.close()
    return sorted(r["id"] for r in rows)


def alerts_ids() -> list[int]:
    """Lots with expiry still on_shelf — the universe the alert bar reads."""
    c = connect()
    rows = c.execute(
        "SELECT id FROM lots WHERE status='on_shelf' AND qty_remain>0 "
        "AND expiry IS NOT NULL",
    ).fetchall()
    c.close()
    return sorted(r["id"] for r in rows)


def expired_rows_snapshot(c) -> dict:
    """Full content of every already-expired row, keyed by id.

    Used to prove the second sweep leaves expired rows byte-identical.
    """
    return {
        r["id"]: tuple(r[f] for f in _ROW_FIELDS)
        for r in c.execute("SELECT * FROM lots WHERE status='expired'").fetchall()
    }


def build_report(before_ids, candidate_ids, after_ids, today, *,
                 expired_before=None, expired_after=None,
                 ok=True, error=None):
    """Assemble the reconciliation report.

    before_ids     : on-shelf ids when the sweep transaction started
    candidate_ids  : ids selected as expired inside the transaction
    after_ids      : on-shelf ids observed AFTER commit (new connection)
    expired_before : {id: field-tuple} of expired rows read in the txn
    expired_after  : {id: field-tuple} of the same rows read after commit
    """
    before_set, after_set = set(before_ids), set(after_ids)
    expect_removed = set(candidate_ids)
    changed = sorted(before_set - after_set)
    mismatches = []

    if ok:
        if changed != sorted(expect_removed):
            mismatches.append({
                "type": "removed_set_mismatch",
                "expected": sorted(expect_removed),
                "actual": changed,
            })
        leaked = sorted(expect_removed & after_set)
        if leaked:
            mismatches.append({"type": "expired_still_on_shelf", "ids": leaked})
        # 顶条一致性: the alert bar reads the same on-shelf universe, so
        # no swept id may survive in it and it cannot show floating lots.
        alert_set = set(alerts_ids())
        leaked_in_bar = sorted(expect_removed & alert_set)
        if leaked_in_bar:
            mismatches.append({"type": "expired_still_in_alerts", "ids": leaked_in_bar})
        floating = sorted(alert_set - after_set)
        if floating:
            mismatches.append({"type": "alerts_floating_lots", "ids": floating})

    # Second-run guarantee: already-expired rows must be untouched.
    modified_expired = []
    expired_before = expired_before or {}
    expired_after = expired_after or {}
    for lid, vals in expired_before.items():
        if expired_after.get(lid) != vals:
            modified_expired.append(lid)
    if modified_expired:
        mismatches.append({"type": "expired_row_modified", "ids": sorted(modified_expired)})

    return {
        "today": today,
        "ok": ok and not mismatches,
        "before_ids": sorted(before_ids),
        "candidate_ids": sorted(candidate_ids),
        "changed_ids": changed,
        "after_ids": sorted(after_ids),
        "already_expired_untouched": sorted(
            lid for lid in expired_before
            if expired_after.get(lid) == expired_before[lid]),
        "mismatches": mismatches,
        **({"error": error} if error else {}),
    }
