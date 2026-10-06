"""HTTP entrypoint tests: status codes never mix success with rollback."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tests.conftest import ids_on_shelf, statuses

pytestmark = pytest.mark.usefixtures("fresh_db")

TODAY = "2026-10-06"
EXPIRED_SEED = [1, 2, 4]
KEEP_SEED = [3]
# /fridge keeps the seed's negative-qty dirty lot (status on_shelf);
# candidate selection and reconciliation ignore non-positive stock.
DIRTY_NEG_LOT = 5


def _fridge_ids(client, layer=None):
    q = "/api/fridge" + (f"?layer={layer}" if layer else "")
    return {x["id"] for x in client.get(q).json()}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    with TestClient(app) as c:
        yield c


def test_sweep_success_200_and_views_agree(client):
    r = client.post("/api/expire-sweep")
    assert r.status_code == 200
    body = r.json()
    assert body["expired_ids"] == EXPIRED_SEED
    assert body["report"]["ok"] is True

    # 全层 / 层页 / 顶条 must all reflect the same post-commit set.
    fridge = _fridge_ids(client)
    by_layer = set()
    for L in ("upper", "mid", "lower"):
        by_layer |= _fridge_ids(client, L)
    bar = {a["id"] for a in client.get("/api/alerts").json()}
    assert fridge == set(KEEP_SEED + [DIRTY_NEG_LOT])
    assert by_layer == fridge
    assert bar <= fridge and EXPIRED_SEED[0] not in bar


def test_sweep_twice_both_200_second_empty(client):
    r1 = client.post("/api/expire-sweep")
    r2 = client.post("/api/expire-sweep")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["expired_ids"] == EXPIRED_SEED
    assert r2.json()["expired_ids"] == []
    assert r2.json()["report"]["changed_ids"] == []


def test_injected_failure_550_and_views_unchanged(client):
    before = ids_on_shelf()
    r = client.post("/api/expire-sweep", headers={"X-Fault-Inject": "before_commit"})
    assert r.status_code == 550                       # never confused with 200/0
    body = r.json()
    assert body["committed"] is False
    assert body["report"]["rolled_back_clean"] is True
    assert ids_on_shelf() == before
    # all three views still show the pre-submit set (fridge also keeps
    # the pre-existing negative-qty dirty lot, unchanged)
    assert _fridge_ids(client) == set(before) | {DIRTY_NEG_LOT}
    assert {x["id"] for x in client.get("/api/fridge?layer=upper").json()} == set(before[:2])
    assert all(v == "on_shelf" for v in statuses().values())


def test_injection_disabled_returns_400_not_silent_success(monkeypatch):
    monkeypatch.delenv("ENABLE_FAULT_INJECTION", raising=False)
    with TestClient(app) as c:
        r = c.post("/api/expire-sweep", headers={"X-Fault-Inject": "before_commit"})
        assert r.status_code == 400
        # and no sweep happened
        assert all(v == "on_shelf" for v in statuses().values())


def test_bad_fault_point_400(client):
    r = client.post("/api/expire-sweep", headers={"X-Fault-Inject": "halfway"})
    assert r.status_code == 400


def test_consume_after_failed_sweep_still_sees_both_lots(client):
    # Failed sweep first
    rs = client.post("/api/expire-sweep", headers={"X-Fault-Inject": "before_commit"})
    assert rs.status_code == 550
    # consume 2 of item 1: FEFO must take lot 2 then lot 1 — both still there
    rc = client.post("/api/consume", json={"item_id": 1, "qty": 2})
    assert rc.status_code == 200
    ded = [(d["lot_id"], d["take"]) for d in rc.json()["deductions"]]
    assert ded == [(2, 1.0), (1, 1.0)]
