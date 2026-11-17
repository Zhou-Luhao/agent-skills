"""贷款本息拆分 + 信用卡语义：纯算术、贷款还款 WritePlan 与 add 流程接线。

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
import loans
import posting


def account(name, balance, type_="现金", rate=None, repay=None):
    """账户 id → 字段 dict（与 master_data 落库字段一致）。"""
    fields = {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(balance),
        "当前余额": float(balance),
        "状态": ["在用"],
        "备注": "",
    }
    if rate is not None:
        fields["年利率"] = rate
    if repay is not None:
        fields["还款方式"] = [repay]
    return fields


class SplitPaymentTest(unittest.TestCase):
    """loans.split_payment 的单位契约：年利率是 Base 原值（小数），绝不 /100。"""

    def test_split_standard(self):
        # 剩余本金 500000、年利率单元格 0.042、还款总额 8500。
        interest, principal = loans.split_payment(500000, 0.042, 8500)

        self.assertEqual(interest, 1750.0)
        self.assertEqual(principal, 6750.0)
        self.assertEqual(round(interest + principal, 2), 8500.0)

    def test_split_zero_rate(self):
        self.assertEqual(loans.split_payment(500000, 0.0, 8500), (0.0, 8500.0))

    def test_split_rejects_double_conversion(self):
        # 把百分数 4.2 直接传进来（而不是 0.042）必须炸，而不是算出 100× 利息。
        with self.assertRaises(loans.LoanError) as raised:
            loans.split_payment(500000, 4.2, 8500)

        message = str(raised.exception)
        self.assertIn("年利率", message)
        self.assertIn("0.042", message)

    def test_overpayment_raises(self):
        # 利息 1750；本金 = 501750.01 − 1750 = 500000.01 > 剩余本金。
        with self.assertRaises(loans.LoanError) as raised:
            loans.split_payment(500000, 0.042, 501750.01)

        message = str(raised.exception)
        self.assertIn("剩余本金", message)
        self.assertIn("人工核对", message)

    def test_payment_does_not_cover_interest(self):
        # 利息 1750，只还 1000 ⇒ 本金为负。
        with self.assertRaises(loans.LoanError) as raised:
            loans.split_payment(500000, 0.042, 1000)

        message = str(raised.exception)
        self.assertIn("利息", message)
        self.assertIn("-750.0", message)

    def test_total_must_be_positive(self):
        for total in (0, -1):
            with self.subTest(total=total):
                with self.assertRaises(loans.LoanError) as raised:
                    loans.split_payment(500000, 0.042, total)
                self.assertIn("还款总额", str(raised.exception))

    def test_remaining_must_be_positive(self):
        for remaining in (0, -500000):
            with self.subTest(remaining=remaining):
                with self.assertRaises(loans.LoanError) as raised:
                    loans.split_payment(remaining, 0.042, 8500)
                self.assertIn("剩余本金", str(raised.exception))

    def test_rate_outside_zero_one_rejected(self):
        for rate in (-0.01, 1.01, 4.2):
            with self.subTest(rate=rate):
                with self.assertRaises(loans.LoanError):
                    loans.split_payment(500000, rate, 8500)

    def test_non_numeric_inputs_rejected(self):
        for remaining, rate, total in (
            ("x", 0.042, 8500),
            (500000, "x", 8500),
            (500000, 0.042, "x"),
            (True, 0.042, 8500),
            (float("nan"), 0.042, 8500),
        ):
            with self.subTest(remaining=remaining, rate=rate, total=total):
                with self.assertRaises(loans.LoanError):
                    loans.split_payment(remaining, rate, total)

    def test_split_sums_to_total_and_rounds_two_decimals(self):
        cases = (
            (500000, 0.042, 8500),
            (123456.78, 0.0325, 1000),
            (1000000, 0.0435, 9999.99),
            (1, 1.0, 1.0),
            (300000, 0.0, 1234.56),
        )
        for remaining, rate, total in cases:
            with self.subTest(remaining=remaining, rate=rate, total=total):
                interest, principal = loans.split_payment(remaining, rate, total)
                self.assertEqual(interest, round(remaining * rate / 12, 2))
                self.assertEqual(principal, round(total - interest, 2))
                self.assertEqual(round(interest + principal, 2), round(total, 2))
                self.assertEqual(round(interest, 2), interest)
                self.assertEqual(round(principal, 2), principal)
                self.assertLessEqual(principal, remaining)


class LoanRepayPlanTest(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            "acc_bank": account("招行工资卡", 100000.0, "银行卡"),
            "acc_loan": account(
                "房贷", -500000.0, "负债", rate=0.042, repay="等额本息"
            ),
            "acc_cash": account("现金", 1000.0, "现金"),
            "acc_credit": account("信用卡", -2000.0, "负债", rate=0.0),
        }

    def entry(self, **overrides):
        data = {
            "kind": "loan_repay",
            "date": "2026-11-17",
            "amount": 8500,
            "account": "acc_bank",
            "loan_account": "acc_loan",
            "interest_category": "cat_interest",
            "summary": "11月房贷",
            "note": "",
        }
        data.update(overrides)
        return data

    def rows(self, plan):
        return [
            fields for table, fields in plan["create"] if table == "收支明细"
        ]

    def update_for(self, plan, record_id):
        for table, item_id, fields in plan["update"]:
            if table == "账户" and item_id == record_id:
                return fields
        self.fail("plan 中没有账户 %s 的 update" % record_id)

    def tags(self, rows):
        return [
            re.search(r"#G[0-9a-f]{8}", row["备注"]).group(0) for row in rows
        ]

    def test_repay_two_records(self):
        plan = posting.plan_add(self.entry(), self.accounts, {})

        rows = self.rows(plan)
        self.assertEqual(len(rows), 2)
        # ① 利息支出：分类落在 expense 分类上。
        self.assertEqual(rows[0]["类型"], ["支出"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_bank"}])
        self.assertEqual(rows[0]["分类"], [{"id": "cat_interest"}])
        self.assertEqual(rows[0]["金额"], 1750.0)
        self.assertNotIn("转入账户", rows[0])
        # ② 本金转账：银行卡 → 负债账户，不带分类。
        self.assertEqual(rows[1]["类型"], ["转账"])
        self.assertEqual(rows[1]["账户"], [{"id": "acc_bank"}])
        self.assertEqual(rows[1]["转入账户"], [{"id": "acc_loan"}])
        self.assertEqual(rows[1]["金额"], 6750.0)
        self.assertNotIn("分类", rows[1])
        # 同组 tag，备注互相注明拆分。
        tags = self.tags(rows)
        self.assertEqual(tags[0], tags[1])
        for row in rows:
            self.assertIn("本金6750.00+利息1750.00", row["备注"])
        # 本金 + 利息 = 还款总额。
        self.assertEqual(sum(row["金额"] for row in rows), 8500.0)

    def test_missing_rate_is_data_error(self):
        del self.accounts["acc_loan"]["年利率"]

        with self.assertRaises(posting.PostingDataError) as caught:
            posting.plan_add(self.entry(), self.accounts, {})

        self.assertIn("年利率", str(caught.exception))

    def test_repay_balances(self):
        plan = posting.plan_add(self.entry(), self.accounts, {})

        self.assertEqual(
            self.update_for(plan, "acc_bank"), {"当前余额": 91500.0}
        )
        # 负债余额 -500000，只被本金推动：-500000 + 6750 = -493250。
        self.assertEqual(
            self.update_for(plan, "acc_loan"), {"当前余额": -493250.0}
        )
        self.assertEqual(
            {item[1] for item in plan["update"]}, {"acc_bank", "acc_loan"}
        )

    def test_repay_custom_note_keeps_split_text(self):
        plan = posting.plan_add(
            self.entry(note="11月房贷："), self.accounts, {}
        )

        for row in self.rows(plan):
            self.assertTrue(
                row["备注"].startswith("11月房贷：本金6750.00+利息1750.00")
            )

    def test_loan_account_must_be_liability(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(loan_account="acc_cash"), self.accounts, {}
            )

        message = str(raised.exception)
        self.assertIn("负债", message)
        self.assertIn("acc_cash", message)

    def test_loan_account_must_exist(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(loan_account="acc_missing"), self.accounts, {}
            )

        self.assertIn("acc_missing", str(raised.exception))

    def test_loan_account_cannot_be_the_payer(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(loan_account="acc_bank"), self.accounts, {}
            )

        self.assertIn("不能相同", str(raised.exception))

    def test_interest_category_required(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(interest_category=None), self.accounts, {}
            )

        self.assertIn("利息分类", str(raised.exception))

    def test_missing_rate_is_actionable(self):
        self.accounts["acc_loan"].pop("年利率")

        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(self.entry(), self.accounts, {})

        message = str(raised.exception)
        self.assertIn("年利率", message)
        self.assertIn("account edit", message)

    def test_percent_rate_is_rejected_not_divided_again(self):
        self.accounts["acc_loan"]["年利率"] = 4.2

        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(self.entry(), self.accounts, {})

        self.assertIn("年利率", str(raised.exception))

    def test_loan_repay_rejects_transfer_flags(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(to_account="acc_loan"), self.accounts, {}
            )

        self.assertIn("转入账户", str(raised.exception))

    def test_loan_repay_missing_loan_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.plan_add(
                self.entry(loan_account=None), self.accounts, {}
            )

        self.assertIn("负债账户", str(raised.exception))

    # --- 信用卡语义：无需新 kind，用既有 expense / transfer 固化。 ---

    def test_card_repay_is_transfer(self):
        plan = posting.plan_add(
            {
                "kind": "transfer",
                "date": "2026-11-17",
                "amount": 2000,
                "account": "acc_bank",
                "to_account": "acc_credit",
                "summary": "还信用卡",
                "note": "",
            },
            self.accounts,
            {},
        )

        rows = self.rows(plan)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["类型"], ["转账"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_bank"}])
        self.assertEqual(rows[0]["转入账户"], [{"id": "acc_credit"}])
        self.assertNotIn("分类", rows[0])
        self.assertEqual(
            self.update_for(plan, "acc_bank"), {"当前余额": 98000.0}
        )
        # 负债余额 -2000 → 0：还款让负债趋零。
        self.assertEqual(
            self.update_for(plan, "acc_credit"), {"当前余额": 0.0}
        )

    def test_card_spend_more_negative(self):
        plan = posting.plan_add(
            {
                "kind": "expense",
                "date": "2026-11-17",
                "amount": 500,
                "account": "acc_credit",
                "category": "cat_food",
                "summary": "网购",
                "note": "",
            },
            self.accounts,
            {},
        )

        rows = self.rows(plan)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["类型"], ["支出"])
        self.assertEqual(rows[0]["账户"], [{"id": "acc_credit"}])
        self.assertEqual(rows[0]["分类"], [{"id": "cat_food"}])
        self.assertEqual(
            self.update_for(plan, "acc_credit"), {"当前余额": -2500.0}
        )


# ---------------------------------------------------------------------------
# add 流程：实时解析账户/分类，并按序写飞书（全部 mock）。
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


FIXED_INSTANT = datetime.datetime(
    2026, 11, 17, 16, 30, tzinfo=datetime.timezone.utc
)


def flow_account(record_id, name, type_, balance, rate=None, repay=None):
    return {
        "record_id": record_id,
        "fields": account(name, balance, type_, rate=rate, repay=repay),
    }


def flow_category(record_id, full_name, type_):
    return {
        "record_id": record_id,
        "fields": {"分类": full_name, "类型": [type_]},
    }


class LoanFlowTest(unittest.TestCase):
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
            flow_account(
                "recA2",
                "房贷",
                "负债",
                -500000,
                rate=0.042,
                repay="等额本息",
            ),
            flow_account("recA3", "信用卡", "负债", -2000, rate=0.0),
            flow_account("recA4", "现金", "现金", 1000),
        ]
        self.categories = [
            flow_category("recC1", "支出·餐饮·午餐", "支出"),
            flow_category("recC2", "支出·金融·利息支出", "支出"),
            flow_category("recC3", "收入·工资", "收入"),
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
            return []
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
        if table_id != "tbl_account":
            self.fail("更新了未预期的表 %s" % table_id)
        for record_id, fields in updates.items():
            for item in self.accounts:
                if item["record_id"] == record_id:
                    item["fields"].update(copy.deepcopy(fields))
                    break
            else:
                self.fail("更新了不存在的账户 %s" % record_id)

    def account_balance(self, record_id):
        for item in self.accounts:
            if item["record_id"] == record_id:
                return item["fields"]["当前余额"]
        self.fail("没有账户 %s" % record_id)

    def rows_of(self, type_):
        return [
            row["fields"]
            for row in self.entry_rows
            if row["fields"]["类型"] == [type_]
        ]

    def tags(self, rows):
        return [
            re.search(r"#G[0-9a-f]{8}", row["备注"]).group(0) for row in rows
        ]

    def test_acceptance_4(self):
        # spec 验收 4：还房贷 8500（等额本息）→ 利息支出 + 本金转账，
        # 负债只减本金，利息进当月支出；缺省分类为 支出·金融·利息支出。
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "8500",
                    "--account",
                    "招行工资卡",
                    "--loan-account",
                    "房贷",
                    "--summary",
                    "11月房贷",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("已记账", out.getvalue())
        self.assertIn("贷款还款", out.getvalue())
        self.assertEqual(len(self.entry_rows), 2)

        expense_rows = self.rows_of("支出")
        transfer_rows = self.rows_of("转账")
        self.assertEqual(len(expense_rows), 1)
        self.assertEqual(len(transfer_rows), 1)

        interest = expense_rows[0]
        self.assertEqual(interest["金额"], 1750.0)
        self.assertEqual(interest["账户"], [{"id": "recA1"}])
        self.assertEqual(interest["分类"], [{"id": "recC2"}])
        self.assertEqual(interest["日期"], "2026-11-17 00:00")

        principal = transfer_rows[0]
        self.assertEqual(principal["金额"], 6750.0)
        self.assertEqual(principal["账户"], [{"id": "recA1"}])
        self.assertEqual(principal["转入账户"], [{"id": "recA2"}])
        self.assertNotIn("分类", principal)

        tags = self.tags([interest, principal])
        self.assertEqual(tags[0], tags[1])
        for row in (interest, principal):
            self.assertIn("本金6750.00+利息1750.00", row["备注"])

        # 银行只出总额；负债只减本金（-500000 + 6750）。
        self.assertEqual(self.account_balance("recA1"), 91500.0)
        self.assertEqual(self.account_balance("recA2"), -493250.0)
        # 当月支出统计只含利息，本金（转账）不计。
        self.assertEqual(sum(row["金额"] for row in expense_rows), 1750.0)
        self.assertEqual(
            sum(row["金额"] for row in (r["fields"] for r in self.entry_rows)),
            8500.0,
        )

    def test_cli_loan_repay_explicit_category(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "8500",
                    "--account",
                    "招行工资卡",
                    "--loan-account",
                    "房贷",
                    "--category",
                    "支出·餐饮·午餐",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(
            self.rows_of("支出")[0]["分类"], [{"id": "recC1"}]
        )

    def test_cli_loan_repay_requires_loan_account(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--amount",
                    "8500",
                    "--account",
                    "招行工资卡",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--loan-account", err.getvalue())
        self.assertEqual(self.entry_rows, [])
        self.assertFalse(self.calls)

    def test_cli_loan_repay_requires_amount(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--account",
                    "招行工资卡",
                    "--loan-account",
                    "房贷",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--amount", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def test_cli_loan_repay_rejects_transfer_flags(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--amount",
                    "8500",
                    "--account",
                    "招行工资卡",
                    "--loan-account",
                    "房贷",
                    "--to-account",
                    "现金",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--to-account", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def test_cli_other_kind_rejects_loan_account(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "招行工资卡",
                    "--category",
                    "支出·餐饮·午餐",
                    "--loan-account",
                    "房贷",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--loan-account", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def test_cli_rejects_percent_rate(self):
        self.accounts[1]["fields"]["年利率"] = 4.2

        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "loan_repay",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "8500",
                    "--account",
                    "招行工资卡",
                    "--loan-account",
                    "房贷",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("年利率", err.getvalue())
        self.assertEqual(self.entry_rows, [])

    def test_flow_rejects_non_liability_loan_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            add_flow.execute(
                self.cfg,
                {
                    "kind": "loan_repay",
                    "date": "2026-11-17",
                    "amount": 8500,
                    "account": "招行工资卡",
                    "loan_account": "现金",
                    "category": "支出·金融·利息支出",
                },
            )

        self.assertIn("负债", str(raised.exception))
        self.assertEqual(self.entry_rows, [])


if __name__ == "__main__":
    unittest.main()