"""Auto-publish dispatcher for approved queue items.

Approval is still the human gate. After approval, this module attempts a real
platform post only where an adapter is implemented and the platform is enabled.
Unsupported platforms are marked with a clear blocker on the queue item.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

import distribution_queue
import jobs
import youtube_auth
import youtube_upload
from hermes_store import CONFIG_DIR, CONTENT_DIR

DISTRIBUTION_CONFIG = CONFIG_DIR / "distribution.yaml"


def _load_distribution_config() -> dict[str, Any]:
    if yaml and DISTRIBUTION_CONFIG.exists():
        return yaml.safe_load(DISTRIBUTION_CONFIG.read_text(encoding="utf-8")) or {}
    return {}


def _platform_config(platform: str) -> dict[str, Any]:
    cfg = _load_distribution_config()
    return ((cfg.get("platforms") or {}).get(platform)) or {}


def _kit_path(row: dict[str, Any]) -> Path | None:
    raw = row.get("kit_dir") or row.get("metadata_file")
    if not raw:
        return None
    path = Path(str(raw).replace("\\", "/"))
    if path.name == "metadata.json":
        path = path.parent
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(CONTENT_DIR.parent / path)
        candidates.append(CONTENT_DIR / path)
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _youtube_slug_and_index(row: dict[str, Any]) -> tuple[str, int]:
    kit = _kit_path(row)
    if not kit:
        raise ValueError("YouTube kit folder is missing for this queue item")
    rel = kit.resolve().relative_to(CONTENT_DIR.resolve())
    parts = rel.parts
    if len(parts) < 4 or parts[1] != "distribution" or parts[2] != "youtube":
        raise ValueError(f"Unexpected YouTube kit folder: {kit}")
    try:
        return parts[0], int(parts[3])
    except ValueError as exc:
        raise ValueError(f"Invalid YouTube clip index: {parts[3]}") from exc


def _read_existing_platform_url(row: dict[str, Any]) -> str:
    kit = _kit_path(row)
    if not kit:
        return ""
    metadata_path = kit / "metadata.json"
    if not metadata_path.exists():
        return ""
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return metadata.get("platform_url") or ""


def _unsupported_reason(platform: str) -> str:
    reasons = {
        "x": "X auto-posting needs the official API adapter and credentials before live posts can run.",
        "stocktwits": "Stocktwits auto-posting needs the official API adapter and credentials before live posts can run.",
        "tiktok": "TikTok auto-posting requires Content Posting API approval and credentials. This queue item remains ready for manual upload.",
        "linkedin": "LinkedIn auto-posting needs the official API adapter and credentials before live posts can run.",
        "email": "Email sends are handled by the email queue, not the social publish queue.",
    }
    return reasons.get(platform, f"{platform} auto-posting is not implemented yet.")


def try_auto_publish_after_approval(row: dict[str, Any]) -> dict[str, Any]:
    """Attempt a post after approval and record the outcome on the queue row."""
    platform = row.get("platform", "")
    queue_key = row.get("queue_key", "")
    if not queue_key:
        return {"status": "blocked", "message": "Queue item is missing a queue key."}

    if platform != "youtube":
        reason = _unsupported_reason(platform)
        distribution_queue.record_auto_publish_blocked(queue_key, reason)
        return {"status": "blocked", "message": reason}

    cfg = _platform_config("youtube")
    if cfg.get("auto_upload") is not True:
        reason = "YouTube auto-upload is off in config/distribution.yaml."
        distribution_queue.record_auto_publish_blocked(queue_key, reason)
        return {"status": "blocked", "message": reason}

    if row.get("approval_status") != "approved":
        reason = "Queue item must be approved before auto-publish starts."
        distribution_queue.record_auto_publish_blocked(queue_key, reason)
        return {"status": "blocked", "message": reason}

    existing_url = row.get("platform_url") or _read_existing_platform_url(row)
    if existing_url:
        distribution_queue.record_auto_publish_success(queue_key, existing_url)
        return {"status": "published", "message": "This YouTube Short was already uploaded.", "url": existing_url}

    if not youtube_auth.get_authenticated_credentials():
        reason = "YouTube is not connected. Visit Content -> YouTube setup, then approve again."
        distribution_queue.record_auto_publish_blocked(queue_key, reason)
        return {"status": "blocked", "message": reason}

    try:
        slug, clip_index = _youtube_slug_and_index(row)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        distribution_queue.record_auto_publish_blocked(queue_key, reason)
        return {"status": "blocked", "message": reason}

    job = jobs.create_job(
        kind="youtube-upload",
        source_filename=f"{slug}/clip-{clip_index:02d}",
        meta={
            "slug": slug,
            "clip_index": clip_index,
            "queue_key": queue_key,
            "triggered_from": "publish-queue-approval",
            "privacy_override": cfg.get("default_privacy") or "unlisted",
        },
    )
    distribution_queue.record_auto_publish_started(
        queue_key,
        job_id=job.id,
        message="YouTube upload started after approval.",
    )
    jobs.run_job(
        job.id,
        _youtube_upload_and_record,
        args=(queue_key, slug, clip_index),
        kwargs={"privacy_override": cfg.get("default_privacy") or "unlisted"},
    )
    return {"status": "started", "message": "YouTube upload started.", "job_id": job.id}


def _youtube_upload_and_record(
    job_id: str,
    queue_key: str,
    slug: str,
    clip_index: int,
    *,
    privacy_override: str | None = None,
) -> None:
    try:
        youtube_upload.upload_clip_job(
            job_id,
            slug,
            clip_index,
            privacy_override=privacy_override,
        )
        job = jobs.get_job(job_id)
        url = ((job.meta or {}).get("platform_url") if job else "") or _read_existing_platform_url({"kit_dir": f"content/{slug}/distribution/youtube/{clip_index:02d}"})
        if not url:
            raise RuntimeError("YouTube upload finished but no platform URL was returned.")
        distribution_queue.record_auto_publish_success(queue_key, url, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        distribution_queue.record_auto_publish_failed(queue_key, str(exc), job_id=job_id)
        raise
