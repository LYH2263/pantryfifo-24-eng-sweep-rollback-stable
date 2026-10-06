import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import seed  # noqa: E402
from app.db import connect, enable_wal  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Isolated seeded pantry DB per test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    seed.init_db()
    enable_wal()
    return tmp_path


def ids_on_shelf():
    c = connect()
    rows = c.execute(
        "SELECT id FROM lots WHERE status='on_shelf' AND qty_remain>0 ORDER BY id"
    ).fetchall()
    c.close()
    return [r["id"] for r in rows]


def statuses():
    c = connect()
    rows = c.execute("SELECT id, status FROM lots ORDER BY id").fetchall()
    c.close()
    return {r["id"]: r["status"] for r in rows}
