"""lark-ledger 命令行入口。

退出码约定：0 成功 / 2 参数错误 / 3 未配置或未登录 / 4 lark-cli 调用失败 / 5 数据错误。
已实现 init（建账本）与 account / holding 主数据子命令。
"""

import argparse
import sys
import unicodedata
from typing import Any, List, Optional

import config
import init_flow
import lark
import master_data

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
        if args.command == "account":
            return _run_account(args)
        if args.command == "holding":
            return _run_holding(args)
        print("未知子命令：%s" % args.command, file=sys.stderr)
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
