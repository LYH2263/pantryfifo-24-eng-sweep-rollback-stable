import os, sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"


def connect():
    """A connection configured for short explicit transactions.

    busy_timeout makes a competing writer (sweep vs consume) wait for the
    lock instead of failing immediately; callers serialise via
    BEGIN IMMEDIATE so nobody ever observes a half-committed batch set.
    """
    c = sqlite3.connect(db_path(), timeout=BUSY_TIMEOUT_MS / 1000)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return c


def enable_wal():
    c = connect()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.close()
