"""故障注入辅助（仅用于演练 / e2e，与业务代码分开落地）。

通过环境变量 PANTRY_FAULT_POINT 声明要在哪个注入点炸：

    PANTRY_FAULT_POINT=expire_sweep:before_commit
        下架事务中：候选批已全部 UPDATE、尚未 commit 时抛 FaultInjected，
        事务必须整场回滚（rollback），不得留下任何半 expired 行。

只允许注入这一个点；未设置或值无法识别即为不注入。
生产环境不设置该变量即可，业务路径对本模块零依赖行为。
"""
import os

VALID_POINTS = frozenset({"expire_sweep:before_commit"})


class FaultInjected(RuntimeError):
    """演练用：模拟提交阶段失败（如写库中断 / 磁盘错误）。"""


def configured_point() -> str | None:
    p = os.environ.get("PANTRY_FAULT_POINT", "").strip()
    return p or None


def inject(point: str) -> None:
    """命中注入点则抛 FaultInjected；调用方必须任其回滚并继续外抛。"""
    p = configured_point()
    if p == point:
        # 命中即自毁，保证同进程内第二次调用不会重复注入（“一次性”语义），
        # 避免连跑时第二次还炸。
        os.environ.pop("PANTRY_FAULT_POINT", None)
        raise FaultInjected(f"injected failure at {point}")
    if p is not None and p not in VALID_POINTS:
        # 无法识别的注入点是配置错误，不能被静默当成“不注入”刷绿。
        raise ValueError(f"unknown PANTRY_FAULT_POINT={p!r}, valid={sorted(VALID_POINTS)}")
