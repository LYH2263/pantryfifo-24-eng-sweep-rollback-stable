"""Command-line entrypoint for expiry sweeps. Kept separate from HTTP.

Exit codes are never mixed with 0:
    0  success (committed AND reconciliation clean)
    10 sweep rolled back (injected fault or DB failure)
    11 committed but reconciliation found a view mismatch
    20 second run in a `twice` was non-idempotent (still found rows /
       modified already-expired rows)
    30 snapshot restore failed
    2  bad usage

Fault injection for the CLI comes only from SWEEP_FAULT=begin|before_commit.
"""
import json
import sys

from app import seed
from app.db import enable_wal
from app.engines.expire_sweep import run_expire_sweep
from app.engines.faults import injector_from_env
from app.engines.snapshot import restore_snapshot, take_snapshot

EXIT_OK = 0
EXIT_ROLLED_BACK = 10
EXIT_RECONCILE = 11
EXIT_NOT_IDEMPOTENT = 20
EXIT_RESTORE = 30
EXIT_USAGE = 2


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def cmd_sweep() -> int:
    res = run_expire_sweep(injector=injector_from_env())
    _emit(res)
    if not res["committed"]:
        return EXIT_ROLLED_BACK
    if not res["report"]["ok"]:
        return EXIT_RECONCILE
    return EXIT_OK


def cmd_twice() -> int:
    """Two successful runs in a row: 2nd list empty, expired rows untouched.

    The second run only exists on the SUCCESS path: if run1 rolls back,
    stop immediately (running a clean sweep after an injected failure would
    hide the rollback).
    """
    r1 = run_expire_sweep(injector=injector_from_env())
    if not r1["committed"]:
        _emit({"run1": r1})
        return EXIT_ROLLED_BACK
    if not r1["report"]["ok"]:
        _emit({"run1": r1})
        return EXIT_RECONCILE
    r2 = run_expire_sweep(injector=None)  # never inject the idempotent run
    _emit({"run1": r1, "run2": r2})

    if not r2["committed"]:
        return EXIT_ROLLED_BACK
    if not r2["report"]["ok"]:
        return EXIT_RECONCILE
    if r2["expired_ids"] != []:
        return EXIT_NOT_IDEMPOTENT
    # already-expired rows from run1 must be untouched by run2
    expected_untouched = set(r1["expired_ids"])
    untouched2 = set(r2["report"]["already_expired_untouched"])
    if not expected_untouched <= untouched2:
        return EXIT_NOT_IDEMPOTENT
    return EXIT_OK


def cmd_snapshot(path: str) -> int:
    _emit(take_snapshot(path))
    return EXIT_OK


def cmd_restore(path: str) -> int:
    try:
        rep = restore_snapshot(path)
    except Exception as e:
        _emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return EXIT_RESTORE
    _emit(rep)
    return EXIT_OK if rep["ok"] else EXIT_RESTORE


USAGE = (
    "usage:\n"
    "  python -m app.cli sweep\n"
    "  python -m app.cli twice\n"
    "  python -m app.cli snapshot <path>\n"
    "  python -m app.cli restore <path>\n"
)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    cmd, args = argv[0], argv[1:]
    if cmd not in ("sweep", "twice", "snapshot", "restore"):
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    # Standalone process: no FastAPI startup hook, so init the schema here.
    seed.init_db()
    enable_wal()
    if cmd == "sweep":
        return cmd_sweep()
    if cmd == "twice":
        return cmd_twice()
    if cmd == "snapshot" and len(args) == 1:
        return cmd_snapshot(args[0])
    if cmd == "restore" and len(args) == 1:
        return cmd_restore(args[0])
    sys.stderr.write(USAGE)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
