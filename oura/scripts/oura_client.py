"""Oura API v2 client: endpoint contracts, pagination, retry, cache access.

The 19 collection endpoints split into five families that take different query
parameters and expose different attribution / dedupe keys. Mixing them up is the
single easiest way to fetch the wrong window and never notice, because the server
silently ignores query parameters an endpoint does not declare.
"""
from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

import oura_auth

BASE_URL = "https://api.ouraring.com/v2/usercollection"
USER_AGENT = "oura-skill/0.1.0"
MAX_RETRIES = 3

# family -> how the endpoint behaves:
#   daily       : day-keyed summaries, start_date/end_date, dedupe by id
#   range       : start_day..end_day spans, start_date/end_date, dedupe by id
#   timeseries  : start_datetime/end_datetime, no id, dedupe by timestamp
#   nodate      : no date parameters at all
#   single      : one object, no parameters
ENDPOINTS: Dict[str, Dict[str, str]] = {
    "daily_sleep": {"family": "daily", "dedupe": "id"},
    "daily_readiness": {"family": "daily", "dedupe": "id"},
    "daily_activity": {"family": "daily", "dedupe": "id"},
    "daily_spo2": {"family": "daily", "dedupe": "id"},
    "daily_stress": {"family": "daily", "dedupe": "id"},
    "daily_resilience": {"family": "daily", "dedupe": "id"},
    "daily_cardiovascular_age": {"family": "daily", "dedupe": "id"},
    "vO2_max": {"family": "daily", "dedupe": "id"},
    "sleep": {"family": "daily", "dedupe": "id"},
    "sleep_time": {"family": "daily", "dedupe": "id"},
    "tag": {"family": "daily", "dedupe": "id"},
    "workout": {"family": "daily", "dedupe": "id"},
    "session": {"family": "daily", "dedupe": "id"},
    "rest_mode_period": {"family": "range", "dedupe": "id"},
    "enhanced_tag": {"family": "range", "dedupe": "id"},
    "heartrate": {"family": "timeseries", "dedupe": "timestamp"},
    "ring_battery_level": {"family": "timeseries", "dedupe": "timestamp"},
    "ring_configuration": {"family": "nodate", "dedupe": "id"},
    "personal_info": {"family": "single", "dedupe": "none"},
}


class ApiError(Exception):
    """A non-retryable HTTP failure from the Oura API."""

    def __init__(self, status: int, message: Optional[str] = None) -> None:
        self.status = status
        super().__init__(message or ("Oura API 返回 HTTP %s" % status))


def build_params(endpoint: str, start: datetime.date, end: datetime.date) -> Dict[str, str]:
    """Query parameters for one endpoint, with the 1-day over-fetch hedge.

    Both ends are widened by a day so an inclusive/exclusive mismatch cannot
    silently drop the boundary documents; the caller filters by attribution key.
    """
    family = ENDPOINTS[endpoint]["family"]
    widened_start = start - datetime.timedelta(days=1)
    widened_end = end + datetime.timedelta(days=1)
    if family in ("daily", "range"):
        return {"start_date": str(widened_start), "end_date": str(widened_end)}
    if family == "timeseries":
        return {
            "start_datetime": "%sT00:00:00" % widened_start,
            "end_datetime": "%sT00:00:00" % widened_end,
        }
    return {}


def _retry_delay(headers, attempt: int) -> float:
    """Delay before a 429 retry: headers first, exponential backoff last."""
    if headers:
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            try:
                return max(0.0, float(reset) - time.time())
            except (TypeError, ValueError):
                pass
    return 0.5 * (2 ** (attempt - 1))


def _get(url: str, params: Dict[str, str], token: str) -> Tuple[object, object]:
    """Single HTTP GET. HTTPError is left to the caller so it can retry."""
    from urllib.parse import urlencode

    query = ""
    if params:
        query = "?" + urlencode(params)
    request = urllib.request.Request(
        url + query,
        headers={"Authorization": "Bearer %s" % token, "User-Agent": USER_AGENT},
    )
    response = urllib.request.urlopen(request, timeout=30)
    return response, getattr(response, "headers", {})


def fetch_endpoint(endpoint: str, start: datetime.date, end: datetime.date) -> List[dict]:
    """Fetch every page of one endpoint for the given window."""
    meta = ENDPOINTS[endpoint]
    base_params = build_params(endpoint, start, end)
    token = oura_auth.ensure_valid_token()
    url = "%s/%s" % (BASE_URL, endpoint)

    documents: List[dict] = []
    next_token: Optional[str] = None
    refreshed = False
    attempts = 0

    while True:
        params = dict(base_params)
        if next_token:
            params["next_token"] = next_token
        try:
            handle, _headers = _get(url, params, token)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not refreshed:
                refreshed = True
                token = oura_auth.refresh_tokens().access_token
                continue
            if exc.code == 429 and attempts < MAX_RETRIES:
                attempts += 1
                time.sleep(_retry_delay(getattr(exc, "headers", None), attempts))
                continue
            raise ApiError(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Airplane mode, DNS failure, captive portal: a traceback here would
            # read to the user as "the script crashed". Note this must come
            # AFTER HTTPError, which is a URLError subclass.
            raise ApiError(0, "无法连接 Oura API（%s）" % exc)

        raw = handle.read()  # type: ignore[attr-defined]
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            # A proxy or gateway HTML error page must not be reported as a
            # bad date argument.
            raise ApiError(-1, "Oura API 返回的不是合法 JSON")
        if meta["family"] == "single":
            return [payload]

        documents.extend(payload.get("data") or [])
        next_token = payload.get("next_token")
        if not next_token:
            break

    return documents


# ---------------------------------------------------------------- cache layer

FRESHNESS_TTL_SECONDS = 24 * 3600
ALWAYS_REFRESH_DAYS = 2


def cache_dir():
    """Directory holding one JSON file per endpoint."""
    import os
    from pathlib import Path

    override = os.environ.get("OURA_CACHE_DIR")
    if override:
        path = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        path = base / "oura-skill"
    else:
        path = Path.home() / ".cache" / "oura-skill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(endpoint: str):
    return cache_dir() / ("%s.json" % endpoint)


def load_cache(endpoint: str) -> Dict[str, object]:
    path = _cache_path(endpoint)
    if not path.is_file():
        return {"fetched_at": 0.0, "fetched_ranges": [], "documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"fetched_at": 0.0, "fetched_ranges": [], "documents": []}
    data.setdefault("fetched_at", 0.0)
    data.setdefault("fetched_ranges", [])
    data.setdefault("documents", [])
    return data


def save_cache(endpoint: str, data: Dict[str, object]) -> None:
    _cache_path(endpoint).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def merge_documents(endpoint: str, existing: List[dict], incoming: List[dict]) -> List[dict]:
    """Merge by the family's dedupe key, preserving insertion order.

    A document that lacks the key is always kept: dropping it would lose data,
    and two keyless rows are not evidence of the same row.
    """
    key = ENDPOINTS[endpoint]["dedupe"]
    if key == "none":
        # A single-document endpoint (personal_info) has no identity key, and
        # appending would keep the oldest copy forever.
        return list(incoming) if incoming else list(existing)
    merged: Dict[object, dict] = {}
    keyless: List[dict] = []
    for doc in list(existing) + list(incoming):
        marker = doc.get(key)
        if marker is None:
            keyless.append(doc)
        else:
            merged[marker] = doc
    return list(merged.values()) + keyless


def needs_fetch(endpoint: str, start: datetime.date, end: datetime.date) -> bool:
    """True when the cache cannot answer this window without hitting the API."""
    cache = load_cache(endpoint)
    ranges = cache.get("fetched_ranges") or []

    # Sleep data only syncs when the user opens the Oura app, so recent days are
    # never trustworthy from cache.
    today = datetime.date.today()
    if end >= today - datetime.timedelta(days=ALWAYS_REFRESH_DAYS - 1):
        return True

    for span in ranges:
        try:
            span_start = datetime.date.fromisoformat(str(span["start"]))
            span_end = datetime.date.fromisoformat(str(span["end"]))
            fetched_at = float(span.get("fetched_at", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if span_start <= start and span_end >= end:
            if time.time() - fetched_at < FRESHNESS_TTL_SECONDS:
                return False
    return True


def record_range(endpoint: str, start: datetime.date, end: datetime.date) -> None:
    """Remember that this window was fetched, so coverage can be judged later."""
    cache = load_cache(endpoint)
    spans = list(cache.get("fetched_ranges") or [])
    spans.append({"start": str(start), "end": str(end), "fetched_at": time.time()})
    cache["fetched_ranges"] = spans[-90:]
    cache["fetched_at"] = time.time()
    save_cache(endpoint, cache)
