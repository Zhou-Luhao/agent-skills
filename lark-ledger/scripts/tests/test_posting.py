"""记账核心纯逻辑：不 mock 网络、不读配置，只验证算术与 WritePlan 契约。"""

import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import posting


def account(name, balance, type_="现金"):
    """账户 id → 字段 dict（与 master_data 落库字段一致）。"""
    return {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(balance),
        "当前余额": float(balance),
        "状态": ["在用"],
        "备注": "",
    }


class PostingTestBase(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            "acc_cash": account("现金", 1000.0),
            "acc_bank": account("招行工资卡", 8000.0, "银行卡"),
            "acc_credit": account("信用卡", -2000.0, "负债"),
        }

    def entry(self, **overrides):
        entry = {
            "kind": "expense",
            "date": "2026-11-17",
            "amount": 35,
            "account": "acc_cash",
            "category": "cat_food",
            "summary": "午饭",
            "note": "",
        }
        entry.update(overrides)
        return entry

    def update_for(self, plan, account_id):
        for table, record_id, fields in plan["update"]:
            if table == "账户" and record_id == account_id:
                return fields
        self.fail("plan 中没有账户 %s 的 update" % account_id)


class PlanAddTest(PostingTestBase):
    def test_expense_debits_account(self):
        plan = posting.plan_add(self.entry(amount=35), self.accounts, {})

        self.assertEqual(
            self.update_for(plan, "acc_cash"), {"当前余额": 965.0}
        )

    def test_income_credits_account(self):
        plan = posting.plan_add(
            self.entry(kind="income", amount=250.5), self.accounts, {}
        )

        self.assertEqual(
            self.update_for(plan, "acc_cash"), {"当前余额": 1250.5}
        )

    def test_transfer_moves_both(self):
        plan = posting.plan_add(
            self.entry(
                kind="transfer",
                amount=100,
                account="acc_bank",
                to_account="acc_cash",
                category=None,
            ),
            self.accounts,
            {},
        )

        self.assertEqual(
            self.update_for(plan, "acc_bank"), {"当前余额": 7900.0}
        )
        self.assertEqual(
            self.update_for(plan, "acc_cash"), {"当前余额": 1100.0}
        )
        table, fields = plan["create"][0]
        self.assertEqual(table, "收支明细")
        self.assertEqual(fields["类型"], ["转账"])
        self.assertEqual(fields["账户"], [{"id": "acc_bank"}])
        self.assertEqual(fields["转入账户"], [{"id": "acc_cash"}])
        self.assertNotIn("分类", fields)

    def test_card_spend_more_negative(self):
        plan = posting.plan_add(
            self.entry(amount=500, account="acc_credit"), self.accounts, {}
        )

        self.assertEqual(
            self.update_for(plan, "acc_credit"), {"当前余额": -2500.0}
        )

    def test_rounding_two_decimals(self):
        accounts = {"acc_cash": account("现金", 0.1)}
        plan = posting.plan_add(
            self.entry(kind="income", amount=0.2, account="acc_cash"),
            accounts,
            {},
        )

        balance = self.update_for(plan, "acc_cash")["当前余额"]
        self.assertEqual(balance, 0.3)
        self.assertNotEqual(repr(balance), "0.30000000000000004")

    def test_plan_shape(self):
        plan = posting.plan_add(self.entry(), self.accounts, {})

        self.assertIsInstance(plan["create"], list)
        self.assertIsInstance(plan["update"], list)
        table, fields = plan["create"][0]
        self.assertEqual(table, "收支明细")
        # date 保持字符串，ms 转换留给 I/O 层。
        self.assertEqual(fields["日期"], "2026-11-17")
        self.assertEqual(fields["金额"], 35.0)
        self.assertEqual(fields["分类"], [{"id": "cat_food"}])
        self.assertIn(
            ("账户", "acc_cash", {"当前余额": 965.0}), plan["update"]
        )

    def test_balance_deltas(self):
        self.assertEqual(
            posting.balance_deltas(self.entry(), self.accounts),
            {"acc_cash": -35.0},
        )
        self.assertEqual(
            posting.balance_deltas(
                self.entry(
                    kind="transfer",
                    account="acc_bank",
                    to_account="acc_cash",
                ),
                self.accounts,
            ),
            {"acc_bank": -35.0, "acc_cash": 35.0},
        )


class ValidateEntryTest(PostingTestBase):
    def test_validate_rejects_zero_amount(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(self.entry(amount=0), self.accounts)
        self.assertIn("金额", str(raised.exception))

        with self.assertRaises(posting.PostingError):
            posting.validate_entry(self.entry(amount=-1), self.accounts)

    def test_validate_transfer_needs_target(self):
        entry = self.entry(kind="transfer", to_account=None, category=None)

        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(entry, self.accounts)
        self.assertIn("转入账户", str(raised.exception))

    def test_validate_transfer_rejects_same_account(self):
        entry = self.entry(
            kind="transfer", to_account="acc_cash", category=None
        )

        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(entry, self.accounts)
        self.assertIn("不能相同", str(raised.exception))

    def test_validate_rejects_unknown_account(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(
                self.entry(account="acc_missing"), self.accounts
            )
        self.assertIn("acc_missing", str(raised.exception))

        with self.assertRaises(posting.PostingError):
            posting.validate_entry(
                self.entry(
                    kind="transfer",
                    to_account="acc_missing",
                    category=None,
                ),
                self.accounts,
            )

    def test_validate_income_needs_category(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(
                self.entry(kind="income", category=None), self.accounts
            )
        self.assertIn("分类", str(raised.exception))

    def test_validate_rejects_unknown_kind(self):
        with self.assertRaises(posting.PostingError) as raised:
            posting.validate_entry(
                self.entry(kind="invest_buy"), self.accounts
            )
        self.assertIn("invest_buy", str(raised.exception))


class HoldingMathTest(unittest.TestCase):
    def test_weighted_cost(self):
        self.assertEqual(posting.weighted_cost(1000, 10, 1000, 12), 11.0)

    def test_weighted_cost_rounds_four(self):
        self.assertEqual(posting.weighted_cost(3, 1, 1, 2), 1.25)

    def test_realized_pnl(self):
        self.assertEqual(posting.realized_pnl(1200, 100, 10), 200.0)
        self.assertEqual(posting.realized_pnl(800, 100, 10), -200.0)

    def test_group_tag_shape(self):
        tag = posting.group_tag()

        self.assertRegex(tag, r"^ #G[0-9a-f]{8}$")
        self.assertIsNotNone(re.match(r"^ #G[0-9a-f]{8}$", tag))

    def test_group_tag_uses_random_bits(self):
        with mock.patch.object(
            posting.random, "getrandbits", return_value=0x1A2B3C4D
        ):
            self.assertEqual(posting.group_tag(), " #G1a2b3c4d")


if __name__ == "__main__":
    unittest.main()