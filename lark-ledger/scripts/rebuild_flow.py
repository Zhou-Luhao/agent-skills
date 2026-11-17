"""修正与重算流程：edit 改一行、delete 整组删除、rebuild 全量重放。

一致性基准（本模块最重要的约定）：
- 账户/持仓的「应为值」一律来自 `posting.replay`：从 初始余额 /
  期初份额 / 期初成本 起，按 (日期, 创建时间, record_id) 逐笔重放全部明细；
- 因此 edit / delete 对受影响对象的修正结果，与一次 from-scratch rebuild
  完全相同；当前余额、当前份额只用于展示差异，绝不作为计算输入；
- 重放不可信（tainted）时**绝不写回**：edit / delete 在写任何数据之前先对
  「投影后的明细」重放，含 diagnose 的组直接中止；rebuild --apply 同样拒绝。
  dry-run 只打印诊断并退出 0；
- 绝不写公式字段（市值 / 浮动盈亏）。持仓「状态」由重放后的份额派生
  （持有 / 已清仓），会随 rebuild、edit、delete 一起修正。

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
# lark.search_records 的 limit 是「总上限」，必须按页循环把整表读尽。
PAGE_SIZE = lark.INLINE_PAGE_LIMIT
# 防御：即使账本异常膨胀也不做无界扫描（超过即拒绝写入）。
MAX_ENTRIES = 100000


class ConfirmationRequired(RuntimeError):
    """多明细组删除缺少 --yes；此时没有写入任何数据。"""


class TaintedReplay(RuntimeError):
    """重放无法完整解释账本：调用方必须先修数据，绝不写回。"""

    def __init__(self, diagnostics) -> None:
        self.diagnostics = [str(message) for message in (diagnostics or [])]
        detail = "；".join(self.diagnostics) or "未知原因"
        super().__init__("重放不可信：%s" % detail)


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
    """分页读尽全部明细（search_records 的 limit 是总上限，不是页大小）。

    fields 不是 dict 的异常记录不会被丢掉：交给 replay 生成诊断并置
    tainted，让所有写回操作在修数据前中止。
    """
    app_token, table_id = _table(cfg, ENTRY_TABLE)
    records: List[dict] = []
    offset = 0
    while True:
        page = lark.search_records(
            app_token, table_id, limit=PAGE_SIZE, offset=offset
        )
        records.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
        if len(records) > MAX_ENTRIES:
            raise TaintedReplay(
                [
                    "明细行数超过 %d 行上限，已中止（未写入任何数据）"
                    % MAX_ENTRIES
                ]
            )
    return [record for record in records if isinstance(record, dict)]


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


def _choice_text(fields: Any, name: str) -> str:
    """单选/文本单元格 → 文本（状态等字段）。"""
    if not isinstance(fields, dict):
        return ""
    value = fields.get(name)
    if isinstance(value, str):
        return value.strip()
    items = value if isinstance(value, (list, tuple)) else []
    for item in items:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            for key in ("text", "name", "id"):
                raw = item.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
    return ""


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


def _updates_from_replay(
    replayed: dict,
    accounts: Dict[str, dict],
    holdings: Dict[str, dict],
    account_ids,
    holding_ids,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """把 replay 的绝对值转成受影响账户/持仓的更新列表。

    只更新传入 id 的对象；其它账户/持仓的漂移交给 rebuild 处理。
    持仓同时写 当前份额 / 成本价 / 状态，三者在同一次更新里保持一致。
    """
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
        current_status = _choice_text(holdings[holding_id], "状态")
        shares_differ = current_shares is None or not _same(
            current_shares, state["shares"], 4
        )
        cost_differ = current_cost is None or not _same(
            current_cost, state["cost"], 4
        )
        status_differ = current_status != state["status"]
        if shares_differ or cost_differ or status_differ:
            updates.append(
                (
                    HOLDING_TABLE,
                    holding_id,
                    {
                        "当前份额": state["shares"],
                        "成本价": state["cost"],
                        "状态": [state["status"]],
                    },
                )
            )
    return updates


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


def _project_edit(
    entries: List[dict], record_id: str, changes: Dict[str, Any]
) -> List[dict]:
    """在内存里投影「改完这一行之后」的明细，用于写库前的安全重放。"""
    projected: List[dict] = []
    for row in entries:
        if row.get("record_id") != record_id:
            projected.append(row)
            continue
        new_row = dict(row)
        fields = dict(row.get("fields") or {})
        fields.update(changes)
        new_row["fields"] = fields
        projected.append(new_row)
    return projected


def run_edit(
    cfg: dict,
    record_id: str,
    amount: Any = None,
    date: Any = None,
    account: Any = None,
    category: Any = None,
    note: Any = None,
) -> dict:
    """改一条明细，然后按全量 replay 修正该组影响的账户/持仓。

    先对「投影后的明细」重放；tainted 时抛 TaintedReplay，明细行也还没写，
    保证「重放不可信 ⇒ 什么都没写」。
    """
    record_id = _require_record_id(record_id)
    entries = _entries(cfg)
    row = _find(entries, record_id)
    source_fields = row.get("fields")
    if not isinstance(source_fields, dict):
        raise posting.PostingError(
            "明细「%s」的 fields 不是对象，无法编辑；请先到飞书表格修复该行"
            % record_id
        )
    fields = source_fields

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

    projected = _project_edit(entries, record_id, changes)
    accounts, holdings = _snapshot(cfg)
    replayed = posting.replay(projected, accounts, holdings)
    if replayed["tainted"]:
        raise TaintedReplay(replayed["diagnostics"])

    new_row = _find(projected, record_id)
    affected_before = posting.group_object_ids(
        posting.group_rows(entries, row)
    )
    affected_after = posting.group_object_ids(
        posting.group_rows(projected, new_row)
    )
    updates = _updates_from_replay(
        replayed,
        accounts,
        holdings,
        affected_before["accounts"] | affected_after["accounts"],
        affected_before["holdings"] | affected_after["holdings"],
    )

    app_token, table_id = _table(cfg, ENTRY_TABLE)
    lark.update_records(app_token, table_id, {record_id: _cell_fields(changes)})
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
        "diagnostics": list(replayed["diagnostics"]),
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
        fields = row.get("fields")
        if not isinstance(fields, dict):
            fields = {}
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


def _project_delete(
    entries: List[dict], record_ids: List[str]
) -> List[dict]:
    """内存里投影「删完这一组之后」的明细。"""
    removed = set(record_ids)
    return [row for row in entries if row.get("record_id") not in removed]


def run_delete(cfg: dict, record_id: str, yes: bool = False) -> dict:
    """按 #G tag 自动扩组删除，再用 replay 修正受影响的账户/持仓。

    删除前先对「投影删除后的明细」重放；tainted 时抛 TaintedReplay，
    一行都不删，保证「重放不可信 ⇒ 什么都没写」。
    """
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
    accounts, holdings = _snapshot(cfg)
    replayed = posting.replay(
        _project_delete(entries, record_ids), accounts, holdings
    )
    if replayed["tainted"]:
        raise TaintedReplay(replayed["diagnostics"])
    updates = _updates_from_replay(
        replayed,
        accounts,
        holdings,
        affected["accounts"],
        affected["holdings"],
    )

    app_token, table_id = _table(cfg, ENTRY_TABLE)
    lark.delete_records(app_token, table_id, record_ids)
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
        "diagnostics": list(replayed["diagnostics"]),
    }


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------


def run_rebuild(cfg: dict, apply: bool = False) -> dict:
    """重建全部账户余额与持仓，返回差异；apply=False 只读。

    tainted 的 replay（有诊断）在 apply=True 时抛 TaintedReplay，一行不写；
    dry-run 照常返回 diff/diagnostics 供展示。
    """
    entries = _entries(cfg)
    accounts, holdings = _snapshot(cfg)
    replayed = posting.replay(entries, accounts, holdings)
    diagnostics = list(replayed["diagnostics"])
    tainted = bool(replayed["tainted"])

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
        current_status = _choice_text(holdings[holding_id], "状态")
        shares_differ = current_shares is None or not _same(
            current_shares, state["shares"], 4
        )
        cost_differ = current_cost is None or not _same(
            current_cost, state["cost"], 4
        )
        status_differ = current_status != state["status"]
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
        if status_differ:
            diff.append(
                {
                    "对象": _holding_label(holding_id, holdings),
                    "字段": "状态",
                    "现值": current_status or None,
                    "应为": state["status"],
                }
            )
        if shares_differ or cost_differ or status_differ:
            # 份额/成本/状态一起写：三者共同构成持仓的重建结果。
            updates.append(
                (
                    HOLDING_TABLE,
                    holding_id,
                    {
                        "当前份额": state["shares"],
                        "成本价": state["cost"],
                        "状态": [state["status"]],
                    },
                )
            )

    if apply and tainted:
        raise TaintedReplay(diagnostics)

    applied = False
    updated = 0
    if apply and updates:
        updated = _write_updates(cfg, updates)
        applied = True

    return {
        "diff": diff,
        "applied": applied,
        "updated": updated,
        "tainted": tainted,
        "diagnostics": diagnostics,
        "tainted_groups": list(replayed["tainted_groups"]),
    }