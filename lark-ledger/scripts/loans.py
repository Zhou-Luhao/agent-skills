"""贷款本息拆分纯逻辑。

单位契约（Task 3 已定稿，勿再改）：Base「年利率」字段是 percentage 样式，
单元格里存的是**小数**（4.2% ⇒ 0.042）。本模块的 `annual_rate_fraction`
直接接收该原值，公式 `interest = round(remaining × rate / 12, 2)`，
**任何地方都不再除以 100**；把 4.2 当小数传进来会被 0..1 边界直接拒绝。

本模块是纯算术层：不 import lark / config / subprocess，不读写文件、网络或时钟。
"""

import math
from typing import Any, Tuple

# 利率是小数口径：4.2% = 0.042；合法区间含端点 [0, 1]。
_MIN_RATE = 0.0
_MAX_RATE = 1.0


class LoanError(ValueError):
    """贷款还款参数不合法：利率单位、还款总额、剩余本金或提前还款边界。"""


def _number(value: Any, label: str) -> float:
    """转成有限浮点数；布尔值、非数字、NaN/Inf 都给出可行动的报错。"""
    if isinstance(value, bool):
        raise LoanError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LoanError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise LoanError("%s必须是有限数字，实际是 %r" % (label, value))
    return number


def split_payment(
    remaining: float,
    annual_rate_fraction: float,
    total: float,
) -> Tuple[float, float]:
    """把一笔还款拆成 `(利息, 本金)`，两者都 round 到分。

    - `remaining`：剩余本金，**正数**（负债账户余额是负数，调用方先取反）；
    - `annual_rate_fraction`：Base 年利率单元格原值（4.2% ⇒ 0.042），不再 /100；
    - `total`：用户实际还款总额。

    提前还款/利率变动等需要人工核对的边界一律抛 `LoanError`，不静默截断。
    """
    remaining_value = _number(remaining, "剩余本金")
    rate = _number(annual_rate_fraction, "年利率")
    total_value = _number(total, "还款总额")

    if total_value <= 0:
        raise LoanError(
            "还款总额必须大于 0，实际是 %r；请检查用户的还款金额"
            % (total,)
        )
    if remaining_value <= 0:
        raise LoanError(
            "剩余本金必须大于 0，实际是 %r；负债余额应为负数（剩余本金），"
            "若贷款已还清请勿再记还款" % (remaining,)
        )
    if not _MIN_RATE <= rate <= _MAX_RATE:
        raise LoanError(
            "年利率必须按小数传入且在 %g~%g 之间（4.2%% 传 0.042），实际是 %r；"
            "脚本使用 Base 单元格原值，内部不再除以 100"
            % (_MIN_RATE, _MAX_RATE, rate)
        )

    # 月利率 = 年利率 / 12；利率已是小数，绝不再 /100。
    interest = round(remaining_value * rate / 12.0, 2)
    principal = round(total_value - interest, 2)

    if principal < 0:
        raise LoanError(
            "还款总额 %.2f 不足以覆盖本期利息 %.2f（本金 %.2f）；"
            "请检查还款金额或年利率，确认后重新记账"
            % (total_value, interest, principal)
        )
    if principal > remaining_value:
        raise LoanError(
            "本金 %.2f 超过剩余本金 %.2f：疑似提前还款或金额异常；"
            "v1 不做提前还款计算，请人工核对后再记账"
            % (principal, remaining_value)
        )

    return interest, principal