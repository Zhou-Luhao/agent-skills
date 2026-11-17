"""资产快照：净值口径、周触发、落表与配置标记。

职责边界：
- `plan_snapshot` / `due_weekly` 是纯逻辑：不读文件、不读时钟、不碰网络；
- 本模块其余部分负责 lark I/O：分页读账户/持仓/快照，按「日期-账户」唯一键
  先 search 再 create/update（飞书不按业务键自动 upsert）；
- 净值口径（spec）：
  * 理财账户一行 = 该账户闲置现金（当前余额）+ 其名下持仓市值（份额×最新价）；
  * 其他账户 = 当前余额；负债余额本身为负，直接相加即净资产；
  * 只给「在用」账户落快照，避免销户账户污染历史曲线；
- `maybe_weekly` 是 add 成功后的最佳努力钩子：任何失败只警告、绝不抛回
  add_flow；写入失败时不得把日期记进配置，否则下周会误以为已完成。

落表契约与 posting/quotes 相同：

    {"create": [(表名, 字段dict), ...],
     "update": [(表名, record_id, 字段dict), ...]}

日期字段在纯逻辑层保持 "YYYY-MM-DD"，写入前由 `_cell_fields` 补 " 00:00"
（飞书 datetime CellValue）。
"""

import datetime
import math
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config
import lark
import posting

WritePlan = posting.WritePlan

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 快照表会随周数无限增长，必须按页读到"短页"为止；不能用总量上限静默截断。
PAGE_SIZE = 200

SNAPSHOT_TABLE = "资产快照"
ACCOUNT_TABLE = posting.ACCOUNT_TABLE
HOLDING_TABLE = posting.HOLDING_TABLE

PRIMARY_FIELD = "日期-账户"
DATE_FIELD = "日期"
ACCOUNT_FIELD = "账户"
VALUE_FIELD = "余额/市值"
NOTE_FIELD = "备注"

ACCOUNT_NAME_FIELD = "账户名称"
ACCOUNT_TYPE_FIELD = "类型"
ACCOUNT_STATUS_FIELD = "状态"
ACCOUNT_BALANCE_FIELD = "当前余额"

HOLDING_ACCOUNT_FIELD = "理财账户"
HOLDING_SHARES_FIELD = "当前份额"
HOLDING_LATEST_PRICE_FIELD = "最新价"

STATUS_ACTIVE = "在用"
TYPE_WEALTH = "理财"

__all__ = [
    "WritePlan",
    "SnapshotError",
    "SNAPSHOT_TABLE",
    "plan_snapshot",
    "due_weekly",
    "maybe_weekly",
    "run_snapshot",
]


class SnapshotError(RuntimeError):
    """资产快照的数据/配置不合法（缺字段、重名、日期非法等）。"""


def _now() -> datetime.datetime:
    """当前时刻（UTC+8）；单独成函数便于测试注入，不用机器本地时区。"""
    return datetime.datetime.now(TZ_UTC8)


def _local_date(moment: Any) -> datetime.date:
    """date/datetime → 日期；aware datetime 先换算到 UTC+8。"""
    if isinstance(moment, datetime.datetime):
        if moment.tzinfo is not None:
            return moment.astimezone(TZ_UTC8).date()
        return moment.date()
    if isinstance(moment, datetime.date):
        return moment
    raise SnapshotError("日期必须是 date/datetime，实际是 %r" % (moment,))


def _as_date(value: Any) -> datetime.date:
    """接受 date/datetime/ISO 字符串；无法解析时抛 SnapshotError。"""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _local_date(value)
    if isinstance(value, str):
        text = value.strip()
        if DATE_PATTERN.fullmatch(text):
            try:
                return datetime.date.fromisoformat(text)
            except ValueError:
                pass
    raise SnapshotError("无法解析日期：%r（应为 YYYY-MM-DD）" % (value,))


def _date_text(value: Any) -> str:
    return _as_date(value).isoformat()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _option(value: Any) -> str:
    """单选单元格 → 文本；兼容本仓库的 `["选项"]` 写法。"""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return _text(value)


def _fields(record: Any) -> Dict[str, Any]:
    """兼容 lark 记录 `{"record_id","fields"}` 与裸字段 dict。"""
    if not isinstance(record, dict):
        return {}
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _record_id(record: Any) -> str:
    value = record.get("record_id") if isinstance(record, dict) else None
    return _text(value)


def _rows(values: Any) -> List[Any]:
    """dict（record_id/键 → 行）或 list（行数组）统一成行数组。"""
    if values is None:
        return []
    if isinstance(values, dict):
        return list(values.values())
    if isinstance(values, (list, tuple)):
        return list(values)
    raise SnapshotError(
        "记录集合应为 dict 或 list，实际是 %r" % (type(values).__name__,)
    )


def _link_ids(value: Any) -> List[str]:
    """关联单元格 → record_id 列表；兼容 [{"id": ...}]、dict、字符串。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    ids: List[str] = []
    for item in values:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                ids.append(item_id)
    return ids


def _number(value: Any, label: str) -> float:
    """数字单元格 → float；None/空串按 0 处理，非法值显式报错。"""
    if value is None:
        return 0.0
    if isinstance(value, str) and not value.strip():
        return 0.0
    if isinstance(value, bool):
        raise SnapshotError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SnapshotError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise SnapshotError("%s必须是有限数字，实际是 %r" % (label, value))
    return number


def _table(cfg: dict, table_name: str) -> Tuple[str, str]:
    """从配置读 (app_token, table_id)；缺配置按"未初始化"报错。"""
    if not isinstance(cfg, dict):
        raise config.ConfigError("配置必须是 JSON 对象")
    app_token = cfg.get("app_token")
    if not isinstance(app_token, str) or not app_token:
        raise config.ConfigError(
            "配置缺少 app_token，请先运行 `ledger.py init`"
        )
    tables = cfg.get("tables")
    entry = tables.get(table_name) if isinstance(tables, dict) else None
    table_id = entry.get("table_id") if isinstance(entry, dict) else None
    if not isinstance(table_id, str) or not table_id:
        raise config.ConfigError(
            "配置里缺少表「%s」的 table_id，请先运行 `ledger.py init`"
            % table_name
        )
    return app_token, table_id


def _search_all(app_token: str, table_id: str) -> List[dict]:
    """按 200 行/页读到短页为止；翻页不推进时显式报错，绝不静默截断。

    快照表只增不减：固定上限之外的行一旦漏读，同一个「日期-账户」就会被
    再建一行，周线出现重复点。这里与 `quotes_flow._search_all` 同一分页
    约定，并在后端返回重复页（游标没前进）时抛 LarkError。
    """
    records: List[dict] = []
    offset = 0
    previous_first_id: Optional[str] = None
    while True:
        page = list(
            lark.search_records(
                app_token, table_id, limit=PAGE_SIZE, offset=offset
            )
            or []
        )
        if not page:
            return records
        records.extend(page)
        if len(page) > PAGE_SIZE:
            # 后端（或测试桩）没遵守 limit，已一次返回更多行：视为读尽。
            return records
        if len(page) < PAGE_SIZE:
            return records
        first_id = _record_id(page[0])
        if first_id and first_id == previous_first_id:
            raise lark.LarkError(
                "读取表 %s 时分页未推进（offset=%d，page_size=%d）："
                "拒绝静默截断" % (table_id, offset, PAGE_SIZE)
            )
        previous_first_id = first_id
        offset += len(page)


def _cell_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """纯逻辑层 → 飞书 CellValue：日期字段补 " 00:00"（与 add_flow 一致）。"""
    converted = dict(fields)
    for name, value in converted.items():
        if (
            name.endswith("日期")
            and isinstance(value, str)
            and DATE_PATTERN.fullmatch(value)
        ):
            converted[name] = value + " 00:00"
    return converted


def _index_records(records: List[dict], table_name: str) -> Dict[str, dict]:
    """按 record_id 建索引；缺 record_id 的记录是坏数据，不能静默跳过。"""
    indexed: Dict[str, dict] = {}
    for record in records:
        record_id = _record_id(record)
        if not record_id:
            raise SnapshotError(
                "表「%s」有记录缺少 record_id，无法安全落快照" % table_name
            )
        indexed[record_id] = record
    return indexed


def _holdings_by_account(holdings: Any) -> Dict[str, float]:
    """账户 record_id → 名下持仓市值合计（份额×最新价）。

    一条持仓只取第一个关联账户，避免重复关联导致市值被算多次；
    未关联账户的持仓无法归属，忽略（不会把市值塞进别的账户）。
    """
    totals: Dict[str, float] = {}
    for record in _rows(holdings):
        fields = _fields(record)
        account_ids = _link_ids(fields.get(HOLDING_ACCOUNT_FIELD))
        if not account_ids:
            continue
        shares = _number(fields.get(HOLDING_SHARES_FIELD), "持仓当前份额")
        price = _number(fields.get(HOLDING_LATEST_PRICE_FIELD), "持仓最新价")
        account_id = account_ids[0]
        totals[account_id] = totals.get(account_id, 0.0) + shares * price
    return totals


def _existing_by_key(rows: Any) -> Dict[str, str]:
    """既有快照行 → {「日期-账户」: record_id}，供 update 而不是重复 create。"""
    existing: Dict[str, str] = {}
    for raw_key, record in _iter_existing(rows):
        fields = _fields(record)
        key = _text(fields.get(PRIMARY_FIELD))
        if not key and isinstance(raw_key, str) and "_" in raw_key:
            key = raw_key.strip()
        if not key:
            continue
        record_id = _record_id(record) or existing.get(key, "")
        if key in existing and existing[key] and record_id:
            if existing[key] != record_id:
                raise SnapshotError(
                    "资产快照里「%s」有两条不同记录（%s、%s），"
                    "请先消除重复主键" % (key, existing[key], record_id)
                )
        existing[key] = record_id
    return existing


def _iter_existing(rows: Any) -> List[Tuple[Any, Any]]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        return list(rows.items())
    if isinstance(rows, (list, tuple)):
        return [(None, record) for record in rows]
    raise SnapshotError(
        "既有快照行应为 dict 或 list，实际是 %r" % (type(rows).__name__,)
    )


def plan_snapshot(
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
    date_str: str,
    rows: Any = None,
) -> WritePlan:
    """每账户一行；理财值 = 现金 + 名下持仓市值；同键已存在则 update。

    `accounts` / `holdings` 是 record_id → 记录（或裸字段 dict）；
    `rows` 是既有资产快照行（list 或 主键→行 dict）：命中
    `f"{date_str}_{账户名称}"` 的行转为 update，绝不重复创建。
    """
    day = _date_text(date_str)
    market_values = _holdings_by_account(holdings)
    existing = _existing_by_key(rows)

    entries: List[Tuple[str, str, float]] = []
    seen_names: Dict[str, str] = {}
    for account_id, record in _account_items(accounts):
        fields = _fields(record)
        if _option(fields.get(ACCOUNT_STATUS_FIELD)) != STATUS_ACTIVE:
            continue
        name = _text(fields.get(ACCOUNT_NAME_FIELD))
        if not name:
            raise SnapshotError(
                "账户 %s 缺少「账户名称」，无法生成快照主字段" % account_id
            )
        if name in seen_names:
            raise SnapshotError(
                "账户「%s」重名（%s、%s）：快照主字段会冲突，"
                "请先消除重名" % (name, seen_names[name], account_id)
            )
        seen_names[name] = account_id

        value = _number(
            fields.get(ACCOUNT_BALANCE_FIELD),
            "账户「%s」的当前余额" % name,
        )
        if _option(fields.get(ACCOUNT_TYPE_FIELD)) == TYPE_WEALTH:
            value += market_values.get(account_id, 0.0)
        value = round(value, 2)
        if value == 0:
            value = 0.0  # 避免 -0.0 写进表里
        entries.append((name, account_id, value))

    entries.sort(key=lambda item: (item[0], item[1]))

    creates: List[Tuple[str, Dict[str, Any]]] = []
    updates: List[Tuple[str, str, Dict[str, Any]]] = []
    for name, account_id, value in entries:
        key = "%s_%s" % (day, name)
        fields = {
            PRIMARY_FIELD: key,
            DATE_FIELD: day,
            ACCOUNT_FIELD: [{"id": account_id}],
            VALUE_FIELD: value,
        }
        if key not in existing:
            # 新行留空备注；已存在的行不碰备注，避免覆盖用户手写的说明。
            fields[NOTE_FIELD] = ""
            creates.append((SNAPSHOT_TABLE, fields))
            continue
        record_id = existing[key]
        if not record_id:
            raise SnapshotError(
                "资产快照「%s」缺少 record_id，无法更新；"
                "请先到飞书表格核对该行" % key
            )
        updates.append((SNAPSHOT_TABLE, record_id, fields))

    return {"create": creates, "update": updates}


def _account_items(accounts: Dict[str, dict]) -> List[Tuple[str, Any]]:
    """账户映射 → (record_id, 记录) 列表；非法入参显式报错。"""
    if accounts is None:
        return []
    if not isinstance(accounts, dict):
        raise SnapshotError(
            "accounts 应为 record_id → 记录 的 dict，实际是 %r"
            % (type(accounts).__name__,)
        )
    items: List[Tuple[str, Any]] = []
    for raw_id, record in accounts.items():
        account_id = _text(raw_id) or _record_id(record)
        if not account_id:
            raise SnapshotError("账户记录缺少 record_id，无法生成快照")
        items.append((account_id, record))
    return items


def due_weekly(
    today: Any, last_snapshot: Optional[str]
) -> Optional[str]:
    """新一周首次触发时返回"上周日"；本周已补过则 None。

    目标日期恒为"本周一的前一天"（上周日）。`last_snapshot` 是数据日期
    （上次落的上周日），因此只要它不早于目标日期，就说明这一周的补落
    已经做过（或已有更晚的快照），直接返回 None——这样同一周的后续
    add 不会反复重写。更早（或从没落过）→ 返回上周日。

    未来日期/损坏的配置也不触发回补，避免倒着补出历史缺口。
    """
    current = _as_date(today)
    monday = current - datetime.timedelta(days=current.weekday())
    previous_sunday = monday - datetime.timedelta(days=1)

    if last_snapshot is None:
        return previous_sunday.isoformat()
    if isinstance(last_snapshot, str) and not last_snapshot.strip():
        return previous_sunday.isoformat()
    try:
        last = _as_date(last_snapshot)
    except SnapshotError:
        # 配置损坏时按"没落过"处理：下一次成功写入会覆盖它，避免永久卡死。
        return previous_sunday.isoformat()

    if last >= previous_sunday:
        return None
    return previous_sunday.isoformat()


def _write_plan(app_token: str, table_id: str, plan: WritePlan) -> int:
    """先批 create、再逐条 update，返回写入行数。"""
    written = 0
    creates = [
        _cell_fields(fields) for _, fields in plan.get("create") or []
    ]
    if creates:
        lark.create_records(app_token, table_id, creates)
        written += len(creates)
    for _, record_id, fields in plan.get("update") or []:
        lark.update_record(
            app_token, table_id, record_id, _cell_fields(fields)
        )
        written += 1
    return written


def run_snapshot(cfg: dict, date_str: Optional[str] = None) -> dict:
    """为指定日期（缺省今天 UTC+8）落一份资产快照。

    同一日期重复执行只会 update 既有行，不会产生重复主键。
    返回 `{"written": n, "date": "YYYY-MM-DD"}`；写表失败向上抛 LarkError。
    """
    if date_str is None:
        day = _local_date(_now()).isoformat()
    else:
        day = _date_text(date_str)

    app_token, account_table = _table(cfg, ACCOUNT_TABLE)
    _, holding_table = _table(cfg, HOLDING_TABLE)
    _, snapshot_table = _table(cfg, SNAPSHOT_TABLE)

    accounts = _index_records(
        _search_all(app_token, account_table), ACCOUNT_TABLE
    )
    holdings = _index_records(
        _search_all(app_token, holding_table), HOLDING_TABLE
    )
    rows = _search_all(app_token, snapshot_table)

    plan = plan_snapshot(accounts, holdings, day, rows=rows)
    written = _write_plan(app_token, snapshot_table, plan)
    return {"written": written, "date": day}


def maybe_weekly(
    cfg: dict, now: Optional[datetime.datetime] = None
) -> Optional[dict]:
    """add 成功后的周快照钩子：到期才写，任何异常只警告、绝不抛出。

    只有整轮写入成功后才把上周日记进 `cfg["last_snapshot_date"]`；
    写入或保存配置失败时保持旧值，下一次 add 仍会重试（写入本身幂等）。
    """
    moment = _now() if now is None else now
    try:
        last_snapshot = (
            cfg.get("last_snapshot_date") if isinstance(cfg, dict) else None
        )
        due = due_weekly(_local_date(moment), last_snapshot)
        if due is None:
            return None

        result = run_snapshot(cfg, due)

        previous = cfg.get("last_snapshot_date")
        cfg["last_snapshot_date"] = due
        try:
            config.save_config(cfg)
        except Exception:
            cfg["last_snapshot_date"] = previous
            raise
        return result
    except Exception as exc:  # 后置钩子边界：任何失败都只警告。
        warning = "资产快照失败（明细已落库，不影响记账）：%s" % exc
        print("警告：%s" % warning, file=sys.stderr)
        return {"warning": warning, "errors": [warning]}
