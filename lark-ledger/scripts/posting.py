"""记账核心纯逻辑：收支/转账的余额变动与 WritePlan 构造。

本模块是纯算术层：不 import lark / config / subprocess，不读写文件、网络或时钟。
所有 I/O 与日期→毫秒转换由调用方负责，日期在这里始终保持 "YYYY-MM-DD" 字符串。

约定：
- `WritePlan = {"create": [(表名, 字段dict), ...],
                "update": [(表名, record_id, 字段dict), ...]}`
- 金额在边界统一 `round(x, 2)`，成本价 `round(x, 4)`；
- 账户的「当前余额」对负债为负数（剩余本金），支出让余额更负是自然结果，
  无需按账户类型分支。
"""

import math
import random
from typing import Any, Dict, List, Tuple

# 表名是 WritePlan 的键，集中在此便于下游引用。
ENTRY_TABLE = "收支明细"
ACCOUNT_TABLE = "账户"

# entry["kind"] → 收支明细「类型」单选值。
KIND_LABELS = {"income": "收入", "expense": "支出", "transfer": "转账"}

WritePlan = Dict[str, List[Any]]


class PostingError(ValueError):
    """记账参数不合法：类型、金额、账户、分类等。"""


def _number(value: Any, label: str) -> float:
    """转成有限浮点数；布尔值、非数字、NaN/Inf 都给出可行动的报错。"""
    if isinstance(value, bool):
        raise PostingError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PostingError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise PostingError("%s必须是有限数字，实际是 %r" % (label, value))
    return number


def _kind(entry: Any) -> str:
    if not isinstance(entry, dict):
        raise PostingError("记账参数必须是 dict，实际是 %r" % (entry,))
    kind = entry.get("kind")
    if kind not in KIND_LABELS:
        raise PostingError(
            "不支持的记账类型 %r；只能是 income（收入）/ expense（支出）/ "
            "transfer（转账）" % (kind,)
        )
    return kind


def _amount(entry: dict) -> float:
    """金额恒正，并在此抹平两位小数。"""
    raw = entry.get("amount")
    amount = round(_number(raw, "金额"), 2)
    if amount <= 0:
        raise PostingError(
            "金额必须大于 0，实际是 %r；请检查录入的金额" % (raw,)
        )
    return amount


def _account_label(account_id: str, accounts: Dict[str, dict]) -> str:
    fields = accounts.get(account_id)
    if isinstance(fields, dict):
        name = fields.get("账户名称")
        if isinstance(name, str) and name:
            return "%s（%s）" % (name, account_id)
    return account_id


def _account_id(
    entry: dict,
    key: str,
    label: str,
    accounts: Dict[str, dict],
) -> str:
    """取出账户 record_id，并确认它存在于调用方给的账户快照里。"""
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PostingError("%s不能为空：请提供%s的 record_id" % (label, label))
    account_id = value.strip()
    if not isinstance(accounts, dict) or account_id not in accounts:
        raise PostingError(
            "找不到%s「%s」：请先用 `ledger.py account list` 查看可用账户"
            % (label, account_id)
        )
    return account_id


def _same_account_error(account_id: str, accounts: Dict[str, dict]) -> PostingError:
    return PostingError(
        "转账的转出账户和转入账户不能相同：%s；请确认两个账户不是同一个"
        % _account_label(account_id, accounts)
    )


def validate_entry(entry: dict, accounts: Dict[str, dict]) -> None:
    """校验一笔收支/转账；不合法时抛 PostingError（ValueError 子类）。

    只校验本层能确定的事：类型、金额、账户存在性、转账目标、收支分类；
    分类/账户的名称解析与存在性由上游 I/O 层先完成，这里收 record_id。
    """
    kind = _kind(entry)
    _amount(entry)
    account = _account_id(entry, "account", "账户", accounts)

    if kind == "transfer":
        target = _account_id(entry, "to_account", "转入账户", accounts)
        if target == account:
            raise _same_account_error(account, accounts)
        return

    category = entry.get("category")
    if not isinstance(category, str) or not category.strip():
        raise PostingError(
            "%s必须指定分类（category），例如「支出·餐饮·午餐」"
            % KIND_LABELS[kind]
        )


def balance_deltas(entry: dict, accounts: Dict[str, dict]) -> Dict[str, float]:
    """返回这笔明细对账户余额的变动：id → ±金额。

    支出为负、收入为正、转账是「来源 −、转入 +」。余额变化在 plan_add 里
    与当前余额相加后统一 round 2；这里保持两位小数的增量。
    """
    kind = _kind(entry)
    amount = _amount(entry)
    account = _account_id(entry, "account", "账户", accounts)

    if kind == "expense":
        return {account: round(-amount, 2)}
    if kind == "income":
        return {account: round(amount, 2)}

    target = _account_id(entry, "to_account", "转入账户", accounts)
    if target == account:
        raise _same_account_error(account, accounts)
    return {account: round(-amount, 2), target: round(amount, 2)}


def _current_balance(account_id: str, accounts: Dict[str, dict]) -> float:
    fields = accounts.get(account_id)
    if not isinstance(fields, dict) or "当前余额" not in fields:
        raise PostingError(
            "账户「%s」缺少「当前余额」字段，无法更新余额；请检查账户表"
            % _account_label(account_id, accounts)
        )
    return _number(
        fields["当前余额"],
        "账户「%s」的当前余额" % _account_label(account_id, accounts),
    )


def plan_add(
    entry: dict,
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
) -> WritePlan:
    """把一笔收支/转账翻译成 WritePlan。

    create 一条「收支明细」，update 每个受影响账户的「当前余额」；
    调用方必须先 create 明细、再 update 余额（顺序由 I/O 层保证）。
    `holdings` 供后续投资分支使用，收支/转账不读写持仓。
    """
    validate_entry(entry, accounts)
    kind = _kind(entry)
    amount = _amount(entry)
    account = _account_id(entry, "account", "账户", accounts)

    fields: Dict[str, Any] = {
        "摘要": entry.get("summary") or "",
        "日期": entry.get("date"),
        "类型": [KIND_LABELS[kind]],
        "金额": amount,
        "账户": [{"id": account}],
    }
    if kind == "transfer":
        target = _account_id(entry, "to_account", "转入账户", accounts)
        fields["转入账户"] = [{"id": target}]
    else:
        category = entry.get("category").strip()
        fields["分类"] = [{"id": category}]
    fields["备注"] = entry.get("note") or ""

    updates: List[Tuple[str, str, dict]] = []
    for account_id, delta in balance_deltas(entry, accounts).items():
        balance = round(_current_balance(account_id, accounts) + delta, 2)
        updates.append((ACCOUNT_TABLE, account_id, {"当前余额": balance}))

    return {"create": [(ENTRY_TABLE, fields)], "update": updates}


def weighted_cost(
    old_shares: float,
    old_cost: float,
    add_shares: float,
    price: float,
) -> float:
    """买入后的加权成本价：(原份额×原成本 + 新增份额×成交价) / 总份额，round 4。"""
    old_shares_value = _number(old_shares, "原持仓份额")
    old_cost_value = _number(old_cost, "原成本价")
    add_shares_value = _number(add_shares, "新增份额")
    price_value = _number(price, "成交价")

    if old_shares_value < 0 or add_shares_value < 0:
        raise PostingError(
            "持仓份额不能为负数：原份额 %r，新增份额 %r"
            % (old_shares, add_shares)
        )
    if old_cost_value < 0 or price_value < 0:
        raise PostingError(
            "成本价/成交价不能为负数：原成本 %r，成交价 %r"
            % (old_cost, price)
        )

    total_shares = old_shares_value + add_shares_value
    if total_shares <= 0:
        return 0.0
    total_cost = (
        old_shares_value * old_cost_value + add_shares_value * price_value
    )
    return round(total_cost / total_shares, 4)


def realized_pnl(proceeds: float, cost: float, shares: float) -> float:
    """卖出已实现盈亏 = 到账金额 − 成本价×卖出份额，round 2（亏损为负）。"""
    proceeds_value = _number(proceeds, "到账金额")
    cost_value = _number(cost, "成本价")
    shares_value = _number(shares, "卖出份额")
    return round(proceeds_value - cost_value * shares_value, 2)


def group_tag() -> str:
    """生成同组明细的备注 tag（如 " #G1a2b3c4d"），32 位随机数足够避免碰撞。"""
    return " #G%08x" % random.getrandbits(32)