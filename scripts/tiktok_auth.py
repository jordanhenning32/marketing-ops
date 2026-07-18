"""TikTok OAuth 2.0 (Login Kit) helper.

Drives the console routes:
    GET  /content/tiktok/auth/start      -> redirect to TikTok consent screen
    GET  /content/tiktok/auth/callback   -> exchange code -> store tokens
    POST /content/tiktok/auth/paste-code -> manual code paste (fallback)
    POST /content/tiktok/auth/disconnect -> delete cached tokens

One-time setup:
    1. Create an app at developers.tiktok.com.
    2. Add Login Kit + Content Posting API; request scopes user.info.basic +
       video.upload (drafts/inbox) + video.publish (direct public post, audited apps).
    3. Register a redirect URI. Either:
         - http://127.0.0.1:8876/content/tiktok/auth/callback  (auto flow), or
         - https://shadowedgetools.com/tiktok/callback         (then set
           TIKTOK_REDIRECT_URI to it and use the paste-code fallback).
    4. Put TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env.

Tokens are stored in state/.tiktok/token.json (gitignored). We never commit tokens.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlencode

import httpx

_ROOT = Path(__file__).resolve().parent.parent
try:  # load .env so the client key/secret are available standalone too
    from dotenv import load_dotenv

    for _candidate in (_ROOT / ".env", _ROOT / "config" / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
except Exception:  # pragma: no cover
    pass

TT_DIR = _ROOT / "state" / ".tiktok"
TOKEN_PATH = TT_DIR / "token.json"
STATE_PATH = TT_DIR / "auth_state.json"

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USERINFO_URL = "https://open.tiktokapis.com/v2/user/info/"
# video.upload = inbox/drafts (works as soon as Content Posting is approved).
# video.publish = Direct Post (public) — a SEPARATE TikTok approval. Requesting it
# before the app is approved makes the WHOLE OAuth fail ("scope" error), which would
# also block drafts. So it's opt-in: set TIKTOK_DIRECT_POST=1 only once Direct Post
# is approved on the app, then restart + reconnect.
BASE_SCOPES = ["user.info.basic", "video.upload"]


def _direct_post_enabled() -> bool:
    return os.environ.get("TIKTOK_DIRECT_POST", "").strip().lower() in ("1", "true", "yes", "on")


def stats_enabled() -> bool:
    """user.info.stats (follower/like counts) is a SEPARATE TikTok scope. Like
    video.publish, requesting it before it's approved fails the whole OAuth, so
    it's opt-in: set TIKTOK_STATS=1 only once the scope is approved, then reconnect."""
    return os.environ.get("TIKTOK_STATS", "").strip().lower() in ("1", "true", "yes", "on")


def oauth_scopes() -> list[str]:
    scopes = list(BASE_SCOPES)
    if _direct_post_enabled():
        scopes.append("video.publish")
    if stats_enabled():
        scopes.append("user.info.stats")
    return scopes


@dataclass
class TTAuthStatus:
    connected: bool
    has_credentials: bool
    open_id: str | None = None
    display_name: str | None = None
    scope: str | None = None
    expires_at: float | None = None
    error: str | None = None


def client_key() -> str:
    return os.environ.get("TIKTOK_CLIENT_KEY", "").strip()


def client_secret() -> str:
    return os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()


def has_credentials() -> bool:
    return bool(client_key() and client_secret())


def _ensure_dir() -> None:
    TT_DIR.mkdir(parents=True, exist_ok=True)


def load_token() -> Optional[dict]:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_token(tok: dict) -> None:
    _ensure_dir()
    TOKEN_PATH.write_text(json.dumps(tok, indent=2), encoding="utf-8")


def is_connected() -> bool:
    return TOKEN_PATH.exists()


def authorize_url(redirect_uri: str, state: str) -> str:
    """Build the consent URL and stash the redirect_uri/state for the exchange."""
    _ensure_dir()
    STATE_PATH.write_text(
        json.dumps({"state": state, "redirect_uri": redirect_uri}), encoding="utf-8"
    )
    params = {
        "client_key": client_key(),
        "scope": ",".join(oauth_scopes()),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urlencode(params)


def _token_request(grant: dict) -> dict:
    body = {"client_key": client_key(), "client_secret": client_secret(), **grant}
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        resp = client.post(
            TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    data = resp.json()
    if "access_token" not in data:
        try:  # stash the exact response (no secret in error payloads) for diagnosis
            _ensure_dir()
            safe = {k: (f"<{len(str(v))} chars>" if k == "code" else v) for k, v in grant.items()}
            (TT_DIR / "last_token_error.json").write_text(
                json.dumps({"response": data, "sent": safe}, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        desc = data.get("error_description") or data.get("error") or data
        log_id = data.get("log_id")
        raise RuntimeError(f"TikTok token error: {desc}" + (f" (log_id {log_id})" if log_id else ""))
    now = time.time()
    tok = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "open_id": data.get("open_id"),
        "scope": data.get("scope"),
        "expires_at": now + int(data.get("expires_in", 86400)) - 60,
        "refresh_expires_at": now + int(data.get("refresh_expires_in", 0) or 0),
    }
    _save_token({**(load_token() or {}), **{k: v for k, v in tok.items() if v is not None}})
    return tok


def _clean_code(raw: str) -> str:
    """Normalize a pasted authorization code: tolerate a full URL, a 'code='
    prefix, a trailing '&state=...' / '#...', and URL-encoding (e.g. %2A -> *)."""
    raw = (raw or "").strip()
    if "code=" in raw:
        raw = raw.split("code=", 1)[1]
    for sep in ("&", "#", " ", "\n", "\t"):
        raw = raw.split(sep, 1)[0]
    return unquote(raw).strip()


def finish_auth(code: str, redirect_uri: str | None, returned_state: str | None) -> dict:
    """Exchange the authorization code for tokens and persist them."""
    saved = {}
    try:
        saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    if returned_state and saved.get("state") and returned_state != saved["state"]:
        raise RuntimeError("OAuth state mismatch (possible CSRF). Restart the connect flow.")
    ruri = redirect_uri or saved.get("redirect_uri")
    if not ruri:
        raise RuntimeError("Missing redirect_uri for token exchange.")
    tok = _token_request({"code": _clean_code(code), "grant_type": "authorization_code", "redirect_uri": ruri})
    try:
        STATE_PATH.unlink()
    except OSError:
        pass
    return tok


def get_access_token() -> Optional[str]:
    """Return a valid access token, refreshing if expired."""
    tok = load_token()
    if not tok:
        return None
    if tok.get("expires_at", 0) <= time.time() and tok.get("refresh_token"):
        try:
            tok = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        except Exception:
            return tok.get("access_token")
    return tok.get("access_token")


def fetch_user_info() -> dict:
    token = get_access_token()
    if not token:
        return {}
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        resp = client.get(
            USERINFO_URL,
            params={"fields": "open_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {token}"},
        )
    return ((resp.json() or {}).get("data") or {}).get("user") or {}


def fetch_user_stats() -> dict:
    """Follower / total-like / video counts from the TikTok user.info endpoint.

    Requires the user.info.stats scope (gated behind TIKTOK_STATS — see
    stats_enabled). Returns {} when unauthorized, disconnected, or on error so
    callers can no-op safely until the scope is approved."""
    if not stats_enabled():
        return {}
    token = get_access_token()
    if not token:
        return {}
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
            resp = client.get(
                USERINFO_URL,
                params={"fields": "follower_count,likes_count,video_count"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if getattr(resp, "status_code", 200) >= 400:
            return {}
        user = ((resp.json() or {}).get("data") or {}).get("user") or {}
    except Exception:
        return {}
    if not user:
        return {}
    return {
        "followers": user.get("follower_count"),
        "likes": user.get("likes_count"),
        "videos": user.get("video_count"),
    }


def disconnect() -> None:
    for p in (TOKEN_PATH, STATE_PATH):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def get_status() -> TTAuthStatus:
    """Connection + account info for the settings UI."""
    status = TTAuthStatus(connected=False, has_credentials=has_credentials())
    if not status.has_credentials:
        status.error = "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set in .env"
        return status
    tok = load_token()
    if not tok:
        return status
    status.connected = True
    status.open_id = tok.get("open_id")
    status.scope = tok.get("scope")
    status.expires_at = tok.get("expires_at")
    status.display_name = tok.get("display_name")
    try:
        user = fetch_user_info()
        if user.get("display_name"):
            status.display_name = user["display_name"]
            tok["display_name"] = user["display_name"]
            _save_token(tok)
    except Exception as exc:  # noqa: BLE001
        status.error = f"account lookup failed: {exc}"
    return status
