"""YouTube OAuth 2.0 helper.

The Console exposes routes that drive this module:
    GET  /content/youtube/auth/start    -> redirects user to Google consent screen
    GET  /content/youtube/auth/callback -> exchanges code for tokens, stores them
    GET  /content/youtube/auth/status   -> JSON: connected? channel name? expiry?
    POST /content/youtube/auth/disconnect -> deletes cached tokens

One-time user setup (documented on the YouTube tab, /youtube):
    1. Open https://console.cloud.google.com/
    2. Create a Project (or reuse one) and enable "YouTube Data API v3"
    3. APIs & Services -> OAuth consent screen -> External, fill in basics
    4. APIs & Services -> Credentials -> Create OAuth client ID -> "Web application"
    5. Authorized redirect URI: http://localhost:8876/content/youtube/auth/callback
    6. Download the client JSON, save as: state/.youtube/client_secret.json
    7. Click "Connect YouTube" in the Console — browser will open the consent flow.

The downloaded JSON is gitignored. The refresh-token JSON is gitignored too.
We never commit tokens to source.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

ROOT = Path(__file__).resolve().parent.parent
YT_DIR = ROOT / "state" / ".youtube"
CLIENT_SECRET_PATH = YT_DIR / "client_secret.json"
TOKEN_PATH = YT_DIR / "token.json"
STATE_PATH = YT_DIR / "auth_state.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # for channel-id lookup
    # Manage scope — lets us EDIT existing videos' descriptions (videos().update)
    # so we can backfill tracked /checklist links into already-published uploads.
    # Adding this requires a one-time reconnect on /youtube to grant it.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


@dataclass
class YTAuthStatus:
    connected: bool
    has_client_secret: bool
    channel_id: str | None = None
    channel_title: str | None = None
    last_refreshed: str | None = None
    error: str | None = None
    needs_reconnect: bool = False  # token on disk but can't authenticate (expired/revoked)


def _ensure_dir() -> None:
    YT_DIR.mkdir(parents=True, exist_ok=True)


def has_client_secret() -> bool:
    return CLIENT_SECRET_PATH.exists()


def token_on_disk() -> bool:
    """Whether a token file exists at all (says nothing about whether it works)."""
    return TOKEN_PATH.exists()


def is_connected() -> bool:
    """True only if the stored token can actually authenticate right now — not
    merely that a token file exists. A revoked/expired refresh token returns
    False here, so callers prompt a reconnect instead of failing silently."""
    return get_authenticated_credentials() is not None


def connection_state() -> str:
    """Cheap tri-state for the UI: 'ok' | 'needs_reconnect' | 'not_setup'.

    Skips the channel-name lookup get_status() does, so it's safe to call on
    every render. 'needs_reconnect' = a token is present but no longer
    authenticates (the case that used to read as connected and fail silently)."""
    if not has_client_secret() or not token_on_disk():
        return "not_setup"
    return "ok" if get_authenticated_credentials() is not None else "needs_reconnect"


def _load_credentials() -> Optional[Credentials]:
    if not TOKEN_PATH.exists():
        return None
    try:
        # Load with the token's OWN stored scopes (not the app SCOPES). Passing the
        # full SCOPES list here makes google-auth raise "scope has changed" on refresh
        # whenever SCOPES gains an entry the existing token wasn't granted (e.g. adding
        # youtube.force-ssl) — which would silently drop a working connection. New
        # connections still request the full SCOPES via the OAuth flow below.
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    except (ValueError, json.JSONDecodeError):
        return None
    # Auto-refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            _save_credentials(creds)
        except Exception:  # noqa: BLE001
            return creds  # let caller see expired state
    return creds


def _save_credentials(creds: Credentials) -> None:
    _ensure_dir()
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    if creds.expiry:
        data["expiry"] = creds.expiry.isoformat()
    TOKEN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_authenticated_credentials() -> Optional[Credentials]:
    """Return live, refreshed credentials or None if not connected."""
    creds = _load_credentials()
    if not creds:
        return None
    if not creds.valid:
        return None
    return creds


def _build_flow(redirect_uri: str, *, code_verifier: str | None = None) -> Flow:
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(
            f"client_secret.json not found at {CLIENT_SECRET_PATH}. "
            "See the YouTube tab (/youtube) for setup instructions."
        )
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    return flow


def start_auth(redirect_uri: str) -> str:
    """Build the consent-screen URL and stash the per-flow state."""
    _ensure_dir()
    flow = _build_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # force a refresh_token even on re-auth
    )
    STATE_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "code_verifier": flow.code_verifier,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return auth_url


def finish_auth(code: str, redirect_uri: str, returned_state: str | None) -> Credentials:
    """Exchange the auth code for tokens and persist them."""
    if not STATE_PATH.exists():
        raise RuntimeError("OAuth state file missing — restart the connect flow.")
    saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if returned_state and saved.get("state") and returned_state != saved["state"]:
        raise RuntimeError("OAuth state mismatch (possible CSRF). Aborting.")
    flow = _build_flow(redirect_uri, code_verifier=saved.get("code_verifier"))
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    try:
        STATE_PATH.unlink()
    except OSError:
        pass
    return creds


def disconnect() -> None:
    for p in (TOKEN_PATH, STATE_PATH):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def get_status() -> YTAuthStatus:
    """Used by the UI to show connection + channel info."""
    status = YTAuthStatus(
        connected=False,
        has_client_secret=has_client_secret(),
    )
    if not status.has_client_secret:
        status.error = "client_secret.json not found"
        return status
    creds = _load_credentials()  # attempts a refresh if the access token expired
    if not creds:
        return status  # no token on disk -> not connected, not yet set up

    if not creds.valid:
        # Token exists but can't authenticate (refresh failed / revoked / expired).
        # Previously this read as connected (refresh_token field still present) and
        # the daily pull failed silently. Surface it so the UI can prompt a reconnect.
        status.needs_reconnect = True
        status.error = "YouTube token expired — reconnect to resume stats & uploads."
        return status

    status.connected = True
    if creds.expiry:
        status.last_refreshed = creds.expiry.isoformat()

    # Best-effort channel name lookup so the UI can confirm the right account
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items") or []
        if items:
            status.channel_id = items[0].get("id")
            status.channel_title = (items[0].get("snippet") or {}).get("title")
    except Exception as exc:  # noqa: BLE001
        status.error = f"channel lookup failed: {exc}"
    return status
