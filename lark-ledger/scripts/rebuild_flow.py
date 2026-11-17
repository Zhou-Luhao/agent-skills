"""修正与重算流程：edit 改一行、delete 整组删除、rebuild 全量重放。

一致性基准（本模块最重要的约定）：
- 账户/持仓的「应为值」一律来自 `posting.replay`：从 初始余额 /
  期初份额 / 期初成本 起，按 (日期, 创建时间, record_id) 逐笔重放全部明细；
- 因此 edit / delete 对受影响对象的修正结果，与一次 from-scratch rebuild
  完全相同；当前余额、当前份额只用于展示差异，绝不作为计算输入；
- 绝不写公式字段（市值 / 浮动盈亏），也不改持仓状态。

写入顺序沿用 add 流程的约定：先改/删明细，再更新账户与持仓。任一步失败都
抛 PartialWriteError，保留已完成/未完成步骤，不把部分成功伪装成整体成功。
"""

import datetime
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import add_flow
import config
import lark
import master_data
import posting

ENTRY_TABLE = posting.ENTRY_TABLE
ACCOUNT_TABLE = posting.ACCOUNT_TABLE
HOLDING_TABLE = posting.HOLDING_TABLE

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG_PATTERN = re.compile(r"#G[0-9a-fA-F]{8}")


class ConfirmationRequired(RuntimeError):
    """多明细组删除缺少 --yes；此时没有写入任何数据。"""


def _table(cfg: dict, table_name: str) -> Tuple[str, str]:
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


def _entries(cfg: dict) -> List[dict]:
    """读取全部明细（个人账本量级，2000 行足够）。"""
    app_token, table_id = _table(cfg, ENTRY_TABLE)
    records = lark.search_records(app_token, table_id, limit=2000)
    return [
        record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("fields"), dict)
    ]


def _snapshot(cfg: dict) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """账户/持仓快照：record_id → fields（只读当前值用于展示差异）。"""
    accounts: Dict[str, dict] = {}
    for record in master_data.account_list(cfg):
        record_id = record.get("record_id")
        if isinstance(record_id, str) and isinstance(record.get("fields"), dict):
            accounts[record_id] = record["fields"]
    holdings: Dict[str, dict] = {}
    for record in master_data.holding_list(cfg):
        record_id = record.get("record_id")
        if isinstance(record_id, str) and isinstance(record.get("fields"), dict):
            holdings[record_id] = record["fields"]
    return accounts, holdings


def _find(entries: List[dict], record_id: str) -> dict:
    for row in entries:
        if row.get("record_id") == record_id:
            return row
    raise posting.PostingError(
        "找不到明细「%s」：请确认 record_id 是否正确（可先到飞书明细表复制）"
        % record_id
    )


def _number(fields: Any, name: str) -> Optional[float]:
    """读取数值字段；缺失/非数字返回 None（由调用方决定如何报告）。"""
    if not isinstance(fields, dict):
        return None
    value = fields.get(name)
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _same(left: float, right: float, digits: int) -> bool:
    return abs(round(left, digits) - round(right, digits)) < 1e-9


def _cell_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """纯逻辑/行字段 → 飞书 CellValue：日期补 " 00:00"。"""
    converted = dict(fields)
    for name, value in converted.items():
        if (
            name == "日期"
            and isinstance(value, str)
            and _DATE_PATTERN.fullmatch(value)
        ):
            converted[name] = value + " 00:00"
    return converted


def _account_label(account_id: str, accounts: Dict[str, dict]) -> str:
    fields = accounts.get(account_id)
    name = fields.get("账户名称") if isinstance(fields, dict) else None
    if isinstance(name, str) and name:
        return "账户「%s」（%s）" % (name, account_id)
    return "账户「%s」" % account_id


def _holding_label(holding_id: str, holdings: Dict[str, dict]) -> str:
    fields = holdings.get(holding_id)
    name = fields.get("持仓名称") if isinstance(fields, dict) else None
    if isinstance(name, str) and name:
        return "持仓「%s」（%s）" % (name, holding_id)
    return "持仓「%s」" % holding_id


def _recompute(
    cfg: dict,
    account_ids,
    holding_ids,
    entries: Optional[List[dict]] = None,
) -> Tuple[List[Tuple[str, str, Dict[str, Any]]], List[str]]:
    """用 replay 的绝对值生成受影响对象的更新列表。

    只更新传入 id 的对象；其它账户/持仓的漂移交给 rebuild 处理。
    """
    rows = _entries(cfg) if entries is None else entries
    accounts, holdings = _snapshot(cfg)
    replayed = posting.replay(rows, accounts, holdings)

    updates: List[Tuple[str, str, Dict[str, Any]]] = []
    for account_id in sorted(set(account_ids)):
        if account_id not in accounts or account_id not in replayed["accounts"]:
            continue
        expected = replayed["accounts"][account_id]
        current = _number(accounts[account_id], "当前余额")
        if current is None or not _same(current, expected, 2):
            updates.append(
                (ACCOUNT_TABLE, account_id, {"当前余额": expected})
            )

    for holding_id in sorted(set(holding_ids)):
        if holding_id not in holdings or holding_id not in replayed["holdings"]:
            continue
        state = replayed["holdings"][holding_id]
        current_shares = _number(holdings[holding_id], "当前份额")
        current_cost = _number(holdings[holding_id], "成本价")
        fields: Dict[str, Any] = {}
        if current_shares is None or not _same(
            current_shares, state["shares"], 4
        ):
            fields["当前份额"] = state["shares"]
        if current_cost is None or not _same(current_cost, state["cost"], 4):
            fields["成本价"] = state["cost"]
        if fields:
            updates.append((HOLDING_TABLE, holding_id, fields))

    return updates, list(replayed["diagnostics"])


def _write_updates(
    cfg: dict, updates: List[Tuple[str, str, Dict[str, Any]]]
) -> int:
    """按表批量写入账户/持仓；失败时把已完成步骤与未完成步骤交给调用方。"""
    if not updates:
        return 0
    grouped: Dict[str, Dict[str, dict]] = {}
    for table_name, record_id, fields in updates:
        grouped.setdefault(table_name, {})[record_id] = fields

    order = [ACCOUNT_TABLE, HOLDING_TABLE]
    order.extend(name for name in grouped if name not in order)
    completed: List[str] = []
    for table_name in order:
        if table_name not in grouped:
            continue
        app_token, table_id = _table(cfg, table_name)
        batch = grouped[table_name]
        try:
            lark.update_records(app_token, table_id, batch)
        except lark.LarkError as exc:
            raise add_flow.PartialWriteError(
                completed=completed,
                unfinished=[
                    "更新「%s」%s" % (table_name, record_id)
                    for record_id in batch
                ],
                cause=exc,
                failed_step="更新「%s」" % table_name,
            ) from exc
        completed.extend(
            "更新「%s」%s" % (table_name, record_id) for record_id in batch
        )
    return sum(len(batch) for batch in grouped.values())


def _require_record_id(record_id: Any) -> str:
    if not isinstance(record_id, str) or not record_id.strip():
        raise posting.PostingError("明细 record_id 不能为空")
    return record_id.strip()


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def _positive_amount(value: Any) -> float:
    if isinstance(value, bool):
        raise posting.PostingError("金额必须是数字，实际是布尔值")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise posting.PostingError("金额必须是数字，实际是 %r" % (value,))
    if not math.isfinite(amount) or amount <= 0:
        raise posting.PostingError(
            "金额必须大于 0，实际是 %r；请检查要改成的金额" % (value,)
        )
    return round(amount, 2)


def _valid_date(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _DATE_PATTERN.fullmatch(text):
        raise posting.PostingError(
            "日期必须是 YYYY-MM-DD 格式的字符串，实际是 %r；例如 2026-11-17"
            % (value,)
        )
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise posting.PostingError(
            "日期 %r 不存在；请检查月份和日期（例如 2026-02-30 不合法）"
            % (value,)
        )
    return text


def _category_change(
    cfg: dict, fields: Dict[str, Any], category: Any
) -> List[dict]:
    """把 --category 解析成关联单元格，并校验方向与明细类型一致。"""
    kind = posting.row_type(fields)
    if kind == "转账":
        raise posting.PostingError(
            "转账明细不需要分类（分类只用于收入/支出）；请确认要改的明细"
        )
    if kind not in ("收入", "支出"):
        raise posting.PostingError(
            "明细类型不是收入/支出，无法设置分类；请先修正明细「类型」"
        )
    expected = "income" if kind == "收入" else "expense"
    actual = add_flow.category_kind(cfg, category)
    if actual is None:
        raise posting.PostingError(
            "分类「%s」的「类型」缺失或不可用，无法确认方向；"
            "请在分类表把类型设为 支出/收入" % (category,)
        )
    if actual != expected:
        raise posting.PostingError(
            "分类「%s」不能用于%s明细；请改用「%s」类型的分类"
            % (
                category,
                kind,
                "收入" if kind == "收入" else "支出",
            )
        )
    return [{"id": add_flow.resolve_category(cfg, category)}]


def _note_with_tag(note: str, tag: Optional[str]) -> str:
    """改备注时保留组 tag，避免把多明细组拆散。"""
    cleaned = _TAG_PATTERN.sub("", note).strip()
    if tag is None:
        return cleaned
    return ("%s %s" % (cleaned, tag)).strip()


def run_edit(
    cfg: dict,
    record_id: str,
    amount: Any = None,
    date: Any = None,
    account: Any = None,
    category: Any = None,
    note: Any = None,
) -> dict:
    """改一条明细，然后按全量 replay 修正该组影响的账户/持仓。"""
    record_id = _require_record_id(record_id)
    entries = _entries(cfg)
    row = _find(entries, record_id)
    fields = row.get("fields") or {}

    changes: Dict[str, Any] = {}
    if amount is not None:
        changes["金额"] = _positive_amount(amount)
    if date is not None:
        changes["日期"] = _valid_date(date)
    if account is not None:
        if posting.row_type(fields) == "转账" and fields.get("持仓"):
            raise posting.PostingError(
                "理财买卖的份额行不能直接改账户：请改同组的银行卡↔理财转账行，"
                "或先 delete 整组后重新记账"
            )
        resolved = master_data.resolve_account(cfg, account)
        changes["账户"] = [{"id": resolved["record_id"]}]
    if category is not None:
        changes["分类"] = _category_change(cfg, fields, category)
    if note is not None:
        if not isinstance(note, str):
            raise posting.PostingError("备注必须是文本，实际是 %r" % (note,))
        tag = posting.note_tag(posting.row_note(fields))
        changes["备注"] = _note_with_tag(note, tag)
    if not changes:
        raise posting.PostingError(
            "edit 至少指定一个要修改的字段"
            "（--amount/--date/--account/--category/--note）"
        )

    affected_before = posting.group_object_ids(
        posting.group_rows(entries, row)
    )
    app_token, table_id = _table(cfg, ENTRY_TABLE)
    lark.update_records(app_token, table_id, {record_id: _cell_fields(changes)})

    new_entries = _entries(cfg)
    new_row = _find(new_entries, record_id)
    affected_after = posting.group_object_ids(
        posting.group_rows(new_entries, new_row)
    )
    updates, diagnostics = _recompute(
        cfg,
        affected_before["accounts"] | affected_after["accounts"],
        affected_before["holdings"] | affected_after["holdings"],
        entries=new_entries,
    )
    try:
        updated = _write_updates(cfg, updates)
    except add_flow.PartialWriteError as exc:
        # 明细已经改好了：把它记进已完成，避免报告成“什么都没发生”。
        raise add_flow.PartialWriteError(
            completed=["更新明细 %s" % record_id] + exc.completed,
            unfinished=exc.unfinished,
            cause=exc.cause,
            failed_step=exc.failed_step,
        ) from exc
    return {
        "record_id": record_id,
        "changed": sorted(changes),
        "updated": updated,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def _print_delete_targets(group: List[dict]) -> None:
    """删除前逐条打印目标，让用户知道整组会连带删掉哪些行。"""
    tag = None
    for row in group:
        tag = posting.row_tag(row)
        if tag:
            break
    title = "将删除 %d 条明细" % len(group)
    if tag:
        title += "（组 %s）" % tag
    print(title + "：")
    for index, row in enumerate(group, 1):
        fields = row.get("fields") or {}
        date = posting.row_date(fields) or "?"
        kind = posting.row_type(fields) or "?"
        amount = _number(fields, "金额")
        amount_text = "?" if amount is None else "%.2f" % amount
        summary = fields.get("摘要") or ""
        note = posting.row_note(fields)
        print(
            "  %d. %s  %s  %s %s  摘要：%s  备注：%s"
            % (
                index,
                row.get("record_id", "?"),
                date,
                kind,
                amount_text,
                summary,
                note,
            )
        )


def run_delete(cfg: dict, record_id: str, yes: bool = False) -> dict:
    """按 #G tag 自动扩组删除，再用 replay 修正受影响的账户/持仓。"""
    record_id = _require_record_id(record_id)
    entries = _entries(cfg)
    row = _find(entries, record_id)
    group = posting.sort_rows(posting.group_rows(entries, row))
    _print_delete_targets(group)

    if len(group) > 1 and not yes:
        raise ConfirmationRequired(
            "同组 %d 条明细需要 --yes 确认后才会删除；未做任何修改"
            % len(group)
        )

    record_ids = [
        item.get("record_id")
        for item in group
        if isinstance(item.get("record_id"), str) and item.get("record_id")
    ]
    if not record_ids or len(record_ids) != len(group):
        # 整组删除必须一次删全：任何一行缺 record_id 都不允许删一半。
        raise posting.PostingError(
            "要删除的明细缺少 record_id，已中止（未删除任何数据）"
        )

    affected = posting.group_object_ids(group)
    app_token, table_id = _table(cfg, ENTRY_TABLE)
    lark.delete_records(app_token, table_id, record_ids)

    updates, diagnostics = _recompute(
        cfg, affected["accounts"], affected["holdings"]
    )
    try:
        updated = _write_updates(cfg, updates)
    except add_flow.PartialWriteError as exc:
        # 明细已经删掉了：把删除步骤记进已完成，避免报告成“什么都没发生”。
        raise add_flow.PartialWriteError(
            completed=["删除明细 %s" % "、".join(record_ids)] + exc.completed,
            unfinished=exc.unfinished,
            cause=exc.cause,
            failed_step=exc.failed_step,
        ) from exc
    return {
        "deleted": record_ids,
        "updated": updated,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


def run_rebuild(cfg: dict, apply: bool = False) -> dict:
    """重建全部账户余额与持仓份额/成本，返回差异；apply=False 只读。"""
    entries = _entries(cfg)
    accounts, holdings = _snapshot(cfg)
    replayed = posting.replay(entries, accounts, holdings)

    diff: List[dict] = []
    updates: List[Tuple[str, str, Dict[str, Any]]] = []

    for account_id in sorted(replayed["accounts"]):
        if account_id not in accounts:
            continue
        expected = replayed["accounts"][account_id]
        current = _number(accounts[account_id], "当前余额")
        if current is not None and _same(current, expected, 2):
            continue
        diff.append(
            {
                "对象": _account_label(account_id, accounts),
                "字段": "当前余额",
                "现值": current,
                "应为": expected,
            }
        )
        updates.append((ACCOUNT_TABLE, account_id, {"当前余额": expected}))

    for holding_id in sorted(replayed["holdings"]):
        if holding_id not in holdings:
            continue
        state = replayed["holdings"][holding_id]
        current_shares = _number(holdings[holding_id], "当前份额")
        current_cost = _number(holdings[holding_id], "成本价")
        shares_differ = current_shares is None or not _same(
            current_shares, state["shares"], 4
        )
        cost_differ = current_cost is None or not _same(
            current_cost, state["cost"], 4
        )
        if shares_differ:
            diff.append(
                {
                    "对象": _holding_label(holding_id, holdings),
                    "字段": "当前份额",
                    "现值": current_shares,
                    "应为": state["shares"],
                }
            )
        if cost_differ:
            diff.append(
                {
                    "对象": _holding_label(holding_id, holdings),
                    "字段": "成本价",
                    "现值": current_cost,
                    "应为": state["cost"],
                }
            )
        if shares_differ or cost_differ:
            # 份额与成本价一起写：两者共同构成持仓的重建结果，避免只改一半。
            updates.append(
                (
                    HOLDING_TABLE,
                    holding_id,
                    {
                        "当前份额": state["shares"],
                        "成本价": state["cost"],
                    },
                )
            )

    applied = False
    updated = 0
    if apply and updates:
        updated = _write_updates(cfg, updates)
        applied = True

    return {
        "diff": diff,
        "applied": applied,
        "updated": updated,
        "diagnostics": list(replayed["diagnostics"]),
    }