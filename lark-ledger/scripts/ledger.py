"""lark-ledger 命令行入口。

退出码约定：0 成功 / 2 参数错误 / 3 未配置或未登录 / 4 lark-cli 调用失败 / 5 数据错误。
已实现 init（建账本）、add（记一笔）、edit / delete / rebuild（修正与重算）
与 account / holding 主数据子命令。
"""

import argparse
import sys
import unicodedata
from typing import Any, List, Optional

import add_flow
import config
import init_flow
import lark
import master_data
import posting
import rebuild_flow

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_READY = 3
EXIT_LARK = 4
EXIT_DATA = 5

ACCOUNT_COLUMNS = (
    "账户名称",
    "类型",
    "初始余额",
    "当前余额",
    "年利率",
    "还款方式",
    "状态",
    "备注",
)
HOLDING_COLUMNS = (
    "持仓名称",
    "代码",
    "品种",
    "理财账户",
    "当前份额",
    "成本价",
    "最新价",
    "最新价日期",
    "市值",
    "浮动盈亏",
    "状态",
    "期初份额",
    "期初成本",
)


def _add_account_parsers(subparsers) -> None:
    account_parser = subparsers.add_parser(
        "account", help="账户主数据：新增、修改、列表、销户"
    )
    actions = account_parser.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="新增账户")
    add.add_argument("--name", required=True, help="账户名称")
    add.add_argument(
        "--type",
        dest="type_",
        required=True,
        choices=master_data.ACCOUNT_TYPES,
        help="账户类型：%(choices)s",
    )
    add.add_argument(
        "--opening",
        required=True,
        type=float,
        help="初始余额（负债填负数，如房贷 -500000）",
    )
    add.add_argument(
        "--rate",
        type=float,
        default=None,
        help="年利率（百分数，如 4.2 表示 4.2%%；仅负债用）",
    )
    add.add_argument(
        "--repay",
        choices=master_data.REPAY_METHODS,
        default=None,
        help="还款方式（仅负债用）：%(choices)s",
    )

    edit = actions.add_parser("edit", help="修改账户字段")
    edit.add_argument("ref", help="账户名称或 record_id")
    edit.add_argument("--name", dest="name", default=None, help="账户名称")
    edit.add_argument(
        "--type",
        dest="type_",
        choices=master_data.ACCOUNT_TYPES,
        default=None,
        help="账户类型：%(choices)s",
    )
    edit.add_argument(
        "--opening", type=float, default=None, help="初始余额（改后建议 rebuild）"
    )
    edit.add_argument(
        "--rate",
        type=float,
        default=None,
        help="年利率（百分数，如 4.2 表示 4.2%%）",
    )
    edit.add_argument(
        "--repay", choices=master_data.REPAY_METHODS, default=None, help="还款方式"
    )
    edit.add_argument(
        "--status",
        choices=master_data.ACCOUNT_STATUSES,
        default=None,
        help="状态：%(choices)s",
    )
    edit.add_argument("--note", default=None, help="备注")

    actions.add_parser("list", help="列出全部账户")
    close = actions.add_parser("close", help="销户（状态改为已销户）")
    close.add_argument("ref", help="账户名称或 record_id")


def _add_holding_parsers(subparsers) -> None:
    holding_parser = subparsers.add_parser(
        "holding", help="持仓主数据：新增、修改、列表"
    )
    actions = holding_parser.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="新增持仓")
    add.add_argument("--name", required=True, help="持仓名称")
    add.add_argument("--code", required=True, help="代码，如 sh600519 / 110011")
    add.add_argument(
        "--kind",
        required=True,
        choices=master_data.HOLDING_KINDS,
        help="品种：%(choices)s",
    )
    add.add_argument("--account", required=True, help="所属理财账户名称或 record_id")
    add.add_argument("--shares", required=True, type=float, help="当前份额")
    add.add_argument("--cost", required=True, type=float, help="成本价")

    edit = actions.add_parser("edit", help="修改持仓字段")
    edit.add_argument("ref", help="持仓名称、代码或 record_id")
    edit.add_argument("--name", default=None, help="持仓名称")
    edit.add_argument("--code", default=None, help="代码")
    edit.add_argument(
        "--kind",
        choices=master_data.HOLDING_KINDS,
        default=None,
        help="品种：%(choices)s",
    )
    edit.add_argument("--account", default=None, help="所属理财账户名称或 record_id")
    edit.add_argument(
        "--shares", type=float, default=None, help="当前份额（只改当前值）"
    )
    edit.add_argument(
        "--cost", type=float, default=None, help="成本价（只改当前值）"
    )
    edit.add_argument(
        "--opening-shares", type=float, default=None, help="只改期初份额"
    )
    edit.add_argument(
        "--opening-cost", type=float, default=None, help="只改期初成本"
    )
    edit.add_argument(
        "--status",
        choices=master_data.HOLDING_STATUSES,
        default=None,
        help="状态：%(choices)s",
    )

    actions.add_parser("list", help="列出全部持仓")


def _add_add_parser(subparsers) -> None:
    add = subparsers.add_parser(
        "add",
        help=(
            "记一笔收入/支出/转账/理财买卖/分红/贷款还款"
            "（明细 + 账户余额/持仓联动）"
        ),
    )
    add.add_argument(
        "--kind",
        choices=("auto",) + posting.SUPPORTED_KINDS,
        default="auto",
        help=(
            "记账类型（默认 auto：有 --to-account 即转账；否则按 --category"
            " 在分类表的「类型」判断收支，类型缺失时必须显式指定）"
        ),
    )
    add.add_argument(
        "--date",
        default=None,
        help="日期 YYYY-MM-DD（默认：今天，UTC+8）",
    )
    add.add_argument(
        "--amount",
        default=None,
        type=float,
        help=(
            "金额必须大于 0（方向由 --kind 或分类类型决定，不看金额符号）；"
            "收入/支出/分红/贷款还款必填。理财买入可用 --amount 按 --price 换算份额，"
            "脚本会按 价格×份额 重算转账金额；不要与 --shares 同用"
        ),
    )
    add.add_argument("--account", required=True, help="账户名称或 record_id")
    add.add_argument(
        "--to-account", default=None, help="转入账户（转账/理财卖出）"
    )
    add.add_argument(
        "--loan-account",
        dest="loan_account",
        default=None,
        help="负债账户名称或 record_id（贷款还款必填）",
    )
    add.add_argument(
        "--category",
        default=None,
        help=(
            "分类全名，如 支出·餐饮·午餐（收入/支出必填；"
            "理财卖出可选，缺省按品种路由到「收入·理财收益」下；"
            "分红缺省为 收入·理财收益·分红；"
            "贷款还款缺省为 支出·金融·利息支出）"
        ),
    )
    add.add_argument(
        "--holding", default=None, help="持仓代码/名称/record_id（理财买卖必填）"
    )
    add.add_argument(
        "--price", type=float, default=None, help="成交价/净值（理财买卖必填）"
    )
    add.add_argument(
        "--shares",
        type=float,
        default=None,
        help="成交份额（理财买卖必填；买入也可用 --amount ÷ --price 换算）",
    )
    add.add_argument(
        "--from-account", default=None, help="转出账户（理财买入必填）"
    )
    add.add_argument("--summary", default=None, help="摘要，如 午饭")
    add.add_argument("--note", default=None, help="备注")


def _add_edit_parser(subparsers) -> None:
    edit = subparsers.add_parser(
        "edit", help="修改一条明细并联动重算受影响账户/持仓"
    )
    edit.add_argument("record_id", help="明细 record_id")
    edit.add_argument(
        "--amount", type=float, default=None, help="新金额（必须大于 0）"
    )
    edit.add_argument("--date", default=None, help="新日期 YYYY-MM-DD")
    edit.add_argument("--account", default=None, help="新账户名称或 record_id")
    edit.add_argument(
        "--category", default=None, help="新分类全名（仅收入/支出明细）"
    )
    edit.add_argument(
        "--note",
        default=None,
        help="新备注；同组 #G tag 会自动保留，不会拆散多明细组",
    )


def _add_delete_parser(subparsers) -> None:
    delete = subparsers.add_parser(
        "delete", help="删除一条明细（同 #G 组自动整组删除并反向联动）"
    )
    delete.add_argument("record_id", help="明细 record_id")
    delete.add_argument(
        "--yes",
        action="store_true",
        help="整组删除（同一 #G 组超过 1 行）时必须显式确认",
    )


def _add_rebuild_parser(subparsers) -> None:
    rebuild = subparsers.add_parser(
        "rebuild",
        help="从期初 + 全部明细重算账户余额与持仓（默认 dry-run）",
    )
    rebuild.add_argument(
        "--apply",
        action="store_true",
        help="写入修正值；缺省只打印差异，不写任何数据",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger.py",
        description="飞书多维表格个人记账",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="创建账本 Base 并写入预置分类（幂等）"
    )
    init_parser.add_argument(
        "--folder-token",
        default=None,
        help="云盘文件夹 token；缺省创建到“我的文档库”",
    )
    init_parser.add_argument(
        "--base-name",
        default=init_flow.DEFAULT_BASE_NAME,
        help="Base 名称（默认：%(default)s）",
    )

    _add_add_parser(subparsers)
    _add_edit_parser(subparsers)
    _add_delete_parser(subparsers)
    _add_rebuild_parser(subparsers)
    _add_account_parsers(subparsers)
    _add_holding_parsers(subparsers)
    return parser


def _run_init(args: argparse.Namespace) -> int:
    cfg = init_flow.run_init(args.folder_token, args.base_name)
    print(
        "初始化完成：%s，%d 张表，预置分类已写入。"
        % (args.base_name, len(cfg.get("tables", {})))
    )
    print("配置文件：%s" % config.CONFIG_PATH)
    return EXIT_OK


def _usage_error(message: str) -> int:
    print("参数错误：%s" % message, file=sys.stderr)
    return EXIT_USAGE


_AUTO_AMBIGUOUS = "方向不明确，请用 --kind income|expense 指定"

# kind → 不允许出现的开关。宁可报错，也不静默丢弃用户给的交易参数。
_FORBIDDEN_ADD_FLAGS = {
    "transfer": (
        "holding",
        "price",
        "shares",
        "from_account",
        "loan_account",
        "category",
    ),
    "income": (
        "to_account",
        "from_account",
        "loan_account",
        "holding",
        "price",
        "shares",
    ),
    "expense": (
        "to_account",
        "from_account",
        "loan_account",
        "holding",
        "price",
        "shares",
    ),
    "invest_buy": ("to_account", "loan_account", "category"),
    "invest_sell": ("amount", "from_account", "loan_account"),
    "dividend": (
        "to_account",
        "from_account",
        "loan_account",
        "holding",
        "price",
        "shares",
    ),
    "loan_repay": ("to_account", "from_account", "holding", "price", "shares"),
}
_ADD_FLAG_LABELS = {
    "amount": "--amount",
    "to_account": "--to-account",
    "loan_account": "--loan-account",
    "category": "--category",
    "holding": "--holding",
    "price": "--price",
    "shares": "--shares",
    "from_account": "--from-account",
}


def _reject_forbidden_add_flags(
    args: argparse.Namespace, kind: str
) -> Optional[int]:
    for attribute in _FORBIDDEN_ADD_FLAGS.get(kind, ()):
        if getattr(args, attribute) is not None:
            return _usage_error(
                "%s不需要 %s；请去掉该参数，或确认记账类型"
                % (
                    posting.KIND_LABELS[kind],
                    _ADD_FLAG_LABELS[attribute],
                )
            )
    return None


def _run_add(args: argparse.Namespace) -> int:
    cfg = None
    if args.kind == "auto":
        # auto 只看 --to-account 与分类表的「类型」，绝不用金额正负猜方向。
        if args.to_account is not None:
            kind = "transfer"
        elif args.category is None:
            return _usage_error(
                _AUTO_AMBIGUOUS
                + "（--kind auto 需要 --category 才能按分类类型判断方向）"
            )
        else:
            cfg = config.load_config()
            kind = add_flow.category_kind(cfg, args.category)
            if kind is None:
                return _usage_error(
                    _AUTO_AMBIGUOUS
                    + "（分类「%s」的「类型」缺失或不可用）" % args.category
                )
    else:
        kind = args.kind

    rejected = _reject_forbidden_add_flags(args, kind)
    if rejected is not None:
        return rejected

    if kind == "transfer":
        if args.amount is None:
            return _usage_error("转账必须指定 --amount（金额必须大于 0）")
        if args.to_account is None:
            return _usage_error("转账必须指定 --to-account（转入账户）")
    elif kind in ("income", "expense"):
        if args.amount is None:
            return _usage_error(
                "%s必须指定 --amount（金额必须大于 0）"
                % posting.KIND_LABELS[kind]
            )
        if args.category is None:
            return _usage_error(
                "%s必须指定 --category，例如 支出·餐饮·午餐"
                % posting.KIND_LABELS[kind]
            )
    elif kind == "invest_buy":
        if args.holding is None:
            return _usage_error("理财买入必须指定 --holding（持仓代码/名称）")
        if args.price is None:
            return _usage_error("理财买入必须指定 --price（成交价/净值）")
        if args.from_account is None:
            return _usage_error("理财买入必须指定 --from-account（转出账户）")
        if args.amount is not None and args.shares is not None:
            return _usage_error(
                "理财买入不能同时指定 --amount 与 --shares：请二选一；"
                "转账金额由脚本按 价格×份额 重算"
            )
        if args.shares is None and args.amount is None:
            return _usage_error(
                "理财买入必须指定 --shares（份额）或用 --amount 按净值换算"
            )
    elif kind == "invest_sell":
        if args.holding is None:
            return _usage_error("理财卖出必须指定 --holding（持仓代码/名称）")
        if args.price is None:
            return _usage_error("理财卖出必须指定 --price（成交价/净值）")
        if args.shares is None:
            return _usage_error("理财卖出必须指定 --shares（卖出份额）")
        if args.to_account is None:
            return _usage_error("理财卖出必须指定 --to-account（转入账户）")
    elif kind == "dividend":
        if args.amount is None:
            return _usage_error("理财分红必须指定 --amount（分红金额）")
    elif kind == "loan_repay":
        if args.amount is None:
            return _usage_error("贷款还款必须指定 --amount（还款总额）")
        if args.loan_account is None:
            return _usage_error(
                "贷款还款必须指定 --loan-account（负债账户名称或 record_id）"
            )

    entry = {
        "kind": kind,
        "date": args.date or add_flow.today_utc8(),
        "account": args.account,
        "summary": args.summary or "",
        "note": args.note or "",
    }
    if kind == "transfer":
        entry["amount"] = args.amount
        entry["to_account"] = args.to_account
    elif kind in ("income", "expense"):
        entry["amount"] = args.amount
        entry["category"] = args.category
    elif kind == "invest_buy":
        entry["holding"] = args.holding
        entry["price"] = args.price
        entry["from_account"] = args.from_account
        if args.shares is not None:
            entry["shares"] = args.shares
        if args.amount is not None:
            entry["amount"] = args.amount
    elif kind == "invest_sell":
        entry["holding"] = args.holding
        entry["price"] = args.price
        entry["shares"] = args.shares
        entry["to_account"] = args.to_account
        if args.category is not None:
            entry["category"] = args.category
    elif kind == "dividend":
        entry["amount"] = args.amount
        entry["category"] = args.category or "收入·理财收益·分红"
    elif kind == "loan_repay":
        entry["amount"] = args.amount
        entry["loan_account"] = args.loan_account
        entry["category"] = (
            args.category or add_flow.DEFAULT_INTEREST_CATEGORY
        )

    if cfg is None:
        cfg = config.load_config()
    result = add_flow.execute(cfg, entry)
    display_amount = (
        posting.trade_amount(entry)
        if kind in posting.INVEST_KINDS
        else args.amount
    )
    print(
        "已记账：%s %.2f 元 · 账户 %s（日期 %s，明细 %s，更新 %d 项）"
        % (
            posting.KIND_LABELS[kind],
            display_amount,
            args.account,
            entry["date"],
            "、".join(result["created"]) or "无",
            result["updated"],
        )
    )
    return EXIT_OK


def _run_edit(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    result = rebuild_flow.run_edit(
        cfg,
        args.record_id,
        amount=args.amount,
        date=args.date,
        account=args.account,
        category=args.category,
        note=args.note,
    )
    _print_diagnostics(result.get("diagnostics"))
    print(
        "已修改明细 %s：变更 %s；重算更新 %d 项"
        % (
            result["record_id"],
            "、".join(result["changed"]),
            result["updated"],
        )
    )
    return EXIT_OK


def _run_delete(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    try:
        result = rebuild_flow.run_delete(
            cfg, args.record_id, yes=args.yes
        )
    except rebuild_flow.ConfirmationRequired as exc:
        print("未删除：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    _print_diagnostics(result.get("diagnostics"))
    print(
        "已删除 %d 条明细：%s；重算更新 %d 项"
        % (
            len(result["deleted"]),
            "、".join(result["deleted"]),
            result["updated"],
        )
    )
    return EXIT_OK


def _diff_text(value: Any) -> str:
    return "—" if value is None else _cell_text(value)


def _run_rebuild(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    result = rebuild_flow.run_rebuild(cfg, apply=args.apply)
    _print_diagnostics(result.get("diagnostics"))
    diff = result["diff"]
    if not diff:
        print("账实一致，无需修正。")
        return EXIT_OK
    rows = [
        [
            item["对象"],
            item["字段"],
            _diff_text(item["现值"]),
            _diff_text(item["应为"]),
        ]
        for item in diff
    ]
    print(_format_table(("对象", "字段", "现值", "应为"), rows))
    if args.apply:
        print("已修正 %d 项余额/持仓。" % result["updated"])
    else:
        print("dry-run：未写入任何数据；确认无误后加 --apply。")
    return EXIT_OK


def _print_diagnostics(diagnostics) -> None:
    for message in diagnostics or []:
        print("诊断：%s" % message, file=sys.stderr)


def _cell_text(value: Any) -> str:
    """把 CellValue 渲染成表格里的文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return "%g" % value
    if isinstance(value, (int, str)):
        return str(value)
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
    return str(value)


def _account_cell(name: str, value: Any) -> str:
    # 年利率按小数存储，展示时还原成百分数。
    if name == "年利率" and isinstance(value, (int, float)) and not isinstance(
        value, bool
    ):
        return "%g%%" % (value * 100)
    return _cell_text(value)


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列，避免中文表格错位。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _format_table(columns, rows) -> str:
    if not rows:
        return "暂无数据。"
    widths = [
        max([_display_width(column)] + [_display_width(row[index]) for row in rows])
        for index, column in enumerate(columns)
    ]
    lines = [
        "  ".join(
            _pad(column, widths[index]) for index, column in enumerate(columns)
        ).rstrip(),
        "  ".join("-" * width for width in widths),
    ]
    for row in rows:
        lines.append(
            "  ".join(
                _pad(row[index], widths[index])
                for index in range(len(columns))
            ).rstrip()
        )
    return "\n".join(lines)


def _account_rows(records: List[dict]) -> List[List[str]]:
    return [
        [
            _account_cell(name, record["fields"].get(name))
            for name in ACCOUNT_COLUMNS
        ]
        for record in records
    ]


def _account_name_map(records: List[dict]) -> dict:
    return {
        record["record_id"]: _cell_text(record["fields"].get("账户名称"))
        for record in records
        if record.get("record_id")
    }


def _holding_rows(records: List[dict], account_names: dict) -> List[List[str]]:
    rows = []
    for record in records:
        fields = record["fields"]
        row = []
        for name in HOLDING_COLUMNS:
            value = fields.get(name)
            if name == "理财账户" and isinstance(value, list):
                row.append(
                    "、".join(
                        account_names.get(item.get("id"), item.get("id", ""))
                        if isinstance(item, dict)
                        else _cell_text(item)
                        for item in value
                    )
                )
            else:
                row.append(_cell_text(value))
        rows.append(row)
    return rows


def _account_edit_fields(args: argparse.Namespace) -> dict:
    fields = {}
    if args.name is not None:
        fields["账户名称"] = args.name
    if args.type_ is not None:
        fields["类型"] = args.type_
    if args.opening is not None:
        fields["初始余额"] = args.opening
    if args.rate is not None:
        fields["年利率"] = master_data.rate_from_percent(args.rate)
    if args.repay is not None:
        fields["还款方式"] = args.repay
    if args.status is not None:
        fields["状态"] = args.status
    if args.note is not None:
        fields["备注"] = args.note
    return fields


def _holding_edit_fields(args: argparse.Namespace) -> dict:
    fields = {}
    if args.name is not None:
        fields["持仓名称"] = args.name
    if args.code is not None:
        fields["代码"] = args.code
    if args.kind is not None:
        fields["品种"] = args.kind
    if args.account is not None:
        fields["理财账户"] = args.account
    if args.shares is not None:
        fields["当前份额"] = args.shares
    if args.cost is not None:
        fields["成本价"] = args.cost
    if args.opening_shares is not None:
        fields["期初份额"] = args.opening_shares
    if args.opening_cost is not None:
        fields["期初成本"] = args.opening_cost
    if args.status is not None:
        fields["状态"] = args.status
    return fields


def _run_account(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    if args.action == "add":
        rate = (
            master_data.rate_from_percent(args.rate)
            if args.rate is not None
            else None
        )
        record_id = master_data.account_add(
            cfg, args.name, args.type_, args.opening, rate=rate, repay=args.repay
        )
        print("已添加账户：%s（%s）" % (args.name, record_id))
        return EXIT_OK

    if args.action == "edit":
        fields = _account_edit_fields(args)
        if not fields:
            return _usage_error(
                "account edit 至少指定一个要修改的字段（--name/--type/--opening/"
                "--rate/--repay/--status/--note）"
            )
        record_id = master_data.account_edit(cfg, args.ref, **fields)
        print("已更新账户：%s（%s）" % (args.ref, record_id))
        return EXIT_OK

    if args.action == "list":
        records = master_data.account_list(cfg)
        print(_format_table(ACCOUNT_COLUMNS, _account_rows(records)))
        return EXIT_OK

    if args.action == "close":
        record_id = master_data.account_close(cfg, args.ref)
        print("已销户：%s（%s）" % (args.ref, record_id))
        return EXIT_OK

    return _usage_error("未知 account 子命令：%s" % args.action)


def _run_holding(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    if args.action == "add":
        record_id = master_data.holding_add(
            cfg,
            args.name,
            args.code,
            args.kind,
            args.account,
            args.shares,
            args.cost,
        )
        print("已添加持仓：%s（%s）" % (args.name, record_id))
        return EXIT_OK

    if args.action == "edit":
        fields = _holding_edit_fields(args)
        if not fields:
            return _usage_error(
                "holding edit 至少指定一个要修改的字段（--name/--code/--kind/"
                "--account/--shares/--cost/--opening-shares/--opening-cost/"
                "--status）"
            )
        record_id = master_data.holding_edit(cfg, args.ref, **fields)
        print("已更新持仓：%s（%s）" % (args.ref, record_id))
        return EXIT_OK

    if args.action == "list":
        records = master_data.holding_list(cfg)
        account_names = _account_name_map(master_data.account_list(cfg))
        print(
            _format_table(
                HOLDING_COLUMNS, _holding_rows(records, account_names)
            )
        )
        return EXIT_OK

    return _usage_error("未知 holding 子命令：%s" % args.action)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 用 SystemExit 表达用法错误（2）与 --help（0）。
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        if args.command == "init":
            return _run_init(args)
        if args.command == "add":
            return _run_add(args)
        if args.command == "edit":
            return _run_edit(args)
        if args.command == "delete":
            return _run_delete(args)
        if args.command == "rebuild":
            return _run_rebuild(args)
        if args.command == "account":
            return _run_account(args)
        if args.command == "holding":
            return _run_holding(args)
        print("未知子命令：%s" % args.command, file=sys.stderr)
        return EXIT_USAGE
    except add_flow.PartialWriteError as exc:
        print("记账部分失败：%s" % exc, file=sys.stderr)
        print("失败步骤：%s" % (exc.failed_step or "未知"), file=sys.stderr)
        if exc.created_ids:
            print(
                "可能已写入的明细：%s（请到飞书表格核对该行）"
                % "、".join(exc.created_ids),
                file=sys.stderr,
            )
        print(
            "已完成：%s" % ("、".join(exc.completed) or "无"),
            file=sys.stderr,
        )
        print(
            "未完成：%s" % ("、".join(exc.unfinished) or "无"),
            file=sys.stderr,
        )
        print(
            "请到飞书表格核对后手工补齐未完成步骤；明细可能已经写入。",
            file=sys.stderr,
        )
        return EXIT_LARK
    except posting.PostingError as exc:
        print("参数错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except init_flow.NotLoggedInError as exc:
        # 消息本身已是用户可读的登录引导，不再加前缀。
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_READY
    except master_data.MasterDataError as exc:
        print("数据错误：%s" % exc, file=sys.stderr)
        return EXIT_DATA
    except init_flow.InitError as exc:
        print("数据错误：%s" % exc, file=sys.stderr)
        return EXIT_DATA
    except lark.LarkError as exc:
        print("飞书调用失败：%s" % exc, file=sys.stderr)
        return EXIT_LARK
    except config.ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return EXIT_NOT_READY


if __name__ == "__main__":
    sys.exit(main())
