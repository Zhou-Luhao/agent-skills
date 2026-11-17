"""行情后置流程：窗口判定、逐代码抓取、落表与配置标记。

职责边界：
- `quotes` 负责解析/窗口/计划等纯逻辑与两个 HTTP 抓取函数；
- 本模块负责 lark I/O 与编排：读持仓/快照表、按 `code_kind` 路由抓取、
  把 `plan_updates` 的计划按序写入，成功后才记 `last_quotes_date`；
- 单个代码/数据源失败只收集为警告，绝不中断整轮，也绝不伪造"待回补"
  行（源故障 ≠ 净值未公布）；写表/配置失败则抛出，由 `maybe_run` 兜住，
  不影响 add_flow 已落库的明细。
"""

import datetime
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import config
import lark
import quotes

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 行情快照会无限增长，必须按页读到"短页"为止；不能用总量上限静默截断。
PAGE_SIZE = 200
STATUS_HELD = "持有"


def _now() -> datetime.datetime:
    """当前时刻（UTC+8）；单独成函数便于测试注入，不用机器本地时区。"""
    return datetime.datetime.now(TZ_UTC8)


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


def _fields(record: Any) -> Dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _record_id(record: Any) -> str:
    value = record.get("record_id") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _status(fields: Dict[str, Any]) -> str:
    value = fields.get(quotes.STATUS_FIELD)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return _text(value)


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


def _load_held_holdings(
    app_token: str, table_id: str
) -> Dict[str, dict]:
    """状态=持有的持仓，按 record_id 索引（plan_updates 的入参形状）。"""
    records = _search_all(app_token, table_id)
    held: Dict[str, dict] = {}
    for record in records:
        fields = _fields(record)
        record_id = _record_id(record)
        code = _text(fields.get(quotes.CODE_FIELD))
        if not record_id or not code:
            continue
        if _status(fields) != STATUS_HELD:
            continue
        held[record_id] = record
    return held


def _search_all(app_token: str, table_id: str) -> List[dict]:
    """按 200 行/页读到短页为止；翻页不推进时显式报错，绝不静默截断。

    行情快照只增不减：一旦超过旧的固定上限，窗口行会落在切片外，
    `plan_updates` 就会给同一个「代码_日期」再建一行。这里改为
    分页读尽，并在后端返回重复页（游标没前进）时抛 LarkError。
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


def _load_quote_rows(app_token: str, table_id: str) -> List[dict]:
    return _search_all(app_token, table_id)


def _row_code(fields: Dict[str, Any]) -> str:
    """行代码：优先「代码」字段，退回「代码-日期」主字段的前缀。"""
    code = _text(fields.get(quotes.CODE_FIELD))
    if code:
        return code
    return _text(fields.get(quotes.PRIMARY_FIELD)).rpartition("_")[0]


def _pending_dates(rows: List[dict]) -> List[datetime.date]:
    """已落库的待回补日期（用于把 4~7 天账龄的缺口并进窗口）。"""
    days: List[datetime.date] = []
    for record in rows:
        fields = _fields(record)
        if _status(fields) != quotes.STATUS_PENDING:
            continue
        try:
            days.append(quotes._as_date(fields.get(quotes.DATE_FIELD)))
        except ValueError:
            continue
    return days


def _pending_codes(
    rows: List[dict], window: List[datetime.date]
) -> Set[str]:
    """窗口内待回补行涉及的代码（含已清仓持仓——它们仍需要回补）。"""
    days = set(window)
    codes: Set[str] = set()
    for record in rows:
        fields = _fields(record)
        if _status(fields) != quotes.STATUS_PENDING:
            continue
        try:
            day = quotes._as_date(fields.get(quotes.DATE_FIELD))
        except ValueError:
            continue
        if day not in days:
            continue
        code = _row_code(fields)
        if code:
            codes.add(code)
    return codes


def _fetch_all(
    codes: List[str], window: List[datetime.date]
) -> Tuple[Dict[str, Dict[str, float]], List[str], Set[str]]:
    """逐代码抓取；单源失败只记录警告，返回 (成功结果, 警告, 失败代码)。"""
    fetched: Dict[str, Dict[str, float]] = {}
    errors: List[str] = []
    failed: Set[str] = set()
    start = window[0].isoformat()
    end = window[-1].isoformat()

    for code in codes:
        try:
            kind = quotes.code_kind(code)
            if kind == "stock":
                values = quotes.fetch_stock(code)
            else:
                values = quotes.fetch_fund(code, start, end)
        except (quotes.QuoteSourceError, OSError, ValueError) as exc:
            failed.add(code)
            errors.append("%s：%s" % (code, exc))
            continue
        if isinstance(values, dict):
            fetched[code] = values
        else:
            failed.add(code)
            errors.append("%s：行情响应不是字典" % (code,))
    return fetched, errors, failed


def _write_plan(
    app_token: str, table_ids: Dict[str, str], plan: quotes.WritePlan
) -> int:
    """按计划顺序先 create（连续同表合批）后 update，返回写入条数。"""
    updated = 0

    creates = list(plan.get("create") or [])
    start = 0
    while start < len(creates):
        table_name = creates[start][0]
        batch: List[Dict[str, Any]] = []
        while start < len(creates) and creates[start][0] == table_name:
            batch.append(_cell_fields(creates[start][1]))
            start += 1
        if table_name not in table_ids:
            raise config.ConfigError(
                "配置里缺少表「%s」的 table_id，请先运行 `ledger.py init`"
                % table_name
            )
        lark.create_records(app_token, table_ids[table_name], batch)
        updated += len(batch)

    for table_name, record_id, fields in plan.get("update") or []:
        if table_name not in table_ids:
            raise config.ConfigError(
                "配置里缺少表「%s」的 table_id，请先运行 `ledger.py init`"
                % table_name
            )
        lark.update_record(
            app_token, table_ids[table_name], record_id, _cell_fields(fields)
        )
        updated += 1
    return updated


def _count_pending(
    rows: List[dict], plan: quotes.WritePlan, window: List[datetime.date]
) -> int:
    """本轮结束后仍未落值的窗口内待回补行数（供 CLI 摘要）。"""
    window_days = set(window)
    updated_ids = {
        record_id
        for table_name, record_id, _ in plan.get("update") or []
        if table_name == quotes.QUOTE_TABLE
    }
    pending = sum(
        1
        for table_name, fields in plan.get("create") or []
        if table_name == quotes.QUOTE_TABLE
        and fields.get(quotes.STATUS_FIELD) == [quotes.STATUS_PENDING]
    )
    for record in rows:
        fields = _fields(record)
        record_id = _record_id(record)
        if not record_id or record_id in updated_ids:
            continue
        if _status(fields) != quotes.STATUS_PENDING:
            continue
        try:
            day = quotes._as_date(fields.get(quotes.DATE_FIELD))
        except ValueError:
            continue
        if day in window_days:
            pending += 1
    return pending


def run_quotes(
    cfg: dict,
    force: bool = False,
    now: Optional[datetime.datetime] = None,
) -> dict:
    """拉取并回补行情；成功写入后记账 `last_quotes_date`。

    - 非 force 且 `quotes.should_run` 不满足 → `{"skipped": True}`；
    - 单代码源失败 → 警告进 `errors`，其余代码照常落表；
    - 全部待抓代码都失败 → 不写表、不记 `last_quotes_date`（同日可重试）；
    - 写表失败向上抛 `lark.LarkError`（此时不写配置，下次可重试）。
    """
    moment = _now() if now is None else now
    if not isinstance(cfg, dict):
        raise config.ConfigError("配置必须是 JSON 对象")
    if not force and not quotes.should_run(moment, cfg.get("last_quotes_date")):
        return {"skipped": True}

    app_token, holding_table = _table(cfg, quotes.HOLDING_TABLE)
    _, quote_table = _table(cfg, quotes.QUOTE_TABLE)
    table_ids = {
        quotes.HOLDING_TABLE: holding_table,
        quotes.QUOTE_TABLE: quote_table,
    }

    held = _load_held_holdings(app_token, holding_table)
    rows = _load_quote_rows(app_token, quote_table)

    today = moment.date()
    window = quotes.quote_window(today, _pending_dates(rows))

    codes = sorted(
        {
            _text(_fields(record).get(quotes.CODE_FIELD))
            for record in held.values()
            if _text(_fields(record).get(quotes.CODE_FIELD))
        }
        | _pending_codes(rows, window)
    )
    fetched, errors, failed_codes = _fetch_all(codes, window)

    # 全部待抓代码都失败：没有任何成功写入，不能标记"今天已跑过"，
    # 这样同一交易时段的下一次 add 还会重试；只要有一个成功就照常记账。
    if codes and failed_codes == set(codes):
        pending = _count_pending(
            rows, {"create": [], "update": []}, window
        )
        return {"updated": 0, "pending": pending, "errors": errors}

    # 源故障的代码不参加 plan：否则会造出"待回补"假象、掩盖接口故障。
    plan_holdings = {
        record_id: record
        for record_id, record in held.items()
        if _text(_fields(record).get(quotes.CODE_FIELD)) not in failed_codes
    }
    plan = quotes.plan_updates(plan_holdings, rows, window, fetched, today)

    updated = _write_plan(app_token, table_ids, plan)
    pending = _count_pending(rows, plan, window)

    cfg["last_quotes_date"] = today.isoformat()
    config.save_config(cfg)
    return {"updated": updated, "pending": pending, "errors": errors}


def maybe_run(
    cfg: dict, now: Optional[datetime.datetime] = None
) -> dict:
    """add 成功后的行情钩子：窗口满足才跑，任何异常都转警告返回。

    这是"记账已落库"之后的最佳努力步骤：失败只影响本次行情更新，
    绝不把异常抛回 add_flow，更不会阻塞或回滚记账。
    """
    moment = _now() if now is None else now
    try:
        if not quotes.should_run(
            moment,
            cfg.get("last_quotes_date") if isinstance(cfg, dict) else None,
        ):
            return {"skipped": True}
        return run_quotes(cfg, force=False, now=moment)
    except Exception as exc:  # 后置钩子边界：任何失败都只警告。
        warning = "行情更新失败（明细已落库，不影响记账）：%s" % exc
        print("警告：%s" % warning, file=sys.stderr)
        return {"warning": warning, "errors": [warning]}
