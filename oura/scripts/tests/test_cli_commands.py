import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura

D = __import__("datetime").date


class ReportDirTest(unittest.TestCase):
    def test_out_flag_wins(self):
        self.assertEqual(oura.resolve_report_dir("/tmp/x"), pathlib.Path("/tmp/x"))

    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"OURA_REPORT_DIR": "/tmp/envdir"}):
            self.assertEqual(oura.resolve_report_dir(None), pathlib.Path("/tmp/envdir"))


class DatasetMapTest(unittest.TestCase):
    def test_fourteen_datasets(self):
        self.assertEqual(len(oura.DATASET_TO_ENDPOINTS), 14)

    def test_tag_maps_to_both_endpoints(self):
        self.assertEqual(set(oura.DATASET_TO_ENDPOINTS["tag"]), {"tag", "enhanced_tag"})

    def test_sleep_maps_to_both(self):
        self.assertEqual(set(oura.DATASET_TO_ENDPOINTS["sleep"]), {"daily_sleep", "sleep"})


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HOME": self.tmp.name, "OURA_CACHE_DIR": self.tmp.name,
            "OURA_REPORT_DIR": self.tmp.name,
            "OURA_CLIENT_ID": "cid", "OURA_CLIENT_SECRET": "cs"})
        self.env.start()
        self.browser = patch("oura.webbrowser.open", return_value=True)
        self.browser.start()

    def tearDown(self):
        self.browser.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oura.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_token_exit_3(self):
        code, _, _ = self._run(["check"])
        self.assertEqual(code, 3)

    def test_query_list_works_without_credentials(self):
        with patch.dict(os.environ, {"OURA_CLIENT_ID": "", "OURA_CLIENT_SECRET": ""}):
            code, out, _ = self._run(["query", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("sleep", out)

    def test_invalid_date_exit_2(self):
        code, _, _ = self._run(["daily", "--date", "2026-02-30"])
        self.assertEqual(code, 2)

    def test_empty_window_exit_5(self):
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", return_value={"fetched_at": 0, "documents": [],
                                                    "fetched_ranges": []}):
            code, _, err = self._run(["daily", "--date", "2026-11-10"])
        self.assertEqual(code, 5)
        self.assertIn("无数据", err)

    def test_missing_sync_hint_in_error(self):
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", return_value={"fetched_at": 0, "documents": [],
                                                    "fetched_ranges": []}):
            _, _, err = self._run(["daily", "--date", "2026-11-10"])
        self.assertIn("同步", err)

    def test_daily_writes_html_and_md(self):
        fix = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures"
                          / "daily_full.json").read_text())
        caches = {ep: {"fetched_at": 9e12, "fetched_ranges": [], "documents": docs}
                  for ep, docs in fix.items()}
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", side_effect=lambda ep: caches.get(
                 ep, {"fetched_at": 9e12, "fetched_ranges": [], "documents": []})):
            code, out, _ = self._run(["daily", "--date", "2026-11-10"])
        self.assertEqual(code, 0)
        self.assertIn(".html", out)
        written = list(pathlib.Path(self.tmp.name).iterdir())
        self.assertTrue(any(p.suffix == ".html" for p in written))
        self.assertTrue(any(p.suffix == ".md" for p in written))

    def test_weekly_writes_report(self):
        fix = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures"
                          / "daily_full.json").read_text())
        caches = {ep: {"fetched_at": 9e12, "fetched_ranges": [], "documents": docs}
                  for ep, docs in fix.items()}
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", side_effect=lambda ep: caches.get(
                 ep, {"fetched_at": 9e12, "fetched_ranges": [], "documents": []})):
            code, out, _ = self._run(["weekly", "--date", "2026-11-10"])
        self.assertEqual(code, 0)
        names = [p.name for p in pathlib.Path(self.tmp.name).iterdir()]
        self.assertTrue(any("weekly" in n for n in names))

    def test_query_prints_table(self):
        fix = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures"
                          / "daily_full.json").read_text())
        caches = {ep: {"fetched_at": 9e12, "fetched_ranges": [], "documents": docs}
                  for ep, docs in fix.items()}
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", side_effect=lambda ep: caches.get(
                 ep, {"fetched_at": 9e12, "fetched_ranges": [], "documents": []})):
            code, out, _ = self._run(["query", "sleep", "--start", "2026-11-10",
                                      "--end", "2026-11-10"])
        self.assertEqual(code, 0)
        self.assertIn("2026-11-10", out)
        self.assertIn("82", out)

    def test_query_json_outputs_documents(self):
        fix = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures"
                          / "daily_full.json").read_text())
        caches = {ep: {"fetched_at": 9e12, "fetched_ranges": [], "documents": docs}
                  for ep, docs in fix.items()}
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", side_effect=lambda ep: caches.get(
                 ep, {"fetched_at": 9e12, "fetched_ranges": [], "documents": []})):
            code, out, _ = self._run(["query", "spo2", "--start", "2026-11-10",
                                      "--end", "2026-11-10", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIn("daily_spo2", parsed)

    def test_check_reports_credential_presence_without_values(self):
        with patch("oura.ensure_valid_token", return_value="tok-1234"), \
             patch("oura._probe_personal_info",
                   return_value={"age": 30, "height": 180.0, "weight": 70.0}):
            code, out, _ = self._run(["check"])
        self.assertEqual(code, 0)
        self.assertIn("OURA_CLIENT_ID", out)
        self.assertNotIn("cs", out.replace("OURA_CLIENT_SECRET", ""))


if __name__ == "__main__":
    unittest.main()

class ContractTest(unittest.TestCase):
    """I2/I3/I4/I5/M3/M4: 退出码与文档承诺的契约。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HOME": self.tmp.name, "OURA_CACHE_DIR": self.tmp.name,
            "OURA_REPORT_DIR": self.tmp.name,
            "OURA_CLIENT_ID": "cid", "OURA_CLIENT_SECRET": "cs"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oura.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_fetch_accepts_refresh_flag(self):
        parser = oura.build_parser()
        args = parser.parse_args(["fetch", "--refresh"])
        self.assertTrue(args.refresh)

    def test_refresh_failure_exit_3_with_authorize_hint(self):
        import oura_auth
        with patch("oura.ensure_valid_token",
                   side_effect=oura_auth.RefreshFailed("refresh token 已失效")):
            code, _, err = self._run(["check"])
        self.assertEqual(code, 3)
        self.assertIn("authorize", err)

    def test_network_error_exit_4_not_traceback(self):
        from oura_client import ApiError
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=True), \
             patch("oura.fetch_endpoint", side_effect=ApiError(0, "无法连接 Oura API")):
            code, _, err = self._run(["fetch", "--start", "2026-11-01",
                                      "--end", "2026-11-02"])
        self.assertEqual(code, 4)
        self.assertIn("网络", err)

    def test_bad_json_from_api_is_not_reported_as_date_error(self):
        from oura_client import ApiError
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=True), \
             patch("oura.fetch_endpoint", side_effect=ApiError(-1, "响应不是合法 JSON")):
            code, _, err = self._run(["fetch", "--start", "2026-11-01",
                                      "--end", "2026-11-02"])
        self.assertEqual(code, 4)
        self.assertNotIn("日期格式", err)

    def test_query_json_empty_window_exits_5(self):
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", return_value={"fetched_at": 0, "documents": [],
                                                    "fetched_ranges": []}):
            code, out, _ = self._run(["query", "spo2", "--start", "2026-11-10",
                                      "--end", "2026-11-10", "--json"])
        self.assertEqual(code, 5)

    def test_query_rows_sorted_by_date(self):
        docs = [{"id": "b", "day": "2026-11-12", "score": 60},
                {"id": "a", "day": "2026-11-10", "score": 80}]
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", return_value={
                 "fetched_at": 1.0, "fetched_ranges": [], "documents": docs}):
            code, out, _ = self._run(["query", "readiness", "--start", "2026-11-10",
                                      "--end", "2026-11-12"])
        self.assertEqual(code, 0)
        self.assertLess(out.index("2026-11-10"), out.index("2026-11-12"))

    def test_query_personal_info_not_filtered_by_day(self):
        with patch("oura.ensure_valid_token", return_value="tok"), \
             patch("oura.needs_fetch", return_value=False), \
             patch("oura.load_cache", return_value={
                 "fetched_at": 1.0, "fetched_ranges": [],
                 "documents": [{"id": "u1", "age": 30, "email": "x@y.z"}]}):
            code, out, _ = self._run(["query", "personal_info"])
        self.assertEqual(code, 0)
        self.assertIn("30", out)
