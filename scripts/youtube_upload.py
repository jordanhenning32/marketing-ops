"""YouTube videos.insert wrapper with resumable upload + progress callbacks.

Used as a job target:

    job = jobs.create_job(kind='youtube-upload', source_filename=clip.name, meta={...})
    jobs.run_job(job.id, upload_clip_job, args=(slug, clip_index), kwargs={...})

The function reads `distribution/youtube/<idx>/metadata.json` + `caption.txt`
+ `title.txt`, uploads the linked clip, and writes back `uploaded_at` + URL.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import jobs  # noqa: E402
import youtube_auth  # noqa: E402

ROOT = _HERE.parent
CONTENT_DIR = ROOT / "content"
DISTRIBUTION_CONFIG = ROOT / "config" / "distribution.yaml"
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
RAW_DOMAIN_RE = re.compile(r"(?i)\bshadowedgetools\.com\b")
FOREIGN_DOMAIN_RE = re.compile(
    r"(?i)(?<![@\w.-])(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|ly|app|dev|ai|me|us|biz|info|tv|gg|link|site|xyz)(?:/[^\s]*)?"
)
ALLOWED_SITE_RE = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.)?shadowedgetools\.com(?:[/?#]\S*)?"
)


def _load_distribution_config() -> dict:
    if not DISTRIBUTION_CONFIG.exists():
        return {}
    try:
        return yaml.safe_load(DISTRIBUTION_CONFIG.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _yt_config() -> dict:
    cfg = _load_distribution_config()
    return ((cfg.get("platforms") or {}).get("youtube")) or {}


def _kit_paths(slug: str, clip_index: int) -> dict:
    folder = CONTENT_DIR / slug / "distribution" / "youtube" / f"{clip_index:02d}"
    return {
        "folder": folder,
        "title": folder / "title.txt",
        "caption": folder / "caption.txt",
        "metadata": folder / "metadata.json",
        "source_clip": folder / "source-clip.txt",
    }


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _read_metadata(kit: dict) -> dict:
    if not kit["metadata"].exists():
        return {}
    try:
        return json.loads(kit["metadata"].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _resolve_clip_path(kit: dict, slug: str) -> Path:
    """The pipeline writes an absolute path into source-clip.txt. Honor that."""
    src = _read_text(kit["source_clip"]).strip()
    if src:
        p = Path(src)
        if p.exists():
            return p
    # fallback: walk metadata.json for clip_relpath
    if kit["metadata"].exists():
        try:
            md = json.loads(kit["metadata"].read_text(encoding="utf-8"))
            rel = md.get("clip_relpath")
            if rel:
                cand = CONTENT_DIR / slug / rel
                if cand.exists():
                    return cand
        except (json.JSONDecodeError, OSError):
            pass
    raise FileNotFoundError(f"Could not resolve clip path for {slug}/{clip_index}")


def _resolve_thumbnail_path(kit: dict, slug: str) -> Path | None:
    if kit["metadata"].exists():
        try:
            md = json.loads(kit["metadata"].read_text(encoding="utf-8"))
            rel = md.get("thumbnail_relpath")
            if rel:
                cand = CONTENT_DIR / slug / rel
                if cand.exists():
                    return cand
        except (json.JSONDecodeError, OSError):
            pass
    for name in ("thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png"):
        cand = kit["folder"] / name
        if cand.exists():
            return cand
    return None


def _sanitize_description_for_youtube(text: str) -> str:
    """Keep copy professional while avoiding YouTube invalidDescription rejects."""
    text = CONTROL_CHAR_RE.sub(" ", text).replace("\r\n", "\n").replace("\r", "\n")
    safe_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        line = ALLOWED_SITE_RE.sub(
            lambda match: match.group(0) if match.group(0).lower().startswith("http") else f"https://{match.group(0)}",
            line,
        )
        remaining = ALLOWED_SITE_RE.sub("", line)
        if URL_RE.search(remaining) or RAW_DOMAIN_RE.search(remaining) or FOREIGN_DOMAIN_RE.search(remaining):
            continue
        safe_lines.append(line)
    cleaned = "\n".join(safe_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        cleaned = (
            "Quick Shadow Edge Tools walkthrough for NinjaTrader 8 traders.\n\n"
            "Tools and education only. Not financial advice.\n\n"
            "#Shorts #NinjaTrader #FuturesTrading"
        )
    return cleaned[:4900]


def _make_description(caption: str, footer: str | None) -> str:
    body = caption.strip()
    if footer:
        body += "\n" + footer.rstrip()
    return _sanitize_description_for_youtube(body)


def _hashtag_tags(caption: str, default_tags: list[str]) -> list[str]:
    """Extract #hashtags from caption + add config defaults, deduped."""
    seen = []
    for tag in re.findall(r"#(\w+)", caption):
        t = tag.lower()
        if t not in seen:
            seen.append(t)
    for t in default_tags or []:
        t = t.lower()
        if t not in seen:
            seen.append(t)
    return seen[:30]  # YouTube hard cap is ~500 chars across all tags


def _ensure_shorts_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip() or "Shadow Edge Tools"
    if "#shorts" in title.lower():
        return title[:100]
    suffix = " #Shorts"
    if len(title) + len(suffix) <= 100:
        return title + suffix
    return title[:100 - len(suffix)].rstrip() + suffix


def _shorts_url(video_id: str) -> str:
    # Both URL forms work; the Shorts URL is what creators want shared
    return f"https://www.youtube.com/shorts/{video_id}"


def _fallback_description(title: str) -> str:
    return (
        f"{title.strip()}\n\n"
        "Quick Shadow Edge Tools walkthrough for NinjaTrader 8 traders.\n\n"
        "Tools and education only. Not financial advice."
    )[:4900]


def _is_invalid_description_error(exc: Exception) -> bool:
    text = str(exc)
    return "invalidDescription" in text or "invalid video description" in text


def _wrap_resumable(media: MediaFileUpload, request, log_fn, update_fn) -> dict:
    """Run a resumable upload; report progress via the supplied callbacks."""
    response = None
    last_pct = -1
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as exc:
            raise RuntimeError(f"YouTube API error: {exc}") from exc
        if status:
            pct = int(status.progress() * 100)
            if pct >= last_pct + 5:
                log_fn(f"Uploading: {pct}%")
                # Map upload progress 0–100 to job 30–95
                update_fn(progress_pct=30 + int(pct * 0.65))
                last_pct = pct
    return response


def upload_clip_job(
    job_id: str,
    slug: str,
    clip_index: int,
    *,
    privacy_override: str | None = None,
    title_override: str | None = None,
) -> None:
    """Job target: upload a single clip to YouTube using its distribution kit."""

    def log(msg: str) -> None:
        jobs.log_job(job_id, msg)

    def step(msg: str, pct: int) -> None:
        jobs.update_job(job_id, current_step=msg, progress_pct=pct)
        log(msg)

    step("Loading credentials", 5)
    creds = youtube_auth.get_authenticated_credentials()
    if not creds:
        raise RuntimeError(
            "YouTube not connected — visit /youtube and click Connect."
        )

    cfg = _yt_config()
    if not cfg.get("enabled", True):
        raise RuntimeError("YouTube uploads are disabled in config/distribution.yaml.")

    kit = _kit_paths(slug, clip_index)
    if not kit["folder"].exists():
        raise FileNotFoundError(f"YouTube kit folder missing: {kit['folder']}")

    metadata = _read_metadata(kit)
    content_type = str(metadata.get("content_type") or ("long_form" if clip_index == 0 else "short")).lower()
    title = title_override or _read_text(kit["title"]).strip() or f"{slug} #{clip_index:02d}"
    if content_type == "long_form":
        title = re.sub(r"\s+", " ", title).strip()[:100] or "Shadow Edge Tools"
    else:
        title = _ensure_shorts_title(title)
    caption = _read_text(kit["caption"])
    description = _make_description(caption, cfg.get("description_footer"))
    tags = _hashtag_tags(caption, cfg.get("default_tags") or [])
    privacy = (privacy_override or cfg.get("default_privacy") or "unlisted").lower()
    if privacy not in {"public", "unlisted", "private"}:
        privacy = "unlisted"
    category_id = str(cfg.get("default_category_id", 22))

    clip_path = _resolve_clip_path(kit, slug)
    thumbnail_path = _resolve_thumbnail_path(kit, slug)
    if not thumbnail_path:
        raise RuntimeError(
            "YouTube upload requires thumbnail.jpg in the YouTube kit. "
            "Rerun the video pipeline so it can generate the designed thumbnail."
        )
    log(f"Clip: {clip_path}")
    log(f"Thumbnail: {thumbnail_path}")
    log(f"Title: {title}")
    log(f"Privacy: {privacy} · Category: {category_id} · Tags: {len(tags)}")

    log(f"Description: {len(description)} chars")
    step("Building YouTube client", 15)
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    def make_upload_request(upload_body: dict):
        media = MediaFileUpload(
            str(clip_path),
            chunksize=4 * 1024 * 1024,
            resumable=True,
            mimetype="video/mp4",
        )
        request = yt.videos().insert(part="snippet,status", body=upload_body, media_body=media)
        return media, request

    step("Starting resumable upload", 25)
    media, request = make_upload_request(body)
    try:
        response = _wrap_resumable(
            media,
            request,
            log_fn=log,
            update_fn=lambda **kw: jobs.update_job(job_id, **kw),
        )
    except RuntimeError as exc:
        if not _is_invalid_description_error(exc):
            raise
        log("YouTube rejected the description; retrying once with a minimal safe description.")
        fallback_body = json.loads(json.dumps(body))
        description = _fallback_description(title)
        fallback_body["snippet"]["description"] = description
        media, request = make_upload_request(fallback_body)
        response = _wrap_resumable(
            media,
            request,
            log_fn=log,
            update_fn=lambda **kw: jobs.update_job(job_id, **kw),
        )

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"Unexpected YouTube response: {response}")

    video_url = f"https://www.youtube.com/watch?v={video_id}" if content_type == "long_form" else _shorts_url(video_id)
    log(f"Uploaded: {video_url}")

    thumbnail_uploaded = False
    thumbnail_error = None
    thumbnail_upload_exc: RuntimeError | None = None
    try:
        step("Uploading thumbnail", 96)
        media = MediaFileUpload(
            str(thumbnail_path),
            mimetype="image/jpeg" if thumbnail_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png",
            resumable=False,
        )
        yt.thumbnails().set(videoId=video_id, media_body=media).execute()
        thumbnail_uploaded = True
        log(f"Thumbnail set: {thumbnail_path.name}")
    except Exception as exc:  # noqa: BLE001
        thumbnail_error = str(exc)
        log(f"Thumbnail upload failed: {thumbnail_error}")
        thumbnail_upload_exc = RuntimeError(f"YouTube thumbnail upload failed: {thumbnail_error}")

    # Persist back into the kit metadata
    metadata.update({
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "platform_url": video_url,
        "video_id": video_id,
        "content_type": content_type,
        "privacy": privacy,
        "title_used": title,
        "description_used": description[:5000],
        "tags_used": tags,
        "thumbnail_path": str(thumbnail_path) if thumbnail_path else None,
        "thumbnail_uploaded": thumbnail_uploaded,
        "thumbnail_error": thumbnail_error,
    })
    kit["metadata"].write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if thumbnail_upload_exc:
        raise thumbnail_upload_exc

    step("Done", 100)
    jobs.update_job(
        job_id,
        state="done",
        result_slug=slug,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        progress_pct=100,
    )
    # Stash the URL on the job so the UI can deep-link
    job = jobs.get_job(job_id)
    if job:
        job.meta["video_id"] = video_id
        job.meta["platform_url"] = video_url
        job.meta["clip_index"] = clip_index
        jobs.update_job(job_id, **{})  # touch the file
        # actually rewrite meta — simplest:
        path = job.to_path()
        from dataclasses import asdict
        path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")
