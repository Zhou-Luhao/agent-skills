"""report / query 编排：拉数、强制快照、聚合、渲染与查询。

职责边界：

- 纯计算（期间、收支口径、净值序列、持仓/负债视图）全部在 ``aggregate``；
- 渲染（自包含 HTML / Markdown）全部在 ``render``；
- 本模块只负责 lark I/O 与编排：强制落一份当前快照，分页读尽五张表，
  把实时净值补成曲线最后一个点（同一天只保留一个点），写双产物；
- ``query`` 复用同一套读取与聚合函数，输出稳定的 dict 形状供 CLI 渲染。

失败边界：报告不能因为行情过期或快照失败而中止。快照失败（含零行写入）
只向 stderr 打一条警告，报告继续用现有快照与实时估值渲染；行情是否最新
不在本层判断——最新价字段是什么就用什么。
"""

import datetime
import math
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

import aggregate
import render
import snapshot_flow

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ENTRY_TABLE = "收支明细"
CATEGORY_TABLE = "分类"
ACCOUNT_TABLE = "账户"
HOLDING_TABLE = "持仓"
SNAPSHOT_TABLE = snapshot_flow.SNAPSHOT_TABLE
PERIODS = ("week", "month")
QUERY_WHAT = ("balances", "summary", "holdings")

ACCOUNT_NAME_FIELD = "账户名称"
ACCOUNT_TYPE_FIELD = "类型"
ACCOUNT_BALANCE_FIELD = "当前余额"
VALUE_FIELD = snapshot_flow.VALUE_FIELD

PathLike = Union[str, pathlib.Path]


class ReportError(ValueError):
    """报告/查询的输入或落盘不合法。"""


def _now() -> datetime.datetime:
    """当前时刻（UTC+8）；单独成函数便于测试注入。"""
    return datetime.datetime.now(TZ_UTC8)


def _as_date(value: Any, label: str) -> datetime.date:
    """日期入参 → date；兼容 date/datetime 与 "YYYY-MM-DD[ 00:00]" 文本。"""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            return value.astimezone(TZ_UTC8).date()
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        prefix = value.strip()[:10]
        if DATE_PATTERN.match(prefix):
            try:
                return datetime.date.fromisoformat(prefix)
            except ValueError:
                pass
    raise ReportError("%s必须是 YYYY-MM-DD，实际是 %r" % (label, value))


def _fields(record: Any) -> Dict[str, Any]:
    """兼容 lark 记录 ``{"record_id", "fields"}`` 与裸字段 dict。"""
    if not isinstance(record, dict):
        return {}
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _record_id(record: Any) -> str:
    value = record.get("record_id") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


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


def _option(value: Any) -> str:
    """单选单元格 → 文本；兼容本仓库的 ``["选项"]`` 写法。"""
    if isinstance(value, (list, tuple)):
        return _cell_text(value[0]) if value else ""
    return _cell_text(value)


def _number(value: Any, label: str) -> float:
    """必填数字 → 有限 float；布尔、空白、NaN/Inf 都显式报错。"""
    if isinstance(value, bool):
        raise ReportError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ReportError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise ReportError("%s必须是有限数字，实际是 %r" % (label, value))
    return number


def _money(value: float) -> float:
    """金额统一到分，并把 -0.0 归一成 0.0。"""
    rounded = round(value, 2)
    return 0.0 if rounded == 0 else rounded


def _read_table(cfg: dict, table_name: str) -> List[dict]:
    """读尽一张表：复用快照流程的分页实现，绝不静默截断。"""
    app_token, table_id = snapshot_flow._table(cfg, table_name)
    return snapshot_flow._search_all(app_token, table_id)


def _category_map(records: List[dict]) -> Dict[str, Dict[str, Any]]:
    """分类表 → ``record_id -> 字段`` 全量映射（不做任何抽样/过滤）。"""
    mapping: Dict[str, Dict[str, Any]] = {}
    for record in records:
        record_id = _record_id(record)
        if record_id:
            mapping[record_id] = _fields(record)
    return mapping


def _force_snapshot(cfg: dict) -> Optional[str]:
    """强制落当前快照；任何失败只返回警告，绝不抛给报告。"""
    try:
        result = snapshot_flow.run_snapshot(cfg)
    except Exception as exc:  # 快照是增强项：失败不能拖垮报告。
        return "资产快照失败（报告继续使用现有快照与实时估值）：%s" % exc
    warning = result.get("warning") if isinstance(result, dict) else None
    if warning:
        return "资产快照未写全（报告继续使用现有快照与实时估值）：%s" % warning
    return None


def _index_records(records: List[dict]) -> Dict[str, dict]:
    """按 record_id 建索引；缺 id 的记录直接跳过（不能生成快照行）。"""
    indexed: Dict[str, dict] = {}
    for record in records:
        record_id = _record_id(record)
        if record_id:
            indexed[record_id] = record
    return indexed


def _live_net_worth(
    accounts: List[dict], holdings: List[dict], day: datetime.date
) -> float:
    """实时净值：复用快照口径（在用账户余额 + 理财名下持仓市值）。"""
    plan = snapshot_flow.plan_snapshot(
        _index_records(accounts),
        _index_records(holdings),
        day.isoformat(),
    )
    total = 0.0
    for _, fields in plan.get("create") or []:
        total += _number(fields.get(VALUE_FIELD), "实时净值的账户余额")
    return _money(total)


def _period_points(
    snapshots: List[dict],
    start: datetime.date,
    end: datetime.date,
    live_day: datetime.date,
    live_total: Optional[float],
) -> List[List[Any]]:
    """期间内快照序列 + 实时末点；同一天只保留实时值，不产生重复点。"""
    points: List[Tuple[str, float]] = []
    for day, total in aggregate.net_worth_series(snapshots):
        if start <= datetime.date.fromisoformat(day) <= end:
            points.append((day, total))
    if live_total is not None:
        live_date = live_day.isoformat()
        points = [point for point in points if point[0] != live_date]
        points.append((live_date, live_total))
        points.sort(key=lambda point: point[0])
    return [[day, total] for day, total in points]


def _output_dir(out_dir: Optional[PathLike]) -> pathlib.Path:
    """输出目录：显式目录优先；缺省 ~/Downloads/lark-ledger（运行时取 home）。"""
    if out_dir is None:
        return pathlib.Path.home() / "Downloads" / "lark-ledger"
    return pathlib.Path(out_dir).expanduser()


def run_report(
    cfg: dict,
    period: str,
    anchor: Optional[str] = None,
    out_dir: Optional[PathLike] = None,
) -> dict:
    """生成周报/月报，返回 ``{"html", "md", "summary"}``。

    步骤：解析期间 → 强制落当前快照（失败仅警告）→ 读尽五张表 → 聚合 →
    把实时净值补成曲线末点（同日期替换，不重复）→ 渲染并写双产物。
    """
    if period not in PERIODS:
        raise ReportError(
            "period 只支持 week 或 month，实际是 %r" % (period,)
        )

    now = _now()
    anchor_day = _as_date(anchor, "锚点日期") if anchor is not None else now.date()
    start, end = aggregate.period_range(period, anchor_day)

    warning = _force_snapshot(cfg)
    if warning:
        print("警告：%s" % warning, file=sys.stderr)

    entries = _read_table(cfg, ENTRY_TABLE)
    categories = _category_map(_read_table(cfg, CATEGORY_TABLE))
    accounts = _read_table(cfg, ACCOUNT_TABLE)
    holdings = _read_table(cfg, HOLDING_TABLE)
    snapshots = _read_table(cfg, SNAPSHOT_TABLE)

    try:
        summary = aggregate.summarize(entries, categories, start, end)
        holdings_rows = aggregate.holdings_view(holdings)
        liabilities = aggregate.liabilities_view(accounts)
    except aggregate.AggregateError as exc:
        raise ReportError(str(exc)) from exc

    live_total: Optional[float] = None
    try:
        live_total = _live_net_worth(accounts, holdings, now.date())
    except Exception as exc:  # 实时估值失败也不能让报告消失。
        print(
            "警告：实时净值计算失败，曲线使用已有快照：%s" % exc,
            file=sys.stderr,
        )

    try:
        points = _period_points(
            snapshots, start, end, now.date(), live_total
        )
    except aggregate.AggregateError as exc:
        raise ReportError(str(exc)) from exc
    data = {
        "meta": {
            "period": period,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        },
        "summary": summary,
        "net_worth": points,
        "holdings": holdings_rows,
        "liabilities": liabilities,
    }

    directory = _output_dir(out_dir)
    stem = "ledger-%s-%s" % (period, end.isoformat())
    html_path = directory / (stem + ".html")
    md_path = directory / (stem + ".md")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render.render_html(data), encoding="utf-8")
        md_path.write_text(render.render_markdown(data), encoding="utf-8")
    except OSError as exc:
        raise ReportError("写不了报告文件 %s：%s" % (directory, exc)) from exc

    return {"html": html_path, "md": md_path, "summary": summary}


def _category_labels(fields: Dict[str, Any]) -> List[str]:
    """分类可用的展示名：一级、二级、一级·二级、分类全名。"""
    first = _cell_text(fields.get("一级"))
    second = _cell_text(fields.get("二级"))
    full = _cell_text(fields.get("分类"))
    labels: List[str] = []
    for label in (first, second, full):
        if label and label not in labels:
            labels.append(label)
    pair = "·".join(part for part in (first, second) if part)
    if pair and pair not in labels:
        labels.append(pair)
    return labels


def _category_known(categories: Dict[str, Dict[str, Any]], name: str) -> bool:
    return any(name in _category_labels(fields) for fields in categories.values())


def _row_matches(row: Dict[str, Any], name: str) -> bool:
    """分类行匹配：支持一级、二级、一级·二级与「收入/支出·一级·二级」。"""
    first = str(row.get("一级") or "")
    second = str(row.get("二级") or "")
    pair = "·".join(part for part in (first, second) if part)
    if name in (first, second, pair):
        return True
    parts = {part for part in name.split("·") if part}
    return bool(first and second and {first, second} <= parts)


def _filter_summary(summary: dict, category: str) -> dict:
    """把总结过滤到单一分类，并只按该分类的行重算收入/支出/结余。"""
    expense_rows = [
        row for row in summary.get("by_category") or []
        if _row_matches(row, category)
    ]
    income_rows = [
        row for row in summary.get("income_by_category") or []
        if _row_matches(row, category)
    ]
    income = _money(sum(row["金额"] for row in income_rows))
    expense = _money(sum(row["金额"] for row in expense_rows))
    return {
        "income": income,
        "expense": expense,
        "net": _money(income - expense),
        "by_category": expense_rows,
        "income_by_category": income_rows,
    }


def _query_balances(cfg: dict) -> dict:
    """账户余额：名称 + 类型 + 当前余额，按名称/类型排序。"""
    rows: List[dict] = []
    total = 0.0
    for record in _read_table(cfg, ACCOUNT_TABLE):
        fields = _fields(record)
        name = _cell_text(fields.get(ACCOUNT_NAME_FIELD)) or _record_id(record)
        label = name or "未命名账户"
        balance = _number(
            fields.get(ACCOUNT_BALANCE_FIELD), "账户「%s」的当前余额" % label
        )
        rows.append(
            {
                "名称": label,
                "类型": _option(fields.get(ACCOUNT_TYPE_FIELD)),
                "余额": _money(balance),
            }
        )
        total += balance
    rows.sort(key=lambda row: (row["名称"], row["类型"]))
    return {"what": "balances", "balances": rows, "total": _money(total)}


def _query_range(
    start: Optional[str], end: Optional[str]
) -> Tuple[datetime.date, datetime.date]:
    """summary 区间：默认本月；只给一端时另一端用今天/当月 1 日补齐。"""
    today = _now().date()
    if start is None and end is None:
        return aggregate.period_range("month", today)
    if end is None:
        start_day = _as_date(start, "开始日期")
        return start_day, today
    end_day = _as_date(end, "结束日期")
    if start is None:
        return end_day.replace(day=1), end_day
    start_day = _as_date(start, "开始日期")
    if start_day > end_day:
        raise ReportError(
            "开始日期 %s 晚于结束日期 %s"
            % (start_day.isoformat(), end_day.isoformat())
        )
    return start_day, end_day


def _query_summary(
    cfg: dict, start: Optional[str], end: Optional[str], category: Optional[str]
) -> dict:
    start_day, end_day = _query_range(start, end)
    entries = _read_table(cfg, ENTRY_TABLE)
    categories = _category_map(_read_table(cfg, CATEGORY_TABLE))

    try:
        summary = aggregate.summarize(entries, categories, start_day, end_day)
    except aggregate.AggregateError as exc:
        raise ReportError(str(exc)) from exc

    name = category.strip() if isinstance(category, str) else ""
    if category is not None and not name:
        raise ReportError("--category 不能为空")
    if name:
        if not _category_known(categories, name):
            known = sorted(
                {
                    label
                    for fields in categories.values()
                    for label in _category_labels(fields)
                }
            )
            raise ReportError(
                "未知分类「%s」；可用分类：%s"
                % (name, "、".join(known) or "（分类表为空）")
            )
        summary = _filter_summary(summary, name)

    result = {
        "what": "summary",
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "category": name or None,
    }
    result.update(summary)
    return result


def run_query(
    cfg: dict,
    what: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """对话查询：``balances`` / ``summary`` / ``holdings``。

    - balances：全部账户的名称、类型、当前余额与合计（负债余额为负）；
    - summary：闭区间收支（默认本月），可只保留一个分类并重算合计；
    - holdings：未清仓持仓的份额/最新价/市值/浮盈。
    """
    if what not in QUERY_WHAT:
        raise ReportError(
            "query 只支持 balances、summary、holdings，实际是 %r" % (what,)
        )
    if what == "balances":
        return _query_balances(cfg)
    if what == "holdings":
        holdings = _read_table(cfg, HOLDING_TABLE)
        try:
            rows = aggregate.holdings_view(holdings)
        except aggregate.AggregateError as exc:
            raise ReportError(str(exc)) from exc
        return {
            "what": "holdings",
            "holdings": rows,
            "total": _money(sum(row["市值"] for row in rows)),
        }
    return _query_summary(cfg, start, end, category)