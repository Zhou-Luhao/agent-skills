import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura_auth


class AuthStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HOME": self.tmp.name,
            "OURA_CLIENT_ID": "cid-123",
            "OURA_CLIENT_SECRET": "csecret-456",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_start_authorization_url_shape(self):
        url = oura_auth.start_authorization()
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.ouraring.com")
        self.assertEqual(parsed.path, "/v2/oauth/authorize")
        q = parse_qs(parsed.query)
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["client_id"], ["cid-123"])
        self.assertEqual(q["redirect_uri"], ["http://localhost:8080/callback"])
        self.assertTrue(len(q["state"][0]) >= 16)

    def test_scope_excludes_email(self):
        url = oura_auth.start_authorization()
        scopes = parse_qs(urlparse(url).query)["scope"][0].split()
        self.assertNotIn("extapi:email", scopes)
        for required in ("extapi:daily", "extapi:heartrate", "extapi:workout",
                         "extapi:tag", "extapi:session", "extapi:spo2",
                         "extapi:personal", "extapi:heart_health",
                         "extapi:stress", "extapi:ring_configuration"):
            self.assertIn(required, scopes)

    def test_pending_written_with_mode_600_and_matching_state(self):
        url = oura_auth.start_authorization()
        state = parse_qs(urlparse(url).query)["state"][0]
        path = oura_auth.config_dir() / "pending_auth.json"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text())
        self.assertEqual(data["state"], state)
        self.assertEqual(data["redirect_uri"], "http://localhost:8080/callback")
        self.assertIn("created_at", data)

    def test_endpoints_match_live_metadata(self):
        self.assertEqual(oura_auth.AUTHORIZE_URL,
                         "https://api.ouraring.com/v2/oauth/authorize")
        self.assertEqual(oura_auth.TOKEN_URL,
                         "https://api.ouraring.com/v2/oauth/token")

    def test_custom_port_changes_redirect_uri(self):
        url = oura_auth.start_authorization(port=9123)
        self.assertEqual(parse_qs(urlparse(url).query)["redirect_uri"],
                         ["http://localhost:9123/callback"])

    def test_missing_credentials_raises(self):
        with patch.dict(os.environ, {"OURA_CLIENT_ID": "", "OURA_CLIENT_SECRET": ""}):
            with self.assertRaises(oura_auth.CredentialError):
                oura_auth.start_authorization()


if __name__ == "__main__":
    unittest.main()