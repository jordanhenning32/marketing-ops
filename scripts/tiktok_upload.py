"""TikTok Content Posting API — push a short video to drafts OR publish it live.

Two flows:
  * upload_to_inbox(video_path)  -> sends the clip to the creator's TikTok inbox/
    drafts (scope `video.upload`). Works for unaudited apps; the creator finishes
    and posts it by hand in the TikTok app.
  * direct_post(video_path, ...) -> publishes the clip directly to the connected
    account (scope `video.publish`, audited apps only). TikTok requires a
    creator_info pre-check, which we use to validate the requested privacy level.

Public entry points:
    upload_to_inbox(video_path)            -> {"publish_id": ...}
    query_creator_info()                   -> TikTok creator constraints payload
    direct_post(video_path, title=...)     -> {"publish_id": ...}
    publish_status(publish_id)             -> TikTok status payload
"""
from __future__ import annotations

from pathlib import Path

import httpx

import tiktok_auth

INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
DIRECT_POST_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

MAX_SINGLE_CHUNK = 64 * 1024 * 1024  # TikTok single-chunk upload ceiling


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _require_token() -> str:
    token = tiktok_auth.get_access_token()
    if not token:
        raise RuntimeError("Not connected to TikTok — connect the account first.")
    return token


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _validate_video_size(video_path: Path) -> int:
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    size = video_path.stat().st_size
    if size <= 0:
        raise RuntimeError("video file is empty")
    if size > MAX_SINGLE_CHUNK:
        raise RuntimeError(f"video is {size/1048576:.1f}MB; single-chunk upload max is 64MB")
    return size


def _single_chunk_source(size: int) -> dict:
    return {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": size,        # whole file as one chunk (short clips)
        "total_chunk_count": 1,
    }


def _init_and_get_upload(client: httpx.Client, url: str, body: dict, token: str, *, what: str) -> tuple[str, str]:
    resp = client.post(url, json=body, headers=_auth_headers(token))
    payload = resp.json()
    err = payload.get("error") or {}
    if err.get("code") not in (None, "ok", ""):
        raise RuntimeError(f"TikTok {what} failed: {err.get('message') or err}")
    data = payload.get("data") or {}
    upload_url = data.get("upload_url")
    if not upload_url:
        raise RuntimeError(f"TikTok {what} returned no upload_url: {payload}")
    return data.get("publish_id"), upload_url


def _put_whole_file(client: httpx.Client, upload_url: str, video_path: Path, size: int) -> None:
    content = video_path.read_bytes()
    put = client.put(
        upload_url,
        content=content,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{size - 1}/{size}",
            "Content-Length": str(size),
        },
    )
    if put.status_code not in (200, 201, 206):
        raise RuntimeError(f"TikTok upload failed: HTTP {put.status_code} {put.text[:300]}")


# ---------------------------------------------------------------------------
# Inbox / drafts (video.upload)
# ---------------------------------------------------------------------------

def upload_to_inbox(video_path: str | Path, *, progress_cb=None) -> dict:
    """Upload one short video to the connected account's TikTok drafts/inbox."""
    token = _require_token()
    video_path = Path(video_path)
    size = _validate_video_size(video_path)

    init_body = {"source_info": _single_chunk_source(size)}
    if progress_cb:
        progress_cb("Initializing TikTok upload")
    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        publish_id, upload_url = _init_and_get_upload(client, INBOX_INIT_URL, init_body, token, what="init")
        if progress_cb:
            progress_cb(f"Uploading {size/1048576:.1f}MB to TikTok")
        _put_whole_file(client, upload_url, video_path, size)
    if progress_cb:
        progress_cb("Sent — TikTok is moving it into your drafts")
    return {"publish_id": publish_id}


# ---------------------------------------------------------------------------
# Direct public post (video.publish — audited apps)
# ---------------------------------------------------------------------------

def query_creator_info() -> dict:
    """Query the connected creator's posting constraints.

    TikTok REQUIRES this before a direct post. The returned data includes
    `privacy_level_options` (which we validate against), `max_video_post_duration_sec`,
    and the creator's account-level duet/stitch/comment toggles.
    """
    token = _require_token()
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        resp = client.post(CREATOR_INFO_URL, headers=_auth_headers(token))
    payload = resp.json() or {}
    err = payload.get("error") or {}
    if err.get("code") not in (None, "ok", ""):
        raise RuntimeError(f"TikTok creator_info failed: {err.get('message') or err}")
    return payload.get("data") or {}


def direct_post(
    video_path: str | Path,
    *,
    title: str,
    privacy_level: str = "PUBLIC_TO_EVERYONE",
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    brand_organic_toggle: bool = False,
    brand_content_toggle: bool = False,
    creator_info: dict | None = None,
    progress_cb=None,
) -> dict:
    """Publish one short video DIRECTLY (public) to the connected TikTok account.

    Requires the `video.publish` scope on an audited app. We pull creator_info
    first and refuse if the requested privacy_level isn't permitted — that's the
    clean signal that the app is still unaudited for public posts.

    `brand_organic_toggle` discloses "promoting my own brand/business" (the honest
    setting for Shadow Edge product clips); `brand_content_toggle` is third-party
    branded content (forces public + has extra rules) — leave it off.
    """
    token = _require_token()
    video_path = Path(video_path)
    size = _validate_video_size(video_path)

    if progress_cb:
        progress_cb("Checking TikTok account posting settings")
    if creator_info is None:
        creator_info = query_creator_info()
    allowed = creator_info.get("privacy_level_options") or []
    if allowed and privacy_level not in allowed:
        raise RuntimeError(
            f"TikTok won't allow privacy '{privacy_level}' for this account/app "
            f"(allowed: {', '.join(allowed) or 'none'}). If PUBLIC_TO_EVERYONE is "
            "missing, the app is likely still unaudited for public direct posts."
        )

    post_info = {
        "title": (title or "").strip()[:2200],
        "privacy_level": privacy_level,
        "disable_comment": bool(disable_comment),
        "disable_duet": bool(disable_duet),
        "disable_stitch": bool(disable_stitch),
    }
    if brand_organic_toggle or brand_content_toggle:
        post_info["brand_organic_toggle"] = bool(brand_organic_toggle)
        post_info["brand_content_toggle"] = bool(brand_content_toggle)

    init_body = {"post_info": post_info, "source_info": _single_chunk_source(size)}

    if progress_cb:
        progress_cb("Initializing TikTok direct post")
    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        publish_id, upload_url = _init_and_get_upload(
            client, DIRECT_POST_INIT_URL, init_body, token, what="direct-post init"
        )
        if progress_cb:
            progress_cb(f"Uploading {size/1048576:.1f}MB to TikTok")
        _put_whole_file(client, upload_url, video_path, size)
    if progress_cb:
        progress_cb("Sent — TikTok is processing and will publish shortly")
    return {"publish_id": publish_id}


def publish_status(publish_id: str) -> dict:
    token = _require_token()
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        resp = client.post(
            STATUS_URL,
            json={"publish_id": publish_id},
            headers=_auth_headers(token),
        )
    return (resp.json() or {}).get("data") or {}
