"""报告聚合：收支口径、净值序列、持仓与负债视图。

纯计算层：不 import lark / config，不读写文件、网络或时钟。调用方直接把
表行传进来——既接受 lark 记录 ``{"record_id", "fields"}``，也接受裸字段
dict；日期接受 ``date`` / ``datetime`` / ``"YYYY-MM-DD"`` 字符串 / 飞书读回
的毫秒时间戳。

口径（spec 核心流程 report 与「净值口径」）：

- 收支统计只认 类型=收入 / 支出，**类型=转账整条排除**：理财申购赎回、
  贷款本金、信用卡还款都不是收入或支出；
- 贷款利息是一条「支出·金融·利息支出」明细，因此自然计入支出；本金腿是
  转账，被上面的规则挡掉；
- 已实现盈亏是一条收入明细，允许为负：**负收入直接冲减收入合计**；
- 收支按 日期 字段的闭区间 ``[start, end]`` 过滤；
- 分类来自分类表（record_id → 一级/二级/分类）；分类记录缺失时归入
  未分类，不让报告崩掉；
- 净资产序列来自资产快照：按日期 Σ余额/市值，负债行本身为负，直接相加。
"""

import calendar
import datetime
import math
import re
from typing import Any, Dict, List, Optional, Tuple

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INCOME = "收入"
EXPENSE = "支出"
UNCATEGORIZED = "未分类"
CLOSED_HOLDING = "已清仓"
LIABILITY_TYPE = "负债"

DATE_FIELD = "日期"
TYPE_FIELD = "类型"
AMOUNT_FIELD = "金额"
CATEGORY_FIELD = "分类"
SUMMARY_FIELD = "摘要"

ACCOUNT_NAME_FIELD = "账户名称"
ACCOUNT_TYPE_FIELD = "类型"
ACCOUNT_BALANCE_FIELD = "当前余额"
ACCOUNT_RATE_FIELD = "年利率"

HOLDING_NAME_FIELD = "持仓名称"
HOLDING_CODE_FIELD = "代码"
HOLDING_SHARES_FIELD = "当前份额"
HOLDING_COST_FIELD = "成本价"
HOLDING_PRICE_FIELD = "最新价"
HOLDING_VALUE_FIELD = "市值"
HOLDING_PNL_FIELD = "浮动盈亏"
HOLDING_STATUS_FIELD = "状态"

SNAPSHOT_DATE_FIELD = "日期"
SNAPSHOT_VALUE_FIELD = "余额/市值"


class AggregateError(ValueError):
    """聚合输入不合法：未知期间、无法解析的日期/数字、反向区间等。"""


def _fields(record: Any) -> Dict[str, Any]:
    """兼容 lark 记录 ``{"record_id", "fields"}`` 与裸字段 dict。"""
    if not isinstance(record, dict):
        return {}
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _record_id(record: Any) -> str:
    value = record.get("record_id") if isinstance(record, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _rows(values: Any) -> List[Any]:
    """dict（record_id/键 → 行）或 list（行数组）统一成行数组。"""
    if values is None:
        return []
    if isinstance(values, dict):
        return list(values.values())
    if isinstance(values, (list, tuple)):
        return list(values)
    raise AggregateError(
        "记录集合应为 dict 或 list，实际是 %r" % (type(values).__name__,)
    )


def _cell_text(value: Any) -> str:
    """单元格 → 文本：扁平字符串、单选数组、富文本/关联的 dict 片段。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "".join(_cell_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "id"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        return ""
    return ""


def _option(value: Any) -> str:
    """单选单元格 → 文本；兼容本仓库的 ``["选项"]`` 写法。"""
    if isinstance(value, (list, tuple)):
        return _cell_text(value[0]) if value else ""
    return _cell_text(value)


def _number(value: Any, label: str) -> float:
    """必填数字 → 有限 float；布尔、空白、非数字与 NaN/Inf 都显式报错。"""
    if isinstance(value, bool):
        raise AggregateError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AggregateError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise AggregateError("%s必须是有限数字，实际是 %r" % (label, value))
    return number


def _optional_number(value: Any, label: str) -> Optional[float]:
    """可选数字单元格：None / 空白 → None；其余必须是有限数字。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _number(value, label)


def _as_date(value: Any, label: str) -> datetime.date:
    """日期单元格 → date；支持 date/datetime/ISO 字符串/毫秒时间戳。"""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            return value.astimezone(TZ_UTC8).date()
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, bool):
        raise AggregateError("%s无法解析为日期：%r" % (label, value))
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise AggregateError("%s无法解析为日期：%r" % (label, value))
        try:
            moment = datetime.datetime.fromtimestamp(value / 1000.0, tz=TZ_UTC8)
        except (OverflowError, OSError, ValueError):
            raise AggregateError("%s无法解析为日期：%r" % (label, value))
        return moment.date()
    if isinstance(value, str):
        text = value.strip()
        # 飞书可能回 "YYYY-MM-DD 00:00" 或完整 ISO；取日期前缀。
        prefix = text[:10]
        if DATE_PATTERN.match(prefix):
            try:
                return datetime.date.fromisoformat(prefix)
            except ValueError:
                pass
    raise AggregateError(
        "%s无法解析为日期：%r（应为 YYYY-MM-DD 或毫秒时间戳）" % (label, value)
    )


def _money(value: float) -> float:
    """金额统一到分，并把 -0.0 归一成 0.0。"""
    rounded = round(value, 2)
    return 0.0 if rounded == 0 else rounded


def period_range(period: str, anchor: Any) -> Tuple[datetime.date, datetime.date]:
    """报告期间：week = anchor 所在 ISO 周一~周日；month = 当月 1 日~末日。"""
    day = _as_date(anchor, "锚点日期")
    if period == "week":
        start = day - datetime.timedelta(days=day.weekday())
        return start, start + datetime.timedelta(days=6)
    if period == "month":
        start = day.replace(day=1)
        last_day = calendar.monthrange(day.year, day.month)[1]
        return start, day.replace(day=last_day)
    raise AggregateError(
        "period 只支持 week 或 month，实际是 %r" % (period,)
    )


def _link_ids(value: Any) -> List[str]:
    """关联单元格 → record_id 列表；兼容 [{"id": ...}]、dict、字符串。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        raw_values = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        return []
    ids: List[str] = []
    for item in raw_values:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id.strip():
                ids.append(item_id.strip())
    return ids


def _category_index(categories: Any) -> Dict[str, Tuple[str, str]]:
    """分类表 → {record_id: (一级, 二级)}；容忍缺字段，缺失一级归未分类。"""
    if categories is None:
        return {}
    if isinstance(categories, dict):
        items = list(categories.items())
    elif isinstance(categories, (list, tuple)):
        items = [(_record_id(record), record) for record in categories]
    else:
        raise AggregateError(
            "categories 应为 record_id → 分类记录 的 dict，实际是 %r"
            % (type(categories).__name__,)
        )

    index: Dict[str, Tuple[str, str]] = {}
    for raw_key, record in items:
        record_id = _cell_text(raw_key) or _record_id(record)
        if not record_id:
            continue
        fields = _fields(record)
        first = _cell_text(fields.get("一级"))
        second = _cell_text(fields.get("二级"))
        if not first or not second:
            parts = [
                part for part in _cell_text(fields.get("分类")).split("·") if part
            ]
            if len(parts) >= 3:
                first = first or parts[1]
                second = second or parts[2]
            elif len(parts) == 2:
                first = first or parts[0]
                second = second or parts[1]
        index[record_id] = (first or UNCATEGORIZED, second)
    return index


def _entry_category(
    fields: Dict[str, Any],
    index: Dict[str, Tuple[str, str]],
) -> Tuple[str, str]:
    """明细的分类：取第一条关联；记录缺失（或没填分类）→ 未分类。"""
    for category_id in _link_ids(fields.get(CATEGORY_FIELD)):
        if category_id in index:
            return index[category_id]
    return (UNCATEGORIZED, "")


def _label(record: Any, field: str) -> str:
    """报错里可读的行标识：摘要 > record_id > 泛称。"""
    text = _cell_text(_fields(record).get(field))
    return text or _record_id(record) or "未命名明细"


def _signed_amounts(
    entries: Any,
    index: Dict[str, Tuple[str, str]],
    start: datetime.date,
    end: datetime.date,
) -> Tuple[
    float,
    float,
    Dict[Tuple[str, str], float],
    Dict[Tuple[str, str], float],
]:
    """按闭区间汇总收入/支出（排除转账），返回 (收入, 支出, 支出分类, 收入分类)。"""
    income = 0.0
    expense = 0.0
    expense_groups: Dict[Tuple[str, str], float] = {}
    income_groups: Dict[Tuple[str, str], float] = {}

    for record in _rows(entries):
        fields = _fields(record)
        kind = _option(fields.get(TYPE_FIELD))
        if kind not in (INCOME, EXPENSE):
            # 转账（以及无法识别的脏类型）不参与收支统计。
            continue
        label = "明细「%s」的日期" % (_label(record, SUMMARY_FIELD),)
        day = _as_date(fields.get(DATE_FIELD), label)
        if day < start or day > end:
            continue
        amount = _number(
            fields.get(AMOUNT_FIELD), "明细「%s」的金额" % (_label(record, SUMMARY_FIELD),)
        )
        key = _entry_category(fields, index)
        if kind == INCOME:
            income += amount
            income_groups[key] = income_groups.get(key, 0.0) + amount
        else:
            expense += amount
            expense_groups[key] = expense_groups.get(key, 0.0) + amount

    return income, expense, expense_groups, income_groups


def _group_rows(groups: Dict[Tuple[str, str], float]) -> List[dict]:
    """分类合计 → 行数组；金额降序，同额按一级/二级升序，保证输出稳定。"""
    rows = [
        {"一级": first, "二级": second, "金额": _money(total)}
        for (first, second), total in groups.items()
    ]
    rows.sort(key=lambda row: (-row["金额"], row["一级"], row["二级"]))
    return rows


def summarize(
    entries: Any,
    categories: Any,
    start: Any,
    end: Any,
) -> dict:
    """统计闭区间 ``[start, end]`` 内的收入/支出、结余与分类明细。

    只统计 类型=收入 / 支出；转账整条排除。收入金额按原符号累加，因此
    已实现亏损（负收入）会冲减收入合计；``net = income - expense``。
    区间内的行若缺日期或金额会显式报错，不静默漏账。
    """
    start_day = _as_date(start, "统计开始日期")
    end_day = _as_date(end, "统计结束日期")
    if start_day > end_day:
        raise AggregateError(
            "统计开始日期 %s 晚于结束日期 %s"
            % (start_day.isoformat(), end_day.isoformat())
        )

    index = _category_index(categories)
    income, expense, expense_groups, income_groups = _signed_amounts(
        entries, index, start_day, end_day
    )
    income = _money(income)
    expense = _money(expense)
    return {
        "income": income,
        "expense": expense,
        "net": _money(income - expense),
        "by_category": _group_rows(expense_groups),
        "income_by_category": _group_rows(income_groups),
    }


def net_worth_series(rows: Any) -> List[Tuple[str, float]]:
    """资产快照 → [(YYYY-MM-DD, Σ余额/市值)]，按日期升序。

    负债行的余额本身为负，直接相加即自然抵减；同一天多账户求和。
    缺日期或余额/市值会显式报错（与快照写入的必填口径一致），
    不把账户静默从净值曲线上抹掉。
    """
    totals: Dict[str, float] = {}
    for record in _rows(rows):
        fields = _fields(record)
        day = _as_date(
            fields.get(SNAPSHOT_DATE_FIELD), "资产快照的日期"
        ).isoformat()
        value = _number(
            fields.get(SNAPSHOT_VALUE_FIELD),
            "资产快照 %s 的余额/市值" % day,
        )
        totals[day] = totals.get(day, 0.0) + value
    return [(day, _money(totals[day])) for day in sorted(totals)]


def holdings_view(holdings: Any) -> List[dict]:
    """持仓 → 份额/最新价/市值/浮盈视图，按代码升序。

    市值优先取表内公式值，缺失时用 份额×最新价 兜底；浮盈同理优先取
    「浮动盈亏」公式值，缺失时用 (最新价−成本价)×份额 兜底。最新价未知
    （行情尚未回补）时市值与浮盈按 0 展示，不虚构负浮盈。
    """
    rows: List[dict] = []
    for record in _rows(holdings):
        fields = _fields(record)
        if _option(fields.get(HOLDING_STATUS_FIELD)) == CLOSED_HOLDING:
            continue
        name = _cell_text(fields.get(HOLDING_NAME_FIELD))
        code = _cell_text(fields.get(HOLDING_CODE_FIELD))
        label = name or code or _record_id(record) or "未命名持仓"

        shares = _optional_number(
            fields.get(HOLDING_SHARES_FIELD), "持仓「%s」的当前份额" % label
        )
        price = _optional_number(
            fields.get(HOLDING_PRICE_FIELD), "持仓「%s」的最新价" % label
        )
        cost = _optional_number(
            fields.get(HOLDING_COST_FIELD), "持仓「%s」的成本价" % label
        )

        market_value = _optional_number(
            fields.get(HOLDING_VALUE_FIELD), "持仓「%s」的市值" % label
        )
        if market_value is None:
            market_value = (shares or 0.0) * (price or 0.0)

        pnl = _optional_number(
            fields.get(HOLDING_PNL_FIELD), "持仓「%s」的浮动盈亏" % label
        )
        if pnl is None:
            if price is not None and cost is not None:
                pnl = (price - cost) * (shares or 0.0)
            else:
                pnl = 0.0

        rows.append(
            {
                "名称": name,
                "代码": code,
                "份额": round(shares or 0.0, 4),
                "最新价": round(price or 0.0, 4),
                "市值": _money(market_value),
                "浮盈": _money(pnl),
            }
        )
    rows.sort(key=lambda row: (row["代码"], row["名称"]))
    return rows


def liabilities_view(accounts: Any, categories: Any = None) -> List[dict]:
    """负债账户概览：剩余本金 = abs(当前余额)，年利率按百分数展示。

    Base 的年利率单元格存小数（4.2% ⇒ 0.042），这里 ×100 还原成 4.2，
    不再做别的换算。``categories`` 参数为 Task 15 的调用约定预留，负债
    概览当前不按分类拆分，接受但不参与计算。
    """
    del categories  # 预留参数：负债概览暂不需要分类信息。
    rows: List[dict] = []
    for record in _rows(accounts):
        fields = _fields(record)
        if _option(fields.get(ACCOUNT_TYPE_FIELD)) != LIABILITY_TYPE:
            continue
        name = _cell_text(fields.get(ACCOUNT_NAME_FIELD))
        label = name or _record_id(record) or "未命名负债账户"
        balance = _number(
            fields.get(ACCOUNT_BALANCE_FIELD), "负债账户「%s」的当前余额" % label
        )
        rate = _optional_number(
            fields.get(ACCOUNT_RATE_FIELD), "负债账户「%s」的年利率" % label
        )
        rows.append(
            {
                "名称": name,
                "剩余本金": _money(abs(balance)),
                "年利率": _money((rate or 0.0) * 100.0),
            }
        )
    rows.sort(key=lambda row: (row["名称"], row["剩余本金"]))
    return rows