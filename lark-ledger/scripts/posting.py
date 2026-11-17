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

import datetime
import math
import random
import re
from typing import Any, Callable, Dict, List, Tuple

# 表名是 WritePlan 的键，集中在此便于下游引用。
ENTRY_TABLE = "收支明细"
ACCOUNT_TABLE = "账户"
HOLDING_TABLE = "持仓"

# 本层支持的 kind 单独成集：KIND_LABELS 只是「明细类型」的显示名，
# Task 8 会增加新的 label，但不能因此让本层接受尚未实现的 kind。
SUPPORTED_KINDS = (
    "income",
    "expense",
    "transfer",
    "invest_buy",
    "invest_sell",
    "dividend",
)
KIND_LABELS = {
    "income": "收入",
    "expense": "支出",
    "transfer": "转账",
    "invest_buy": "理财买入",
    "invest_sell": "理财卖出",
    "dividend": "理财分红",
}
INVEST_KINDS = ("invest_buy", "invest_sell")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
    if kind not in SUPPORTED_KINDS:
        supported = " / ".join(
            "%s（%s）" % (name, KIND_LABELS[name]) for name in SUPPORTED_KINDS
        )
        raise PostingError(
            "不支持的记账类型 %r；当前只支持 %s" % (kind, supported)
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


def _present(value: Any) -> bool:
    """字段是否被真正赋值：None、空串、纯空白都视为未提供。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _entry_date(entry: dict) -> str:
    """日期只接受 "YYYY-MM-DD"（不在这里补今天，默认值由 add_flow 负责）。"""
    value = entry.get("date")
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        raise PostingError(
            "日期必须是 YYYY-MM-DD 格式的字符串，实际是 %r；例如 2026-11-17"
            % (value,)
        )
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise PostingError(
            "日期 %r 不存在；请检查月份和日期（例如 2026-02-30 不合法）"
            % (value,)
        )
    return value


def _reject_present(entry: dict, key: str, message: str) -> None:
    if _present(entry.get(key)):
        raise PostingError(message)


def _number_text(value: float) -> str:
    """份额/价格在报错里的可读形式：整数不带 ".0"，其余保留原值。"""
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text or "0"


def shares_from_amount(amount: float, price: float) -> float:
    """按成交金额换算份额：round(金额 / 成交价, 2)，用于"一万元买基金"式录入。"""
    amount_value = round(_number(amount, "金额"), 2)
    if amount_value <= 0:
        raise PostingError(
            "金额必须大于 0，实际是 %r；请检查录入的金额" % (amount,)
        )
    price_value = _number(price, "成交价")
    if price_value <= 0:
        raise PostingError(
            "成交价必须大于 0，实际是 %r；请检查录入的成交价/净值" % (price,)
        )
    return round(amount_value / price_value, 2)


def _price(entry: dict) -> float:
    """成交价/净值：必须大于 0，边界统一 round 4。"""
    raw = entry.get("price")
    price = round(_number(raw, "成交价"), 4)
    if price <= 0:
        raise PostingError(
            "成交价必须大于 0，实际是 %r；请检查录入的成交价/净值" % (raw,)
        )
    return price


def _shares(entry: dict, kind: str) -> float:
    """买卖份额：必须大于 0；买入缺省份额时允许用金额 ÷ 成交价换算。"""
    raw = entry.get("shares")
    if _present(raw):
        shares = round(_number(raw, "份额"), 4)
    elif kind == "invest_buy" and _present(entry.get("amount")):
        shares = shares_from_amount(entry.get("amount"), entry.get("price"))
    else:
        raise PostingError(
            "理财买卖必须指定份额（shares）；买入也可以用金额（amount）"
            " ÷ 成交价换算份额"
        )
    if shares <= 0:
        raise PostingError(
            "份额必须大于 0，实际是 %r；请检查录入的份额" % (raw,)
        )
    return shares


def _invest_amount(entry: dict) -> float:
    """理财买卖金额 = round(成交价 × 份额, 2)。"""
    kind = _kind(entry)
    return round(_price(entry) * _shares(entry, kind), 2)


def _amount_for(kind: str, entry: dict) -> float:
    """按 kind 取金额：投资买卖由价格×份额推导，其余用用户录入的金额。"""
    if kind in INVEST_KINDS:
        return _invest_amount(entry)
    return _amount(entry)


def _holding_label(holding_id: str, holdings: Dict[str, dict]) -> str:
    fields = holdings.get(holding_id)
    if isinstance(fields, dict):
        name = fields.get("持仓名称")
        if isinstance(name, str) and name:
            return "%s（%s）" % (name, holding_id)
    return holding_id


def _holding_id(entry: dict, holdings: Dict[str, dict]) -> str:
    """取出持仓 record_id，并确认它存在于调用方给的持仓快照里。"""
    value = entry.get("holding")
    if not isinstance(value, str) or not value.strip():
        raise PostingError(
            "理财买卖必须指定持仓（holding）：请提供持仓代码、名称或 record_id"
        )
    holding_id = value.strip()
    if not isinstance(holdings, dict) or holding_id not in holdings:
        raise PostingError(
            "找不到持仓「%s」：请先用 `ledger.py holding list` 查看可用持仓"
            % holding_id
        )
    return holding_id


def _holding_shares(holding_id: str, holdings: Dict[str, dict]) -> float:
    fields = holdings.get(holding_id)
    if not isinstance(fields, dict) or "当前份额" not in fields:
        raise PostingError(
            "持仓「%s」缺少「当前份额」字段，无法买卖；请检查持仓表"
            % _holding_label(holding_id, holdings)
        )
    return _number(
        fields["当前份额"],
        "持仓「%s」的当前份额" % _holding_label(holding_id, holdings),
    )


def _holding_cost(holding_id: str, holdings: Dict[str, dict]) -> float:
    fields = holdings.get(holding_id) or {}
    return _number(
        fields.get("成本价", 0),
        "持仓「%s」的成本价" % _holding_label(holding_id, holdings),
    )


def _tagged_note(entry: dict, tag: str) -> str:
    """一组明细共用的备注 = 用户备注 + 组 tag（如 " #G1a2b3c4d"）。"""
    return "%s%s" % (entry.get("note") or "", tag)


def _validate_cash(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """收入/支出：必有分类、不得带转入账户。"""
    _reject_present(
        entry,
        "to_account",
        "%s不需要转入账户（to_account）：只有转账（transfer）才有转入账户；"
        "请确认记账类型" % KIND_LABELS[kind],
    )
    category = entry.get("category")
    if not isinstance(category, str) or not category.strip():
        raise PostingError(
            "%s必须指定分类（category），例如「支出·餐饮·午餐」"
            % KIND_LABELS[kind]
        )


def _validate_transfer(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """转账：必有不同转入账户、不得带分类。"""
    target = _account_id(entry, "to_account", "转入账户", accounts)
    if target == account:
        raise _same_account_error(account, accounts)
    _reject_present(
        entry,
        "category",
        "转账不需要分类（category）：分类只用于收入/支出；请去掉分类参数",
    )


def _validate_invest_buy(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """理财买入：价格/份额/持仓/转出账户齐全；理财账户只作为买入目的地。"""
    _price(entry)
    _shares(entry, kind)
    _holding_id(entry, holdings)
    source = _account_id(entry, "from_account", "转出账户", accounts)
    if source == account:
        raise PostingError(
            "理财买入的转出账户不能与理财账户相同：%s；"
            "请确认资金来自银行卡/现金账户"
            % _account_label(account, accounts)
        )
    _reject_present(
        entry,
        "to_account",
        "理财买入不需要转入账户（to_account）：份额买入由脚本记录；"
        "请确认记账类型",
    )
    _reject_present(
        entry,
        "category",
        "理财买入不需要分类（category）：买卖明细的类型固定为转账；"
        "请去掉分类参数",
    )
    _reject_present(
        entry,
        "pnl_category",
        "理财买入不需要已实现盈亏分类（pnl_category）：只有卖出才计算盈亏",
    )


def _validate_invest_sell(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """理财卖出：持仓/价格/份额/转入账户/盈亏分类齐全，且份额不得超过持有。"""
    _price(entry)
    shares = _shares(entry, kind)
    holding_id = _holding_id(entry, holdings)
    target = _account_id(entry, "to_account", "转入账户", accounts)
    if target == account:
        raise PostingError(
            "理财卖出的转入账户不能与理财账户相同：%s；"
            "请确认资金回到哪个银行卡/现金账户"
            % _account_label(account, accounts)
        )
    _reject_present(
        entry,
        "from_account",
        "理财卖出不需要转出账户（from_account）：资金从理财账户转出；"
        "请确认记账类型",
    )
    category = entry.get("pnl_category")
    if not isinstance(category, str) or not category.strip():
        raise PostingError(
            "理财卖出必须指定已实现盈亏分类（pnl_category）："
            "盈利记收入、亏损记负收入；请提供分类 record_id"
        )
    available = _holding_shares(holding_id, holdings)
    if shares > available + 1e-9:
        raise PostingError(
            "卖出份额 %s 超过持仓「%s」的当前份额 %s；可用份额 %s"
            % (
                _number_text(shares),
                _holding_label(holding_id, holdings),
                _number_text(available),
                _number_text(available),
            )
        )


def _validate_dividend(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """理财分红：金额已在 validate_entry 校验，这里要求收入分类且不带买卖字段。"""
    category = entry.get("pnl_category")
    if not isinstance(category, str) or not category.strip():
        raise PostingError(
            "理财分红必须指定分类（pnl_category），例如「收入·理财收益·分红」；"
            "请提供分类 record_id"
        )
    _reject_present(
        entry,
        "to_account",
        "理财分红不需要转入账户（to_account）：分红直接进入理财账户现金",
    )
    _reject_present(
        entry,
        "from_account",
        "理财分红不需要转出账户（from_account）：分红直接进入理财账户现金",
    )
    for key, label in (("holding", "持仓"), ("price", "成交价"), ("shares", "份额")):
        _reject_present(
            entry,
            key,
            "理财分红不需要%s（%s）：分红只记收入与账户余额；"
            "如果是卖出请用 --kind invest_sell" % (label, key),
        )


VALIDATORS: Dict[str, Callable[..., None]] = {
    "income": _validate_cash,
    "expense": _validate_cash,
    "transfer": _validate_transfer,
    "invest_buy": _validate_invest_buy,
    "invest_sell": _validate_invest_sell,
    "dividend": _validate_dividend,
}


def validate_entry(
    entry: dict,
    accounts: Dict[str, dict],
    holdings: Dict[str, dict] = None,
) -> None:
    """校验一笔记账；不合法时抛 PostingError（ValueError 子类）。

    只校验本层能确定的事：类型、日期、金额、账户/持仓存在性、转账目标、
    收支分类；名称解析与存在性由上游 I/O 层先完成，这里收 record_id。
    """
    kind = _kind(entry)
    _entry_date(entry)
    if kind in INVEST_KINDS:
        # 投资买卖的金额由价格×份额推导，用户录入的 amount 只作买入换算。
        _price(entry)
        _shares(entry, kind)
    else:
        _amount(entry)
    account = _account_id(entry, "account", "账户", accounts)
    VALIDATORS[kind](kind, entry, accounts, account, holdings)


def balance_deltas(entry: dict, accounts: Dict[str, dict]) -> Dict[str, float]:
    """返回这笔明细对账户余额的变动：id → ±金额。

    支出为负、收入为正、转账是「来源 −、转入 +」。余额变化在 plan_add 里
    与当前余额相加后统一 round 2；这里保持两位小数的增量。
    """
    kind = _kind(entry)
    amount = _amount_for(kind, entry)
    account = _account_id(entry, "account", "账户", accounts)

    if kind == "expense":
        return {account: round(-amount, 2)}
    if kind == "income":
        return {account: round(amount, 2)}
    if kind == "invest_buy":
        source = _account_id(entry, "from_account", "转出账户", accounts)
        return {source: round(-amount, 2)}
    if kind == "invest_sell":
        target = _account_id(entry, "to_account", "转入账户", accounts)
        return {target: round(amount, 2)}
    if kind == "dividend":
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


def _detail_fields(
    kind: str,
    entry: dict,
    account: str,
    amount: float,
) -> Dict[str, Any]:
    """收支明细的公共字段；日期保持字符串，毫秒转换留给 I/O 层。"""
    return {
        "摘要": entry.get("summary") or "",
        "日期": entry.get("date"),
        "类型": [KIND_LABELS[kind]],
        "金额": amount,
        "账户": [{"id": account}],
        "备注": entry.get("note") or "",
    }


def _plan_cash(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> Dict[str, Any]:
    fields = _detail_fields(kind, entry, account, amount)
    fields["分类"] = [{"id": entry["category"].strip()}]
    return fields


def _plan_transfer(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> Dict[str, Any]:
    fields = _detail_fields(kind, entry, account, amount)
    target = _account_id(entry, "to_account", "转入账户", accounts)
    fields["转入账户"] = [{"id": target}]
    return fields


def _trade_detail(
    entry: dict,
    account: str,
    amount: float,
    holding_id: str,
    price: float,
    shares: float,
    note: str,
    target: str,
) -> Dict[str, Any]:
    """买卖明细行：类型=转账，账户与转入账户同为理财账户（现金↔持仓自转）。"""
    fields = _detail_fields("transfer", entry, account, amount)
    fields["转入账户"] = [{"id": target}]
    fields["持仓"] = [{"id": holding_id}]
    fields["成交价"] = price
    fields["份额"] = shares
    fields["备注"] = note
    return fields


def _plan_invest_buy(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """买入两条明细：①银行卡→理财转账；②理财现金→持仓自转。"""
    source = _account_id(entry, "from_account", "转出账户", accounts)
    holding_id = _holding_id(entry, holdings)
    price = _price(entry)
    shares = _shares(entry, kind)
    note = _tagged_note(entry, group_tag())

    transfer = _detail_fields("transfer", entry, source, amount)
    transfer["转入账户"] = [{"id": account}]
    transfer["备注"] = note
    trade = _trade_detail(
        entry, account, amount, holding_id, price, shares, note, account
    )
    return [(ENTRY_TABLE, transfer), (ENTRY_TABLE, trade)]


def _plan_invest_sell(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """卖出三条明细：①理财→银行卡转账；②已实现盈亏收入；③份额→现金自转。"""
    target = _account_id(entry, "to_account", "转入账户", accounts)
    holding_id = _holding_id(entry, holdings)
    price = _price(entry)
    shares = _shares(entry, kind)
    note = _tagged_note(entry, group_tag())

    transfer = _detail_fields("transfer", entry, account, amount)
    transfer["转入账户"] = [{"id": target}]
    transfer["备注"] = note

    pnl = realized_pnl(amount, _holding_cost(holding_id, holdings), shares)
    pnl_entry = dict(entry, summary=entry.get("summary") or "已实现盈亏")
    pnl_row = _detail_fields("income", pnl_entry, account, pnl)
    pnl_row["分类"] = [{"id": entry["pnl_category"].strip()}]
    pnl_row["备注"] = note

    trade = _trade_detail(
        entry, account, amount, holding_id, price, shares, note, account
    )
    return [(ENTRY_TABLE, transfer), (ENTRY_TABLE, pnl_row), (ENTRY_TABLE, trade)]


def _plan_dividend(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """分红一条收入明细，分类由 add_flow 按用户口述解析后放进 pnl_category。"""
    fields = _detail_fields("income", entry, account, amount)
    fields["分类"] = [{"id": entry["pnl_category"].strip()}]
    fields["备注"] = _tagged_note(entry, group_tag())
    return [(ENTRY_TABLE, fields)]


PLANNERS: Dict[str, Callable[..., Any]] = {
    "income": _plan_cash,
    "expense": _plan_cash,
    "transfer": _plan_transfer,
    "invest_buy": _plan_invest_buy,
    "invest_sell": _plan_invest_sell,
    "dividend": _plan_dividend,
}


def _holding_update(
    kind: str,
    entry: dict,
    holdings: Dict[str, dict],
) -> List[Tuple[str, str, dict]]:
    """买卖后的持仓：份额增减、买入重算加权成本、卖光置已清仓。"""
    holding_id = _holding_id(entry, holdings)
    current = _holding_shares(holding_id, holdings)
    shares = _shares(entry, kind)

    if kind == "invest_buy":
        cost = _holding_cost(holding_id, holdings)
        return [
            (
                HOLDING_TABLE,
                holding_id,
                {
                    "当前份额": round(current + shares, 4),
                    "成本价": weighted_cost(current, cost, shares, _price(entry)),
                    # 已清仓的持仓再次买入即恢复持有，避免报表继续跳过它。
                    "状态": ["持有"],
                },
            )
        ]

    remaining = round(current - shares, 4)
    if abs(remaining) < 1e-9:
        remaining = 0.0
    update: Dict[str, Any] = {"当前份额": remaining}
    if remaining == 0:
        update["状态"] = ["已清仓"]
    return [(HOLDING_TABLE, holding_id, update)]


def plan_add(
    entry: dict,
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
) -> WritePlan:
    """把一笔记账翻译成 WritePlan。

    create 一条或多条「收支明细」，update 每个受影响账户的「当前余额」；
    投资买卖还会 update 持仓份额/成本。调用方必须先 create 明细、
    再 update 余额/持仓（顺序由 I/O 层保证）。
    """
    validate_entry(entry, accounts, holdings)
    kind = _kind(entry)
    amount = _amount_for(kind, entry)
    account = _account_id(entry, "account", "账户", accounts)
    planned = PLANNERS[kind](kind, entry, accounts, account, amount, holdings)
    creates = (
        [(ENTRY_TABLE, planned)]
        if isinstance(planned, dict)
        else list(planned)
    )

    updates: List[Tuple[str, str, dict]] = []
    for account_id, delta in balance_deltas(entry, accounts).items():
        balance = round(_current_balance(account_id, accounts) + delta, 2)
        updates.append((ACCOUNT_TABLE, account_id, {"当前余额": balance}))
    if kind in INVEST_KINDS:
        updates.extend(_holding_update(kind, entry, holdings))

    return {"create": creates, "update": updates}


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
