import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura


class CliAuthorizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HOME": self.tmp.name,
            "OURA_CLIENT_ID": "cid-123", "OURA_CLIENT_SECRET": "csecret-456"})
        self.env.start()
        # Never let a test drive the developer's real browser: without this,
        # every authorize test pops a tab at api.ouraring.com with a dummy
        # client_id.
        self.browser = patch("oura.webbrowser.open", return_value=True)
        self.browser_mock = self.browser.start()

    def tearDown(self):
        self.browser.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oura.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_authorize_prints_url_and_exits_zero(self):
        code, out, _ = self._run(["authorize"])
        self.assertEqual(code, 0)
        self.assertIn("https://api.ouraring.com/v2/oauth/authorize", out)
        self.assertIn("粘贴", out)   # 提示用户复制回调 URL
        # 回归守卫：浏览器调用必须走 mock，否则测试会在开发机上弹真实标签页
        self.assertTrue(self.browser_mock.called)

    def test_authorize_does_not_print_secret(self):
        _, out, err = self._run(["authorize"])
        self.assertNotIn("csecret-456", out + err)

    def test_authorize_with_redirect_url_completes(self):
        self._run(["authorize"])
        import oura_auth
        state = json.loads(
            (oura_auth.config_dir() / "pending_auth.json").read_text())["state"]
        with patch("oura_auth._post_token", return_value={
                "access_token": "acc-123456", "refresh_token": "ref-123456",
                "expires_in": 3600, "token_type": "Bearer", "scope": "daily"}):
            code, out, _ = self._run(
                ["authorize", "--redirect-url",
                 "http://localhost:8080/callback?code=c1&state=%s" % state])
        self.assertEqual(code, 0)
        self.assertIn("acc-…", out)
        self.assertNotIn("acc-123456", out)

    def test_authorize_opens_browser_best_effort(self):
        with patch("oura.webbrowser.open", return_value=True) as opener:
            code, out, _ = self._run(["authorize"])
        self.assertEqual(code, 0)
        opener.assert_called_once()
        self.assertIn("api.ouraring.com/v2/oauth/authorize", opener.call_args[0][0])

    def test_authorize_survives_browser_failure(self):
        with patch("oura.webbrowser.open", side_effect=RuntimeError("no browser")):
            code, out, _ = self._run(["authorize"])
        self.assertEqual(code, 0)
        self.assertIn("https://api.ouraring.com/v2/oauth/authorize", out)

    def test_missing_credentials_exit_3(self):
        with patch.dict(os.environ, {"OURA_CLIENT_ID": "", "OURA_CLIENT_SECRET": ""}):
            code, _, err = self._run(["authorize"])
        self.assertEqual(code, 3)
        self.assertIn("OURA_CLIENT_ID", err)

    def test_state_mismatch_exit_2(self):
        self._run(["authorize"])
        code, _, _ = self._run(
            ["authorize", "--redirect-url",
             "http://localhost:8080/callback?code=c1&state=WRONG"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()