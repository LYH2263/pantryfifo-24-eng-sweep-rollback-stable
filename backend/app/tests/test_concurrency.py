"""Concurrency: a failed sweep must never expose half-expired batches to consume.

SQLite serialises writers via BEGIN IMMEDIATE + busy_timeout. We hold the
sweep transaction open (updates done, commit pending) and prove:
  1. a fresh reader still sees the pre-submit state (WAL, no dirty reads);
  2. a concurrent /consume blocks on the write lock;
  3. after the sweep is forced to roll back, consume deducts from the FULLY
     restored on-shelf set, never from a half-expired one.
"""
import threading
import time

import pytest

from app.engines.expire_sweep import run_expire_sweep
from app.engines.faults import InjectedFault
from app.tests.conftest import ids_on_shelf, statuses

pytestmark = pytest.mark.usefixtures("fresh_db")

TODAY = "2026-10-06"


def test_consume_cannot_land_on_half_expired_batch():
    held = threading.Event()
    release = threading.Event()

    def inject(point):
        if point == "before_commit":
            held.set()
            assert release.wait(timeout=10)
            raise InjectedFault(point)

    sweep_out = {}

    def sweep_thread():
        sweep_out["res"] = run_expire_sweep(today=TODAY, injector=inject)

    t = threading.Thread(target=sweep_thread)
    t.start()
    assert held.wait(timeout=5)

    try:
        # (1) Lock held, UPDATEs issued — readers must see the OLD state.
        assert ids_on_shelf() == [1, 2, 3, 4]
        assert all(v == "on_shelf" for v in statuses().values())

        # (2) Consume launched now must block behind the sweep's write lock.
        from app.main import ConsumeIn, consume
        cons_out = {}

        def consume_thread():
            cons_out["r"] = consume(ConsumeIn(item_id=1, qty=2))

        tc = threading.Thread(target=consume_thread)
        start = time.monotonic()
        tc.start()
        time.sleep(0.6)
        assert tc.is_alive(), "consume slipped past the held sweep lock"

        # Inject failure: whole run rolls back, lock releases.
        release.set()
        tc.join(timeout=5)
        t.join(timeout=5)
        waited = time.monotonic() - start
        assert waited >= 0.5
    finally:
        release.set()
        t.join(timeout=5)

    assert sweep_out["res"]["committed"] is False
    assert sweep_out["res"]["report"]["rolled_back_clean"] is True

    # (3) Consume saw the fully restored set: FEFO over lots 2 (09-28) and
    #     1 (10-01), both on_shelf — not a world where only one was expired.
    ded = [(d["lot_id"], d["take"]) for d in cons_out["r"]["deductions"]]
    assert ded == [(2, 1.0), (1, 1.0)]
    assert statuses()[2] == "consumed"
    assert ids_on_shelf() == [1, 3, 4]
