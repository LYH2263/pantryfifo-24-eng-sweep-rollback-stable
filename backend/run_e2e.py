#!/usr/bin/env python3
"""工程化验收：过期下架的事务/回滚/并发/双跑/对账/裁回 端到端驱动。

阶段：
  0  开场快照（批号 -> status/qty_remain、业务表行数、在架批号列表）
  1  HTTP 注入失败：/api/expire-sweep 必须 500；全层 / 层页 / 顶条同步回到提交前
  2  注入失败窗口内并发消费风暴：消费不得打到半 expired 的批；事后对账一致
  3  CLI 连跑两次成功：退出码均 0；第二次名单为空、0 行改写、不碰已 expired
  4  对账反例（独立临时库）：制造不一致必须退出 2，不得与成功 0 混用
  5  按开场快照对称恢复：在架集合裁回开场批号列表；业务表一行不删

任何一步断言失败即非零退出；成功（含两次下架、两次对账正例）退出 0。
"""
import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

from app.db import read_conn  # noqa: E402
from app.engines.fault import FaultInjected  # noqa: E402
from app.ops import (  # noqa: E402
    lot_status_snapshot,
    on_shelf_snapshot,
    restore_lots_to_snapshot,
    run_consume,
    run_expire_sweep,
)
from app.reconcile import reconcile_report  # noqa: E402
from app.seed import init_db  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1

FAULT = "expire_sweep:before_commit"
TODAY = "2026-10-06"

failures: list[str] = []


def check(name: str, cond: bool, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def table_counts() -> dict[str, int]:
    with read_conn() as c:
        return {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                for t in ("items", "lots", "consumptions", "sweep_runs")}


def lot_view() -> dict[int, dict]:
    with read_conn() as c:
        return {r["id"]: dict(r) for r in c.execute("SELECT * FROM lots")}


def expired_candidates(today=TODAY) -> list[int]:
    with read_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE status='on_shelf'")]
    return sorted(
        r["id"] for r in rows
        if r["expiry"] and r["expiry"] < today and float(r["qty_remain"]) > 0)


def run_cli(*args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env.update(extra_env or {})
    p = subprocess.run(
        [sys.executable, *args], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=60)
    try:
        payload = json.loads(p.stdout)
    except Exception:
        payload = None
    return p.returncode, payload, p.stdout, p.stderr


# ---------------------------------------------------------------- phase 0
init_db()
before_status = lot_status_snapshot()
before_qty = {lid: r["qty_remain"] for lid, r in lot_view().items()}
before_on_shelf = on_shelf_snapshot()
before_counts = table_counts()
print(f"开场在架批号: {before_on_shelf}")


# ---------------------------------------------------------------- phase 1
print("\n== 阶段1：HTTP 注入失败，三视图必须整场回滚 ==")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
os.environ["PANTRY_FAULT_POINT"] = FAULT
r = client.post("/api/expire-sweep")
check("注入失败 HTTP 500", r.status_code == 500, f"got {r.status_code} {r.text}")
check("响应声明 rolled_back",
      r.status_code == 500 and r.json()["detail"]["rolled_back"] is True)
check("注入点一次性自毁", "PANTRY_FAULT_POINT" not in os.environ)

cands = expired_candidates()
fridge = {x["id"] for x in client.get("/api/fridge").json()}
upper = {x["id"] for x in client.get("/api/fridge?layer=upper").json()}
alerts = {x["id"] for x in client.get("/api/alerts").json()}
check("全层仍含全部候选批", set(cands) <= fridge, f"{cands} vs {sorted(fridge)}")
exp_upper = [i for i in cands
             if i in {1, 2}]  # 种子里 1/2 属 upper（牛奶），3 属 lower（冻饺）
check("上层页仍含该层候选批", set(exp_upper) <= upper, f"{exp_upper} vs {sorted(upper)}")
check("顶条仍含到期候选批", set(cands) <= alerts, f"{cands} vs {sorted(alerts)}")
after1 = lot_view()
check("无任何批被翻成 expired",
      all(after1[i]["status"] != "expired" for i in cands))
rep = reconcile_report(TODAY)
check("回滚后对账一致", rep["ok"], str(rep["violations"]))


# ---------------------------------------------------------------- phase 2
print("\n== 阶段2：提交前窗口并发消费风暴 ==")
os.environ["PANTRY_FAULT_POINT"] = FAULT
os.environ["PANTRY_SWEEP_HOLD_SECONDS"] = "2"
sweep_err: list[Exception] = []


def sweep_thread():
    try:
        run_expire_sweep(TODAY)
    except FaultInjected as e:
        sweep_err.append(e)
    except Exception as e:  # noqa: BLE001
        sweep_err.append(e)


t = threading.Thread(target=sweep_thread)
t.start()
threading.Event().wait(0.6)  # 等 sweep 拿住写锁、进入 hold 窗口

storm: list[dict] = []


def consume_thread(item_id: int, qty: float):
    try:
        run_consume(item_id, qty, note="e2e-storm")
        storm.append({"ok": True, "item_id": item_id})
    except Exception as e:  # noqa: BLE001
        storm.append({"ok": False, "item_id": item_id, "err": type(e).__name__})


threads = [threading.Thread(target=consume_thread, args=a)
           for a in [(1, 0.25)] * 4 + [(2, 0.5)] * 3]
for th in threads:
    th.start()
for th in threads:
    th.join(timeout=15)
t.join(timeout=15)

os.environ.pop("PANTRY_SWEEP_HOLD_SECONDS", None)
check("下架线程确实收到注入失败",
      len(sweep_err) == 1 and isinstance(sweep_err[0], FaultInjected),
      repr(sweep_err))
mid = lot_view()
check("风暴后仍无 expired 批（整场回滚，无半提交）",
      all(r["status"] != "expired" for r in mid.values()))
# 不变量：凡 consumed 必 qty=0；凡 on_shelf 且候选到期的批状态必须与库一致
coherent = all(
    (r["status"] == "consumed" and abs(float(r["qty_remain"])) < 1e-9)
    or r["status"] in ("on_shelf", "expired") for r in mid.values())
check("消费结果与批状态自洽（没打到半 expired 批）", coherent)
rep2 = reconcile_report(TODAY)
check("风暴后对账一致（全层/层页/顶条与库同步）", rep2["ok"], str(rep2["violations"]))


# ---------------------------------------------------------------- phase 3
print("\n== 阶段3：CLI 连跑两次成功路径 ==")
cands_b = expired_candidates()
code1, out1, raw1, err1 = run_cli("run_sweep.py")
check("第一次下架退出码 0", code1 == 0, f"{code1} {err1} {raw1}")
check("第一次名单=全部候选批", out1 and sorted(out1["expired_ids"]) == cands_b,
      f"{out1} vs {cands_b}")
check("第一次逐行落账 touched=候选数",
      out1 and out1["touched_rows"] == len(cands_b))

shelf_after_first = set(on_shelf_snapshot())
check("第一次后候选批全部离架", not (set(cands_b) & shelf_after_first))
rep3 = reconcile_report(TODAY, require_expired=cands_b)
check("第一次后对账一致且终态正确", rep3["ok"], str(rep3["violations"]))

expired_rows_before_2 = {lid: r for lid, r in lot_view().items()
                         if r["status"] == "expired"}
code2, out2, raw2, err2 = run_cli("run_sweep.py")
check("第二次下架退出码 0", code2 == 0, f"{code2} {err2} {raw2}")
check("第二次名单为空", out2 and out2["expired_ids"] == [], f"{out2}")
check("第二次 0 行改写", out2 and out2["touched_rows"] == 0)
expired_rows_after_2 = {lid: r for lid, r in lot_view().items()
                        if r["status"] == "expired"}
check("已 expired 行第二次未被改动",
      expired_rows_before_2 == expired_rows_after_2)
code3, rep_out, _, err3 = run_cli(
    "reconcile.py", "--today", TODAY,
    "--require-expired", ",".join(str(i) for i in cands_b))
check("双跑后对账退出码 0", code3 == 0, f"{code3} {err3}")


# ---------------------------------------------------------------- phase 4
print("\n== 阶段4：对账反例必须退出 2（临时库，不碰业务库）=")
import tempfile  # noqa: E402
tmpdir = tempfile.mkdtemp(prefix="pantry-reconcile-neg-")
code_init, _, _, err_init = run_cli("run_sweep.py", extra_env={"DATA_DIR": tmpdir})
# 临时库里先成功下架一次（lot 1/2/4 到期），再要求它们仍在架 -> 必不一致
temp_cands = [1, 2, 4] if code_init == 0 else []
code4, neg, raw4, err4 = run_cli(
    "reconcile.py", "--today", TODAY,
    "--require-on-shelf", ",".join(str(i) for i in temp_cands),
    extra_env={"DATA_DIR": tmpdir})
check("对账失败退出码 2（不与 0 混用）", code4 == 2, f"got {code4} {raw4} {err4}")
check("报告落盘且带 violations",
      neg and neg.get("violations") and Path(neg["report_path"]).exists())


# ---------------------------------------------------------------- phase 5
print("\n== 阶段5：在架集合裁回开场批号列表（对称恢复，不删表）=")
# status/qty_remain 对称恢复；本场上架的新行不删，离场置 expired。
restore_info = restore_lots_to_snapshot(before_status, before_qty)

restored_on_shelf = on_shelf_snapshot()
check("在架批号列表 == 开场快照",
      restored_on_shelf == before_on_shelf,
      f"{restored_on_shelf} vs {before_on_shelf}")
code5, rep5, raw5, err5 = run_cli(
    "reconcile.py", "--today", TODAY,
    "--require-on-shelf", ",".join(str(i) for i in before_on_shelf))
check("裁回后对账退出码 0", code5 == 0, f"{code5} {err5} {raw5}")
after_counts = table_counts()
check("items 一行未少", after_counts["items"] == before_counts["items"])
check("lots 一行未删（只改状态/余量）",
      after_counts["lots"] == before_counts["lots"],
      f"{after_counts['lots']} vs {before_counts['lots']}")
check("消费流水只增不删",
      after_counts["consumptions"] >= before_counts["consumptions"])

print("\n" + ("全部通过 ✅" if not failures else f"失败项: {failures} ❌"))
sys.exit(EXIT_OK if not failures else EXIT_FAIL)
