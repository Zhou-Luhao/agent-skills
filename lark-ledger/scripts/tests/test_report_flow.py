"""report / query 命令：拉数、强制快照、聚合、渲染与 CLI 接线。

全部 hermetic：mock 所有 lark 调用，config.CONFIG_PATH 指到临时文件，
时钟由测试注入（report_flow._now），报告写到临时目录，不触网、不读
真实配置、不往用户的 Downloads 里写东西。
"""

import contextlib
import copy
import datetime
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config
import lark
import ledger
import report_flow


UTC8 = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime(2026, 11, 18, 20, 0, tzinfo=UTC8)
TODAY = "2026-11-18"


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def account(record_id, name, type_, balance, status="在用", rate=None):
    fields = {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(balance),
        "当前余额": float(balance),
        "状态": [status],
        "备注": "",
    }
    if rate is not None:
        fields["年利率"] = rate
    return {"record_id": record_id, "fields": fields}


def holding(record_id, account_id, name, code, shares, latest, status="持有"):
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": name,
            "代码": code,
            "品种": ["股票"],
            "理财账户": [{"id": account_id}],
            "当前份额": float(shares),
            "成本价": float(latest),
            "最新价": float(latest),
            "状态": [status],
        },
    }


def category(record_id, first, second):
    return {
        "record_id": record_id,
        "fields": {
            "分类": "支出·%s·%s" % (first, second),
            "一级": first,
            "二级": second,
        },
    }


def entry(record_id, day, type_, amount, category_id=None, summary="测试"):
    fields = {
        "摘要": summary,
        "日期": day,
        "类型": [type_],
        "金额": float(amount),
        "备注": "",
    }
    if category_id is not None:
        fields["分类"] = [{"id": category_id}]
    return {"record_id": record_id, "fields": fields}


def snapshot_row(record_id, date, value):
    return {
        "record_id": record_id,
        "fields": {
            "日期-账户": "%s_测试" % date,
            "日期": date,
            "余额/市值": float(value),
            "备注": "",
        },
    }


class ReportFlowTest(unittest.TestCase):
    """run_report：拉数 → 强制快照 → 聚合 → 双产物落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "收支明细": {"table_id": "tbl_entry", "fields": {}},
            "分类": {"table_id": "tbl_category", "fields": {}},
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
            "资产快照": {"table_id": "tbl_snapshot", "fields": {}},
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )
        patcher = mock.patch("config.CONFIG_PATH", new=self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.entries = [
            entry("e1", "2026-11-17", "支出", 35.0, "c-lunch", "午饭"),
            entry("e2", "2026-11-17", "收入", 30000.0, "c-salary", "工资"),
            entry("e3", "2026-11-17", "转账", 10000.0, summary="还信用卡"),
            entry("e4", "2026-10-05", "支出", 99.0, "c-lunch", "上月的"),
        ]
        self.categories = [
            category("c-lunch", "餐饮", "午餐"),
            category("c-salary", "工资", "月薪"),
        ]
        self.accounts = [
            account("recCash", "现金", "现金", 1000),
            account("recBank", "工资卡", "银行卡", 5000),
            account("recWealth", "证券账户", "理财", 2000),
            account("recLoan", "房贷", "负债", -300000, rate=0.042),
            account(
                "recOldLoan", "已销户贷款", "负债", -500000,
                status="已销户", rate=0.05,
            ),
        ]
        self.holdings = [
            holding("recH1", "recWealth", "贵州茅台", "sh600519", 10, 100.0)
        ]
        self.snapshots = [
            snapshot_row("recS1", "2026-11-16", -290000),
        ]
        self.tables = {
            "tbl_entry": self.entries,
            "tbl_category": self.categories,
            "tbl_account": self.accounts,
            "tbl_holding": self.holdings,
            "tbl_snapshot": self.snapshots,
        }
        self.search_calls = []
        self.search_pages = []
        self.ops = []

        patcher = mock.patch("lark.run_cli", side_effect=AssertionError("不应调用 lark-cli"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("lark.search_records", side_effect=self._search)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "report_flow._now", return_value=NOW
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search(self, app_token, table_id, conditions=None, limit=200, offset=0):
        self.search_calls.append(table_id)
        self.search_pages.append((table_id, offset))
        self.ops.append(table_id)
        rows = self.tables[table_id]
        return copy.deepcopy(rows[offset : offset + limit])

    def _snapshot(self, cfg, date_str=None):
        # 模拟强制落当前快照：返回成功，不改动本测试里的快照行。
        self.snapshotted = (cfg, date_str)
        self.ops.append("snapshot")
        return {"written": 4, "date": TODAY}

    def test_report_writes_both_files(self):
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        html = pathlib.Path(result["html"])
        md = pathlib.Path(result["md"])
        self.assertTrue(html.is_file())
        self.assertTrue(md.is_file())
        self.assertEqual(html.name, "ledger-week-2026-11-22.html")
        self.assertEqual(md.name, "ledger-week-2026-11-22.md")
        # 显式 --out 必须生效：不能悄悄写到默认的 ~/Downloads。
        expected_dir = self.root / "out"
        self.assertEqual(html.parent, expected_dir)
        self.assertEqual(md.parent, expected_dir)

        self.assertEqual(result["summary"]["income"], 30000.0)
        self.assertEqual(result["summary"]["expense"], 35.0)
        self.assertEqual(result["summary"]["net"], 29965.0)
        text = md.read_text(encoding="utf-8")
        self.assertIn("餐饮", text)
        self.assertIn("29,965.00", text)
        self.assertIn("贵州茅台", text)
        self.assertIn("房贷", text)
        self.assertIn("生成于 2026-11-18 20:00", text)
        self.assertIn("<!doctype html>", html.read_text(encoding="utf-8"))

    def test_report_forces_snapshot(self):
        with mock.patch(
            "snapshot_flow.run_snapshot", side_effect=self._snapshot
        ) as run:
            report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        run.assert_called_once()
        self.assertIs(run.call_args[0][0], self.cfg)
        # 不传日期 = 由 snapshot_flow 自己取「今天（UTC+8）」。
        self.assertEqual(len(run.call_args[0]), 1)
        # 快照必须先于读表：曲线要含今天，数据也要包含刚落的那一行。
        self.assertIn("snapshot", self.ops)
        self.assertLess(self.ops.index("snapshot"), self.ops.index("tbl_entry"))

    def test_report_omits_live_point_for_historical_period(self):
        # 10 月报告跑在今天：实时值在期间外，曲线不能接到 2026-11-18。
        self.snapshots.append(snapshot_row("recOct", "2026-10-10", -280000))
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "month", anchor="2026-10-15", out_dir=self.root / "out"
            )

        md = pathlib.Path(result["md"]).read_text(encoding="utf-8")
        self.assertIn("| 2026-10-10 |", md)
        self.assertNotIn("| 2026-11-18 |", md)
        self.assertNotIn("-291,000.00", md)
        self.assertIn("| 当前净资产 | -280,000.00 |", md)

    def test_report_omits_live_point_for_future_period(self):
        # 未来锚点：今天早于期间，不能把今天的点排到曲线最前面。
        self.snapshots.append(snapshot_row("recJan", "2027-01-10", -100000))
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "month", anchor="2027-01-15", out_dir=self.root / "out"
            )

        md = pathlib.Path(result["md"]).read_text(encoding="utf-8")
        dates = re.findall(r"^\| (\d{4}-\d{2}-\d{2}) \| .+? \|$", md, re.M)
        self.assertEqual(dates, ["2027-01-10"])

    def test_report_liabilities_exclude_closed_accounts(self):
        # account close 不改当前余额：已销户贷款绝不能进负债概览。
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        md = pathlib.Path(result["md"]).read_text(encoding="utf-8")
        self.assertIn("房贷", md)
        self.assertNotIn("已销户贷款", md)

    def test_report_forces_a_real_current_snapshot(self):
        # 不 mock run_snapshot：真走一遍落表，确认报告读到了刚写的今天。
        created = []

        def create_records(app_token, table_id, records):
            ids = []
            for fields in records:
                record_id = "recNew%d" % (len(self.snapshots) + 1)
                self.snapshots.append(
                    {"record_id": record_id, "fields": copy.deepcopy(fields)}
                )
                created.append(copy.deepcopy(fields))
                ids.append(record_id)
            return ids

        with mock.patch(
            "snapshot_flow._now", return_value=NOW
        ), mock.patch(
            "lark.create_records", side_effect=create_records
        ):
            result = report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        self.assertEqual(len(created), 4)
        for fields in created:
            # 日期落表必须是飞书 datetime CellValue。
            self.assertEqual(fields["日期"], TODAY + " 00:00")
        md = pathlib.Path(result["md"]).read_text(encoding="utf-8")
        today_rows = re.findall(r"^\| 2026-11-18 \| (.+?) \|$", md, re.M)
        self.assertEqual(len(today_rows), 1)
        self.assertEqual(today_rows[0], "-291,000.00")

    def test_report_default_out_dir(self):
        home = self.root / "home"
        with mock.patch("pathlib.Path.home", return_value=home), mock.patch(
            "snapshot_flow.run_snapshot", side_effect=self._snapshot
        ):
            result = report_flow.run_report(self.cfg, "week", anchor=TODAY)

        expected = home / "Downloads" / "lark-ledger"
        self.assertEqual(pathlib.Path(result["html"]).parent, expected)
        self.assertEqual(pathlib.Path(result["md"]).parent, expected)
        self.assertTrue((expected / "ledger-week-2026-11-22.html").is_file())
        self.assertTrue((expected / "ledger-week-2026-11-22.md").is_file())

    def test_report_appends_live_net_worth_once(self):
        # 快照表里已经有一行「今天」，实时值必须替换它而不是再加一个点。
        self.snapshots.append(snapshot_row("recS2", TODAY, -200000))
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        md = pathlib.Path(result["md"]).read_text(encoding="utf-8")
        today_rows = re.findall(r"^\| 2026-11-18 \| (.+?) \|$", md, re.M)
        self.assertEqual(len(today_rows), 1)
        # 现金 1000 + 工资卡 5000 + 理财(2000 现金 + 10×100 市值) − 房贷 300000
        self.assertEqual(today_rows[0], "-291,000.00")

    def test_report_reads_every_page(self):
        # 250 条区间外的填充行把唯一一笔支出挤到第二页：分页读尽才不会漏账。
        filler = [
            entry("f%03d" % index, "2026-10-01", "支出", 1.0, "c-lunch")
            for index in range(250)
        ]
        self.entries = filler + [
            entry("late", "2026-11-17", "支出", 35.0, "c-lunch", "第二页")
        ]
        self.tables["tbl_entry"] = self.entries

        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            result = report_flow.run_report(
                self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
            )

        self.assertEqual(result["summary"]["expense"], 35.0)
        self.assertIn(("tbl_entry", 0), self.search_pages)
        self.assertIn(("tbl_entry", 200), self.search_pages)

    def test_report_snapshot_failure_warns_but_still_renders(self):
        with mock.patch(
            "snapshot_flow.run_snapshot",
            side_effect=lark.LarkError("飞书挂了"),
        ):
            with captured_output() as (out, err):
                result = report_flow.run_report(
                    self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
                )

        self.assertTrue(pathlib.Path(result["html"]).is_file())
        self.assertTrue(pathlib.Path(result["md"]).is_file())
        self.assertIn("警告", err.getvalue())
        self.assertIn("飞书挂了", err.getvalue())
        # 快照失败不影响收支聚合。
        self.assertEqual(result["summary"]["expense"], 35.0)

    def test_report_rejects_unknown_period(self):
        with self.assertRaises(report_flow.ReportError):
            report_flow.run_report(self.cfg, "quarter", anchor=TODAY)

    def test_report_unusable_out_dir_is_report_error(self):
        blocker = self.root / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            with self.assertRaises(report_flow.ReportError):
                report_flow.run_report(
                    self.cfg, "week", anchor=TODAY, out_dir=blocker
                )

    def test_report_wraps_aggregate_errors(self):
        # 坏快照是数据错误：必须转成 ReportError（CLI 退出 5），不能裸抛。
        self.snapshots[0]["fields"]["余额/市值"] = "不是数字"
        with mock.patch("snapshot_flow.run_snapshot", side_effect=self._snapshot):
            with self.assertRaises(report_flow.ReportError):
                report_flow.run_report(
                    self.cfg, "week", anchor=TODAY, out_dir=self.root / "out"
                )


class QueryFlowTest(unittest.TestCase):
    """run_query：balances / summary / holdings 三种查询形状。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "收支明细": {"table_id": "tbl_entry", "fields": {}},
            "分类": {"table_id": "tbl_category", "fields": {}},
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
            "资产快照": {"table_id": "tbl_snapshot", "fields": {}},
        }
        self.entries = [
            entry("e1", "2026-11-17", "支出", 35.0, "c-lunch", "午饭"),
            entry("e2", "2026-11-17", "收入", 30000.0, "c-salary", "工资"),
            entry("e3", "2026-11-17", "转账", 10000.0, summary="还信用卡"),
        ]
        self.categories = [
            category("c-lunch", "餐饮", "午餐"),
            category("c-salary", "工资", "月薪"),
        ]
        self.accounts = [
            account("recCash", "现金", "现金", 1000),
            account("recLoan", "房贷", "负债", -300000, rate=0.042),
            account(
                "recClosed", "已销户卡", "银行卡", 99999, status="已销户"
            ),
        ]
        self.holdings = [
            holding("recH1", "recWealth", "贵州茅台", "sh600519", 10, 100.0)
        ]
        self.tables = {
            "tbl_entry": self.entries,
            "tbl_category": self.categories,
            "tbl_account": self.accounts,
            "tbl_holding": self.holdings,
            "tbl_snapshot": [],
        }

        patcher = mock.patch("lark.run_cli", side_effect=AssertionError("不应调用 lark-cli"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("lark.search_records", side_effect=self._search)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("report_flow._now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search(self, app_token, table_id, conditions=None, limit=200, offset=0):
        rows = self.tables[table_id]
        return copy.deepcopy(rows[offset : offset + limit])

    def test_query_balances_shape(self):
        result = report_flow.run_query(self.cfg, "balances")

        self.assertEqual(result["what"], "balances")
        rows = {row["名称"]: row for row in result["balances"]}
        self.assertEqual(set(rows["现金"]), {"名称", "类型", "余额", "状态"})
        self.assertEqual(rows["现金"]["类型"], "现金")
        self.assertEqual(rows["现金"]["余额"], 1000.0)
        self.assertEqual(rows["现金"]["状态"], "在用")
        self.assertEqual(rows["房贷"]["类型"], "负债")
        self.assertEqual(rows["房贷"]["余额"], -300000.0)
        # 已销户账户照常展示（含状态），但不计入合计。
        self.assertEqual(rows["已销户卡"]["状态"], "已销户")
        self.assertEqual(rows["已销户卡"]["余额"], 99999.0)
        self.assertEqual(result["total"], -299000.0)

    def test_query_summary_excludes_transfers(self):
        result = report_flow.run_query(
            self.cfg, "summary", "2026-11-16", "2026-11-22"
        )

        self.assertEqual(result["what"], "summary")
        self.assertEqual(result["start"], "2026-11-16")
        self.assertEqual(result["end"], "2026-11-22")
        self.assertEqual(result["income"], 30000.0)
        self.assertEqual(result["expense"], 35.0)
        self.assertEqual(result["net"], 29965.0)
        self.assertEqual(
            result["by_category"],
            [{"一级": "餐饮", "二级": "午餐", "金额": 35.0}],
        )

    def test_query_summary_defaults_to_this_month(self):
        result = report_flow.run_query(self.cfg, "summary")
        self.assertEqual(result["start"], "2026-11-01")
        self.assertEqual(result["end"], "2026-11-30")

    def test_query_summary_filters_by_category(self):
        result = report_flow.run_query(
            self.cfg, "summary", "2026-11-16", "2026-11-22", category="餐饮"
        )
        self.assertEqual(result["category"], "餐饮")
        self.assertEqual(result["income"], 0.0)
        self.assertEqual(result["expense"], 35.0)
        self.assertEqual(result["net"], -35.0)
        self.assertEqual(result["income_by_category"], [])

    def test_query_summary_rejects_unknown_category(self):
        with self.assertRaises(report_flow.ReportError):
            report_flow.run_query(
                self.cfg, "summary", "2026-11-16", "2026-11-22",
                category="不存在的分类",
            )

    def test_query_holdings(self):
        result = report_flow.run_query(self.cfg, "holdings")
        self.assertEqual(result["what"], "holdings")
        self.assertEqual(len(result["holdings"]), 1)
        row = result["holdings"][0]
        self.assertEqual(row["名称"], "贵州茅台")
        self.assertEqual(row["市值"], 1000.0)
        self.assertEqual(row["浮盈"], 0.0)

    def test_query_rejects_unknown_what(self):
        with self.assertRaises(report_flow.ReportError):
            report_flow.run_query(self.cfg, "unknown")


class ReportCliTest(unittest.TestCase):
    """CLI 接线：参数透传、紧凑输出与退出码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {}
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )
        patcher = mock.patch("config.CONFIG_PATH", new=self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_report_exit_0(self):
        result = {
            "html": self.root / "ledger-week-2026-11-22.html",
            "md": self.root / "ledger-week-2026-11-22.md",
            "summary": {},
        }
        with mock.patch(
            "report_flow.run_report", return_value=result
        ) as run:
            with captured_output() as (out, err):
                code = ledger.main(
                    ["report", "--period", "week", "--date", "2026-11-18",
                     "--out", str(self.root)]
                )

        self.assertEqual(code, 0)
        run.assert_called_once()
        args = run.call_args[0]
        self.assertEqual(args[1], "week")
        self.assertEqual(args[2], "2026-11-18")
        self.assertEqual(args[3], str(self.root))
        self.assertIn(str(result["html"]), out.getvalue())
        self.assertIn(str(result["md"]), out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_cli_report_rejects_bad_date(self):
        with mock.patch("report_flow.run_report") as run:
            with captured_output() as (out, err):
                code = ledger.main(["report", "--period", "week", "--date", "2026/11/18"])
        self.assertEqual(code, 2)
        self.assertFalse(run.called)
        self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_cli_query_json(self):
        result = {
            "what": "balances",
            "balances": [{"名称": "现金", "类型": "现金", "余额": 1.0}],
            "total": 1.0,
        }
        with mock.patch("report_flow.run_query", return_value=result):
            with captured_output() as (out, err):
                code = ledger.main(["query", "balances", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), result)

    def test_cli_query_prints_table(self):
        result = {
            "what": "balances",
            "balances": [
                {"名称": "现金", "类型": "现金", "余额": 1.0, "状态": "在用"}
            ],
            "total": 1.0,
        }
        with mock.patch("report_flow.run_query", return_value=result):
            with captured_output() as (out, err):
                code = ledger.main(["query", "balances"])
        self.assertEqual(code, 0)
        self.assertIn("现金", out.getvalue())
        self.assertIn("在用", out.getvalue())
        self.assertIn("合计", out.getvalue())


if __name__ == "__main__":
    unittest.main()
