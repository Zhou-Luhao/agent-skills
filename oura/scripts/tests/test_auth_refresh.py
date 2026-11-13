import os
import tempfile
import time
import unittest
from unittest.mock import patch
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura_auth

REFRESH_RESPONSE = {"access_token": "acc-new", "refresh_token": "ref-new",
                    "expires_in": 3600, "token_type": "Bearer", "scope": "daily"}


class AuthRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HOME": self.tmp.name,
            "OURA_CLIENT_ID": "cid-123", "OURA_CLIENT_SECRET": "csecret-456"})
        self.env.start()
        oura_auth.config_dir()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _store(self, expires_at):
        oura_auth.save_tokens(oura_auth.Tokens(
            access_token="acc-old", refresh_token="ref-old",
            expires_at=expires_at, scope="daily", token_type="Bearer"))

    def test_valid_token_returned_without_http(self):
        self._store(time.time() + 3600)
        with patch("oura_auth._post_token") as post:
            self.assertEqual(oura_auth.ensure_valid_token(), "acc-old")
            post.assert_not_called()

    def test_expiring_token_triggers_refresh(self):
        self._store(time.time() + 30)   # 落在 60 秒缓冲内
        with patch("oura_auth._post_token", return_value=REFRESH_RESPONSE) as post:
            self.assertEqual(oura_auth.ensure_valid_token(), "acc-new")
        self.assertEqual(post.call_args[0][0]["grant_type"], "refresh_token")
        self.assertEqual(post.call_args[0][0]["refresh_token"], "ref-old")
        self.assertEqual(oura_auth.load_tokens().refresh_token, "ref-new")

    def test_no_tokens_raises(self):
        with self.assertRaises(oura_auth.CredentialError):
            oura_auth.ensure_valid_token()

    def test_refresh_failure_raises_refreshfailed(self):
        self._store(time.time() - 10)
        with patch("oura_auth._post_token",
                   side_effect=oura_auth.TokenExchangeError("bad")):
            with self.assertRaises(oura_auth.RefreshFailed):
                oura_auth.ensure_valid_token()

    def test_refresh_rotates_refresh_token(self):
        self._store(time.time() - 10)
        with patch("oura_auth._post_token", return_value=REFRESH_RESPONSE):
            tokens = oura_auth.refresh_tokens()
        self.assertEqual(tokens.access_token, "acc-new")
        self.assertEqual(tokens.refresh_token, "ref-new")


if __name__ == "__main__":
    unittest.main()