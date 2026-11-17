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
    ) -> None:
        self.completed = list(completed)
        self.unfinished = list(unfinished)
        self.cause = cause
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


def resolve_category(cfg: dict, full_name: str) -> str:
    """按主字段全名实时解析分类 record_id；不存在就报错，绝不自动创建。

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
    record_id = matches[0].get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise posting.PostingError(
            "分类「%s」的 record_id 异常：%r" % (target, record_id)
        )
    return record_id


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


def _reject_irrelevant(entry: dict, kind: str) -> None:
    """收支不能带转入账户，转账不能带分类：宁可报错，也不静默丢弃参数。"""
    if kind == "transfer":
        if _present(entry.get("category")):
            raise posting.PostingError(
                "转账不需要分类（category）：分类只用于收入/支出；请去掉分类参数"
            )
        return
    if _present(entry.get("to_account")):
        raise posting.PostingError(
            "%s不需要转入账户（to_account）：只有转账（transfer）才有转入账户；"
            "请确认记账类型" % posting.KIND_LABELS[kind]
        )


def _normalized_entry(
    cfg: dict, entry: dict
) -> Tuple[Dict[str, Any], Dict[str, dict]]:
    """解析名称 → record_id，返回只含该 kind 需要的键的 entry 与账户快照。"""
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

    if kind == "transfer":
        target_ref = entry.get("to_account")
        if not _present(target_ref):
            raise posting.PostingError(
                "转账必须指定转入账户（to_account）"
            )
        target = master_data.resolve_account(cfg, target_ref)
        accounts[target["record_id"]] = target["fields"]
        normalized["to_account"] = target["record_id"]
    else:
        category_ref = entry.get("category")
        if not _present(category_ref):
            raise posting.PostingError(
                "%s必须指定分类（category），例如「支出·餐饮·午餐」"
                % posting.KIND_LABELS[kind]
            )
        normalized["category"] = resolve_category(cfg, category_ref)

    return normalized, accounts


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
    normalized, accounts = _normalized_entry(cfg, entry)
    # 本任务只处理收支/转账；持仓快照供 Task 7 的投资分支使用。
    plan = posting.plan_add(normalized, accounts, {})

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
            ) from exc
        completed.append(_step_label(action, table_name, record_id))

    _run_hooks(cfg, now_utc8() if now is None else now)
    return {"created": created_ids, "updated": updated}