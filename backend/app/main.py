from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import read_conn, write_tx
from app.engines.fault import FaultInjected
from app.ops import (
    ConsumeConflict,
    _ConsumeRejected,
    run_consume,
    run_expire_sweep,
)

app = FastAPI(title="Pantryfifo", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    with read_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM items")]

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    with read_conn() as c:
        return [dict(r) for r in c.execute(q, args)]

@app.get("/api/alerts")
def alerts():
    with read_conn() as c:
        warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
        rows = [dict(r) for r in c.execute(
            """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
               WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    today = date.today()
    today_s = today.isoformat()
    out = []
    for r in rows:
        if r["expiry"] <= today_s:
            r["level"] = "expired"
            out.append(r)
        else:
            delta = (date.fromisoformat(r["expiry"]) - today).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    with write_tx() as c:
        item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
        if not item:
            raise HTTPException(404, "item")
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
        lid = cur.lastrowid
    return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    try:
        return run_consume(body.item_id, body.qty, body.note)
    except KeyError:
        raise HTTPException(404, "item")
    except _ConsumeRejected as e:
        code = 400 if e.result["reason"] == "qty_non_positive" else 409
        raise HTTPException(code, e.result)
    except ConsumeConflict as e:
        raise HTTPException(409, str(e))

@app.post("/api/expire-sweep")
def expire_sweep():
    try:
        return run_expire_sweep()
    except FaultInjected as e:
        # 事务已整场回滚；显式 500，绝不返回 200 让前端误以为下架成功。
        raise HTTPException(500, {"fault": str(e), "rolled_back": True})

@app.get("/api/settings")
def settings():
    with read_conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
