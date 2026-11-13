"""Oura OAuth2 authentication: two-step paste authorization, token storage, refresh.

Step 1 (``start_authorization``) builds the authorize URL and records the CSRF
state in a short-lived pending file. Step 2 (``complete_authorization``, task 2)
parses the browser's redirect URL and exchanges the code for tokens.

No local HTTP server is used: the redirect happens in the user's browser, so the
address bar carries ``code`` and ``state`` even when nothing listens on the
callback port.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

AUTHORIZE_URL = "https://api.ouraring.com/v2/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/v2/oauth/token"
USER_AGENT = "oura-skill/0.1.0"

# Every scope except "extapi:email": the skill never needs the user's address,
# and personal_info's email field is on the PII blacklist anyway.
# Names come from https://api.ouraring.com/.well-known/oauth-authorization-server
# (the `extapi:` prefix is required; the unprefixed names in the published
# OpenAPI file are stale and are rejected).
SCOPES = [
    "extapi:personal", "extapi:daily", "extapi:heartrate", "extapi:workout",
    "extapi:tag", "extapi:session", "extapi:spo2", "extapi:heart_health",
    "extapi:stress", "extapi:ring_configuration",
]

DEFAULT_PORT = 8080
PENDING_TTL_SECONDS = 600


class CredentialError(Exception):
    """Client credentials are missing, or authorization has not happened yet."""


class PendingExpired(Exception):
    """No pending authorization, or it is older than PENDING_TTL_SECONDS."""


class StateMismatch(Exception):
    """The state in the redirect URL does not match the pending state."""


class TokenExchangeError(Exception):
    """The token endpoint rejected the code, or returned an unparsable body."""


class RefreshFailed(Exception):
    """The refresh token was rejected; the user must authorize again."""


REFRESH_BUFFER_SECONDS = 60


def config_dir() -> Path:
    """Directory holding client credentials, pending state and tokens."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path.home() / ".config"
    path = base / "oura-skill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_client_credentials() -> Tuple[str, str]:
    """Return ``(client_id, client_secret)`` from env, falling back to the env file."""
    client_id = os.environ.get("OURA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OURA_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        file_values = _read_env_file(config_dir() / "env")
        client_id = client_id or file_values.get("OURA_CLIENT_ID", "").strip()
        client_secret = client_secret or file_values.get("OURA_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        raise CredentialError(
            "缺少 OURA_CLIENT_ID / OURA_CLIENT_SECRET。"
            "请设置环境变量，或写入 %s (KEY=VALUE, 权限 600)。" % (config_dir() / "env")
        )
    return client_id, client_secret


def _write_private(path: Path, text: str) -> None:
    """Write a secret-bearing file and lock it down to the owner."""
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows: ACLs, not POSIX bits. The file still lives in the user's profile.
        pass


def start_authorization(port: int = DEFAULT_PORT) -> str:
    """Step 1: build the authorize URL and persist the pending state."""
    client_id, _ = load_client_credentials()
    state = secrets.token_urlsafe(32)
    redirect_uri = "http://localhost:%d/callback" % port
    pending = {
        "state": state,
        "redirect_uri": redirect_uri,
        "port": port,
        "scope": " ".join(SCOPES),
        "created_at": time.time(),
    }
    _write_private(
        config_dir() / "pending_auth.json",
        json.dumps(pending, ensure_ascii=False, indent=2),
    )
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
    })
    return "%s?%s" % (AUTHORIZE_URL, query)


def mask(secret: Optional[str]) -> str:
    """Render a credential for logs: first four characters, never the rest."""
    if not secret:
        return "…"
    text = str(secret)
    if len(text) < 5:
        return "…"
    return text[:4] + "…"


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    token_type: str = "Bearer"


def _post_token(payload: Dict[str, str]) -> Dict[str, object]:
    """POST to the token endpoint. Never echoes the payload in errors."""
    body = urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise TokenExchangeError("令牌端点返回 HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise TokenExchangeError("无法连接令牌端点: %s" % exc.reason)
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise TokenExchangeError("令牌端点返回了非 JSON 响应")
    if not isinstance(parsed, dict):
        raise TokenExchangeError("令牌端点返回了意外的响应结构")
    return parsed


def parse_redirect_url(url: str) -> Tuple[str, str]:
    """Extract ``(code, state)`` from the browser's callback URL."""
    query = parse_qs(urlparse(url).query)
    if "error" in query:
        detail = query.get("error_description", [""])[0]
        raise TokenExchangeError(
            ("授权被拒绝: %s %s" % (query["error"][0], detail)).strip())
    codes = query.get("code")
    if not codes:
        raise TokenExchangeError("回调 URL 中缺少 code 参数")
    state = query.get("state", [""])[0]
    return codes[0], state


def _load_pending() -> Dict[str, object]:
    path = config_dir() / "pending_auth.json"
    if not path.is_file():
        raise PendingExpired("没有待完成的授权，请先运行 authorize")
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise PendingExpired("待完成的授权记录已损坏，请重新运行 authorize")
    if time.time() - float(pending.get("created_at", 0)) > PENDING_TTL_SECONDS:
        raise PendingExpired("授权已超过 %d 秒有效期，请重新运行 authorize"
                             % PENDING_TTL_SECONDS)
    return pending


def save_tokens(tokens: Tokens) -> None:
    payload = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "scope": tokens.scope,
        "token_type": tokens.token_type,
    }
    _write_private(config_dir() / "tokens.json",
                   json.dumps(payload, ensure_ascii=False, indent=2))


def load_tokens() -> Optional[Tokens]:
    path = config_dir() / "tokens.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not data.get("access_token"):
        return None
    return Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_at=float(data.get("expires_at", 0)),
        scope=data.get("scope", ""),
        token_type=data.get("token_type", "Bearer"),
    )


def complete_authorization(redirect_url: str) -> Tokens:
    """Step 2: validate state, exchange the code, persist the tokens."""
    pending = _load_pending()
    code, state = parse_redirect_url(redirect_url)
    if state != pending.get("state"):
        raise StateMismatch("state 与本次授权不匹配，请重新运行 authorize")

    client_id, client_secret = load_client_credentials()
    response = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": str(pending.get("redirect_uri", "")),
        "client_id": client_id,
        "client_secret": client_secret,
    })
    access_token = response.get("access_token")
    if not access_token:
        raise TokenExchangeError("令牌响应缺少 access_token")
    tokens = Tokens(
        access_token=str(access_token),
        refresh_token=str(response.get("refresh_token", "")),
        expires_at=time.time() + float(response.get("expires_in", 0) or 0),
        scope=str(response.get("scope", "")),
        token_type=str(response.get("token_type", "Bearer")),
    )
    save_tokens(tokens)
    try:
        (config_dir() / "pending_auth.json").unlink()
    except OSError:
        pass
    return tokens

def refresh_tokens() -> Tokens:
    """Exchange the refresh token for a fresh access token."""
    current = load_tokens()
    if current is None:
        raise CredentialError("尚未完成授权，请先运行 authorize")
    if not current.refresh_token:
        raise RefreshFailed("tokens.json 中没有 refresh_token，请重新运行 authorize")
    client_id, client_secret = load_client_credentials()
    try:
        response = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
    except TokenExchangeError as exc:
        raise RefreshFailed("刷新令牌失败: %s" % exc)
    access_token = response.get("access_token")
    if not access_token:
        raise RefreshFailed("刷新响应缺少 access_token，请重新运行 authorize")
    tokens = Tokens(
        access_token=str(access_token),
        refresh_token=str(response.get("refresh_token") or current.refresh_token),
        expires_at=time.time() + float(response.get("expires_in", 0) or 0),
        scope=str(response.get("scope") or current.scope),
        token_type=str(response.get("token_type") or current.token_type),
    )
    save_tokens(tokens)
    return tokens


def ensure_valid_token() -> str:
    """Return an access token, refreshing it when it is about to expire."""
    current = load_tokens()
    if current is None:
        raise CredentialError("尚未完成授权，请先运行 authorize")
    if current.expires_at - time.time() > REFRESH_BUFFER_SECONDS:
        return current.access_token
    return refresh_tokens().access_token
