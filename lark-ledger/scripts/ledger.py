"""lark-ledger 命令行入口。

退出码约定：0 成功 / 2 参数错误 / 3 未配置或未登录 / 4 lark-cli 调用失败 / 5 数据错误。
本任务只有 init 子命令，其余子命令后续任务补。
"""

import argparse
import sys
from typing import List, Optional

import config
import init_flow
import lark

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_READY = 3
EXIT_LARK = 4
EXIT_DATA = 5


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
    return parser


def _run_init(args: argparse.Namespace) -> int:
    cfg = init_flow.run_init(args.folder_token, args.base_name)
    print(
        "初始化完成：%s，%d 张表，预置分类已写入。"
        % (args.base_name, len(cfg.get("tables", {})))
    )
    print("配置文件：%s" % config.CONFIG_PATH)
    return EXIT_OK


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
        print("未知子命令：%s" % args.command, file=sys.stderr)
        return EXIT_USAGE
    except init_flow.NotLoggedInError as exc:
        # 消息本身已是用户可读的登录引导，不再加前缀。
        print(str(exc), file=sys.stderr)
        return EXIT_NOT_READY
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
