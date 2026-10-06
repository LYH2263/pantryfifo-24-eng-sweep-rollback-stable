"""业务操作层：过期下架与 FEFO 消费。

事务纪律（与入口、故障注入、对账报告分开落地，本模块只管业务）：

- 一次下架 = 一个 BEGIN IMMEDIATE 事务：候选批要么全部 expired 并可见，
  要么整场回滚全部仍是 on_shelf。不存在“顶条已无、全层仍在”的半提交。
- 下架期间写锁在握，并发消费只能在 commit/rollback 之后拿到写锁：
  回滚后批仍是 on_shelf（可消费），提交后批已是 expired（被 WHERE 挡掉），
  消费永远不会打到半 expired 的批。
- 所有写语句都带 status='on_shelf' 行级守护，已 expired/consumed 的行不被二次改写。
"""
import json
import os
import time
from datetime import date, datetime, timezone

from app.db import read_conn, write_tx
from app.engines.fault import inject
from app.engines.fefo import consume_fefo, expire_lots


class ConsumeConflict(RuntimeError):
    """事务内发现批次状态与读到的快照不一致（并发竞争的兜底）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_expire_sweep(today: str | None = None) -> dict:
    """整场事务执行一次过期下架，返回结构化结果。

    成功（含“没有候选批”的空跑）正常返回；注入失败等异常由 write_tx
    保证 rollback 后继续抛出，调用方不得吞掉。
    """
    today = today or date.today().isoformat()
    with write_tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(rows, today)
        # 必须在 UPDATE 前统计：它表示“此前已 expired、本次不会再被碰”的行数。
        already = c.execute(
            "SELECT COUNT(*) n FROM lots WHERE status='expired'").fetchone()["n"]

        touched = 0
        if ids:
            # 行级守护：只有仍在架且确有余量的行允许翻转，
            # 已 expired 的行绝不被二次 UPDATE（第二次跑名单为空，自然 0 行）。
            qmarks = ",".join("?" for _ in ids)
            cur = c.execute(
                f"UPDATE lots SET status='expired' "
                f"WHERE id IN ({qmarks}) AND status='on_shelf' AND qty_remain>0",
                ids,
            )
            touched = cur.rowcount
            if touched != len(ids):
                # 写锁内理论上不可能发生；发生即说明不变量被破坏，宁回滚不刷绿。
                raise ConsumeConflict(
                    f"sweep candidates={len(ids)} but touched={touched}")

        c.execute(
            "INSERT INTO sweep_runs(created_at, today, expired_ids_json, touched_rows) "
            "VALUES (?,?,?,?)",
            (_now(), today, ",".join(str(i) for i in ids), touched),
        )

        # 测试钩子：提交前停留 PANTRY_SWEEP_HOLD_SECONDS 秒（不设置=0，生产零影响），
        # 让 e2e 能在“UPDATE 已执行、未 commit”的窗口里启动并发消费，
        # 验证写锁在握时消费打不进来、回滚后也打不到半 expired 的批。
        hold = float(os.environ.get("PANTRY_SWEEP_HOLD_SECONDS", "0") or 0)
        if hold > 0:
            time.sleep(hold)

        # 唯一注入点：全部 UPDATE 已执行、尚未 commit。炸了必须整场回滚。
        inject("expire_sweep:before_commit")

    return {
        "ran_at": _now(),
        "today": today,
        "expired_ids": ids,
        "touched_rows": touched,
        "already_expired_total": already,
    }


def run_consume(item_id: int, qty: float, note: str = "") -> dict:
    """整场事务执行一次 FEFO 消费，返回 FEFO 计划结果。"""
    with write_tx() as c:
        row = c.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError("item")
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
            (item_id,))]
        result = consume_fefo(lots, qty)
        if not result["ok"]:
            # 业务拒绝（数量非正 / 库存不足）：不做任何写入，回滚后外抛。
            raise _ConsumeRejected(result)

        for d in result["deductions"]:
            cur = c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? "
                "WHERE id=? AND status='on_shelf' AND qty_remain >= ?",
                (d["take"], d["lot_id"], d["take"]),
            )
            if cur.rowcount != 1:
                # 该批在本事务期间已不是“在架且足量”——拒绝扣到半 expired 批。
                raise ConsumeConflict(f"lot {d['lot_id']} changed during consume")
            rem = c.execute(
                "SELECT qty_remain FROM lots WHERE id=? AND status='on_shelf'",
                (d["lot_id"],)).fetchone()
            if rem is None:
                raise ConsumeConflict(f"lot {d['lot_id']} vanished from shelf")
            if rem["qty_remain"] <= 0:
                done = c.execute(
                    "UPDATE lots SET status='consumed', qty_remain=0 "
                    "WHERE id=? AND status='on_shelf' AND qty_remain<=0",
                    (d["lot_id"],))
                if done.rowcount != 1:
                    raise ConsumeConflict(f"lot {d['lot_id']} close failed")

        c.execute(
            "INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
            (note, json.dumps(result), _now()),
        )
    return result


class _ConsumeRejected(Exception):
    def __init__(self, result: dict):
        self.result = result


def on_shelf_snapshot() -> list[int]:
    """本场开始时在架批号列表（只读）。"""
    with read_conn() as c:
        return [r["id"] for r in c.execute(
            "SELECT id FROM lots WHERE status='on_shelf' ORDER BY id")]


def lot_status_snapshot() -> dict[int, str]:
    """本场开始时批号 -> status 的全量快照（只读），供结束时对称恢复。"""
    with read_conn() as c:
        return {r["id"]: r["status"] for r in c.execute(
            "SELECT id, status FROM lots")}


def restore_lots_to_snapshot(before_status: dict[int, str],
                             before_qty: dict[int, float] | None = None) -> dict:
    """按开场快照把 lots 对称恢复（status，必要时含 qty_remain）。

    - 开场即存在的批：status 还原；给了 before_qty 则 qty_remain 一并还原；
    - 本场新增的批（不在快照里）：不删行，置为 expired 离场；
    - 绝不 DELETE 业务表、不碰 items/consumptions/sweep_runs。

    用于 e2e 收尾：把“在架批号列表”精确裁回开场，而不是删光业务表刷绿。
    """
    with write_tx() as c:
        current = [dict(r) for r in c.execute("SELECT id, status FROM lots")]
        restored: list[dict] = []
        for r in current:
            lid = r["id"]
            if lid in before_status:
                want_st = before_status[lid]
                qty = before_qty.get(lid) if before_qty is not None else None
                if qty is not None:
                    c.execute(
                        "UPDATE lots SET status=?, qty_remain=? WHERE id=?",
                        (want_st, qty, lid))
                else:
                    c.execute("UPDATE lots SET status=? WHERE id=?", (want_st, lid))
                restored.append({"lot_id": lid, "status": want_st})
            else:
                c.execute("UPDATE lots SET status='expired' WHERE id=?", (lid,))
                restored.append({"lot_id": lid, "status": "expired", "new_in_run": True})
    return {"restored": restored, "on_shelf_after": on_shelf_snapshot()}
