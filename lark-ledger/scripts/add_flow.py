"""add 流程：分类实时解析 → 纯逻辑 WritePlan → 按序写飞书。

职责边界：
- `posting` 负责算术与校验，日期保持 "YYYY-MM-DD"；
- 本模块负责 I/O：把名称解析成 record_id、把日期转成飞书 datetime 单元格
  （`"YYYY-MM-DD 00:00"`），并保证「先写明细、再更新余额/持仓」；
- 任一飞书写入失败都抛 `PartialWriteError`，其中保留已完成/未完成步骤，
  绝不把部分成功伪装成整体成功。
"""

import datetime
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import config
import lark
import master_data
import posting
import quotes_flow
import snapshot_flow

CATEGORY_TABLE = "分类"
ENTRY_TABLE = posting.ENTRY_TABLE
ACCOUNT_TABLE = posting.ACCOUNT_TABLE

TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 后置步骤失败只报警告，不回滚、不阻塞已落库的明细。
# 存 (模块, 函数名) 而不是函数对象：调用时才取属性，测试/后续任务替换实现也能生效。
_HOOKS = (
    ("行情", quotes_flow, "maybe_run"),
    ("资产快照", snapshot_flow, "maybe_weekly"),
)


class PartialWriteError(RuntimeError):
    """写入中途失败：`completed` 是已成功步骤，`unfinished` 是未完成步骤。"""

    def __init__(
        self,
        completed: List[str],
        unfinished: List[str],
        cause: BaseException,
        created_ids: Optional[List[str]] = None,
        failed_step: Optional[str] = None,
    ) -> None:
        self.completed = list(completed)
        self.unfinished = list(unfinished)
        self.cause = cause
        # 已落库的明细 record_id：部分失败时 CLI 要能指出哪一行可能已存在。
        self.created_ids = list(created_ids or [])
        self.failed_step = failed_step
        # 语义化别名：调用方不必猜属性名。
        self.completed_steps = self.completed
        self.pending = self.unfinished
        self.original_error = cause
        super().__init__(
            "部分写入失败：已完成 %d 步（%s）；未完成 %d 步（%s）；原因：%s"
            % (
                len(self.completed),
                "、".join(self.completed) or "无",
                len(self.unfinished),
                "、".join(self.unfinished) or "无",
                cause,
            )
        )


def _now() -> datetime.datetime:
    """当前时刻（UTC+8）；单独成函数便于测试注入，不用机器本地时区。"""
    return datetime.datetime.now(TZ_UTC8)


def now_utc8() -> datetime.datetime:
    """当前时刻，统一转成 UTC+8。"""
    return _now().astimezone(TZ_UTC8)


def today_utc8() -> str:
    """今天（UTC+8）的 "YYYY-MM-DD"；CLI 缺省日期用它。"""
    return now_utc8().date().isoformat()


def _table(cfg: dict, table_name: str):
    """返回 (app_token, table_id)；配置不完整时按“未配置”处理。"""
    if not isinstance(cfg, dict):
        raise config.ConfigError("配置必须是 JSON 对象")
    app_token = cfg.get("app_token")
    if not isinstance(app_token, str) or not app_token:
        raise config.ConfigError("配置缺少 app_token，请先运行 `ledger.py init`")
    tables = cfg.get("tables")
    entry = tables.get(table_name) if isinstance(tables, dict) else None
    table_id = entry.get("table_id") if isinstance(entry, dict) else None
    if not isinstance(table_id, str) or not table_id:
        raise config.ConfigError(
            "配置里缺少表「%s」的 table_id，请先运行 `ledger.py init`"
            % table_name
        )
    return app_token, table_id


def _present(value: Any) -> bool:
    """字段是否被真正赋值：None、空串、纯空白都视为未提供。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _cell_text(value: Any) -> str:
    """把 CellValue 压成可比较的文本（主字段通常就是 str）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "name", "id"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
                        break
        return "、".join(parts)
    return ""


def _category_type(record: dict) -> Optional[str]:
    """分类记录的「类型」单元格 → "收入"/"支出"；缺失或不可用返回 None。"""
    fields = record.get("fields") if isinstance(record, dict) else None
    if not isinstance(fields, dict):
        return None
    text = _cell_text(fields.get("类型")).strip()
    return text if text in ("收入", "支出") else None


def _resolve_category_record(cfg: dict, full_name: str) -> dict:
    """按主字段全名实时解析分类记录；不存在/重名都抛 PostingError。

    分类表由用户随时增删，所以不做缓存；每次记账都重新查表。
    """
    if not isinstance(full_name, str) or not full_name.strip():
        raise posting.PostingError("分类不能为空：请提供分类全名，如「支出·餐饮·午餐」")
    target = full_name.strip()

    app_token, table_id = _table(cfg, CATEGORY_TABLE)
    records = lark.search_records(app_token, table_id)
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("fields"), dict)
        and _cell_text(record["fields"].get("分类")) == target
    ]

    if not matches:
        raise posting.PostingError(
            "分类不存在：「%s」未在分类表找到；请先在分类表添加该分类，"
            "或换一个已有分类（脚本不会自动创建分类）" % target
        )
    if len(matches) > 1:
        record_ids = "、".join(
            str(record.get("record_id") or "?") for record in matches
        )
        raise posting.PostingError(
            "分类「%s」匹配到 %d 条记录（%s），存在歧义；"
            "请先在「分类」表消除重名" % (target, len(matches), record_ids)
        )
    record = matches[0]
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise posting.PostingError(
            "分类「%s」的 record_id 异常：%r" % (target, record_id)
        )
    return record


def resolve_category(cfg: dict, full_name: str) -> str:
    """按主字段全名实时解析分类 record_id；不存在就报错，绝不自动创建。"""
    return _resolve_category_record(cfg, full_name)["record_id"]


def category_kind(cfg: dict, full_name: str) -> Optional[str]:
    """按分类表「类型」决定方向：支出→expense，收入→income。

    类型缺失或不可用（例如只写了分类名）时返回 None，由调用方要求用户显式
    指定 --kind，绝不按金额正负猜方向。
    """
    record = _resolve_category_record(cfg, full_name)
    return {"支出": "expense", "收入": "income"}.get(_category_type(record))


def _check_category_direction(kind: str, record: dict, full_name: str) -> None:
    """kind 与分类表「类型」必须一致，否则拒绝写入（避免把支出记进收入分类）。"""
    expected = "支出" if kind == "expense" else "收入"
    actual = _category_type(record)
    label = full_name.strip()
    if actual is None:
        raise posting.PostingError(
            "分类「%s」的「类型」缺失或不可用，无法确认%s；"
            "请在分类表把类型设为 支出/收入"
            % (label, posting.KIND_LABELS[kind])
        )
    if actual != expected:
        raise posting.PostingError(
            "分类「%s」的类型是「%s」，不能用于%s；请改用「%s」类型的分类"
            % (label, actual, posting.KIND_LABELS[kind], expected)
        )


def _entry_kind(entry: Any) -> str:
    if not isinstance(entry, dict):
        raise posting.PostingError("记账参数必须是 dict，实际是 %r" % (entry,))
    kind = entry.get("kind")
    if kind not in posting.SUPPORTED_KINDS:
        supported = " / ".join(
            "%s（%s）" % (name, posting.KIND_LABELS[name])
            for name in posting.SUPPORTED_KINDS
        )
        raise posting.PostingError(
            "不支持的记账类型 %r；当前只支持 %s" % (kind, supported)
        )
    return kind


_PNL_CATEGORIES = {
    "股票": "收入·理财收益·股票收益",
    "基金": "收入·理财收益·基金收益",
}

_IGNORED_FIELDS = {
    "to_account": "转入账户（to_account）",
    "from_account": "转出账户（from_account）",
    "category": "分类（category）",
    "holding": "持仓（holding）",
    "price": "成交价（price）",
    "shares": "份额（shares）",
    "pnl_category": "已实现盈亏分类（pnl_category）",
}


def _reject_irrelevant(entry: dict, kind: str) -> None:
    """拒绝与 kind 无关的参数：宁可报错，也不静默丢弃用户输入。"""
    if kind == "transfer":
        _reject_fields(entry, kind, ("category", "from_account", "holding", "price", "shares"))
        return
    if kind in ("income", "expense"):
        _reject_fields(
            entry, kind, ("to_account", "from_account", "holding", "price", "shares")
        )
        return
    if kind == "invest_buy":
        _reject_fields(entry, kind, ("to_account", "category", "pnl_category"))
        return
    if kind == "invest_sell":
        _reject_fields(entry, kind, ("from_account", "pnl_category"))
        return
    if kind == "dividend":
        _reject_fields(
            entry,
            kind,
            ("to_account", "from_account", "holding", "price", "shares"),
        )


def _reject_fields(entry: dict, kind: str, keys) -> None:
    for key in keys:
        if _present(entry.get(key)):
            raise posting.PostingError(
                "%s不需要%s；请去掉该参数，或确认记账类型"
                % (posting.KIND_LABELS[kind], _IGNORED_FIELDS.get(key, key))
            )


def _resolve_holding_snapshot(
    cfg: dict, entry: dict
) -> Tuple[str, Dict[str, dict]]:
    """解析持仓（代码/名称/record_id），返回 record_id 与单条持仓快照。"""
    holding_ref = entry.get("holding")
    if not _present(holding_ref):
        raise posting.PostingError(
            "理财买卖必须指定持仓（--holding 代码/名称/record_id）"
        )
    holding = master_data.resolve_holding(cfg, holding_ref)
    record_id = holding["record_id"]
    return record_id, {record_id: holding["fields"]}


_PNL_CATEGORY_PREFIX = "收入·理财收益"


def _resolve_pnl_category(
    cfg: dict, entry: dict, holding_fields: Dict[str, Any]
) -> str:
    """卖出按持仓品种路由盈亏分类；显式 --category 只能限定在理财收益下。"""
    category_ref = entry.get("category")
    if _present(category_ref):
        record = _resolve_category_record(cfg, category_ref)
        full_name = _cell_text(record["fields"].get("分类")).strip()
        if not full_name.startswith(_PNL_CATEGORY_PREFIX):
            raise posting.PostingError(
                "理财卖出的盈亏分类必须属于「%s」：收到「%s」；"
                "脚本按持仓品种自动路由（股票→%s，基金→%s）"
                % (
                    _PNL_CATEGORY_PREFIX,
                    full_name,
                    _PNL_CATEGORIES["股票"],
                    _PNL_CATEGORIES["基金"],
                )
            )
    else:
        breed = _cell_text(holding_fields.get("品种")).strip()
        category_ref = _PNL_CATEGORIES.get(breed)
        if category_ref is None:
            raise posting.PostingError(
                "无法按持仓品种确定已实现盈亏分类：品种「%s」；"
                "请用 --category 显式指定「%s」下的分类"
                % (breed or "未填写", _PNL_CATEGORY_PREFIX)
            )
        record = _resolve_category_record(cfg, category_ref)
    _check_category_direction("income", record, category_ref)
    return record["record_id"]


def _normalized_entry(
    cfg: dict, entry: dict
) -> Tuple[Dict[str, Any], Dict[str, dict], Dict[str, dict]]:
    """解析名称 → record_id，返回 entry、账户快照与持仓快照。"""
    kind = _entry_kind(entry)
    _reject_irrelevant(entry, kind)

    account_ref = entry.get("account")
    if not _present(account_ref):
        raise posting.PostingError("账户不能为空：请提供账户名称或 record_id")
    account = master_data.resolve_account(cfg, account_ref)
    accounts = {account["record_id"]: account["fields"]}

    normalized: Dict[str, Any] = {
        "kind": kind,
        "date": entry.get("date"),
        "amount": entry.get("amount"),
        "account": account["record_id"],
        "summary": entry.get("summary") or "",
        "note": entry.get("note") or "",
    }
    holdings: Dict[str, dict] = {}

    if kind == "transfer":
        target_ref = entry.get("to_account")
        if not _present(target_ref):
            raise posting.PostingError(
                "转账必须指定转入账户（to_account）"
            )
        target = master_data.resolve_account(cfg, target_ref)
        accounts[target["record_id"]] = target["fields"]
        normalized["to_account"] = target["record_id"]
    elif kind in posting.INVEST_KINDS:
        holding_id, holdings = _resolve_holding_snapshot(cfg, entry)
        normalized["holding"] = holding_id
        normalized["price"] = entry.get("price")
        normalized["shares"] = entry.get("shares")
    elif kind == "dividend":
        category_ref = entry.get("category")
        if not _present(category_ref):
            raise posting.PostingError(
                "理财分红必须指定分类（--category 收入·理财收益·分红）"
            )
        record = _resolve_category_record(cfg, category_ref)
        _check_category_direction("income", record, category_ref)
        normalized["pnl_category"] = record["record_id"]
    else:
        category_ref = entry.get("category")
        if not _present(category_ref):
            raise posting.PostingError(
                "%s必须指定分类（category），例如「支出·餐饮·午餐」"
                % posting.KIND_LABELS[kind]
            )
        record = _resolve_category_record(cfg, category_ref)
        _check_category_direction(kind, record, category_ref)
        normalized["category"] = record["record_id"]

    if kind == "invest_buy":
        source_ref = entry.get("from_account")
        if not _present(source_ref):
            raise posting.PostingError(
                "理财买入必须指定转出账户（--from-account 银行卡/现金）"
            )
        source = master_data.resolve_account(cfg, source_ref)
        accounts[source["record_id"]] = source["fields"]
        normalized["from_account"] = source["record_id"]
    elif kind == "invest_sell":
        target_ref = entry.get("to_account")
        if not _present(target_ref):
            raise posting.PostingError(
                "理财卖出必须指定转入账户（--to-account 银行卡/现金）"
            )
        target = master_data.resolve_account(cfg, target_ref)
        accounts[target["record_id"]] = target["fields"]
        normalized["to_account"] = target["record_id"]
        normalized["pnl_category"] = _resolve_pnl_category(
            cfg, entry, holdings[normalized["holding"]]
        )

    return normalized, accounts, holdings


def _cell_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """纯逻辑层 → 飞书 CellValue：日期字段补 " 00:00"（不写毫秒）。"""
    converted = dict(fields)
    for name, value in converted.items():
        if (
            name.endswith("日期")
            and isinstance(value, str)
            and _DATE_PATTERN.fullmatch(value)
        ):
            converted[name] = value + " 00:00"
    return converted


def _step_label(action: str, table_name: str, record_id: Optional[str]) -> str:
    if action == "create":
        return "创建「%s」" % table_name
    return "更新「%s」%s" % (table_name, record_id)


def _run_hooks(cfg: dict, now: datetime.datetime) -> None:
    for label, module, name in _HOOKS:
        try:
            getattr(module, name)(cfg, now)
        except Exception as exc:  # 后置步骤不是记账的一部分，失败只警告。
            print(
                "警告：%s 后置步骤失败（明细已落库，不影响记账）：%s"
                % (label, exc),
                file=sys.stderr,
            )


def execute(
    cfg: dict, entry: dict, now: Optional[datetime.datetime] = None
) -> dict:
    """校验 → 规划 → 先 create 明细、后 update 余额/持仓，再跑后置钩子。

    返回 `{"created": [record_id...], "updated": 更新条数}`；
    写入中途任何 LarkError 都转成 `PartialWriteError`（带 completed/unfinished）。
    """
    normalized, accounts, holdings = _normalized_entry(cfg, entry)
    plan = posting.plan_add(normalized, accounts, holdings)

    steps = [
        ("create", table_name, None, fields)
        for table_name, fields in plan["create"]
    ] + [
        ("update", table_name, record_id, fields)
        for table_name, record_id, fields in plan["update"]
    ]
    # 先把所有表定位解析好：配置错误必须在写第一条之前暴露，不能写了一半才炸。
    prepared = [(step, _table(cfg, step[1])) for step in steps]

    completed: List[str] = []
    created_ids: List[str] = []
    updated = 0

    for index, (step, location) in enumerate(prepared):
        action, table_name, record_id, fields = step
        app_token, table_id = location
        try:
            if action == "create":
                ids = lark.create_records(
                    app_token, table_id, [_cell_fields(fields)]
                )
                if not isinstance(ids, list) or not ids:
                    raise lark.LarkError(
                        "创建「%s」后飞书未返回 record_id" % table_name
                    )
                created_ids.extend(ids)
            else:
                lark.update_record(
                    app_token, table_id, record_id, _cell_fields(fields)
                )
                updated += 1
        except lark.LarkError as exc:
            raise PartialWriteError(
                completed=completed,
                unfinished=[
                    _step_label(*step[:3]) for step in steps[index:]
                ],
                cause=exc,
                created_ids=created_ids,
                failed_step=_step_label(action, table_name, record_id),
            ) from exc
        completed.append(_step_label(action, table_name, record_id))

    _run_hooks(cfg, now_utc8() if now is None else now)
    return {"created": created_ids, "updated": updated}
