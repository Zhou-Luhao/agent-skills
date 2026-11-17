"""理财买卖 / 已实现盈亏 / 分红：纯逻辑 WritePlan 与 add 流程接线。

纯逻辑用例不 mock 网络、不读配置；流程用例把全部 lark 调用、配置路径与
时钟都替换掉，保证不触网、不依赖真实时间。
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

import add_flow
import config
import ledger
import posting


def account(name, balance, type_="理财"):
    return {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(balance),
        "当前余额": float(balance),
        "状态": ["在用"],
        "备注": "",
    }


def holding(
    name, shares, cost, breed="股票", status="持有", account_id="acc_fund"
):
    return {
        "持仓名称": name,
        "代码": "sh600519",
        "品种": [breed],
        "理财账户": [{"id": account_id}],
        "当前份额": float(shares),
        "成本价": float(cost),
        "状态": [status],
    }


class InvestPlanTestBase(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            "acc_bank": account("招行工资卡", 100000.0, "银行卡"),
            "acc_fund": account("华泰证券", 50000.0),
            "acc_other": account("国泰君安", 0.0),
        }
        self.holdings = {"h1": holding("贵州茅台", 1000, 10)}

    def entry(self, **overrides):
        data = {
            "kind": "invest_buy",
            "date": "2026-11-17",
            "holding": "h1",
            "price": 10.0,
            "shares": 1000.0,
            "account": "acc_fund",
            "from_account": "acc_bank",
            "summary": "买入",
            "note": "",
        }
        data.update(overrides)
        return data

    def sell_entry(self, **overrides):
        data = self.entry(
            kind="invest_sell",
            from_account=None,
            to_account="acc_bank",
            pnl_category="cat_stock",
            price=12.0,
            summary="赎回",
        )
        data.update(overrides)
        return data

    def rows(self, plan):
        return [fields for table, fields in plan["create"] if table == "收支明细"]

    def update_for(self, plan, table, record_id):
        for item_table, item_id, fields in plan["update"]:
            if item_table == table and item_id == record_id:
                return fields
        self.fail("plan 中没有 %s %s 的 update" % (table, record_id))

    def updates_for(self, plan, table, record_id):
        return [
            fields
            for item_table, item_id, fields in plan["update"]
            if item_table == table and item_id == record_id
        ]

    def tags(self, rows):
        return [
            re.search(r"#G[0-9a-f]{8}", row["备注"]).group(0) for row in rows
        ]


class InvestBuyTest(InvestPlanTestBase):
    def test_buy_two_records_group_tag(self):
        self.holdings = {"h1": holding("贵州茅台", 0, 0)}
        plan = posting.plan_add(
            self.entry(price=10, shares=1000), self.accounts, self.holdings
        )

        rows = self.rows(plan)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["类型"], ["转账"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_bank"}])
        self.assertEqual(rows[0]["转入账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[0]["金额"], 10000.0)
        # 第二条是自转的买卖明细：现金换成持仓，理财现金净 0。
        self.assertEqual(rows[1]["类型"], ["转账"])
        self.assertEqual(rows[1]["账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[1]["转入账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[1]["持仓"], [{"id": "h1"}])
        self.assertEqual(rows[1]["成交价"], 10.0)
        self.assertEqual(rows[1]["份额"], 1000.0)
        tags = self.tags(rows)
        self.assertEqual(tags[0], tags[1])

        self.assertEqual(
            self.update_for(plan, "账户", "acc_bank"), {"当前余额": 90000.0}
        )
        self.assertEqual(self.updates_for(plan, "账户", "acc_fund"), [])

    def test_buy_updates_shares_and_cost(self):
        plan = posting.plan_add(
            self.entry(price=12, shares=1000), self.accounts, self.holdings
        )

        update = self.update_for(plan, "持仓", "h1")
        self.assertEqual(update["当前份额"], 2000.0)
        self.assertEqual(update["成本价"], 11.0)

    def test_acceptance_3_shares(self):
        # spec 验收 3：一万元、净值 4.0854 ⇒ 脚本算份额 2447.74。
        self.holdings = {"h1": holding("华夏成长", 0, 0, breed="基金")}
        plan = posting.plan_add(
            self.entry(price=4.0854, shares=None, amount=10000),
            self.accounts,
            self.holdings,
        )

        rows = self.rows(plan)
        self.assertEqual(rows[0]["金额"], 10000.0)
        self.assertEqual(rows[1]["份额"], 2447.74)
        holding_update = self.update_for(plan, "持仓", "h1")
        self.assertEqual(holding_update["当前份额"], 2447.74)
        self.assertEqual(holding_update["成本价"], 4.0854)
        self.assertEqual(
            self.update_for(plan, "账户", "acc_bank"), {"当前余额": 90000.0}
        )


class InvestSellTest(InvestPlanTestBase):
    def test_sell_realized_gain_income(self):
        plan = posting.plan_add(
            self.sell_entry(price=12, shares=1000),
            self.accounts,
            self.holdings,
        )

        rows = self.rows(plan)
        self.assertEqual(len(rows), 3)
        # ① 卖出转账：理财账户 → 银行卡。
        self.assertEqual(rows[0]["类型"], ["转账"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[0]["转入账户"], [{"id": "acc_bank"}])
        self.assertEqual(rows[0]["金额"], 12000.0)
        # ② 已实现盈亏：正数记收入，原值入账。
        self.assertEqual(rows[1]["类型"], ["收入"])
        self.assertEqual(rows[1]["账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[1]["金额"], 2000.0)
        self.assertEqual(rows[1]["分类"], [{"id": "cat_stock"}])
        # ③ 买卖明细自转，承载持仓/成交价/份额。
        self.assertEqual(rows[2]["类型"], ["转账"])
        self.assertEqual(rows[2]["账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[2]["转入账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[2]["持仓"], [{"id": "h1"}])
        self.assertEqual(rows[2]["成交价"], 12.0)
        self.assertEqual(rows[2]["份额"], 1000.0)

        tags = self.tags(rows)
        self.assertEqual(len(set(tags)), 1)
        self.assertEqual(
            self.update_for(plan, "账户", "acc_bank"), {"当前余额": 112000.0}
        )
        self.assertEqual(self.updates_for(plan, "账户", "acc_fund"), [])

    def test_sell_realized_loss_negative(self):
        plan = posting.plan_add(
            self.sell_entry(price=8, shares=1000),
            self.accounts,
            self.holdings,
        )

        pnl_row = self.rows(plan)[1]
        self.assertEqual(pnl_row["类型"], ["收入"])
        self.assertEqual(pnl_row["金额"], -2000.0)
        self.assertEqual(
            self.update_for(plan, "账户", "acc_bank"), {"当前余额": 108000.0}
        )

    def test_sell_full_clears_status(self):
        plan = posting.plan_add(
            self.sell_entry(price=12, shares=1000),
            self.accounts,
            self.holdings,
        )

        update = self.update_for(plan, "持仓", "h1")
        self.assertEqual(update["当前份额"], 0.0)
        self.assertEqual(update["状态"], ["已清仓"])

    def test_sell_partial_keeps_status(self):
        self.holdings = {"h1": holding("贵州茅台", 2000, 10)}
        plan = posting.plan_add(
            self.sell_entry(price=12, shares=500),
            self.accounts,
            self.holdings,
        )

        update = self.update_for(plan, "持仓", "h1")
        self.assertEqual(update["当前份额"], 1500.0)
        self.assertNotIn("状态", update)


class DividendTest(InvestPlanTestBase):
    def test_dividend_cash_income(self):
        plan = posting.plan_add(
            {
                "kind": "dividend",
                "date": "2026-11-17",
                "amount": 500,
                "account": "acc_fund",
                "pnl_category": "cat_div",
                "summary": "分红",
                "note": "",
            },
            self.accounts,
            self.holdings,
        )

        rows = self.rows(plan)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["类型"], ["收入"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_fund"}])
        self.assertEqual(rows[0]["分类"], [{"id": "cat_div"}])
        self.assertEqual(rows[0]["金额"], 500.0)
        self.assertEqual(
            self.update_for(plan, "账户", "acc_fund"), {"当前余额": 50500.0}
        )


class InvestValidationTest(InvestPlanTestBase):
    def test_sell_more_than_held_reports_available(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.sell_entry(shares=1001), self.accounts, self.holdings
            )

        message = str(raised.exception)
        self.assertIn("1001", message)
        self.assertIn("可用份额 1000", message)

    def test_holding_must_exist(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(holding="h_missing"), self.accounts, self.holdings
            )
        self.assertIn("持仓", str(raised.exception))

        with self.assertRaises(posting.PostingError):
            posting.plan_add(self.entry(holding=None), self.accounts, self.holdings)

    def test_price_and_shares_must_be_positive(self):
        for price, shares in ((0, 1000), (-1, 1000), (10, 0), (10, -1)):
            with self.subTest(price=price, shares=shares):
                with self.assertRaises(posting.PostingError):
                    posting.plan_add(
                        self.entry(price=price, shares=shares),
                        self.accounts,
                        self.holdings,
                    )

    def test_buy_needs_from_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(from_account=None), self.accounts, self.holdings
            )
        self.assertIn("转出账户", str(raised.exception))

    def test_sell_needs_to_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.sell_entry(to_account=None), self.accounts, self.holdings
            )
        self.assertIn("转入账户", str(raised.exception))

    def test_account_must_be_investment_type(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(account="acc_bank", from_account="acc_fund"),
                self.accounts,
                self.holdings,
            )
        self.assertIn("理财账户", str(raised.exception))

        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                {
                    "kind": "dividend",
                    "date": "2026-11-17",
                    "amount": 500,
                    "account": "acc_bank",
                    "pnl_category": "cat_div",
                },
                self.accounts,
                self.holdings,
            )
        self.assertIn("理财账户", str(raised.exception))

    def test_holding_must_belong_to_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(account="acc_other", from_account="acc_bank"),
                self.accounts,
                self.holdings,
            )

        message = str(raised.exception)
        self.assertIn("不属于", message)
        self.assertIn("acc_other", message)

    def test_buy_rejects_amount_and_shares_together(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(price=10, shares=1000, amount=10000),
                self.accounts,
                self.holdings,
            )
        self.assertIn("同时", str(raised.exception))

    def test_sell_needs_pnl_category(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.sell_entry(pnl_category=None), self.accounts, self.holdings
            )
        self.assertIn("分类", str(raised.exception))


# ---------------------------------------------------------------------------
# add 流程：实时解析账户/持仓/分类，并按序写飞书（全部 mock）。
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


FIXED_INSTANT = datetime.datetime(
    2026, 11, 17, 16, 30, tzinfo=datetime.timezone.utc
)


def flow_account(record_id, name, type_, balance):
    return {
        "record_id": record_id,
        "fields": account(name, balance, type_),
    }


def flow_holding(record_id, name, code, breed, shares, cost, account_id):
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": name,
            "代码": code,
            "品种": [breed],
            "理财账户": [{"id": account_id}],
            "当前份额": float(shares),
            "成本价": float(cost),
            "状态": ["持有"],
        },
    }


def flow_category(record_id, full_name, type_="收入"):
    return {
        "record_id": record_id,
        "fields": {"分类": full_name, "类型": [type_]},
    }


class InvestFlowTest(unittest.TestCase):
    def setUp(self):
        self._patches = []

        def patch(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            self._patches.append(patcher)
            return patcher.start()

        self.addCleanup(self._stop_patches)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = pathlib.Path(self.tmp.name) / "config.json"
        patch("config.CONFIG_PATH", new=self.config_path)

        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
            "分类": {"table_id": "tbl_category", "fields": {}},
            "收支明细": {"table_id": "tbl_entry", "fields": {}},
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )

        self.accounts = [
            flow_account("recA1", "招行工资卡", "银行卡", 100000),
            flow_account("recA2", "华泰证券", "理财", 50000),
        ]
        self.holdings = [
            flow_holding("recH1", "贵州茅台", "sh600519", "股票", 1000, 10, "recA2"),
            flow_holding("recH2", "富国天惠", "110011", "基金", 1000, 1, "recA2"),
            flow_holding("recH3", "华夏成长", "000001", "基金", 0, 0, "recA2"),
        ]
        self.categories = [
            flow_category("recC1", "收入·理财收益·股票收益"),
            flow_category("recC2", "收入·理财收益·基金收益"),
            flow_category("recC3", "收入·理财收益·分红"),
            flow_category("recC4", "收入·其他"),
            flow_category("recC5", "收入·理财收益·其他"),
        ]
        self.entry_rows = []
        self.calls = []
        self.search_calls = []
        self._next_id = 0

        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        patch("lark.search_records", side_effect=self._search)
        patch("lark.create_records", side_effect=self._create)
        patch("lark.update_records", side_effect=self._update)
        patch("add_flow._now", return_value=FIXED_INSTANT)
        patch("quotes_flow.maybe_run", side_effect=lambda cfg, now=None: None)
        patch("snapshot_flow.maybe_weekly", side_effect=lambda cfg, now=None: None)

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _search(self, app_token, table_id, conditions=None, limit=2000):
        self.search_calls.append(table_id)
        if table_id == "tbl_account":
            return [copy.deepcopy(item) for item in self.accounts[:limit]]
        if table_id == "tbl_category":
            return [copy.deepcopy(item) for item in self.categories[:limit]]
        if table_id == "tbl_holding":
            return [copy.deepcopy(item) for item in self.holdings[:limit]]
        self.fail("不应搜索表 %s" % table_id)

    def _create(self, app_token, table_id, records):
        self.calls.append(("create", table_id))
        if table_id != "tbl_entry":
            self.fail("创建了未预期的表 %s" % table_id)
        ids = []
        for fields in records:
            self._next_id += 1
            record_id = "recNew%d" % self._next_id
            self.entry_rows.append(
                {"record_id": record_id, "fields": copy.deepcopy(fields)}
            )
            ids.append(record_id)
        return ids

    def _update(self, app_token, table_id, updates):
        self.calls.append(("update", table_id, tuple(updates)))
        for record_id, fields in updates.items():
            if table_id == "tbl_account":
                target = self.accounts
            elif table_id == "tbl_holding":
                target = self.holdings
            else:
                self.fail("更新了未预期的表 %s" % table_id)
            for item in target:
                if item["record_id"] == record_id:
                    item["fields"].update(copy.deepcopy(fields))
                    break
            else:
                self.fail("更新了不存在的记录 %s" % record_id)

    def account_balance(self, record_id):
        for item in self.accounts:
            if item["record_id"] == record_id:
                return item["fields"]["当前余额"]
        self.fail("没有账户 %s" % record_id)

    def holding_record(self, record_id):
        for item in self.holdings:
            if item["record_id"] == record_id:
                return item["fields"]
        self.fail("没有持仓 %s" % record_id)

    def income_row(self):
        rows = [
            row["fields"]
            for row in self.entry_rows
            if row["fields"]["类型"] == ["收入"]
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_fund_routes_fund_category(self):
        result = add_flow.execute(
            self.cfg,
            {
                "kind": "invest_sell",
                "date": "2026-11-17",
                "holding": "110011",
                "price": 1.2,
                "shares": 1000,
                "to_account": "招行工资卡",
                "account": "华泰证券",
                "summary": "赎回",
            },
        )

        self.assertEqual(
            result["created"], ["recNew1", "recNew2", "recNew3"]
        )
        pnl_row = self.income_row()
        self.assertEqual(pnl_row["分类"], [{"id": "recC2"}])
        self.assertEqual(pnl_row["金额"], 200.0)

    def test_cli_buy_cash_conservation(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_buy",
                    "--date",
                    "2026-11-17",
                    "--holding",
                    "sh600519",
                    "--price",
                    "12",
                    "--shares",
                    "1000",
                    "--from-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                    "--summary",
                    "买入",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.entry_rows), 2)
        self.assertIn("已记账", out.getvalue())
        self.assertEqual(self.account_balance("recA1"), 88000.0)
        self.assertEqual(self.account_balance("recA2"), 50000.0)
        held = self.holding_record("recH1")
        self.assertEqual(held["当前份额"], 2000.0)
        self.assertEqual(held["成本价"], 11.0)

    def test_cli_buy_by_amount_computes_shares(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_buy",
                    "--date",
                    "2026-11-17",
                    "--holding",
                    "000001",
                    "--price",
                    "4.0854",
                    "--amount",
                    "10000",
                    "--from-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.entry_rows[0]["fields"]["金额"], 10000.0)
        self.assertEqual(self.entry_rows[1]["fields"]["份额"], 2447.74)
        self.assertEqual(self.account_balance("recA1"), 90000.0)
        self.assertEqual(self.account_balance("recA2"), 50000.0)
        held = self.holding_record("recH3")
        self.assertEqual(held["当前份额"], 2447.74)
        self.assertEqual(held["成本价"], 4.0854)

    def test_cli_sell_auto_pnl_income(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_sell",
                    "--date",
                    "2026-11-17",
                    "--holding",
                    "sh600519",
                    "--price",
                    "12",
                    "--shares",
                    "1000",
                    "--to-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                    "--summary",
                    "赎回",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.entry_rows), 3)
        pnl_row = self.income_row()
        self.assertEqual(pnl_row["分类"], [{"id": "recC1"}])
        self.assertEqual(pnl_row["金额"], 2000.0)
        self.assertEqual(self.account_balance("recA1"), 112000.0)
        held = self.holding_record("recH1")
        self.assertEqual(held["当前份额"], 0.0)
        self.assertEqual(held["状态"], ["已清仓"])

    def test_cli_sell_explicit_category_used(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_sell",
                    "--date",
                    "2026-11-17",
                    "--holding",
                    "sh600519",
                    "--price",
                    "12",
                    "--shares",
                    "1000",
                    "--to-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                    "--category",
                    "收入·理财收益·其他",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.income_row()["分类"], [{"id": "recC5"}])

    def test_cli_sell_rejects_non_investment_pnl_category(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_sell",
                    "--holding",
                    "sh600519",
                    "--price",
                    "12",
                    "--shares",
                    "1000",
                    "--to-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                    "--category",
                    "收入·其他",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("收入·理财收益", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def test_cli_dividend_income(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "dividend",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "500",
                    "--account",
                    "华泰证券",
                    "--category",
                    "收入·理财收益·分红",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self.entry_rows), 1)
        row = self.entry_rows[0]["fields"]
        self.assertEqual(row["类型"], ["收入"])
        self.assertEqual(row["分类"], [{"id": "recC3"}])
        self.assertEqual(row["金额"], 500.0)
        self.assertEqual(self.account_balance("recA2"), 50500.0)

    def test_cli_sell_missing_to_account_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_sell",
                    "--holding",
                    "sh600519",
                    "--price",
                    "12",
                    "--shares",
                    "1000",
                    "--account",
                    "华泰证券",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--to-account", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def _assert_rejected(self, argv):
        with captured_output() as (out, err):
            code = ledger.main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(self.entry_rows, [])
        self.assertEqual(self.calls, [])
        return err.getvalue()

    def test_cli_expense_rejects_trade_flags(self):
        message = self._assert_rejected(
            [
                "add",
                "--kind",
                "expense",
                "--amount",
                "5000",
                "--account",
                "招行工资卡",
                "--category",
                "支出·餐饮·午餐",
                "--holding",
                "sh600519",
                "--price",
                "12",
                "--shares",
                "1000",
            ]
        )

        self.assertIn("--holding", message)

    def test_cli_income_rejects_trade_flags(self):
        message = self._assert_rejected(
            [
                "add",
                "--kind",
                "income",
                "--amount",
                "5000",
                "--account",
                "招行工资卡",
                "--category",
                "收入·工资",
                "--holding",
                "sh600519",
                "--price",
                "12",
                "--shares",
                "1000",
            ]
        )

        self.assertIn("--holding", message)

    def test_cli_transfer_rejects_trade_flags(self):
        message = self._assert_rejected(
            [
                "add",
                "--kind",
                "transfer",
                "--amount",
                "1000",
                "--account",
                "招行工资卡",
                "--to-account",
                "现金",
                "--holding",
                "sh600519",
                "--price",
                "12",
                "--shares",
                "1000",
                "--from-account",
                "招行工资卡",
            ]
        )

        self.assertIn("--holding", message)

    def test_cli_buy_rejects_amount_with_shares(self):
        message = self._assert_rejected(
            [
                "add",
                "--kind",
                "invest_buy",
                "--holding",
                "sh600519",
                "--price",
                "12",
                "--shares",
                "1000",
                "--amount",
                "12000",
                "--from-account",
                "招行工资卡",
                "--account",
                "华泰证券",
            ]
        )

        self.assertIn("--amount", message)
        self.assertIn("--shares", message)

    def test_cli_buy_amount_prints_booked_amount(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "invest_buy",
                    "--date",
                    "2026-11-17",
                    "--holding",
                    "000001",
                    "--price",
                    "3",
                    "--amount",
                    "100",
                    "--from-account",
                    "招行工资卡",
                    "--account",
                    "华泰证券",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        # 份额 100/3 = 33.33，账面转账金额按 3×33.33 重算为 99.99。
        self.assertIn("99.99 元", out.getvalue())
        self.assertEqual(self.entry_rows[0]["fields"]["金额"], 99.99)
        self.assertEqual(self.entry_rows[1]["fields"]["份额"], 33.33)
        self.assertEqual(self.account_balance("recA1"), 99900.01)

    def test_cli_dividend_defaults_category(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "dividend",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "500",
                    "--account",
                    "华泰证券",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.entry_rows[0]["fields"]["分类"], [{"id": "recC3"}])


if __name__ == "__main__":
    unittest.main()
