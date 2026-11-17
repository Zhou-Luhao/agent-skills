"""账户 / 持仓主数据：全部 mock lark 调用与本地配置，不触网、不读写真实配置。"""

import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config
import ledger
import master_data


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def account(record_id, name, type_, opening, rate=None, repay=None, status="在用"):
    fields = {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(opening),
        "当前余额": float(opening),
        "状态": [status],
        "备注": "",
    }
    if rate is not None:
        fields["年利率"] = float(rate)
    if repay is not None:
        fields["还款方式"] = [repay]
    return {"record_id": record_id, "fields": fields}


def holding(record_id, name, code, kind, account_id, shares, cost, status="持有"):
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": name,
            "代码": code,
            "品种": [kind],
            "理财账户": [{"id": account_id}],
            "当前份额": float(shares),
            "成本价": float(cost),
            "最新价": 0.0,
            "最新价日期": "",
            "市值": 0.0,
            "浮动盈亏": 0.0,
            "状态": [status],
            "期初份额": float(shares),
            "期初成本": float(cost),
        },
    }


class MasterDataTestBase(unittest.TestCase):
    def setUp(self):
        self._patches = []

        def patch(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            self._patches.append(patcher)
            return patcher.start()

        self.addCleanup(self._stop_patches)

        # 绝不读写真实 ~/.lark-ledger/config.json。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = pathlib.Path(self.tmp.name) / "config.json"
        patch("config.CONFIG_PATH", new=self.config_path)

        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
        }
        self._save_cfg()

        self.accounts = [
            account("recA1", "招行工资卡", "银行卡", 1000),
            account(
                "recA2",
                "房贷",
                "负债",
                -500000,
                rate=0.042,
                repay="等额本息",
            ),
            account("recA3", "华泰证券", "理财", 50000),
            account("recA4", "现金", "现金", 1000),
        ]
        self.holdings = [
            holding("recH1", "贵州茅台", "sh600519", "股票", "recA3", 100, 1500),
            holding("recH2", "易方达基金", "110011", "基金", "recA3", 1000, 4.0),
        ]
        self._next_id = 0

        # 任何真实 lark-cli 调用都必须让测试炸掉。
        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        self.search_calls = []
        self.search_records = patch(
            "master_data.lark.search_records", side_effect=self._search
        )
        self.create_records = patch(
            "master_data.lark.create_records", side_effect=self._create_records
        )
        self.update_record = patch(
            "master_data.lark.update_record", side_effect=self._update_record
        )

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _save_cfg(self):
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )

    def _source(self, table_id):
        if table_id == "tbl_account":
            return self.accounts
        if table_id == "tbl_holding":
            return self.holdings
        self.fail("未知 table_id：%s" % table_id)

    def _search(self, app_token, table_id, conditions=None, limit=2000):
        self.search_calls.append((app_token, table_id, conditions, limit))
        return [
            {"record_id": item["record_id"], "fields": copy.deepcopy(item["fields"])}
            for item in self._source(table_id)[:limit]
        ]

    def _create_records(self, app_token, table_id, records):
        target = self._source(table_id)
        ids = []
        for fields in records:
            self.assertNotIn("市值", fields)
            self.assertNotIn("浮动盈亏", fields)
            self._next_id += 1
            record_id = "recNew%d" % self._next_id
            target.append({"record_id": record_id, "fields": dict(fields)})
            ids.append(record_id)
        return ids

    def _update_record(self, app_token, table_id, record_id, fields):
        for item in self._source(table_id):
            if item["record_id"] == record_id:
                self.assertNotIn("市值", fields)
                self.assertNotIn("浮动盈亏", fields)
                item["fields"].update(fields)
                return None
        self.fail("更新了不存在的记录 %s" % record_id)

    def _record(self, table, record_id):
        for item in self._source(table):
            if item["record_id"] == record_id:
                return item["fields"]
        self.fail("没有记录 %s" % record_id)


class ResolveTest(MasterDataTestBase):
    def test_resolve_account_by_name_and_id(self):
        by_name = master_data.resolve_account(self.cfg, "招行工资卡")
        by_id = master_data.resolve_account(self.cfg, "recA2")

        self.assertEqual(by_name["record_id"], "recA1")
        self.assertEqual(by_name["fields"]["类型"], ["银行卡"])
        self.assertEqual(by_id["record_id"], "recA2")
        self.assertEqual(by_id["fields"]["账户名称"], "房贷")

    def test_resolve_holding_by_code_and_name(self):
        by_code = master_data.resolve_holding(self.cfg, "sh600519")
        by_name = master_data.resolve_holding(self.cfg, "易方达基金")

        self.assertEqual(by_code["record_id"], "recH1")
        self.assertEqual(by_name["record_id"], "recH2")

    def test_resolve_unknown_account_raises_helpful_error(self):
        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.resolve_account(self.cfg, "不存在的卡")

        message = str(caught.exception)
        self.assertIn("不存在的卡", message)
        self.assertIn("账户", message)
        self.assertIn("account list", message)

    def test_resolve_unknown_holding_raises_helpful_error(self):
        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.resolve_holding(self.cfg, "999999")

        self.assertIn("999999", str(caught.exception))
        self.assertIn("持仓", str(caught.exception))

    def test_resolve_ambiguous_account_name_raises(self):
        self.accounts.append(account("recA9", "招行工资卡", "银行卡", 0))

        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.resolve_account(self.cfg, "招行工资卡")

        message = str(caught.exception)
        self.assertIn("歧义", message)
        self.assertIn("recA1", message)
        self.assertIn("recA9", message)

    def test_resolve_ambiguous_holding_code_raises(self):
        self.holdings.append(
            holding("recH9", "另一只茅台", "sh600519", "股票", "recA3", 1, 1)
        )

        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.resolve_holding(self.cfg, "sh600519")

        message = str(caught.exception)
        self.assertIn("歧义", message)
        self.assertIn("recH1", message)
        self.assertIn("recH9", message)

    def test_resolve_blank_ref_raises(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.resolve_account(self.cfg, "  ")


class AccountTest(MasterDataTestBase):
    def test_account_add_writes_expected_fields(self):
        record_id = master_data.account_add(
            self.cfg, "工行卡", "银行卡", 1200.5
        )

        fields = self._record("tbl_account", record_id)
        self.assertEqual(fields["账户名称"], "工行卡")
        self.assertEqual(fields["类型"], ["银行卡"])
        self.assertEqual(fields["初始余额"], 1200.5)
        self.assertEqual(fields["当前余额"], 1200.5)
        self.assertEqual(fields["状态"], ["在用"])
        self.assertNotIn("年利率", fields)
        self.assertNotIn("还款方式", fields)

    def test_account_add_liability_negative_opening(self):
        record_id = master_data.account_add(
            self.cfg,
            "消费贷",
            "负债",
            -500000,
            rate=0.042,
            repay="随借随还",
        )

        fields = self._record("tbl_account", record_id)
        self.assertEqual(fields["初始余额"], -500000)
        self.assertEqual(fields["当前余额"], -500000)
        self.assertEqual(fields["年利率"], 0.042)
        self.assertEqual(fields["还款方式"], ["随借随还"])

    def test_account_add_rejects_invalid_type(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.account_add(self.cfg, "X", "不存在的类型", 1)
        self.assertFalse(self.create_records.called)

    def test_account_add_rejects_invalid_repay(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.account_add(
                self.cfg, "房贷2", "负债", -1, repay="不存在的还款方式"
            )
        self.assertFalse(self.create_records.called)

    def test_account_add_rejects_percent_rate(self):
        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.account_add(self.cfg, "房贷2", "负债", -1, rate=4.2)

        self.assertIn("年利率按小数传入：4.2% 传 0.042", str(caught.exception))
        self.assertFalse(self.create_records.called)

    def test_account_edit_rejects_percent_rate(self):
        with self.assertRaises(master_data.MasterDataError) as caught:
            master_data.account_edit(self.cfg, "房贷", 年利率=4.2)

        self.assertIn("年利率按小数传入：4.2% 传 0.042", str(caught.exception))

    def test_account_edit_updates_only_named_fields(self):
        master_data.account_edit(
            self.cfg, "招行工资卡", 初始余额=2000, 备注="工资卡"
        )

        fields = self._record("tbl_account", "recA1")
        self.assertEqual(fields["初始余额"], 2000)
        self.assertEqual(fields["备注"], "工资卡")
        # 当前余额不因改期初而自动变，等 rebuild 重算。
        self.assertEqual(fields["当前余额"], 1000)

    def test_account_edit_encodes_select_and_accepts_record_id(self):
        master_data.account_edit(self.cfg, "recA2", 类型="现金")

        self.assertEqual(self._record("tbl_account", "recA2")["类型"], ["现金"])

    def test_account_edit_rejects_formula_fields(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.account_edit(self.cfg, "招行工资卡", 市值=1)

    def test_account_list_returns_every_record(self):
        records = master_data.account_list(self.cfg)

        self.assertEqual(
            [item["record_id"] for item in records],
            ["recA1", "recA2", "recA3", "recA4"],
        )
        self.assertEqual(records[0]["fields"]["账户名称"], "招行工资卡")

    def test_account_close_sets_status(self):
        master_data.account_close(self.cfg, "招行工资卡")

        fields = self._record("tbl_account", "recA1")
        self.assertEqual(fields["状态"], ["已销户"])
        self.assertEqual(fields["当前余额"], 1000)

    def test_account_close_unknown_raises(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.account_close(self.cfg, "不存在的卡")


class HoldingTest(MasterDataTestBase):
    def test_holding_add_sets_opening_fields(self):
        record_id = master_data.holding_add(
            self.cfg,
            "宁德时代",
            "sz300750",
            "股票",
            "华泰证券",
            200,
            180.5,
        )

        fields = self._record("tbl_holding", record_id)
        self.assertEqual(fields["持仓名称"], "宁德时代")
        self.assertEqual(fields["代码"], "sz300750")
        self.assertEqual(fields["品种"], ["股票"])
        self.assertEqual(fields["理财账户"], [{"id": "recA3"}])
        self.assertEqual(fields["当前份额"], 200)
        self.assertEqual(fields["期初份额"], 200)
        self.assertEqual(fields["成本价"], 180.5)
        self.assertEqual(fields["期初成本"], 180.5)
        self.assertEqual(fields["状态"], ["持有"])
        self.assertNotIn("市值", fields)
        self.assertNotIn("浮动盈亏", fields)

    def test_holding_add_unknown_account_raises(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.holding_add(
                self.cfg, "宁德时代", "sz300750", "股票", "不存在的账户", 200, 180
            )

    def test_holding_add_rejects_invalid_kind(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.holding_add(
                self.cfg, "X", "x1", "债券", "华泰证券", 1, 1
            )

    def test_holding_edit_shares_and_cost_are_current_only(self):
        master_data.holding_edit(
            self.cfg, "sh600519", shares=150, cost=1600
        )

        fields = self._record("tbl_holding", "recH1")
        self.assertEqual(fields["当前份额"], 150)
        self.assertEqual(fields["期初份额"], 100)
        self.assertEqual(fields["成本价"], 1600)
        self.assertEqual(fields["期初成本"], 1500)
        self.assertEqual(fields["品种"], ["股票"])

    def test_holding_edit_account_writes_link(self):
        master_data.holding_edit(self.cfg, "sh600519", account="现金")

        self.assertEqual(
            self._record("tbl_holding", "recH1")["理财账户"], [{"id": "recA4"}]
        )

    def test_holding_edit_rejects_formula_fields(self):
        with self.assertRaises(master_data.MasterDataError):
            master_data.holding_edit(self.cfg, "sh600519", 浮动盈亏=1)

    def test_holding_list_returns_every_record(self):
        records = master_data.holding_list(self.cfg)

        self.assertEqual(
            [item["record_id"] for item in records], ["recH1", "recH2"]
        )
        self.assertEqual(records[0]["fields"]["持仓名称"], "贵州茅台")


class LedgerCliTest(MasterDataTestBase):
    def test_account_add_cli_converts_rate_percent_to_fraction(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "account",
                    "add",
                    "--name",
                    "工行卡",
                    "--type",
                    "银行卡",
                    "--opening",
                    "-1200.5",
                    "--rate",
                    "4.2",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        fields = self._record("tbl_account", "recNew1")
        self.assertEqual(fields["初始余额"], -1200.5)
        self.assertEqual(fields["年利率"], 0.042)
        self.assertIn("工行卡", out.getvalue())

    def test_account_add_cli_liability(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "account",
                    "add",
                    "--name",
                    "消费贷",
                    "--type",
                    "负债",
                    "--opening",
                    "-500000",
                    "--repay",
                    "等额本金",
                ]
            )

        self.assertEqual(code, 0)
        fields = self._record("tbl_account", "recNew1")
        self.assertEqual(fields["初始余额"], -500000)
        self.assertEqual(fields["状态"], ["在用"])

    def test_invalid_type_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "account",
                    "add",
                    "--name",
                    "X",
                    "--type",
                    "不存在的类型",
                    "--opening",
                    "1",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(self.create_records.called, False)

    def test_invalid_repay_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "account",
                    "add",
                    "--name",
                    "X",
                    "--type",
                    "负债",
                    "--opening",
                    "-1",
                    "--repay",
                    "不存在的还款方式",
                ]
            )

        self.assertEqual(code, 2)
        self.assertFalse(self.create_records.called)

    def test_account_edit_cli_rate_is_percent(self):
        with captured_output() as (out, err):
            code = ledger.main(
                ["account", "edit", "房贷", "--rate", "3.6"]
            )

        self.assertEqual(code, 0)
        self.assertAlmostEqual(
            self._record("tbl_account", "recA2")["年利率"], 0.036, places=10
        )

    def test_account_edit_without_fields_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(["account", "edit", "房贷"])

        self.assertEqual(code, 2)
        self.assertIn("至少", err.getvalue())

    def test_account_list_cli_prints_table(self):
        with captured_output() as (out, err):
            code = ledger.main(["account", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("招行工资卡", out.getvalue())
        self.assertIn("房贷", out.getvalue())

    def test_account_close_cli(self):
        with captured_output() as (out, err):
            code = ledger.main(["account", "close", "招行工资卡"])

        self.assertEqual(code, 0)
        self.assertEqual(
            self._record("tbl_account", "recA1")["状态"], ["已销户"]
        )

    def test_holding_add_cli(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "holding",
                    "add",
                    "--name",
                    "宁德时代",
                    "--code",
                    "sz300750",
                    "--kind",
                    "股票",
                    "--account",
                    "华泰证券",
                    "--shares",
                    "200",
                    "--cost",
                    "180.5",
                ]
            )

        self.assertEqual(code, 0)
        fields = self._record("tbl_holding", "recNew1")
        self.assertEqual(fields["当前份额"], 200)
        self.assertEqual(fields["期初份额"], 200)
        self.assertEqual(fields["理财账户"], [{"id": "recA3"}])
        self.assertIn("宁德时代", out.getvalue())

    def test_holding_edit_cli_shares_then_opening(self):
        with captured_output() as (out, err):
            code = ledger.main(
                ["holding", "edit", "sh600519", "--shares", "120"]
            )

        self.assertEqual(code, 0)
        fields = self._record("tbl_holding", "recH1")
        self.assertEqual(fields["当前份额"], 120)
        self.assertEqual(fields["期初份额"], 100)

        with captured_output() as (out, err):
            code = ledger.main(
                ["holding", "edit", "sh600519", "--opening-shares", "150"]
            )

        self.assertEqual(code, 0)
        fields = self._record("tbl_holding", "recH1")
        self.assertEqual(fields["当前份额"], 120)
        self.assertEqual(fields["期初份额"], 150)

    def test_holding_list_cli_prints_table(self):
        with captured_output() as (out, err):
            code = ledger.main(["holding", "list"])

        self.assertEqual(code, 0)
        self.assertIn("贵州茅台", out.getvalue())
        self.assertIn("sh600519", out.getvalue())

    def test_unknown_account_exit_5(self):
        with captured_output() as (out, err):
            code = ledger.main(["account", "close", "不存在的卡"])

        self.assertEqual(code, 5)
        self.assertIn("数据错误", err.getvalue())

    def test_not_configured_exit_3(self):
        self.config_path.unlink()

        with captured_output() as (out, err):
            code = ledger.main(["account", "list"])

        self.assertEqual(code, 3)
        self.assertIn("配置", err.getvalue())


if __name__ == "__main__":
    unittest.main()
