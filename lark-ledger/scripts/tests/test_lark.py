"""lark 模块：lark-cli 子进程封装的参数拼装与返回值解析。

所有测试都 mock `subprocess.run`，不触碰真实 CLI 或网络。
"""

import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import lark


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["lark-cli"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _envelope(data):
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)


def _cmd(run, index=0):
    return run.call_args_list[index][0][0]


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def _matrix(record_ids, field_names, rows, has_more=False):
    return {
        "data": rows,
        "fields": field_names,
        "record_id_list": record_ids,
        "has_more": has_more,
    }


class RunCliTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_ok_returns_data(self, run):
        run.return_value = _proc(_envelope({"x": 1}))

        self.assertEqual(lark.run_cli(["base", "+table-list"]), {"x": 1})
        self.assertEqual(
            _cmd(run), ["lark-cli", "base", "+table-list", "--format", "json"]
        )
        self.assertEqual(
            run.call_args.kwargs,
            {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "timeout": 60,
            },
        )

    @mock.patch("lark.subprocess.run")
    def test_not_ok_raises(self, run):
        run.return_value = _proc(
            json.dumps(
                {"ok": False, "error": {"code": 99991663, "message": "no auth"}}
            )
        )

        with self.assertRaises(lark.LarkError) as ctx:
            lark.run_cli(["base", "+table-list"])
        self.assertIn("no auth", str(ctx.exception))
        self.assertEqual(ctx.exception.payload["error"]["code"], 99991663)

    @mock.patch("lark.subprocess.run")
    def test_nonzero_exit_raises(self, run):
        run.return_value = _proc(_envelope({"x": 1}), returncode=2, stderr="boom")

        with self.assertRaises(lark.LarkError) as ctx:
            lark.run_cli(["base", "+table-list"])
        self.assertIn("exit 2", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    @mock.patch("lark.subprocess.run")
    def test_bad_json_raises(self, run):
        run.return_value = _proc("not json", stderr="oops")

        with self.assertRaises(lark.LarkError) as ctx:
            lark.run_cli(["base", "+table-list"])
        self.assertIn("JSON", str(ctx.exception))
        self.assertEqual(ctx.exception.payload["stdout"], "not json")
        self.assertEqual(ctx.exception.payload["stderr"], "oops")

    @mock.patch("lark.subprocess.run")
    def test_missing_binary_raises_lark_error(self, run):
        run.side_effect = FileNotFoundError("lark-cli not found")

        with self.assertRaises(lark.LarkError) as ctx:
            lark.run_cli(["auth", "status"])
        self.assertIn("lark-cli", str(ctx.exception))
        self.assertEqual(
            ctx.exception.payload["args"], ["lark-cli", "auth", "status", "--format", "json"]
        )

    @mock.patch("lark.subprocess.run")
    def test_unicode_decode_error_raises_lark_error(self, run):
        run.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "invalid start byte"
        )

        with self.assertRaises(lark.LarkError) as ctx:
            lark.run_cli(["base", "+table-list"])
        self.assertIn("invalid start byte", str(ctx.exception))
        self.assertEqual(
            ctx.exception.payload["args"],
            ["lark-cli", "base", "+table-list", "--format", "json"],
        )

    @mock.patch("lark.subprocess.run")
    def test_missing_data_raises(self, run):
        run.return_value = _proc('{"ok": true}')

        with self.assertRaises(lark.LarkError):
            lark.run_cli(["base", "+table-list"])


class TimeTest(unittest.TestCase):
    def test_to_ms_utc8_midnight(self):
        self.assertEqual(lark.to_ms("2026-11-17"), 1794844800000)

    def test_to_ms_from_ms_roundtrip(self):
        self.assertEqual(lark.from_ms(lark.to_ms("2026-11-17")), "2026-11-17")

    def test_from_ms_utc8_date(self):
        self.assertEqual(lark.from_ms(1794844800000), "2026-11-17")

    def test_date_cell_text_accepts_str_and_ms(self):
        self.assertEqual(lark.date_cell_text("2026-11-17"), "2026-11-17")
        self.assertEqual(
            lark.date_cell_text("2026-11-17 00:00"), "2026-11-17"
        )
        self.assertEqual(
            lark.date_cell_text(1794844800000), "2026-11-17"
        )
        self.assertEqual(lark.date_cell_text(None), "")
        self.assertEqual(lark.date_cell_text(True), "")


class AuthTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_ready_user(self, run):
        run.return_value = _proc(
            json.dumps({"identities": {"user": {"status": "ready"}}})
        )

        self.assertTrue(lark.auth_ok())
        # auth status 只支持 --json，不支持 --format json
        self.assertEqual(_cmd(run), ["lark-cli", "auth", "status", "--json"])

    @mock.patch("lark.subprocess.run")
    def test_idle_session_needs_refresh_is_logged_in(self, run):
        # 真实机器上 access token 过期、下一次调用自动刷新时的信封。
        run.return_value = _proc(
            json.dumps(
                {
                    "identities": {
                        "user": {
                            "status": "needs_refresh",
                            "tokenStatus": "needs_refresh",
                            "available": True,
                        }
                    }
                }
            )
        )

        self.assertTrue(lark.auth_ok())

    @mock.patch("lark.subprocess.run")
    def test_needs_refresh_unavailable_is_false(self, run):
        run.return_value = _proc(
            json.dumps(
                {
                    "identities": {
                        "user": {
                            "status": "needs_refresh",
                            "tokenStatus": "needs_refresh",
                            "available": False,
                        }
                    }
                }
            )
        )

        self.assertFalse(lark.auth_ok())

    @mock.patch("lark.subprocess.run")
    def test_user_not_ready(self, run):
        run.return_value = _proc(
            json.dumps({"identities": {"user": {"status": "expired"}}})
        )

        self.assertFalse(lark.auth_ok())

    @mock.patch("lark.subprocess.run")
    def test_nonzero_exit_is_false(self, run):
        run.return_value = _proc("", returncode=1, stderr="not logged in")

        self.assertFalse(lark.auth_ok())

    @mock.patch("lark.subprocess.run")
    def test_bad_json_is_false(self, run):
        run.return_value = _proc("not json")

        self.assertFalse(lark.auth_ok())

    @mock.patch("lark.subprocess.run")
    def test_missing_binary_is_false(self, run):
        run.side_effect = FileNotFoundError("lark-cli not found")

        self.assertFalse(lark.auth_ok())


class CreateBaseLibraryTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_extracts_tokens(self, run):
        run.return_value = _proc(
            _envelope(
                {
                    "resolved_space_id": "7315",
                    "resolved_by": "my_library",
                    "node_token": "nod1",
                    "obj_token": "app1",
                    "obj_type": "bitable",
                    "node_type": "origin",
                    "title": "个人账本",
                }
            )
        )

        self.assertEqual(
            lark.create_base_library("个人账本"),
            {"app_token": "app1", "node_token": "nod1"},
        )
        self.assertEqual(
            _cmd(run),
            [
                "lark-cli",
                "wiki",
                "+node-create",
                "--space-id",
                "my_library",
                "--obj-type",
                "bitable",
                "--title",
                "个人账本",
                "--format",
                "json",
            ],
        )

    @mock.patch("lark.subprocess.run")
    def test_missing_obj_token_raises_with_payload(self, run):
        payload = {"node_token": "nod1", "title": "个人账本"}
        run.return_value = _proc(_envelope(payload))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.create_base_library("个人账本")
        self.assertEqual(ctx.exception.payload, payload)


class CreateBaseFolderTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_flat_app_token(self, run):
        run.return_value = _proc(_envelope({"app_token": "app1"}))

        self.assertEqual(lark.create_base_folder("个人账本", "fld1"), "app1")
        self.assertEqual(
            _cmd(run),
            [
                "lark-cli",
                "base",
                "+base-create",
                "--name",
                "个人账本",
                "--folder-token",
                "fld1",
                "--format",
                "json",
            ],
        )

    @mock.patch("lark.subprocess.run")
    def test_nested_app_object(self, run):
        run.return_value = _proc(_envelope({"app": {"app_token": "app2"}}))

        self.assertEqual(lark.create_base_folder("个人账本", "fld1"), "app2")

    @mock.patch("lark.subprocess.run")
    def test_flat_base_token(self, run):
        # lark-base reference 把 +base-create 的返回值叫 base_token。
        run.return_value = _proc(_envelope({"base_token": "bascn1"}))

        self.assertEqual(lark.create_base_folder("个人账本", "fld1"), "bascn1")

    @mock.patch("lark.subprocess.run")
    def test_nested_base_token(self, run):
        run.return_value = _proc(_envelope({"base": {"base_token": "bascn2"}}))

        self.assertEqual(lark.create_base_folder("个人账本", "fld1"), "bascn2")

    @mock.patch("lark.subprocess.run")
    def test_missing_app_token_raises(self, run):
        run.return_value = _proc(_envelope({"url": "https://example"}))

        with self.assertRaises(lark.LarkError):
            lark.create_base_folder("个人账本", "fld1")


class CreateTableTest(unittest.TestCase):
    FIELDS = [
        {"name": "日期", "type": "datetime", "style": {"format": "yyyy-MM-dd"}},
        {"name": "金额", "type": "number"},
    ]

    @mock.patch("lark.subprocess.run")
    def test_returns_table_id_and_pins_fields_json(self, run):
        run.return_value = _proc(_envelope({"table_id": "tbl1", "name": "收支明细"}))

        self.assertEqual(
            lark.create_table("app1", "收支明细", self.FIELDS), "tbl1"
        )
        cmd = _cmd(run)
        self.assertEqual(
            cmd[:5],
            ["lark-cli", "base", "+table-create", "--base-token", "app1"],
        )
        self.assertEqual(_flag(cmd, "--name"), "收支明细")
        self.assertEqual(json.loads(_flag(cmd, "--fields")), self.FIELDS)
        self.assertEqual(cmd[-2:], ["--format", "json"])

    @mock.patch("lark.subprocess.run")
    def test_nested_table_id(self, run):
        run.return_value = _proc(_envelope({"table": {"id": "tbl2"}}))

        self.assertEqual(lark.create_table("app1", "账户", self.FIELDS), "tbl2")

    @mock.patch("lark.subprocess.run")
    def test_missing_table_id_raises_with_payload(self, run):
        payload = {"name": "账户"}
        run.return_value = _proc(_envelope(payload))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.create_table("app1", "账户", self.FIELDS)
        self.assertEqual(ctx.exception.payload, payload)


class DeleteTableTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_deletes_with_yes(self, run):
        run.return_value = _proc(_envelope({"deleted": True}))

        lark.delete_table("app1", "tbl1")

        self.assertEqual(
            _cmd(run),
            [
                "lark-cli",
                "base",
                "+table-delete",
                "--base-token",
                "app1",
                "--table-id",
                "tbl1",
                "--yes",
                "--format",
                "json",
            ],
        )


class ListTablesTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_tables_key_and_id_alias(self, run):
        run.return_value = _proc(
            _envelope(
                {"tables": [{"id": "tbl1", "name": "账户"}], "total": 1}
            )
        )

        self.assertEqual(
            lark.list_tables("app1"),
            [{"id": "tbl1", "name": "账户", "table_id": "tbl1"}],
        )
        self.assertEqual(
            _cmd(run),
            ["lark-cli", "base", "+table-list", "--base-token", "app1", "--format", "json"],
        )

    @mock.patch("lark.subprocess.run")
    def test_items_key_fallback(self, run):
        run.return_value = _proc(
            _envelope({"items": [{"table_id": "tbl1", "name": "账户"}]})
        )

        self.assertEqual(
            lark.list_tables("app1"),
            [{"table_id": "tbl1", "name": "账户"}],
        )

    @mock.patch("lark.subprocess.run")
    def test_empty(self, run):
        run.return_value = _proc(_envelope({"tables": [], "total": 0}))

        self.assertEqual(lark.list_tables("app1"), [])

    @mock.patch("lark.subprocess.run")
    def test_missing_tables_key_raises_with_payload(self, run):
        payload = {"total": 0}
        run.return_value = _proc(_envelope(payload))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.list_tables("app1")
        self.assertEqual(ctx.exception.payload, payload)


class ListFieldsTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_fields_key_and_id_alias(self, run):
        run.return_value = _proc(
            _envelope(
                {
                    "fields": [
                        {"id": "fld1", "name": "日期", "type": "datetime"}
                    ],
                    "total": 1,
                }
            )
        )

        self.assertEqual(
            lark.list_fields("app1", "tbl1"),
            [{"id": "fld1", "name": "日期", "type": "datetime", "field_id": "fld1"}],
        )
        self.assertEqual(
            _cmd(run),
            [
                "lark-cli",
                "base",
                "+field-list",
                "--base-token",
                "app1",
                "--table-id",
                "tbl1",
                "--format",
                "json",
            ],
        )

    @mock.patch("lark.subprocess.run")
    def test_empty(self, run):
        run.return_value = _proc(_envelope({"fields": [], "total": 0}))

        self.assertEqual(lark.list_fields("app1", "tbl1"), [])

    @mock.patch("lark.subprocess.run")
    def test_missing_fields_key_raises_with_payload(self, run):
        payload = {"total": 0}
        run.return_value = _proc(_envelope(payload))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.list_fields("app1", "tbl1")
        self.assertEqual(ctx.exception.payload, payload)


class CreateRecordsTest(unittest.TestCase):
    RECORDS = [{"日期": "2026-11-17 00:00", "金额": 35.0}, {"金额": 12.5}]

    @mock.patch("lark.subprocess.run")
    def test_single_batch_body_and_ids(self, run):
        run.return_value = _proc(_envelope({"record_id_list": ["rec1", "rec2"]}))

        self.assertEqual(
            lark.create_records("app1", "tbl1", self.RECORDS), ["rec1", "rec2"]
        )
        cmd = _cmd(run)
        self.assertEqual(
            cmd[:5],
            [
                "lark-cli",
                "base",
                "+record-batch-create",
                "--base-token",
                "app1",
            ],
        )
        self.assertEqual(_flag(cmd, "--table-id"), "tbl1")
        self.assertEqual(
            json.loads(_flag(cmd, "--json")), {"create_records": self.RECORDS}
        )

    @mock.patch("lark.subprocess.run")
    def test_records_array_fallback(self, run):
        run.return_value = _proc(
            _envelope({"records": [{"record_id": "rec1", "fields": {}}]})
        )

        self.assertEqual(
            lark.create_records("app1", "tbl1", [self.RECORDS[0]]), ["rec1"]
        )

    @mock.patch("lark.subprocess.run")
    def test_splits_over_200(self, run):
        records = [{"序号": i} for i in range(201)]
        first_ids = ["rec%03d" % i for i in range(200)]
        run.side_effect = [
            _proc(_envelope({"record_id_list": first_ids})),
            _proc(_envelope({"record_id_list": ["rec200"]})),
        ]

        ids = lark.create_records("app1", "tbl1", records)

        self.assertEqual(len(ids), 201)
        self.assertEqual(ids[-1], "rec200")
        self.assertEqual(run.call_count, 2)
        first = json.loads(_flag(_cmd(run, 0), "--json"))["create_records"]
        second = json.loads(_flag(_cmd(run, 1), "--json"))["create_records"]
        self.assertEqual(len(first), 200)
        self.assertEqual(first, records[:200])
        self.assertEqual(second, records[200:])

    @mock.patch("lark.subprocess.run")
    def test_empty_records_skips_cli(self, run):
        self.assertEqual(lark.create_records("app1", "tbl1", []), [])
        run.assert_not_called()

    @mock.patch("lark.subprocess.run")
    def test_missing_record_ids_raises(self, run):
        run.return_value = _proc(_envelope({"records": []}))

        with self.assertRaises(lark.LarkError):
            lark.create_records("app1", "tbl1", self.RECORDS)

    @mock.patch("lark.subprocess.run")
    def test_partial_id_response_raises(self, run):
        run.return_value = _proc(_envelope({"record_id_list": ["rec1"]}))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.create_records("app1", "tbl1", self.RECORDS)
        self.assertEqual(ctx.exception.payload, {"record_id_list": ["rec1"]})


class UpdateRecordTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_single_update_body(self, run):
        run.return_value = _proc(_envelope({}))
        fields = {"当前余额": 965.5}

        self.assertIsNone(lark.update_record("app1", "tbl1", "rec1", fields))

        cmd = _cmd(run)
        self.assertEqual(
            cmd[:5],
            [
                "lark-cli",
                "base",
                "+record-batch-update",
                "--base-token",
                "app1",
            ],
        )
        self.assertEqual(_flag(cmd, "--table-id"), "tbl1")
        self.assertEqual(
            json.loads(_flag(cmd, "--json")),
            {"update_records": {"rec1": fields}},
        )

    @mock.patch("lark.subprocess.run")
    def test_update_records_splits_over_200(self, run):
        updates = {"rec%03d" % i: {"序号": i} for i in range(201)}
        run.side_effect = [_proc(_envelope({})), _proc(_envelope({}))]

        self.assertIsNone(lark.update_records("app1", "tbl1", updates))

        self.assertEqual(run.call_count, 2)
        first = json.loads(_flag(_cmd(run, 0), "--json"))["update_records"]
        second = json.loads(_flag(_cmd(run, 1), "--json"))["update_records"]
        self.assertEqual(len(first), 200)
        self.assertEqual(len(second), 1)
        self.assertEqual({**first, **second}, updates)

    @mock.patch("lark.subprocess.run")
    def test_empty_updates_skips_cli(self, run):
        self.assertIsNone(lark.update_records("app1", "tbl1", {}))
        run.assert_not_called()


class DeleteRecordsTest(unittest.TestCase):
    @mock.patch("lark.subprocess.run")
    def test_builds_record_id_flags_and_yes(self, run):
        run.return_value = _proc(_envelope({}))

        self.assertIsNone(lark.delete_records("app1", "tbl1", ["rec1", "rec2"]))

        cmd = _cmd(run)
        self.assertEqual(
            cmd[:5],
            [
                "lark-cli",
                "base",
                "+record-delete",
                "--base-token",
                "app1",
            ],
        )
        self.assertEqual(_flag(cmd, "--table-id"), "tbl1")
        self.assertEqual(
            [cmd[i + 1] for i, token in enumerate(cmd) if token == "--record-id"],
            ["rec1", "rec2"],
        )
        self.assertEqual(cmd.count("--yes"), 1)
        self.assertEqual(cmd[-2:], ["--format", "json"])

    @mock.patch("lark.subprocess.run")
    def test_empty_ids_skips_cli(self, run):
        self.assertIsNone(lark.delete_records("app1", "tbl1", []))
        run.assert_not_called()


class SearchTest(unittest.TestCase):
    ROWS = [["2026-11-17", ["支出"], 35.0]]
    NAMES = ["日期", "类型", "金额"]

    @mock.patch("lark.subprocess.run")
    def test_conditions_in_filter_json_and_matrix_conversion(self, run):
        run.return_value = _proc(_envelope(_matrix(["rec1"], self.NAMES, self.ROWS)))
        conditions = [["日期", "==", "2026-11-17"]]

        result = lark.search_records(
            "app1", "tbl1", conditions=conditions, limit=10
        )

        self.assertEqual(
            result,
            [
                {
                    "record_id": "rec1",
                    "fields": {"日期": "2026-11-17", "类型": ["支出"], "金额": 35.0},
                }
            ],
        )
        cmd = _cmd(run)
        self.assertEqual(
            cmd[:5],
            ["lark-cli", "base", "+record-list", "--base-token", "app1"],
        )
        self.assertEqual(_flag(cmd, "--table-id"), "tbl1")
        self.assertEqual(_flag(cmd, "--limit"), "10")
        self.assertEqual(_flag(cmd, "--offset"), "0")
        self.assertEqual(
            json.loads(_flag(cmd, "--filter-json")),
            {"logic": "and", "conditions": conditions},
        )

    @mock.patch("lark.subprocess.run")
    def test_no_conditions_omits_filter(self, run):
        run.return_value = _proc(_envelope(_matrix([], [], [])))

        self.assertEqual(lark.search_records("app1", "tbl1", limit=50), [])

        cmd = _cmd(run)
        self.assertNotIn("--filter-json", cmd)
        self.assertEqual(_flag(cmd, "--limit"), "50")

    @mock.patch("lark.subprocess.run")
    def test_explicit_offset_starts_there(self, run):
        run.return_value = _proc(_envelope(_matrix([], [], [])))

        lark.search_records("app1", "tbl1", limit=50, offset=120)

        self.assertEqual(_flag(_cmd(run), "--offset"), "120")

    @mock.patch("lark.subprocess.run")
    def test_pagination_advances_by_page_size(self, run):
        first_ids = ["rec%03d" % i for i in range(200)]
        first_rows = [[i] for i in range(200)]
        run.side_effect = [
            _proc(_envelope(_matrix(first_ids, ["序号"], first_rows, has_more=True))),
            _proc(_envelope(_matrix(["rec200"], ["序号"], [[200]]))),
        ]

        result = lark.search_records("app1", "tbl1", limit=2000)

        self.assertEqual(len(result), 201)
        self.assertEqual(result[0]["record_id"], "rec000")
        self.assertEqual(result[-1], {"record_id": "rec200", "fields": {"序号": 200}})
        self.assertEqual(run.call_count, 2)
        first, second = _cmd(run, 0), _cmd(run, 1)
        self.assertEqual((_flag(first, "--limit"), _flag(first, "--offset")), ("200", "0"))
        self.assertEqual((_flag(second, "--limit"), _flag(second, "--offset")), ("200", "200"))

    @mock.patch("lark.subprocess.run")
    def test_last_page_respects_remaining_limit(self, run):
        first_ids = ["rec%03d" % i for i in range(200)]
        first_rows = [[i] for i in range(200)]
        run.side_effect = [
            _proc(_envelope(_matrix(first_ids, ["序号"], first_rows, has_more=True))),
            _proc(_envelope(_matrix(["rec200"], ["序号"], [[200]]))),
        ]

        result = lark.search_records("app1", "tbl1", limit=205)

        self.assertEqual(len(result), 201)
        self.assertEqual(_flag(_cmd(run, 1), "--limit"), "5")

    @mock.patch("lark.subprocess.run")
    def test_empty_rows_without_record_id_list(self, run):
        for payload in (
            {"data": [], "fields": ["日期"], "has_more": False},
            {"data": [], "fields": ["日期"], "record_id_list": None, "has_more": False},
        ):
            with self.subTest(payload=payload):
                run.return_value = _proc(_envelope(payload))
                self.assertEqual(lark.search_records("app1", "tbl1"), [])

    @mock.patch("lark.subprocess.run")
    def test_zero_limit_skips_cli(self, run):
        self.assertEqual(lark.search_records("app1", "tbl1", limit=0), [])
        run.assert_not_called()

    @mock.patch("lark.subprocess.run")
    def test_malformed_matrix_raises_with_payload(self, run):
        payload = {"data": [], "fields": ["日期"], "record_id_list": ["rec1"]}
        run.return_value = _proc(_envelope(payload))

        with self.assertRaises(lark.LarkError) as ctx:
            lark.search_records("app1", "tbl1")
        self.assertEqual(ctx.exception.payload, payload)


if __name__ == "__main__":
    unittest.main()
