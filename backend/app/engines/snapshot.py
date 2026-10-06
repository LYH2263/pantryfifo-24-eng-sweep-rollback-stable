"""On-shelf set snapshots for test-scene teardown.

A snapshot records the on-shelf lots present when a scene STARTED.
restore makes the current on-shelf set EXACTLY equal to the snapshot's
id list:

  * snapshot rows are restored (UPDATE) to their snapshotted fields —
    lots swept/consumed during the scene come back to on_shelf;
  * on-shelf lots created during the scene (ids not in the snapshot)
    are flipped to 'expired'.

Everything is an UPDATE, never a DELETE: the business table is never
wiped and every pre-existing row still exists afterwards. If a
snapshot lot has been hard-deleted, restore refuses rather than
silently reporting a green set.
"""
import json
from pathlib import Path

from app.db import connect

_FIELDS = ("item_id", "qty_in", "qty_remain", "expiry", "status", "data_quality")


def take_snapshot(path: str | Path) -> dict:
    c = connect()
    rows = [dict(r) for r in c.execute(
        "SELECT id, item_id, qty_in, qty_remain, expiry, status, data_quality "
        "FROM lots WHERE status='on_shelf' AND qty_remain>0 ORDER BY id").fetchall()]
    c.close()
    snap = {"on_shelf_ids": [r["id"] for r in rows], "rows": rows}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return snap


def load_snapshot(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def restore_snapshot(path: str | Path) -> dict:
    snap = load_snapshot(path)
    keep = set(snap["on_shelf_ids"])
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")

        # Guard: every scene-start lot must still exist. Never let a wiped
        # business table masquerade as a clean trim.
        missing = sorted(
            i for i in keep
            if c.execute("SELECT 1 FROM lots WHERE id=?", (i,)).fetchone() is None)
        if missing:
            raise RuntimeError(f"snapshot lots missing from business table: {missing}")

        # (1) restore snapshot rows to their scene-start state
        restored = []
        for r in snap["rows"]:
            cur = c.execute("SELECT status, qty_remain FROM lots WHERE id=?",
                            (r["id"],)).fetchone()
            if cur["status"] != r["status"] or cur["qty_remain"] != r["qty_remain"]:
                c.execute(
                    "UPDATE lots SET item_id=:item_id, qty_in=:qty_in, "
                    "qty_remain=:qty_remain, expiry=:expiry, status=:status, "
                    "data_quality=:data_quality WHERE id=:id", r)
                restored.append(r["id"])

        # (2) expire scene-created lots still sitting on the shelf
        current = [dict(x) for x in c.execute(
            "SELECT id FROM lots WHERE status='on_shelf' AND qty_remain>0").fetchall()]
        extra = [x["id"] for x in current if x["id"] not in keep]
        for i in extra:
            c.execute(
                "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'",
                (i,))

        c.commit()
        after = [x["id"] for x in c.execute(
            "SELECT id FROM lots WHERE status='on_shelf' AND qty_remain>0 ORDER BY id"
        ).fetchall()]
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {"kept": sorted(keep), "restored": sorted(restored),
            "pruned": sorted(extra), "after_ids": sorted(after),
            "ok": sorted(after) == sorted(keep)}
