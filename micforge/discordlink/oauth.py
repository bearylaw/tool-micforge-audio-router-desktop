"""OAuth2 token exchange for the Discord RPC scopes.

The authorisation *code* arrives over the local IPC socket (the user approves a
prompt inside the Discord client), so there is no browser redirect to catch.
All that is left is swapping that code for an access token, which is one form
POST -- done with urllib so the app needs no HTTP dependency.

Nothing here is called unless the user presses Connect, and the only host
contacted is discord.com.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import log

_log = log.get("discord.oauth")

TOKEN_URL = "https://discord.com/api/oauth2/token"
REVOKE_URL = "https://discord.com/api/oauth2/token/revoke"
DEFAULT_REDIRECT = "http://localhost"
SCOPES = ["rpc", "rpc.voice.read", "identify"]
TIMEOUT = 15.0


class OAuthError(RuntimeError):
    pass


def _post(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "MicForge (https://github.com/bearylaw/tool-micforge-audio-router-desktop)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise OAuthError(f"Discord rejected the request ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Cannot reach Discord: {exc.reason}") from exc
    except Exception as exc:
        raise OAuthError(str(exc)) from exc


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect_uri: str = DEFAULT_REDIRECT) -> dict:
    """Authorisation code -> ``{access_token, refresh_token, expires_at}``."""
    if not (client_id and client_secret and code):
        raise OAuthError("client id, client secret and code are all required")
    data = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })
    return _normalise(data)


def refresh_token(client_id: str, client_secret: str, token: str) -> dict:
    if not token:
        raise OAuthError("no refresh token stored")
    data = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": token,
    })
    return _normalise(data)


def revoke(client_id: str, client_secret: str, token: str) -> bool:
    try:
        _post(REVOKE_URL, {"client_id": client_id, "client_secret": client_secret,
                           "token": token})
        return True
    except OAuthError as exc:
        _log.warning("token revoke failed: %s", exc)
        return False


def _normalise(data: dict) -> dict:
    access = str(data.get("access_token") or "")
    if not access:
        raise OAuthError(f"no access token in the response: {str(data)[:200]}")
    expires_in = float(data.get("expires_in") or 0)
    return {
        "access_token": access,
        "refresh_token": str(data.get("refresh_token") or ""),
        "expires_at": time.time() + expires_in if expires_in else 0.0,
        "scope": str(data.get("scope") or ""),
    }


def is_expired(expires_at: float, margin_s: float = 300.0) -> bool:
    if not expires_at:
        return False
    return time.time() >= (expires_at - margin_s)


def redact(token: str) -> str:
    """For logs and the UI - never print a whole token."""
    if not token:
        return "(none)"
    return f"{token[:4]}...{token[-4:]} ({len(token)} chars)"
