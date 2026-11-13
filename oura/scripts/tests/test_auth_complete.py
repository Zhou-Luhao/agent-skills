import json
import os
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura_auth

TOKEN_RESPONSE = {"access_token": "acc-999", "refresh_token": "ref-888",
                  "expires_in": 86400, "token_type": "Bearer",
                  "scope": "daily spo2"}


def _pending(state="st-abc"):
    d = oura_auth.config_dir()
    (d / "pending_auth.json").write_text(json.dumps({
        "state": state, "redirect_uri": "http://localhost:8080/callback",
        "port": 8080, "scope": "daily", "created_at": time.time()}))


class AuthCompleteTest(unittest.TestCase):
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

    def test_parse_redirect_url(self):
        code, state = oura_auth.parse_redirect_url(
            "http://localhost:8080/callback?code=abc%2F123&state=st-abc")
        self.assertEqual(code, "abc/123")
        self.assertEqual(state, "st-abc")

    def test_parse_rejects_error_callback(self):
        with self.assertRaises(oura_auth.TokenExchangeError):
            oura_auth.parse_redirect_url(
                "http://localhost:8080/callback?error=access_denied&state=st-abc")

    def test_state_mismatch_raises(self):
        _pending("st-abc")
        with self.assertRaises(oura_auth.StateMismatch):
            oura_auth.complete_authorization(
                "http://localhost:8080/callback?code=c1&state=WRONG")

    def test_missing_pending_raises(self):
        with self.assertRaises(oura_auth.PendingExpired):
            oura_auth.complete_authorization(
                "http://localhost:8080/callback?code=c1&state=st-abc")

    def test_expired_pending_raises(self):
        _pending("st-abc")
        p = oura_auth.config_dir() / "pending_auth.json"
        data = json.loads(p.read_text())
        data["created_at"] = time.time() - 601
        p.write_text(json.dumps(data))
        with self.assertRaises(oura_auth.PendingExpired):
            oura_auth.complete_authorization(
                "http://localhost:8080/callback?code=c1&state=st-abc")

    def test_successful_exchange_stores_tokens_and_deletes_pending(self):
        _pending("st-abc")
        with patch("oura_auth._post_token", return_value=TOKEN_RESPONSE) as post:
            tokens = oura_auth.complete_authorization(
                "http://localhost:8080/callback?code=c1&state=st-abc")
        self.assertEqual(tokens.access_token, "acc-999")
        self.assertEqual(tokens.refresh_token, "ref-888")
        self.assertAlmostEqual(tokens.expires_at, time.time() + 86400, delta=5)
        sent = post.call_args[0][0]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "c1")
        self.assertEqual(sent["redirect_uri"], "http://localhost:8080/callback")
        self.assertEqual(sent["client_secret"], "csecret-456")
        self.assertFalse((oura_auth.config_dir() / "pending_auth.json").exists())
        tp = oura_auth.config_dir() / "tokens.json"
        self.assertEqual(stat.S_IMODE(tp.stat().st_mode), 0o600)
        self.assertEqual(json.loads(tp.read_text())["access_token"], "acc-999")

    def test_mask_hides_secret(self):
        self.assertEqual(oura_auth.mask("abcdefgh"), "abcd…")
        self.assertEqual(oura_auth.mask("ab"), "…")


if __name__ == "__main__":
    unittest.main()