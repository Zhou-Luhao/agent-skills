"""账户 / 持仓主数据：按名称或 record_id 解析、增改查、销户。

所有飞书读写都走 `lark` 封装；本模块只做字段编码与业务校验：
- 文本 → str；数字 → float；单选 → `["选项名"]`；关联 → `[{"id": record_id}]`；
- 持仓的 市值 / 浮动盈亏 是公式字段，永不写入；
- 年利率按**小数**存储（4.2% = 0.042）；CLI 的 `--rate` 收百分数，
  通过 `rate_from_percent` 换算后再传给本模块。
"""

import math
from typing import Any, Dict, List, Optional

import config
import lark

ACCOUNT_TABLE = "账户"
HOLDING_TABLE = "持仓"

ACCOUNT_TYPES = ("现金", "微信支付宝", "银行卡", "理财", "负债")
REPAY_METHODS = ("等额本息", "等额本金", "随借随还")
ACCOUNT_STATUSES = ("在用", "已销户")
HOLDING_KINDS = ("股票", "基金")
HOLDING_STATUSES = ("持有", "已清仓")

ACCOUNT_WRITABLE = (
    "账户名称",
    "类型",
    "初始余额",
    "当前余额",
    "年利率",
    "还款方式",
    "状态",
    "备注",
)
HOLDING_WRITABLE = (
    "持仓名称",
    "代码",
    "品种",
    "理财账户",
    "当前份额",
    "成本价",
    "状态",
    "期初份额",
    "期初成本",
)

# 便捷别名：CLI 与脚本都可以用英文参数名，落库前统一成中文字段名。
_ACCOUNT_ALIASES = {
    "name": "账户名称",
    "type": "类型",
    "type_": "类型",
    "opening": "初始余额",
    "rate": "年利率",
    "repay": "还款方式",
    "status": "状态",
    "note": "备注",
}
_HOLDING_ALIASES = {
    "name": "持仓名称",
    "code": "代码",
    "kind": "品种",
    "account": "理财账户",
    "status": "状态",
    "opening_shares": "期初份额",
    "opening_cost": "期初成本",
}


class MasterDataError(RuntimeError):
    """主数据不存在、重名歧义，或字段/取值不合法。"""


def rate_from_percent(percent: Any) -> float:
    """百分数 → 存储用小数：4.2 → 0.042。"""
    # 4.2/100 在二进制浮点里会得到 0.04200000000000001 一类尾数，抹掉无意义的末位。
    return round(_number(percent, "年利率") / 100.0, 10)


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


def _fetch(cfg: dict, table_name: str) -> List[dict]:
    """读出表内全部记录（个人账本量级，一次 2000 条足够）。"""
    app_token, table_id = _table(cfg, table_name)
    records = lark.search_records(app_token, table_id, limit=2000)
    return [
        record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("fields"), dict)
    ]


def _cell_text(value: Any) -> str:
    """把 CellValue 压成可比较/可展示的文本。"""
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


def _resolve(
    cfg: dict,
    table_name: str,
    ref: Any,
    match_fields,
    label: str,
    list_command: str,
) -> dict:
    """按 record_id 或给定字段精确匹配唯一一条记录。"""
    target = _require_text(ref, label)
    matches = []
    for record in _fetch(cfg, table_name):
        if record.get("record_id") == target or any(
            _cell_text(record["fields"].get(field)) == target
            for field in match_fields
        ):
            matches.append(record)
    if not matches:
        raise MasterDataError(
            "找不到%s「%s」：请先用 `%s list` 查看已有%s"
            % (label, target, list_command, label)
        )
    if len(matches) > 1:
        record_ids = "、".join(
            str(record.get("record_id") or "?") for record in matches
        )
        raise MasterDataError(
            "%s「%s」匹配到 %d 条记录（%s），存在歧义，无法确定是哪一条；"
            "请改用 record_id，或先消除重名"
            % (label, target, len(matches), record_ids)
        )
    return matches[0]


def resolve_account(cfg: dict, name_or_id: str) -> dict:
    """按 record_id 或账户名称精确解析账户，唯一命中才返回记录。"""
    return _resolve(
        cfg,
        ACCOUNT_TABLE,
        name_or_id,
        ("账户名称",),
        "账户",
        "ledger.py account",
    )


def resolve_holding(cfg: dict, code_or_name: str) -> dict:
    """按 record_id、代码或持仓名称精确解析持仓，唯一命中才返回记录。"""
    return _resolve(
        cfg,
        HOLDING_TABLE,
        code_or_name,
        ("代码", "持仓名称"),
        "持仓",
        "ledger.py holding",
    )


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MasterDataError("%s不能为空" % label)
    return value.strip()


def _number(value: Any, label: str, non_negative: bool = False) -> float:
    if isinstance(value, bool):
        raise MasterDataError("%s必须是数字，实际是布尔值" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MasterDataError("%s必须是数字，实际是 %r" % (label, value))
    if not math.isfinite(number):
        raise MasterDataError("%s必须是有限数字，实际是 %r" % (label, value))
    if non_negative and number < 0:
        raise MasterDataError("%s不能为负数，实际是 %r" % (label, value))
    return number


def _choice(value: Any, allowed, label: str) -> str:
    if value not in allowed:
        raise MasterDataError(
            "%s只能是 %s，实际是 %r"
            % (label, " / ".join(allowed), value)
        )
    return value


def _created_id(record_ids: List[str], label: str) -> str:
    if len(record_ids) != 1 or not isinstance(record_ids[0], str) or not record_ids[0]:
        raise MasterDataError(
            "创建%s后飞书返回的 record_id 异常：%r" % (label, record_ids)
        )
    return record_ids[0]


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------


def account_add(
    cfg: dict,
    name: str,
    type_: str,
    opening: float,
    rate: Optional[float] = None,
    repay: Optional[str] = None,
) -> str:
    """新增账户，返回 record_id；初始余额与当前余额都写成 opening。

    `rate` 是存储用小数（4.2% 传 0.042）；负债的 opening 传负数。
    """
    opening_value = _number(opening, "初始余额")
    fields: Dict[str, Any] = {
        "账户名称": _require_text(name, "账户名称"),
        "类型": [_choice(type_, ACCOUNT_TYPES, "账户类型")],
        "初始余额": opening_value,
        "当前余额": opening_value,
        "状态": ["在用"],
    }
    if rate is not None:
        fields["年利率"] = _number(rate, "年利率", non_negative=True)
    if repay is not None:
        fields["还款方式"] = [_choice(repay, REPAY_METHODS, "还款方式")]

    app_token, table_id = _table(cfg, ACCOUNT_TABLE)
    record_ids = lark.create_records(app_token, table_id, [fields])
    return _created_id(record_ids, "账户")


def _account_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key, value in raw.items():
        name = _ACCOUNT_ALIASES.get(key, key)
        if name not in ACCOUNT_WRITABLE:
            raise MasterDataError(
                "不支持的账户字段「%s」；可写字段：%s"
                % (key, "、".join(ACCOUNT_WRITABLE))
            )
        fields[name] = _account_value(name, value)
    return fields


def _account_value(name: str, value: Any) -> Any:
    if name == "账户名称":
        return _require_text(value, "账户名称")
    if name == "类型":
        return [_choice(value, ACCOUNT_TYPES, "账户类型")]
    if name in ("初始余额", "当前余额"):
        return _number(value, name)
    if name == "年利率":
        return _number(value, "年利率", non_negative=True)
    if name == "还款方式":
        return [_choice(value, REPAY_METHODS, "还款方式")]
    if name == "状态":
        return [_choice(value, ACCOUNT_STATUSES, "账户状态")]
    if name == "备注":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise MasterDataError("备注必须是文本，实际是 %r" % (value,))
        return value
    raise MasterDataError("不支持的账户字段「%s」" % name)


def account_edit(cfg: dict, ref: str, **fields) -> str:
    """按 record_id / 名称修改账户字段，返回 record_id。"""
    if not fields:
        raise MasterDataError("account edit 至少指定一个要修改的字段")
    record = resolve_account(cfg, ref)
    app_token, table_id = _table(cfg, ACCOUNT_TABLE)
    lark.update_record(
        app_token, table_id, record["record_id"], _account_fields(fields)
    )
    return record["record_id"]


def account_list(cfg: dict) -> List[dict]:
    """列出全部账户（含已销户），保持表内顺序。"""
    return _fetch(cfg, ACCOUNT_TABLE)


def account_close(cfg: dict, ref: str) -> str:
    """销户：状态改为「已销户」，其他字段不动。"""
    record = resolve_account(cfg, ref)
    app_token, table_id = _table(cfg, ACCOUNT_TABLE)
    lark.update_record(
        app_token, table_id, record["record_id"], {"状态": ["已销户"]}
    )
    return record["record_id"]


# ---------------------------------------------------------------------------
# 持仓
# ---------------------------------------------------------------------------


def holding_add(
    cfg: dict,
    name: str,
    code: str,
    kind: str,
    account_ref: str,
    shares: float,
    cost: float,
) -> str:
    """新增持仓，返回 record_id。

    期初份额 / 期初成本与当前值一致，状态默认「持有」；理财账户写成关联。
    """
    account = resolve_account(cfg, account_ref)
    shares_value = _number(shares, "当前份额", non_negative=True)
    cost_value = _number(cost, "成本价", non_negative=True)
    fields: Dict[str, Any] = {
        "持仓名称": _require_text(name, "持仓名称"),
        "代码": _require_text(code, "代码"),
        "品种": [_choice(kind, HOLDING_KINDS, "品种")],
        "理财账户": [{"id": account["record_id"]}],
        "当前份额": shares_value,
        "成本价": cost_value,
        "状态": ["持有"],
        "期初份额": shares_value,
        "期初成本": cost_value,
    }
    app_token, table_id = _table(cfg, HOLDING_TABLE)
    record_ids = lark.create_records(app_token, table_id, [fields])
    return _created_id(record_ids, "持仓")


def _holding_fields(cfg: dict, raw: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key, value in raw.items():
        # shares / cost 是“主数据口径”的便捷别名：当前值与期初值一起改。
        if key == "shares":
            number = _number(value, "当前份额", non_negative=True)
            fields["当前份额"] = number
            fields["期初份额"] = number
            continue
        if key == "cost":
            number = _number(value, "成本价", non_negative=True)
            fields["成本价"] = number
            fields["期初成本"] = number
            continue

        name = _HOLDING_ALIASES.get(key, key)
        if name not in HOLDING_WRITABLE:
            raise MasterDataError(
                "不支持的持仓字段「%s」；可写字段：%s"
                % (key, "、".join(HOLDING_WRITABLE))
            )
        fields[name] = _holding_value(cfg, name, value)
    return fields


def _holding_value(cfg: dict, name: str, value: Any) -> Any:
    if name == "持仓名称":
        return _require_text(value, "持仓名称")
    if name == "代码":
        return _require_text(value, "代码")
    if name == "品种":
        return [_choice(value, HOLDING_KINDS, "品种")]
    if name == "理财账户":
        if isinstance(value, list):
            return value
        account = resolve_account(cfg, value)
        return [{"id": account["record_id"]}]
    if name in ("当前份额", "期初份额"):
        return _number(value, name, non_negative=True)
    if name in ("成本价", "期初成本"):
        return _number(value, name, non_negative=True)
    if name == "状态":
        return [_choice(value, HOLDING_STATUSES, "持仓状态")]
    raise MasterDataError("不支持的持仓字段「%s」" % name)


def holding_edit(cfg: dict, ref: str, **fields) -> str:
    """按 record_id / 代码 / 名称修改持仓字段，返回 record_id。"""
    if not fields:
        raise MasterDataError("holding edit 至少指定一个要修改的字段")
    record = resolve_holding(cfg, ref)
    app_token, table_id = _table(cfg, HOLDING_TABLE)
    lark.update_record(
        app_token, table_id, record["record_id"], _holding_fields(cfg, fields)
    )
    return record["record_id"]


def holding_list(cfg: dict) -> List[dict]:
    """列出全部持仓（含已清仓），保持表内顺序。"""
    return _fetch(cfg, HOLDING_TABLE)