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
from typing import Any, Callable, Dict, List, Optional, Tuple

import loans

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
    "loan_repay",
)
KIND_LABELS = {
    "income": "收入",
    "expense": "支出",
    "transfer": "转账",
    "invest_buy": "理财买入",
    "invest_sell": "理财卖出",
    "dividend": "理财分红",
    "loan_repay": "贷款还款",
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


INVESTMENT_ACCOUNT_TYPE = "理财"
LOAN_ACCOUNT_TYPE = "负债"


def _number_text(value: float) -> str:
    """份额/价格在报错里的可读形式：整数不带 ".0"，其余保留原值。"""
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text or "0"


def _select_text(value: Any) -> str:
    """单选单元格 → 文本；兼容字符串、字符串列表与 [{text/name/id}]。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)):
        return ""
    parts: List[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key in ("text", "name", "id"):
                if isinstance(item.get(key), str):
                    parts.append(item[key])
                    break
    return "、".join(parts)


def _require_investment_account(
    account_id: str, accounts: Dict[str, dict]
) -> None:
    """买卖/分红必须落在「类型=理财」的账户上，否则资金口径会被记错账户。"""
    fields = accounts.get(account_id)
    type_text = _select_text(fields.get("类型") if isinstance(fields, dict) else None)
    types = [part.strip() for part in type_text.split("、") if part.strip()]
    if INVESTMENT_ACCOUNT_TYPE not in types:
        raise PostingError(
            "账户「%s」的类型是「%s」，不是理财账户；理财买卖/分红必须使用"
            "「类型=理财」的账户（先用 `ledger.py account list` 查看）"
            % (_account_label(account_id, accounts), type_text or "未填写")
        )


def _holding_account_ids(holding_id: str, holdings: Dict[str, dict]) -> List[str]:
    """持仓「理财账户」关联单元格 → record_id 列表。"""
    fields = holdings.get(holding_id)
    value = fields.get("理财账户") if isinstance(fields, dict) else None
    items = value if isinstance(value, (list, tuple)) else [value]
    ids: List[str] = []
    for item in items:
        raw = item.get("id") if isinstance(item, dict) else item
        if isinstance(raw, str) and raw.strip():
            ids.append(raw.strip())
    return ids


def _require_holding_owner(
    holding_id: str,
    account_id: str,
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
) -> None:
    """持仓必须属于当前理财账户，避免把买入记到别的账户、污染资产快照。"""
    owners = _holding_account_ids(holding_id, holdings)
    if not owners:
        raise PostingError(
            "持仓「%s」没有关联理财账户（理财账户字段为空）；"
            "请先用 `ledger.py holding edit` 关联账户"
            % _holding_label(holding_id, holdings)
        )
    if account_id not in owners:
        owner_labels = "、".join(
            _account_label(owner, accounts) if owner in accounts else owner
            for owner in owners
        )
        raise PostingError(
            "持仓「%s」不属于理财账户「%s」：它关联的是「%s」；"
            "请使用持仓所属的理财账户记账"
            % (
                _holding_label(holding_id, holdings),
                _account_label(account_id, accounts),
                owner_labels,
            )
        )


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


def trade_amount(entry: dict) -> float:
    """理财买卖的成交金额（价格×份额，round 2）；CLI 展示与记账共用。"""
    return _invest_amount(entry)


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
    _require_investment_account(account, accounts)
    if _present(entry.get("amount")) and _present(entry.get("shares")):
        raise PostingError(
            "理财买入不能同时指定金额（amount）与份额（shares）：请二选一；"
            "转账金额由脚本按 成交价×份额 重算"
        )
    _price(entry)
    _shares(entry, kind)
    holding_id = _holding_id(entry, holdings)
    _require_holding_owner(holding_id, account, accounts, holdings)
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
    _require_investment_account(account, accounts)
    _price(entry)
    shares = _shares(entry, kind)
    holding_id = _holding_id(entry, holdings)
    _require_holding_owner(holding_id, account, accounts, holdings)
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
    """理财分红：金额已在 validate_entry 校验，这里要求理财账户、收入分类且不带买卖字段。"""
    _require_investment_account(account, accounts)
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


def _validate_loan_repay(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    holdings: Dict[str, dict] = None,
) -> None:
    """贷款还款：负债账户存在且类型=负债，付款账户不同，利息分类齐全。"""
    loan_id = _loan_account_id(entry, accounts)
    if loan_id == account:
        raise PostingError(
            "贷款还款的付款账户与负债账户不能相同：%s；"
            "请确认资金从哪张银行卡/现金账户扣款"
            % _account_label(account, accounts)
        )
    _require_liability_account(loan_id, accounts)
    _reject_present(
        entry,
        "to_account",
        "贷款还款不需要转入账户（to_account）：本金转入 loan_account "
        "指定的负债账户；请确认记账类型",
    )
    _reject_present(
        entry,
        "from_account",
        "贷款还款不需要转出账户（from_account）：付款账户用 account；"
        "请确认记账类型",
    )
    for key, label in (
        ("holding", "持仓"),
        ("price", "成交价"),
        ("shares", "份额"),
        ("pnl_category", "已实现盈亏分类"),
    ):
        _reject_present(
            entry,
            key,
            "贷款还款不需要%s（%s）：本息拆分只按 剩余本金×年利率/12 计算；"
            "如果是理财交易请改用对应的 --kind" % (label, key),
        )
    _interest_category(entry)
    # 提前还款/金额不足以覆盖利息/利率单位错误等边界在这里就暴露，
    # 不等到 plan 阶段才炸（validate_entry 可单独调用）。
    _loan_split(entry, accounts)


VALIDATORS: Dict[str, Callable[..., None]] = {
    "income": _validate_cash,
    "expense": _validate_cash,
    "transfer": _validate_transfer,
    "invest_buy": _validate_invest_buy,
    "invest_sell": _validate_invest_sell,
    "dividend": _validate_dividend,
    "loan_repay": _validate_loan_repay,
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
    if kind == "loan_repay":
        loan_id = _loan_account_id(entry, accounts)
        _, principal = _loan_split(entry, accounts)
        # 付款账户只出总额；负债余额只被本金推动（更接近 0）。
        return {account: round(-amount, 2), loan_id: round(principal, 2)}

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


def _require_liability_account(
    account_id: str, accounts: Dict[str, dict]
) -> None:
    """贷款还款只能落在「类型=负债」的账户，否则会把本金记到资产账户上。"""
    fields = accounts.get(account_id)
    type_text = _select_text(fields.get("类型") if isinstance(fields, dict) else None)
    types = [part.strip() for part in type_text.split("、") if part.strip()]
    if LOAN_ACCOUNT_TYPE not in types:
        raise PostingError(
            "账户「%s」的类型是「%s」，不是负债账户；贷款还款的 loan_account"
            " 必须是「类型=负债」的账户（先用 `ledger.py account list` 查看）"
            % (_account_label(account_id, accounts), type_text or "未填写")
        )


def _loan_account_id(entry: dict, accounts: Dict[str, dict]) -> str:
    """取出贷款还款的负债账户 record_id（必须已在账户快照里）。"""
    return _account_id(entry, "loan_account", "负债账户", accounts)


def _interest_category(entry: dict) -> str:
    """利息分类：优先 interest_category，兼容直接调用方传 category。"""
    for key in ("interest_category", "category"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise PostingError(
        "贷款还款必须指定利息分类（interest_category），"
        "例如「支出·金融·利息支出」；请用 --category 指定"
    )


def _loan_rate(loan_id: str, accounts: Dict[str, dict]) -> float:
    """读取负债账户的年利率单元格原值（小数，4.2% ⇒ 0.042）。"""
    fields = accounts.get(loan_id)
    raw = fields.get("年利率") if isinstance(fields, dict) else None
    if not _present(raw):
        raise PostingError(
            "负债账户「%s」缺少「年利率」，无法拆分本息；"
            "请先用 `ledger.py account edit <账户> --rate 4.2` 补年利率"
            % _account_label(loan_id, accounts)
        )
    return _number(
        raw, "负债账户「%s」的年利率" % _account_label(loan_id, accounts)
    )


def _loan_split(entry: dict, accounts: Dict[str, dict]) -> Tuple[float, float]:
    """按剩余本金（负债余额取反）与账户年利率拆分本息，返回 (利息, 本金)。"""
    loan_id = _loan_account_id(entry, accounts)
    total = _amount(entry)
    # 负债余额是负数（剩余本金），算法收正数剩余本金。
    remaining = round(-_current_balance(loan_id, accounts), 2)
    rate = _loan_rate(loan_id, accounts)
    try:
        return loans.split_payment(remaining, rate, total)
    except loans.LoanError as exc:
        raise PostingError(
            "贷款本息拆分失败（负债账户「%s」剩余本金 %.2f、年利率 %s、"
            "还款总额 %.2f）：%s"
            % (
                _account_label(loan_id, accounts),
                remaining,
                _number_text(rate),
                total,
                exc,
            )
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
) -> List[Tuple[str, Dict[str, Any]]]:
    fields = _detail_fields(kind, entry, account, amount)
    fields["分类"] = [{"id": entry["category"].strip()}]
    return [(ENTRY_TABLE, fields)]


def _plan_transfer(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    fields = _detail_fields(kind, entry, account, amount)
    target = _account_id(entry, "to_account", "转入账户", accounts)
    fields["转入账户"] = [{"id": target}]
    return [(ENTRY_TABLE, fields)]


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


def _plan_loan_repay(
    kind: str,
    entry: dict,
    accounts: Dict[str, dict],
    account: str,
    amount: float,
    holdings: Dict[str, dict] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """贷款还款两条明细：①利息支出；②本金转账（银行卡→负债）；同组 tag。"""
    loan_id = _loan_account_id(entry, accounts)
    interest, principal = _loan_split(entry, accounts)
    note = "%s本金%.2f+利息%.2f%s" % (
        entry.get("note") or "",
        principal,
        interest,
        group_tag(),
    )

    interest_row = _detail_fields("expense", entry, account, interest)
    interest_row["分类"] = [{"id": _interest_category(entry)}]
    interest_row["备注"] = note

    principal_row = _detail_fields("transfer", entry, account, principal)
    principal_row["转入账户"] = [{"id": loan_id}]
    principal_row["备注"] = note

    return [(ENTRY_TABLE, interest_row), (ENTRY_TABLE, principal_row)]


PLANNERS: Dict[str, Callable[..., List[Tuple[str, Dict[str, Any]]]]] = {
    "income": _plan_cash,
    "expense": _plan_cash,
    "transfer": _plan_transfer,
    "invest_buy": _plan_invest_buy,
    "invest_sell": _plan_invest_sell,
    "dividend": _plan_dividend,
    "loan_repay": _plan_loan_repay,
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
    creates = list(
        PLANNERS[kind](kind, entry, accounts, account, amount, holdings)
    )

    updates: List[Tuple[str, str, dict]] = []
    for account_id, delta in balance_deltas(entry, accounts).items():
        balance = round(_current_balance(account_id, accounts) + delta, 2)
        updates.append((ACCOUNT_TABLE, account_id, {"当前余额": balance}))
    if kind in INVEST_KINDS:
        updates.extend(_holding_update(kind, entry, holdings))

    return {"create": creates, "update": updates}


# ---------------------------------------------------------------------------
# 修正与重算：把「已落库的明细行」重放成账户余额 / 持仓
#
# 明细行是飞书里的 CellValue，不是 add 的入参：日期可能是
# "YYYY-MM-DD 00:00" 字符串或毫秒时间戳，关联字段是 [{"id": ...}]。
# rebuild 只能信任「期初 + 明细」，所以这里从行数据反推每一笔的余额与
# 持仓变动；组内买卖方向靠同组的转账行（银行卡↔理财）判断。
# ---------------------------------------------------------------------------

_TAG_PATTERN = re.compile(r"#G[0-9a-fA-F]{8}")
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
_EPOCH_UTC = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
ROW_TYPES = ("收入", "支出", "转账")


def _fields_of(row: Any) -> Dict[str, Any]:
    """明细行 → fields；兼容 {"record_id","fields"} 与纯字段 dict。

    `fields` 存在但不是对象时明确报错：这种记录不能被静默丢弃，调用方要
    把它变成诊断行（rebuild 拒绝写入）。
    """
    if not isinstance(row, dict):
        raise PostingError("明细必须是 dict，实际是 %r" % (row,))
    if "fields" in row:
        fields = row.get("fields")
        if not isinstance(fields, dict):
            raise PostingError(
                "明细记录的 fields 不是对象：%r" % (fields,)
            )
        return fields
    return row


def _row_id(row: Any) -> str:
    value = row.get("record_id") if isinstance(row, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _relation_ids(value: Any) -> List[str]:
    """关联单元格 → record_id 列表；兼容 [{"id": ...}]、字符串与列表。"""
    items = value if isinstance(value, (list, tuple)) else [value]
    ids: List[str] = []
    for item in items:
        raw = item.get("id") if isinstance(item, dict) else item
        if isinstance(raw, str) and raw.strip():
            ids.append(raw.strip())
    return ids


def _relation_id(value: Any) -> str:
    ids = _relation_ids(value)
    return ids[0] if ids else ""


def note_tag(note: Any) -> Optional[str]:
    """备注里的组 tag（如 "#G1a2b3c4d"）；没有则 None。"""
    if not isinstance(note, str):
        return None
    match = _TAG_PATTERN.search(note)
    return match.group(0) if match else None


def row_note(fields: Dict[str, Any]) -> str:
    value = fields.get("备注") if isinstance(fields, dict) else None
    return value if isinstance(value, str) else ""


def row_tag(row: Any) -> Optional[str]:
    """明细行的组 tag；无法解析时返回 None（该行自成一组）。"""
    try:
        return note_tag(row_note(_fields_of(row)))
    except PostingError:
        return None


def row_type(fields: Dict[str, Any]) -> str:
    """明细「类型」→ 收入 / 支出 / 转账；无法识别返回空串。"""
    text = _select_text(fields.get("类型") if isinstance(fields, dict) else None)
    text = text.strip()
    return text if text in ROW_TYPES else ""


def _date_from_ms(ms: Any) -> str:
    try:
        moment = _EPOCH_UTC + datetime.timedelta(milliseconds=float(ms))
        return moment.astimezone(_UTC8).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def row_date(fields: Dict[str, Any]) -> str:
    """明细日期 → "YYYY-MM-DD"；支持字符串与毫秒时间戳。"""
    raw = fields.get("日期") if isinstance(fields, dict) else None
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, str):
        text = raw.strip()[:10]
        if _DATE_PATTERN.fullmatch(text):
            try:
                datetime.date.fromisoformat(text)
            except ValueError:
                return ""
            return text
        return ""
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(raw)
    ):
        return _date_from_ms(raw)
    return ""


def _created_ms(fields: Dict[str, Any]) -> float:
    """创建时间 → 毫秒（缺失或不可解析按 0），仅用于稳定排序。"""
    raw = fields.get("创建时间") if isinstance(fields, dict) else None
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else 0.0
    if isinstance(raw, str):
        text = raw.strip()
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                moment = datetime.datetime.strptime(text, pattern).replace(
                    tzinfo=_UTC8
                )
            except ValueError:
                continue
            return moment.timestamp() * 1000
    return 0.0


def sort_rows(rows: List[Any]) -> List[Any]:
    """按 (日期, 创建时间, record_id) 排序，结果与输入顺序无关。"""
    decorated = []
    for index, row in enumerate(rows or []):
        try:
            fields = _fields_of(row)
        except PostingError:
            fields = {}
        decorated.append(
            (
                row_date(fields),
                _created_ms(fields),
                _row_id(row),
                index,
                row,
            )
        )
    decorated.sort(key=lambda item: item[:4])
    return [item[4] for item in decorated]


def _group_key(row: Any) -> str:
    tag = row_tag(row)
    if tag is not None:
        return "tag:" + tag.lower()
    return "record:" + (_row_id(row) or str(id(row)))


def group_rows(entries: List[Any], target: Any) -> List[Any]:
    """同组明细：有 #G tag 时取全部同 tag 行，否则只有该行自身。"""
    tag = row_tag(target)
    if tag is None:
        return [target]
    needle = tag.lower()
    matches = []
    for row in entries or []:
        candidate = row_tag(row)
        if candidate is not None and candidate.lower() == needle:
            matches.append(row)
    if not any(_row_id(row) == _row_id(target) for row in matches):
        matches.append(target)
    return matches


def group_object_ids(group: List[Any]) -> Dict[str, set]:
    """一组明细会碰到的账户 / 持仓 id 集合（用于局部重算）。"""
    accounts = set()
    holdings = set()
    for row in group or []:
        try:
            fields = _fields_of(row)
        except PostingError:
            continue
        account = _relation_id(fields.get("账户"))
        target = _relation_id(fields.get("转入账户"))
        if account:
            accounts.add(account)
        if target:
            accounts.add(target)
        holding = _relation_id(fields.get("持仓"))
        if holding:
            holdings.add(holding)
    return {"accounts": accounts, "holdings": holdings}


def _row_cash_deltas(fields: Dict[str, Any]) -> Dict[str, float]:
    """明细行对账户余额的变动（与 balance_deltas 同一口径）。

    自转行（账户 == 转入账户，理财买卖的份额行）净额为 0；普通转账是
    「来源 −、转入 +」。收入/支出的金额可为负（卖出亏损的已实现盈亏行）。
    """
    kind = row_type(fields)
    if not kind:
        raise PostingError("明细的「类型」不是 %s" % " / ".join(ROW_TYPES))
    amount = round(_number(fields.get("金额"), "明细金额"), 2)
    account = _relation_id(fields.get("账户"))
    if not account:
        raise PostingError("明细缺少「账户」")
    if kind == "收入":
        return {account: amount}
    if kind == "支出":
        return {account: round(-amount, 2)}
    target = _relation_id(fields.get("转入账户"))
    if not target:
        raise PostingError("转账明细缺少「转入账户」")
    if target == account:
        return {}
    return {account: round(-amount, 2), target: amount}


def _trade_direction(row: Any, group: List[Any]) -> Optional[str]:
    """组内买卖方向：同组另一条普通转账行的资金方向决定 buy / sell。"""
    fields = _fields_of(row)
    account = _relation_id(fields.get("账户"))
    if not account:
        return None
    found: Optional[str] = None
    for other in group:
        if other is row:
            continue
        try:
            other_fields = _fields_of(other)
        except PostingError:
            continue
        if row_type(other_fields) != "转账":
            continue
        if _relation_id(other_fields.get("持仓")):
            continue
        source = _relation_id(other_fields.get("账户"))
        target = _relation_id(other_fields.get("转入账户"))
        if not source or not target or source == target:
            continue
        if target == account:
            candidate = "buy"
        elif source == account:
            candidate = "sell"
        else:
            continue
        if found is not None and found != candidate:
            return None
        found = candidate
    return found


def _number_or_none(value: Any) -> Optional[float]:
    try:
        return _number(value, "数值")
    except PostingError:
        return None


def _is_trade_row(row: Any) -> bool:
    """是否为理财买卖的份额行（类型=转账 + 带持仓）。"""
    try:
        fields = _fields_of(row)
    except PostingError:
        return False
    return row_type(fields) == "转账" and bool(
        _relation_id(fields.get("持仓"))
    )


def _account_types(account_id: str, accounts: Dict[str, dict]) -> List[str]:
    fields = accounts.get(account_id)
    text = _select_text(
        fields.get("类型") if isinstance(fields, dict) else None
    )
    return [part.strip() for part in text.split("、") if part.strip()]


def _has_expense_row(rows: List[Any]) -> bool:
    for row in rows:
        try:
            fields = _fields_of(row)
        except PostingError:
            continue
        if row_type(fields) == "支出":
            return True
    return False


def _missing_trade_row(rows: List[Any], accounts: Dict[str, dict]) -> bool:
    """带 tag 的转账行是否像「丢了持仓交易行的理财买入」。

    只有多行操作会被打 #G tag。买入的转账腿是「现金流入理财账户」
    （转入账户=理财），若这样的 tagged 转账行没有持仓交易行，说明该组被
    外部删掉了一行（方向行还在、成交行没了）；按行级 deltas 重放会把理财
    账户闲置现金算错，必须判为不可信。

    不能只看「有一端是理财账户」：贷款还款允许从理财账户（如余额宝）付款，
    会生成 tagged 的 理财→负债 本金转账行，同时组内还有利息支出行——那是
    合法的 loan_repay，不能误判。因此要求：
    - 理财在 **转入账户**（现金流入，买入的方向）；
    - 组内没有「支出」行（排除贷款还款的利息支出）。
    卖出组的方向行缺失由 `_trade_direction` 判空覆盖。
    """
    if _has_expense_row(rows):
        return False
    for row in rows:
        if row_tag(row) is None:
            continue
        try:
            fields = _fields_of(row)
        except PostingError:
            continue
        if row_type(fields) != "转账":
            continue
        source = _relation_id(fields.get("账户"))
        target = _relation_id(fields.get("转入账户"))
        if not source or not target or source == target:
            continue
        if INVESTMENT_ACCOUNT_TYPE in _account_types(target, accounts):
            return True
    return False


def _group_label(group: List[Any]) -> str:
    for row in group:
        tag = row_tag(row)
        if tag:
            return tag
    return _row_id(group[0]) if group else "?"


def _group_account_deltas(
    group: List[Any], accounts: Dict[str, dict]
) -> Dict[str, float]:
    """一组明细 → 与 add 的 `balance_deltas` 同口径的账户变动。

    含持仓交易行的组是理财买卖：理财账户的「当前余额」只是账户内闲置
    现金，买卖本身不改变它——买入时钱从银行卡出去、进入持仓；卖出时份额
    变回现金再转出。因此这里只取外部账户（银行卡/现金）那一条腿：

    - 买入：转出账户 −转账金额（不能给理财账户 +金额，那是行级求和的错误）；
    - 卖出：转入账户 +转账金额（不能扣理财账户，也不要把盈亏行当现金收入）。

    其它组（收支/转账/贷款本息/分红）的行级 deltas 之和恰好等于 add 的
    `balance_deltas`，逐行相加即可。
    """
    rows = list(group or [])
    trade_rows = [row for row in rows if _is_trade_row(row)]
    if not trade_rows:
        if _missing_trade_row(rows, accounts):
            raise PostingError(
                "疑似缺少持仓交易行（方向行），无法按理财买卖重放"
            )
        totals: Dict[str, float] = {}
        for row in rows:
            deltas = _row_cash_deltas(_fields_of(row))
            for account_id, delta in deltas.items():
                totals[account_id] = round(
                    totals.get(account_id, 0.0) + delta, 2
                )
        return totals

    if len(trade_rows) > 1:
        raise PostingError("同组出现多条持仓交易行，无法确定买卖金额")
    trade = trade_rows[0]
    trade_fields = _fields_of(trade)
    account = _relation_id(trade_fields.get("账户"))
    if not account:
        raise PostingError("持仓交易行缺少账户")
    if _relation_id(trade_fields.get("转入账户")) != account:
        raise PostingError("持仓交易行的账户与转入账户不一致")

    direction = _trade_direction(trade, rows)
    if direction is None:
        raise PostingError(
            "无法判断买卖方向（组内缺少银行卡↔理财的转账行）"
        )

    for row in rows:
        if row is trade:
            continue
        fields = _fields_of(row)
        if row_type(fields) != "转账" or _relation_id(fields.get("持仓")):
            continue
        source = _relation_id(fields.get("账户"))
        target = _relation_id(fields.get("转入账户"))
        if not source or not target or source == target:
            continue
        amount = round(_number(fields.get("金额"), "明细金额"), 2)
        if direction == "buy" and target == account:
            return {source: round(-amount, 2)}
        if direction == "sell" and source == account:
            return {target: round(amount, 2)}

    raise PostingError("组内缺少银行卡↔理财的方向转账行，无法重放资金")


def replay(
    entries: List[Any],
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
) -> dict:
    """从 初始余额 / 期初份额 / 期初成本 起逐笔重放全部明细。

    返回 `{"accounts": {id: balance},
           "holdings": {id: {"shares": s, "cost": c, "status": "持有"}},
           "diagnostics": [...],
           "tainted": bool,
           "tainted_groups": [...]}`。

    按 (日期, 创建时间, record_id) 排序后以「明细组」为单位重放：同一
    #G tag 的多行是一笔操作（贷款本息、理财买卖），账户变动按
    `_group_account_deltas` 还原成 add 的 `balance_deltas` 口径。
    绝不对明细行做朴素求和：理财买入的转账行会给理财账户 +金额，而按
    add 语义理财账户闲置现金不变，行级求和会重复计入。

    任何无法完整解释的组/对象都会记一条诊断并置 `tainted=True`（同时
    记录 `tainted_groups`）。诊断包括：缺失账户/持仓、成交价/份额不可用、
    组内缺少方向行、tagged 转账行疑似丢失持仓交易行、期初字段不可用等。
    调用方在 tainted 时不得把重放结果写回账本。
    """
    account_fields = accounts if isinstance(accounts, dict) else {}
    holding_fields = holdings if isinstance(holdings, dict) else {}
    diagnostics: List[str] = []
    tainted_groups: List[str] = []

    def flag(label: str, message: str) -> None:
        diagnostics.append(message)
        if label not in tainted_groups:
            tainted_groups.append(label)

    balances: Dict[str, float] = {}
    for account_id, fields in account_fields.items():
        opening = _number_or_none(
            fields.get("初始余额") if isinstance(fields, dict) else None
        )
        if opening is None:
            flag(
                "账户:%s" % account_id,
                "账户「%s」缺少可用的初始余额，按 0 重算" % account_id,
            )
            opening = 0.0
        balances[account_id] = round(opening, 2)

    states: Dict[str, Dict[str, Any]] = {}
    for holding_id, fields in holding_fields.items():
        shares = _number_or_none(
            fields.get("期初份额") if isinstance(fields, dict) else None
        )
        cost = _number_or_none(
            fields.get("期初成本") if isinstance(fields, dict) else None
        )
        if shares is None:
            flag(
                "持仓:%s" % holding_id,
                "持仓「%s」缺少可用的期初份额，按 0 重算" % holding_id,
            )
            shares = 0.0
        if cost is None:
            flag(
                "持仓:%s" % holding_id,
                "持仓「%s」缺少可用的期初成本，按 0 重算" % holding_id,
            )
            cost = 0.0
        states[holding_id] = {
            "shares": round(shares, 4),
            "cost": round(cost, 4),
        }

    rows = sort_rows(entries)
    groups: Dict[str, List[Any]] = {}
    ordered_groups: List[List[Any]] = []
    for row in rows:
        key = _group_key(row)
        if key not in groups:
            groups[key] = []
            ordered_groups.append(groups[key])
        groups[key].append(row)

    for group in ordered_groups:
        label = _group_label(group)
        try:
            deltas = _group_account_deltas(group, account_fields)
        except PostingError as exc:
            flag(
                label,
                "明细组「%s」无法重放（%s），已整组跳过" % (label, exc),
            )
            continue
        missing = sorted(
            account_id for account_id in deltas if account_id not in balances
        )
        if missing:
            flag(
                label,
                "明细组「%s」引用的账户 %s 不存在，已整组跳过"
                % (label, "、".join(missing)),
            )
            continue
        for account_id, delta in deltas.items():
            balances[account_id] = round(balances[account_id] + delta, 2)

        for row in group:
            if not _is_trade_row(row):
                continue
            record_id = _row_id(row) or "?"
            fields = _fields_of(row)
            trade_account = _relation_id(fields.get("账户"))
            if trade_account and trade_account not in balances:
                flag(
                    label,
                    "明细「%s」引用的账户「%s」不存在，持仓未更新"
                    % (record_id, trade_account),
                )
                continue
            holding_id = _relation_id(fields.get("持仓"))
            if holding_id not in states:
                flag(
                    label,
                    "明细「%s」引用的持仓「%s」不存在，持仓未更新"
                    % (record_id, holding_id),
                )
                continue
            price = _number_or_none(fields.get("成交价"))
            quantity = _number_or_none(fields.get("份额"))
            if price is None or quantity is None or price <= 0 or quantity <= 0:
                flag(
                    label,
                    "明细「%s」的成交价/份额不可用，持仓未更新"
                    % record_id,
                )
                continue
            direction = _trade_direction(row, group)
            if direction is None:
                flag(
                    label,
                    "明细「%s」无法判断买卖方向，持仓未更新" % record_id,
                )
                continue
            state = states[holding_id]
            if direction == "buy":
                try:
                    state["cost"] = weighted_cost(
                        state["shares"], state["cost"], quantity, price
                    )
                except PostingError as exc:
                    flag(
                        label,
                        "明细「%s」买入成本无法重算（%s），持仓未更新"
                        % (record_id, exc),
                    )
                    continue
                state["shares"] = round(state["shares"] + quantity, 4)
            else:
                remaining = round(state["shares"] - quantity, 4)
                if abs(remaining) < 1e-9:
                    remaining = 0.0
                if remaining < 0:
                    flag(
                        label,
                        "持仓「%s」在明细「%s」卖出后份额为负，按 0 处理"
                        % (holding_id, record_id),
                    )
                    remaining = 0.0
                state["shares"] = remaining

    # 状态由重放后的份额派生，避免外部删/改明细后状态卡在旧值。
    for state in states.values():
        state["status"] = (
            "持有" if state["shares"] > 0 else "已清仓"
        )

    return {
        "accounts": balances,
        "holdings": states,
        "diagnostics": diagnostics,
        "tainted": bool(diagnostics),
        "tainted_groups": tainted_groups,
    }


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
