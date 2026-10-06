# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest` / `temp_zone`。

## 过期下架：事务纪律

一次下架 = 一个 `BEGIN IMMEDIATE` 写事务（`app/ops.py: run_expire_sweep`）：

- 候选批要么**整场一起** `expired` 并可见，要么提交失败**整场回滚**，全部仍是
  `on_shelf`。不存在“顶条已无、全层仍在”的半提交。
- 下架写锁在握时，并发消费阻塞等待；回滚后批仍在架（可消费），提交后批已
  `expired`（被 `WHERE status='on_shelf'` 挡掉）。消费不会打到半 expired 的批。
- 所有写语句带 `status='on_shelf'` 行级守护；**连跑第二次名单为空、0 行改写，
  不动任何已 expired 行**。

三件产物分开落地，互不混杂：

| 产物 | 文件 | 说明 |
| --- | --- | --- |
| 入口 | `backend/run_sweep.py` | 跑一次下架；也可直接打 `POST /api/expire-sweep` |
| 故障注入辅助 | `backend/app/engines/fault.py` | 仅读环境变量，业务路径零依赖 |
| 对账报告 | `backend/app/reconcile.py` + `backend/reconcile.py` | 只读重算全层/层页/顶条，报告落 `DATA_DIR/reports/` |

### 退出码（不混用）

`run_sweep.py`：

- `0` 成功提交（含第二次空跑）
- `3` 注入失败，事务已整场回滚
- `4` 未预期错误（不冒充成功）

`reconcile.py`：

- `0` 对账一致
- `2` 对账失败 / 不满足 `--require-on-shelf` / `--require-expired` 终态
- `4` 自身执行异常

### 演练

```bash
# 注入提交失败（整场回滚）
PANTRY_FAULT_POINT=expire_sweep:before_commit python run_sweep.py   # 退出 3

# 成功后连跑第二次（名单空、退出 0）
python run_sweep.py && python run_sweep.py                          # 0 / 0

# 对账（报告同时落 DATA_DIR/reports/）
python reconcile.py --today 2026-10-06

# 端到端验收：回滚三视图 / 并发消费 / 双跑 / 对账反例 / 裁回开场快照
python run_e2e.py
```

`PANTRY_SWEEP_HOLD_SECONDS` 为测试钩子：提交前停留 N 秒，用于在“UPDATE 已执行、
未 commit”窗口制造并发消费；生产不设置即为 0。

### 收尾裁回

`run_e2e.py` 开场记录批号→`status`/`qty_remain` 快照与各业务表行数，结束时
`restore_lots_to_snapshot` 只改 `status`/`qty_remain` 把在架集合裁回开场批号列表，
本场新增批置 `expired` 离场——**一行不 DELETE，不靠删表刷绿**。
