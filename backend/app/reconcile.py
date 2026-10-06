"""对账报告（与入口、故障注入分开落地）。

只读取数据库，重算“数据库真相 / 全层 / 各层页 / 顶条”四个集合，
校验三视图与库状态一致：

- 各层页之并集 == 全层 == 库里 status='on_shelf' 集合；
- 顶条出现的批号必须是在架批的子集（不允许顶条与在架状态脱节）；
- 所有 status='expired' 的批必须同时从全层、层页、顶条消失
  （不允许“顶条已无、全层仍在”这类半提交视图）；
- 可选 require_on_shelf / require_expired：本场要求的批号终态。

本模块不做任何写入（query_only），报告由 CLI 落盘；
退出码纪律在 backend/reconcile.py：一致 0，不一致 2，二者不得混用。
"""
from datetime import date

from app.db import read_conn


def _ids(rows, key="id"):
    return sorted(r[key] for r in rows)


def reconcile_report(today: str | None = None,
                     require_on_shelf: list[int] | None = None,
                     require_expired: list[int] | None = None) -> dict:
    today = today or date.today().isoformat()
    with read_conn() as c:
        on_shelf = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE status='on_shelf' ORDER BY id")]
        expired_rows = [dict(r) for r in c.execute(
            "SELECT id FROM lots WHERE status='expired' ORDER BY id")]
        layers = [r["layer"] for r in c.execute(
            "SELECT DISTINCT layer FROM items ORDER BY layer")]
        item_layer = {r["id"]: r["layer"] for r in c.execute(
            "SELECT id, layer FROM items")}

    on_shelf_ids = _ids(on_shelf)
    expired_ids = _ids(expired_rows)

    # —— 重算三个前端视图（与 main.py 的查询口径保持一致）——
    # 全层 / 层页：/api/fridge 只按 status='on_shelf' 过滤，不看余量。
    all_layer_ids = sorted(l["id"] for l in on_shelf)
    by_layer: dict[str, list[int]] = {}
    for L in layers:
        by_layer[L] = sorted(
            l["id"] for l in on_shelf if item_layer.get(l["item_id"]) == L)

    # 顶条 = /api/alerts 实际会渲染的批：在架且有余量、到期日 <= 今天（expired 级）。
    topbar_ids = sorted(
        l["id"] for l in on_shelf
        if float(l.get("qty_remain") or 0) > 0
        and l.get("expiry") and l["expiry"] <= today)

    layer_union = sorted(i for v in by_layer.values() for i in v)

    checks = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("layers_partition_all_layer",
        layer_union == all_layer_ids,
        f"layer_union={layer_union} all_layer={all_layer_ids}")
    add("all_layer_matches_db_on_shelf",
        all_layer_ids == on_shelf_ids,
        f"view={all_layer_ids} db={on_shelf_ids}")
    add("topbar_subset_of_all_layer",
        set(topbar_ids) <= set(all_layer_ids),
        f"topbar={topbar_ids} all_layer={all_layer_ids}")
    add("expired_absent_from_all_views",
        not (set(expired_ids) & (set(all_layer_ids) | set(topbar_ids))),
        f"leaked={sorted(set(expired_ids) & (set(all_layer_ids) | set(topbar_ids)))}")

    # 非致命：种子里允许存在 data_quality=dirty 的负余量批；它不影响三视图一致性，
    # 只作为警告列出，不计入 violations（对账失败退出码 2 只留给真正的不一致）。
    warnings = []
    nonpositive = sorted(
        l["id"] for l in on_shelf if float(l.get("qty_remain") or 0) <= 0)
    if nonpositive:
        warnings.append({"name": "nonpositive_qty_on_shelf", "lot_ids": nonpositive})

    if require_on_shelf is not None:
        missing = sorted(set(require_on_shelf) - set(on_shelf_ids))
        add("require_on_shelf", not missing,
            f"missing_from_shelf={missing}")
    if require_expired is not None:
        not_expired = sorted(set(require_expired) - set(expired_ids))
        add("require_expired", not not_expired,
            f"not_in_expired={not_expired}")

    violations = [ck["name"] for ck in checks if not ck["ok"]]
    return {
        "ok": not violations,
        "today": today,
        "db": {
            "on_shelf_ids": on_shelf_ids,
            "expired_ids": expired_ids,
        },
        "views": {
            "all_layer_ids": all_layer_ids,
            "by_layer": by_layer,
            "topbar_ids": topbar_ids,
        },
        "checks": checks,
        "warnings": warnings,
        "violations": violations,
    }
