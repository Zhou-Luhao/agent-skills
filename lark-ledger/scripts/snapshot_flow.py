"""资产快照后置流程（Task 12 实现，这里先留空桩）。

记账成功后由 add_flow 调用；任何失败都不得影响已落库的明细。
"""

from typing import Optional


def maybe_weekly(cfg: dict, now: Optional[object] = None) -> None:
    """按周决定是否写资产快照；本任务为空实现。"""
    return None