"""行情后置流程（Task 11 实现，这里先留空桩）。

记账成功后由 add_flow 调用；任何失败都不得影响已落库的明细。
"""

from typing import Optional


def maybe_run(cfg: dict, now: Optional[object] = None) -> None:
    """按时间窗决定是否拉行情；本任务为空实现。"""
    return None