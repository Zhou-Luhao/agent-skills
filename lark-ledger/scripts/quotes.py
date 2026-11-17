"""行情解析、回补窗口与落表计划（纯逻辑层）。

本模块只做字符串/数值/日期计算：不 import lark / config / urllib，
不读文件、不联网、不读时钟。`today` / `now` 一律由调用方传入，
日期在计划里始终保持 "YYYY-MM-DD" 字符串（毫秒时间戳由 I/O 层转换）。

落表契约（与 posting.WritePlan 同形）：

    {"create": [(表名, 字段dict), ...],
     "update": [(表名, record_id, 字段dict), ...]}

「行情快照」以 `代码-日期` 为主字段/业务唯一键；`状态` 是单选，
沿用本仓库对单选字段的列表写法（如 `["已落"]`）。待回补行的
`收盘价/净值` 显式为 None，表示"有日期没价格"。
"""

import datetime
import json
import math
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

import posting

WritePlan = posting.WritePlan

QUOTE_TABLE = "行情快照"
HOLDING_TABLE = posting.HOLDING_TABLE

PRIMARY_FIELD = "代码-日期"
CODE_FIELD = "代码"
DATE_FIELD = "日期"
PRICE_FIELD = "收盘价/净值"
STATUS_FIELD = "状态"
LATEST_PRICE_FIELD = "最新价"
LATEST_DATE_FIELD = "最新价日期"

STATUS_FILLED = "已落"
STATUS_PENDING = "待回补"
STATUS_NO_DATA = "无数据"

RUN_AFTER_HOUR = 15
RUN_AFTER_MINUTE = 5
EXPIRY_DAYS = 7

_TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
_EPOCH_UTC = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

__all__ = [
    "WritePlan",
    "QUOTE_TABLE",
    "code_kind",
    "parse_stock_kline",
    "parse_fund_nav",
    "quote_window",
    "expired_keys",
    "should_run",
    "plan_updates",
]


def _as_date(value: Any) -> datetime.date:
    """接受 date/datetime/ISO 字符串/毫秒时间戳，统一成 date。"""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # lark 日期单元格实际返回 UTC+8 毫秒时间戳；纯算术、不读时钟。
        try:
            moment = _EPOCH_UTC + datetime.timedelta(milliseconds=float(value))
            return moment.astimezone(_TZ_UTC8).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("无法解析日期：%r" % (value,)) from exc
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            try:
                # lark 的日期单元格偶尔是 "YYYY-MM-DD 00:00"。
                return datetime.date.fromisoformat(text[:10])
            except ValueError:
                pass
    raise ValueError("无法解析日期：%r" % (value,))


def _price(value: Any) -> Optional[float]:
    """价格单元格 → float；空值/布尔/非数字/NaN/Inf 返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _fields(row: Any) -> Dict[str, Any]:
    """兼容 lark 记录 `{"record_id","fields"}` 与裸字段 dict。"""
    if not isinstance(row, dict):
        return {}
    fields = row.get("fields")
    return fields if isinstance(fields, dict) else row


def _record_id(row: Any) -> str:
    value = row.get("record_id") if isinstance(row, dict) else None
    return _text(value)


def _status(fields: Dict[str, Any]) -> str:
    value = fields.get(STATUS_FIELD)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return _text(value)


def code_kind(code: str) -> str:
    """`sh`/`sz` + 6 位数字 → stock；6 位纯数字 → fund；其余 ValueError。"""
    if not isinstance(code, str):
        raise ValueError("代码必须是字符串：%r" % (code,))
    if len(code) == 8 and code[:2] in ("sh", "sz") and code[2:].isdigit():
        return "stock"
    if len(code) == 6 and code.isdigit():
        return "fund"
    raise ValueError("无法识别的证券代码：%r" % (code,))


def parse_stock_kline(payload: Any) -> Dict[str, float]:
    """新浪日 K → {day: close}。

    payload 可以是 JSON 字符串或已解析的 list；畸形行跳过，
    整体不是数组或 JSON 无法解析时抛 ValueError。
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("股票日K响应不是合法 JSON：%s" % (exc,)) from exc
    else:
        data = payload
    if not isinstance(data, list):
        raise ValueError("股票日K响应应为数组：%r" % (type(data).__name__,))

    result: Dict[str, float] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            day = _as_date(item.get("day"))
        except ValueError:
            continue
        close = _price(item.get("close"))
        if close is None:
            continue
        result[day.isoformat()] = close
    return result


def parse_fund_nav(envelope: Any) -> Dict[str, float]:
    """东财历史净值 envelope → {FSRQ: float(DWJZ)}。

    `Data`/`LSJZList` 缺失或为空都返回 {}（当日净值未公布是常态）；
    畸形行跳过而不是让整批失败。
    """
    if not isinstance(envelope, dict):
        raise ValueError("基金净值响应应为对象：%r" % (type(envelope).__name__,))
    data = envelope.get("Data")
    if not isinstance(data, dict):
        return {}
    rows = data.get("LSJZList")
    if not isinstance(rows, list):
        return {}

    result: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = _as_date(row.get("FSRQ"))
        except ValueError:
            continue
        nav = _price(row.get("DWJZ"))
        if nav is None:
            continue
        result[day.isoformat()] = nav
    return result


def quote_window(
    today: datetime.date, pending_dates: Iterable[datetime.date]
) -> List[datetime.date]:
    """今天 + 前 2 天，并上所有 0~7 天账龄的待回补日期；升序去重。"""
    base = _as_date(today)
    days = {base, base - datetime.timedelta(days=1), base - datetime.timedelta(days=2)}
    for item in pending_dates or ():
        day = _as_date(item)
        age = (base - day).days
        if 0 <= age <= EXPIRY_DAYS:
            days.add(day)
    return sorted(days)


class _RowInfo(NamedTuple):
    """行情快照行的规范化视图。"""

    key: str
    primary: str
    code: str
    day: datetime.date
    record_id: str
    fields: Dict[str, Any]
    status: str


def _split_key(raw_key: Any) -> Tuple[str, str]:
    text = _text(raw_key)
    if "_" not in text:
        return "", ""
    code, _, day = text.rpartition("_")
    return code, day


def _row_info(raw_key: Any, row: Any) -> Optional[_RowInfo]:
    if not isinstance(row, dict):
        return None
    fields = _fields(row)
    code = _text(fields.get(CODE_FIELD))
    day: Optional[datetime.date] = None
    try:
        day = _as_date(fields.get(DATE_FIELD))
    except ValueError:
        day = None

    key_code, key_day = _split_key(raw_key)
    if not code:
        code = key_code
    if day is None and key_day:
        try:
            day = _as_date(key_day)
        except ValueError:
            day = None
    if not code or day is None:
        return None

    key = "%s_%s" % (code, day.isoformat())
    primary = _text(fields.get(PRIMARY_FIELD)) or key
    return _RowInfo(
        key=key,
        primary=primary,
        code=code,
        day=day,
        record_id=_record_id(row),
        fields=fields,
        status=_status(fields),
    )


def _iter_rows(rows: Any) -> List[Tuple[Any, Any]]:
    """把 dict（业务键→行）或 list（行数组）统一成 (raw_key, row)。"""
    if isinstance(rows, dict):
        return list(rows.items())
    if isinstance(rows, (list, tuple)):
        items: List[Tuple[Any, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            fields = _fields(row)
            # 主字段才是业务键；record_id 只是最后的兜底。
            raw_key = (
                _text(fields.get(PRIMARY_FIELD))
                or _record_id(row)
                or str(index)
            )
            items.append((raw_key, row))
        return items
    if rows is None:
        return []
    raise ValueError("行情行应为 dict 或 list：%r" % (type(rows).__name__,))


def expired_keys(
    rows: List[dict], today: datetime.date
) -> List[str]:
    """状态=待回补 且账龄 >7 天的行 → 主字段键列表（按日期/键排序）。"""
    base = _as_date(today)
    expired: List[_RowInfo] = []
    for raw_key, row in _iter_rows(rows):
        info = _row_info(raw_key, row)
        if info is None:
            continue
        if info.status != STATUS_PENDING:
            continue
        if (base - info.day).days > EXPIRY_DAYS:
            expired.append(info)
    expired.sort(key=lambda info: (info.day, info.primary))
    return [info.primary for info in expired]


def should_run(now: datetime.datetime, last_run: Optional[str]) -> bool:
    """本地时间 15:05 之后，且最近运行日期不是今天。"""
    if not isinstance(now, datetime.datetime):
        raise ValueError("now 必须是 datetime：%r" % (now,))
    # 按调用方给的本地墙钟比较（add_flow._now() 是 Asia/Shanghai 的
    # aware datetime，直接比 .time() 会和 naive 的 15:05 冲突）。
    wall_clock = (now.hour, now.minute)
    return (
        wall_clock >= (RUN_AFTER_HOUR, RUN_AFTER_MINUTE)
        and last_run != now.date().isoformat()
    )


def _normalize_fetched(
    fetched: Any,
) -> Dict[str, Dict[str, float]]:
    """fetched: 代码 → {日期: 价}；日期统一成 ISO 字符串，坏值丢弃。"""
    result: Dict[str, Dict[str, float]] = {}
    if not isinstance(fetched, dict):
        return result
    for raw_code, values in fetched.items():
        code = _text(raw_code)
        if not code or not isinstance(values, dict):
            continue
        day_map: Dict[str, float] = {}
        for raw_day, raw_price in values.items():
            try:
                day = _as_date(raw_day).isoformat()
            except ValueError:
                continue
            price = _price(raw_price)
            if price is not None:
                day_map[day] = price
        result[code] = day_map
    return result


def _new_quote_row(
    code: str, day: str, price: Optional[float], status: str
) -> Dict[str, Any]:
    return {
        PRIMARY_FIELD: "%s_%s" % (code, day),
        CODE_FIELD: code,
        DATE_FIELD: day,
        PRICE_FIELD: price,
        STATUS_FIELD: [status],
    }


def _latest_filled(
    filled: Dict[Tuple[str, str], float], code: str
) -> Optional[Tuple[str, float]]:
    latest_day: Optional[str] = None
    latest_price = 0.0
    for (row_code, day), price in filled.items():
        if row_code != code:
            continue
        if latest_day is None or day > latest_day:
            latest_day, latest_price = day, price
    if latest_day is None:
        return None
    return latest_day, latest_price


def plan_updates(
    holdings: Dict[str, dict],
    rows: Dict[str, dict],
    window: List[datetime.date],
    fetched: Dict[str, Dict[str, float]],
    today: datetime.date,
) -> WritePlan:
    """把窗口、已落库行与本次抓到的价格合成落表计划。

    - 命中新价的既有「待回补/已落」行 → update（待回补同时置已落）；
    - 窗口内没抓到的持仓代码日期 → create 空值「待回补」行；
    - 抓到的窗口外日期、非持仓代码一律忽略；
    - 持仓最新价/最新价日期 = 该代码最新「已落」值（不回退旧日期）；
    - 待回补超过 7 天 → update 状态「无数据」。
    """
    base = _as_date(today)
    window_days = sorted({_as_date(day) for day in window or ()})
    window_text = [day.isoformat() for day in window_days]

    infos: Dict[str, _RowInfo] = {}
    for raw_key, row in _iter_rows(rows or {}):
        info = _row_info(raw_key, row)
        if info is not None:
            infos[info.key] = info

    held: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    if isinstance(holdings, dict):
        for raw_id, row in holdings.items():
            if not isinstance(row, dict):
                continue
            fields = _fields(row)
            code = _text(fields.get(CODE_FIELD))
            record_id = _record_id(row) or _text(raw_id)
            if not code or not record_id:
                continue
            held[code] = (record_id, fields)

    fetch_map = _normalize_fetched(fetched)

    quote_updates: Dict[str, Dict[str, Any]] = {}

    # 1) 过期待回补行：只改状态，不动空价格。
    for key in sorted(infos, key=lambda item: (infos[item].day, item)):
        info = infos[key]
        if info.status != STATUS_PENDING:
            continue
        if (base - info.day).days > EXPIRY_DAYS and info.record_id:
            quote_updates[key] = {STATUS_FIELD: [STATUS_NO_DATA]}

    # 2) 既有已落行提供"最新价"候选。
    filled: Dict[Tuple[str, str], float] = {}
    for info in infos.values():
        if info.status != STATUS_FILLED:
            continue
        price = _price(info.fields.get(PRICE_FIELD))
        if price is not None:
            filled[(info.code, info.day.isoformat())] = price

    # 3) 窗口 × 持仓代码：命中就填，没命中就补空行。
    creates: List[Tuple[str, str, Dict[str, Any]]] = []
    for code in sorted(held):
        values = fetch_map.get(code, {})
        for day in window_text:
            key = "%s_%s" % (code, day)
            info = infos.get(key)
            value = values.get(day)
            if key in quote_updates:
                # 已判过期（无数据）：过期是终态，不能被迟到价格复活。
                continue
            if info is None:
                status = STATUS_FILLED if value is not None else STATUS_PENDING
                creates.append(
                    (code, day, _new_quote_row(code, day, value, status))
                )
                if value is not None:
                    filled[(code, day)] = value
                continue
            if info.status == STATUS_PENDING:
                if value is None or not info.record_id:
                    continue
                quote_updates[key] = {
                    PRICE_FIELD: value,
                    STATUS_FIELD: [STATUS_FILLED],
                }
                filled[(code, day)] = value
            elif info.status == STATUS_FILLED and value is not None:
                filled[(code, day)] = value
                if (
                    info.record_id
                    and _price(info.fields.get(PRICE_FIELD)) != value
                ):
                    quote_updates[key] = {PRICE_FIELD: value}

    creates.sort(key=lambda item: (item[0], item[1]))

    quote_update_list: List[Tuple[str, str, Dict[str, Any]]] = []
    for key in sorted(
        quote_updates, key=lambda item: (infos[item].code, infos[item].day, item)
    ):
        info = infos[key]
        if info.record_id:
            quote_update_list.append(
                (QUOTE_TABLE, info.record_id, quote_updates[key])
            )

    # 4) 持仓最新价：取该代码最新的已落日期（含本轮新落值）。
    holding_updates: List[Tuple[str, str, Dict[str, Any]]] = []
    for code in sorted(held):
        record_id, fields = held[code]
        latest = _latest_filled(filled, code)
        if latest is None:
            continue
        latest_day, latest_price = latest

        current_text = _text(fields.get(LATEST_DATE_FIELD))
        current_day = ""
        if current_text:
            try:
                current_day = _as_date(current_text).isoformat()
            except ValueError:
                current_day = current_text
        if current_day and current_day > latest_day:
            continue

        changes: Dict[str, Any] = {}
        if current_day != latest_day:
            changes[LATEST_DATE_FIELD] = latest_day
        if _price(fields.get(LATEST_PRICE_FIELD)) != latest_price:
            changes[LATEST_PRICE_FIELD] = latest_price
        if changes:
            holding_updates.append((HOLDING_TABLE, record_id, changes))
    holding_updates.sort(key=lambda item: item[1])

    return {
        "create": [(QUOTE_TABLE, fields) for _, _, fields in creates],
        "update": quote_update_list + holding_updates,
    }
