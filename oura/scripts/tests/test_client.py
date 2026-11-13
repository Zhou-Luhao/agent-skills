import datetime
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura_client

D = datetime.date


def _resp(payload):
    return io.BytesIO(json.dumps(payload).encode())


class ParamsTest(unittest.TestCase):
    def test_daily_uses_start_date_and_overfetch(self):
        p = oura_client.build_params("daily_sleep", D(2026, 11, 10), D(2026, 11, 12))
        self.assertEqual(p["start_date"], "2026-11-09")   # 外扩 1 天
        self.assertEqual(p["end_date"], "2026-11-13")
        self.assertNotIn("start_datetime", p)

    def test_timeseries_uses_start_datetime(self):
        p = oura_client.build_params("heartrate", D(2026, 11, 10), D(2026, 11, 12))
        self.assertEqual(p["start_datetime"], "2026-11-09T00:00:00")
        self.assertEqual(p["end_datetime"], "2026-11-13T00:00:00")
        self.assertNotIn("start_date", p)

    def test_nodate_endpoint_has_no_date_params(self):
        p = oura_client.build_params("ring_configuration", D(2026, 11, 10), D(2026, 11, 12))
        self.assertNotIn("start_date", p)
        self.assertNotIn("start_datetime", p)

    def test_every_endpoint_declares_family_and_dedupe(self):
        self.assertEqual(len(oura_client.ENDPOINTS), 19)
        for name, meta in oura_client.ENDPOINTS.items():
            self.assertIn(meta["family"],
                          ("daily", "range", "timeseries", "nodate", "single"))
            self.assertIn(meta["dedupe"], ("id", "timestamp", "none"))
        self.assertEqual(oura_client.ENDPOINTS["heartrate"]["dedupe"], "timestamp")
        self.assertEqual(oura_client.ENDPOINTS["daily_sleep"]["dedupe"], "id")

    def test_families_match_spec(self):
        fams = {}
        for meta in oura_client.ENDPOINTS.values():
            fams[meta["family"]] = fams.get(meta["family"], 0) + 1
        self.assertEqual(fams["daily"], 13)
        self.assertEqual(fams["range"], 2)
        self.assertEqual(fams["timeseries"], 2)
        self.assertEqual(fams["nodate"], 1)
        self.assertEqual(fams["single"], 1)


class FetchTest(unittest.TestCase):
    def test_follows_next_token_until_null(self):
        pages = [(_resp({"data": [{"id": "a"}], "next_token": "t1"}), {}),
                 (_resp({"data": [{"id": "b"}], "next_token": None}), {})]
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get", side_effect=pages) as get:
            docs = oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual([d["id"] for d in docs], ["a", "b"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1][0][1]["next_token"], "t1")

    def test_timeseries_tolerates_missing_next_token(self):
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get",
                   return_value=(_resp({"data": [{"timestamp": "x"}]}), {})):
            docs = oura_client.fetch_endpoint("heartrate", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(len(docs), 1)

    def test_single_endpoint_wraps_object(self):
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get", return_value=(_resp({"age": 30}), {})):
            docs = oura_client.fetch_endpoint("personal_info", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(docs, [{"age": 30}])

    def test_429_uses_retry_after(self):
        err = urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "0"}, io.BytesIO(b""))
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get",
                   side_effect=[err, (_resp({"data": [], "next_token": None}), {})]), \
             patch("oura_client.time.sleep") as sleep:
            oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        sleep.assert_called_once_with(0.0)

    def test_429_exhausts_after_three_retries(self):
        err = urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "0"}, io.BytesIO(b""))
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get", side_effect=[err, err, err, err]), \
             patch("oura_client.time.sleep"):
            with self.assertRaises(oura_client.ApiError) as ctx:
                oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(ctx.exception.status, 429)

    def test_user_agent_header_set(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured.update(dict(req.header_items()))
            return _resp({"data": [], "next_token": None})

        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client.urllib.request.urlopen", side_effect=fake_urlopen):
            oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(captured.get("User-agent"), "oura-skill/0.1.0")

    def test_401_triggers_one_refresh_then_retry(self):
        err = urllib.error.HTTPError("u", 401, "unauth", {}, io.BytesIO(b""))
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_auth.refresh_tokens"), \
             patch("oura_client._get",
                   side_effect=[err, (_resp({"data": [], "next_token": None}), {})]):
            docs = oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(docs, [])

    def test_403_raises_apierror(self):
        err = urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b""))
        with patch("oura_auth.ensure_valid_token", return_value="tok"), \
             patch("oura_client._get", side_effect=[err]):
            with self.assertRaises(oura_client.ApiError) as ctx:
                oura_client.fetch_endpoint("daily_sleep", D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()