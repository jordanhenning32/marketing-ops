"""Platform-neutral publish queue for compliant campaign assets."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from compliance import check_campaign_folder
from hermes_store import CONFIG_DIR, CONTENT_DIR, STATE_DIR, append_jsonl, read_jsonl, utc_now_iso, write_jsonl, write_text

QUEUE_PATH = STATE_DIR / "publish-queue.jsonl"
LOG_PATH = STATE_DIR / "publish-log.jsonl"

ASSET_MAP = {
    "x": "01-x-thread.md",
    "stocktwits": "02-stocktwits.md",
    "tiktok": "03-tiktok.md",
    "linkedin": "04-linkedin.md",
    "youtube": "05-youtube.md",
    "email": "06-email.md",
}
DEFAULT_TIMES = {
    "x": "09:15 America/New_York",
    "stocktwits": "10:00 America/New_York",
    "tiktok": "12:00 America/New_York",
    "linkedin": "13:30 America/New_York",
    "youtube": "manual after recording",
    "email": "manual after list review",
}
VIDEO_PLATFORMS = {"tiktok", "youtube"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


def load_platforms() -> dict[str, Any]:
    path = CONFIG_DIR / "platforms.yaml"
    if yaml and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    return data.get("platforms", {})


def _resolve_path(value: str | None, campaign_dir: Path | None = None) -> Path | None:
    if not value:
        return None
    raw = str(value).strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    else:
        if campaign_dir is not None:
            candidates.append(campaign_dir / path)
            candidates.append(campaign_dir.parent.parent / path)
        candidates.append(CONTENT_DIR.parent / path)
        candidates.append(CONTENT_DIR / path)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved.exists():
            return resolved
    return None


def _queue_row_has_video(row: dict[str, Any], campaign_dir: Path | None = None) -> bool:
    if row.get("platform") not in VIDEO_PLATFORMS:
        return True
    for key in ("video_file", "media_file"):
        path = _resolve_path(row.get(key), campaign_dir)
        if path and path.suffix.lower() in VIDEO_EXTS and path.stat().st_size > 0:
            return True
    return False


def _normalize_video_requirement(row: dict[str, Any], campaign_dir: Path | None = None) -> dict[str, Any]:
    row = dict(row)
    if row.get("platform") not in VIDEO_PLATFORMS:
        return row
    row["requires_video"] = True
    row.setdefault("media_type", "short_video")
    if _queue_row_has_video(row, campaign_dir):
        row["video_status"] = "attached"
        if row.get("status") == "needs_video":
            row["status"] = "queued"
        if row.get("approval_status") == "blocked":
            row["approval_status"] = "pending"
    else:
        row["video_status"] = "missing"
        row.setdefault("video_help", "Generate or attach a 9:16 short video before approving this post.")
        if row.get("status", "queued") in {"queued", "pending", "approved"}:
            row["status"] = "needs_video"
        if row.get("approval_status", "pending") in {"pending", "approved"}:
            row["approval_status"] = "blocked"
    return row


def list_queue() -> list[dict[str, Any]]:
    return [_normalize_video_requirement(row) for row in read_jsonl(QUEUE_PATH)]


def _extract_utm_url(text: str) -> str:
    urls = re.findall(r"https?://[^\s)<>]+", text)
    for url in urls:
        if "utm_" in url:
            return url.rstrip(".,")
    return urls[-1].rstrip(".,") if urls else ""


def _extract_cta(text: str, utm_url: str) -> str:
    for line in text.splitlines():
        if line.strip().lower().startswith("cta:"):
            return line.strip()[4:].strip()
    return utm_url or "manual review required"


def _body_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.lower().startswith("cta:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _asset_rel(asset_path: Path, campaign_dir: Path) -> str:
    try:
        return str(asset_path.resolve().relative_to(campaign_dir.parent.parent.resolve())).replace("\\", "/")
    except ValueError:
        return str(asset_path).replace("\\", "/")


def _workspace_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(CONTENT_DIR.parent.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _video_attachment_from_kit(campaign_dir: Path, platform: str, kit_dir: Path) -> dict[str, Any]:
    metadata_path = kit_dir / "metadata.json"
    metadata = _load_json(metadata_path)
    clip_path = _resolve_path(metadata.get("clip_relpath"), campaign_dir)
    if not clip_path and (kit_dir / "source-clip.txt").exists():
        clip_path = _resolve_path((kit_dir / "source-clip.txt").read_text(encoding="utf-8").strip(), campaign_dir)
    if not clip_path or clip_path.suffix.lower() not in VIDEO_EXTS or clip_path.stat().st_size <= 0:
        return {}

    caption_path = kit_dir / "caption.txt"
    title_path = kit_dir / "title.txt"
    title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else metadata.get("title", "")
    attachment = {
        "requires_video": True,
        "video_status": "attached",
        "media_type": "short_video",
        "video_file": _asset_rel(clip_path, campaign_dir),
        "media_file": _asset_rel(clip_path, campaign_dir),
        "kit_dir": _asset_rel(kit_dir, campaign_dir),
        "metadata_file": _asset_rel(metadata_path, campaign_dir) if metadata_path.exists() else "",
        "caption_file": _asset_rel(caption_path, campaign_dir) if caption_path.exists() else "",
        "short_clip_index": kit_dir.name,
        "title": title,
    }
    thumbnail_path = _resolve_path(metadata.get("thumbnail_relpath"), campaign_dir)
    if thumbnail_path and thumbnail_path.exists():
        attachment["thumbnail_file"] = _asset_rel(thumbnail_path, campaign_dir)
    return attachment


def _video_kit_rows(campaign_dir: Path, campaign_id: str, platform: str, cfg: dict[str, Any], compliance_status: str) -> list[dict[str, Any]]:
    base = campaign_dir / "distribution" / platform
    if not base.exists() or not base.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    mode = cfg.get("publish_mode", "manual")
    if cfg.get("api_enabled") is not True:
        mode = "kit" if mode == "kit" else ("queue" if mode == "queue" else "manual")
    for kit_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        attachment = _video_attachment_from_kit(campaign_dir, platform, kit_dir)
        if not attachment:
            continue
        caption_path = kit_dir / "caption.txt"
        caption = caption_path.read_text(encoding="utf-8") if caption_path.exists() else attachment.get("title", "")
        utm_url = _extract_utm_url(caption)
        clip_id = attachment.get("short_clip_index") or kit_dir.name
        key = f"{campaign_id}|{platform}|clip-{clip_id}"
        rows.append({
            "queue_key": key,
            "campaign_id": campaign_id,
            "platform": platform,
            "asset_file": attachment["video_file"],
            "scheduled_time": cfg.get("scheduled_time") or DEFAULT_TIMES.get(platform, "manual"),
            "publish_mode": mode,
            "api_enabled": bool(cfg.get("api_enabled")),
            "compliance_status": compliance_status,
            "cta": _extract_cta(caption, utm_url),
            "utm_url": utm_url,
            "utm": utm_url,
            "body_text": _body_text(caption),
            "content_type": "short_video",
            **attachment,
        })
    return rows


def _missing_video_fields(platform: str) -> dict[str, Any]:
    return {
        "requires_video": True,
        "video_status": "missing",
        "media_type": "short_video",
        "video_file": "",
        "media_file": "",
        "video_help": f"{platform.title()} posts need a 9:16 short video before approval.",
    }


def _upsert_queue_row(rows: list[dict[str, Any]], existing: set[str], campaign_dir: Path, platform: str, base_row: dict[str, Any]) -> dict[str, Any] | None:
    key = base_row["queue_key"]
    if key in existing:
        for idx, existing_row in enumerate(rows):
            if existing_row.get("queue_key") == key:
                changed = False
                for field, value in base_row.items():
                    if field in {"queue_key", "campaign_id", "platform"}:
                        continue
                    if value and existing_row.get(field) != value:
                        if field in {"video_file", "media_file", "video_status", "kit_dir", "metadata_file", "caption_file", "thumbnail_file", "content_type", "media_type", "requires_video", "video_help"} or not existing_row.get(field) or existing_row.get(field) in {"see asset file", "manual review required"}:
                            existing_row[field] = value
                            changed = True
                existing_row.setdefault("approval_status", "pending")
                existing_row.setdefault("manual_url", "")
                existing_row = _normalize_video_requirement(existing_row, campaign_dir)
                rows[idx] = existing_row
                write_manual_kit(campaign_dir, platform, existing_row)
                if changed:
                    write_jsonl(QUEUE_PATH, rows)
                break
        return None
    row = {
        **base_row,
        "status": "needs_video" if base_row.get("video_status") == "missing" else "queued",
        "approval_status": "blocked" if base_row.get("video_status") == "missing" else "pending",
        "created_at": utc_now_iso(),
        "manual_url": "",
    }
    row = _normalize_video_requirement(row, campaign_dir)
    rows.append(row)
    existing.add(key)
    write_manual_kit(campaign_dir, platform, row)
    return row


def write_manual_kit(campaign_dir: Path, platform: str, row: dict[str, Any]) -> None:
    if row.get("content_type") == "short_video" and row.get("kit_dir"):
        kit = _resolve_path(row.get("kit_dir"), campaign_dir) or (campaign_dir / "distribution" / platform / str(row.get("short_clip_index", "")))
        write_text(kit / "queue-metadata.json", json.dumps(row, indent=2, ensure_ascii=False) + "\n")
        return
    kit = campaign_dir / "distribution" / platform
    write_text(kit / "body.txt", row.get("body_text", "") + "\n")
    write_text(kit / "caption.txt", row.get("cta", "") + "\n")
    write_text(kit / "metadata.json", json.dumps(row, indent=2, ensure_ascii=False) + "\n")


def queue_campaign(campaign_dir: Path, campaign_id: str) -> list[dict[str, Any]]:
    campaign_dir = Path(campaign_dir)
    compliance = check_campaign_folder(campaign_dir)
    if not compliance.passed:
        raise ValueError(f"campaign compliance is {compliance.status}; refusing queue")
    existing = {row.get("queue_key") for row in list_queue()}
    rows = list_queue()
    created: list[dict[str, Any]] = []
    platforms = load_platforms()
    video_kit_platforms: set[str] = set()
    for platform in VIDEO_PLATFORMS:
        cfg = platforms.get(platform, {"enabled": True, "publish_mode": "manual", "api_enabled": False})
        if not cfg.get("enabled", True):
            continue
        kit_rows = _video_kit_rows(campaign_dir, campaign_id, platform, cfg, compliance.status)
        if kit_rows:
            video_kit_platforms.add(platform)
        for base_row in kit_rows:
            row = _upsert_queue_row(rows, existing, campaign_dir, platform, base_row)
            if row:
                created.append(row)
    for platform, filename in ASSET_MAP.items():
        if platform in video_kit_platforms:
            continue
        asset_path = campaign_dir / filename
        if not asset_path.exists():
            continue
        text = asset_path.read_text(encoding="utf-8")
        cfg = platforms.get(platform, {"enabled": True, "publish_mode": "manual", "api_enabled": False})
        if not cfg.get("enabled", True):
            continue
        mode = cfg.get("publish_mode", "manual")
        if cfg.get("api_enabled") is not True:
            mode = "kit" if mode == "kit" else ("queue" if mode == "queue" else "manual")
        key = f"{campaign_id}|{platform}|{filename}"
        utm_url = _extract_utm_url(text)
        base_row = {
            "queue_key": key,
            "campaign_id": campaign_id,
            "platform": platform,
            "asset_file": _asset_rel(asset_path, campaign_dir),
            "scheduled_time": cfg.get("scheduled_time") or DEFAULT_TIMES.get(platform, "manual"),
            "publish_mode": mode,
            "api_enabled": bool(cfg.get("api_enabled")),
            "compliance_status": compliance.status,
            "cta": _extract_cta(text, utm_url),
            "utm_url": utm_url,
            "utm": utm_url,
            "body_text": _body_text(text),
        }
        if platform in VIDEO_PLATFORMS:
            base_row.update(_missing_video_fields(platform))
        row = _upsert_queue_row(rows, existing, campaign_dir, platform, base_row)
        if row:
            created.append(row)
    if created:
        write_jsonl(QUEUE_PATH, rows)
    return created


def update_queue_item(queue_key: str, action: str, *, manual_url: str = "", reason: str = "") -> dict[str, Any]:
    rows = list_queue()
    for idx, row in enumerate(rows):
        if row.get("queue_key") != queue_key:
            continue
        now = utc_now_iso()
        if action == "approve":
            if row.get("platform") in VIDEO_PLATFORMS and not _queue_row_has_video(row):
                raise PermissionError(f"{row.get('platform', 'video')} posts need a short video attached before approval")
            row["approval_status"] = "approved"
            row["approved_at"] = now
        elif action == "mark_published":
            if row.get("approval_status") != "approved":
                raise PermissionError("publish queue item must be approved before marking published")
            if not manual_url:
                raise ValueError("manual_url is required when marking published")
            row["status"] = "published_manual"
            row["manual_url"] = manual_url
            row["published_at"] = now
        elif action == "skip":
            row["status"] = "skipped"
            row["skip_reason"] = reason
            row["skipped_at"] = now
        else:
            raise ValueError(f"unsupported publish queue action: {action}")
        rows[idx] = row
        write_jsonl(QUEUE_PATH, rows)
        append_jsonl(LOG_PATH, {"event": action, "queue_key": queue_key, "platform": row.get("platform"), "campaign_id": row.get("campaign_id"), "manual_url": manual_url, "reason": reason, "timestamp": now})
        return row
    raise KeyError(f"publish queue item not found: {queue_key}")


def patch_queue_item(queue_key: str, patch: dict[str, Any], *, event: str = "patch", reason: str = "") -> dict[str, Any]:
    rows = list_queue()
    for idx, row in enumerate(rows):
        if row.get("queue_key") != queue_key:
            continue
        now = utc_now_iso()
        row.update(patch)
        row["updated_at"] = now
        rows[idx] = row
        write_jsonl(QUEUE_PATH, rows)
        append_jsonl(LOG_PATH, {
            "event": event,
            "queue_key": queue_key,
            "platform": row.get("platform"),
            "campaign_id": row.get("campaign_id"),
            "reason": reason,
            "timestamp": now,
            **{k: v for k, v in patch.items() if k in {"auto_publish_status", "auto_publish_job_id", "platform_url", "manual_url", "status"}},
        })
        return row
    raise KeyError(f"publish queue item not found: {queue_key}")


def record_auto_publish_started(queue_key: str, *, job_id: str, message: str = "") -> dict[str, Any]:
    return patch_queue_item(
        queue_key,
        {
            "auto_publish_status": "started",
            "auto_publish_job_id": job_id,
            "auto_publish_message": message or "Auto-publish job started.",
            "auto_publish_started_at": utc_now_iso(),
        },
        event="auto_publish_started",
        reason=message,
    )


def record_auto_publish_blocked(queue_key: str, reason: str) -> dict[str, Any]:
    return patch_queue_item(
        queue_key,
        {
            "auto_publish_status": "blocked",
            "auto_publish_message": reason,
            "auto_publish_blocked_at": utc_now_iso(),
        },
        event="auto_publish_blocked",
        reason=reason,
    )


def record_auto_publish_failed(queue_key: str, error: str, *, job_id: str = "") -> dict[str, Any]:
    patch = {
        "auto_publish_status": "failed",
        "auto_publish_message": error,
        "auto_publish_failed_at": utc_now_iso(),
    }
    if job_id:
        patch["auto_publish_job_id"] = job_id
    return patch_queue_item(queue_key, patch, event="auto_publish_failed", reason=error)


def record_auto_publish_success(queue_key: str, url: str, *, job_id: str = "") -> dict[str, Any]:
    patch = {
        "status": "published_auto",
        "auto_publish_status": "published",
        "auto_publish_message": "Posted automatically.",
        "platform_url": url,
        "manual_url": url,
        "published_at": utc_now_iso(),
        "published_via": "auto_publish",
    }
    if job_id:
        patch["auto_publish_job_id"] = job_id
    return patch_queue_item(queue_key, patch, event="auto_publish_success", reason=url)


def attach_video(queue_key: str, video_file: str, *, title: str = "") -> dict[str, Any]:
    video_path = _resolve_path(video_file)
    if not video_path or video_path.suffix.lower() not in VIDEO_EXTS or video_path.stat().st_size <= 0:
        raise ValueError("Choose a valid short video file before attaching it")

    rows = list_queue()
    for idx, row in enumerate(rows):
        if row.get("queue_key") != queue_key:
            continue
        if row.get("platform") not in VIDEO_PLATFORMS:
            raise ValueError("Only TikTok and YouTube queue items can attach short videos")
        rel = _workspace_rel(video_path)
        row["video_file"] = rel
        row["media_file"] = rel
        row["requires_video"] = True
        row["media_type"] = "short_video"
        row["content_type"] = row.get("content_type") or "short_video"
        row["video_status"] = "attached"
        row["status"] = "queued"
        if row.get("approval_status") == "blocked":
            row["approval_status"] = "pending"
        if title and not row.get("title"):
            row["title"] = title
        now = utc_now_iso()
        row["video_attached_at"] = now
        rows[idx] = row
        write_jsonl(QUEUE_PATH, rows)
        append_jsonl(LOG_PATH, {
            "event": "attach_video",
            "queue_key": queue_key,
            "platform": row.get("platform"),
            "campaign_id": row.get("campaign_id"),
            "video_file": rel,
            "timestamp": now,
        })
        return row
    raise KeyError(f"publish queue item not found: {queue_key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue")
    q.add_argument("campaign_dir")
    q.add_argument("--campaign-id", required=True)
    action = sub.add_parser("action")
    action.add_argument("queue_key")
    action.add_argument("action", choices=["approve", "mark_published", "skip"])
    action.add_argument("--manual-url", default="")
    action.add_argument("--reason", default="")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.cmd == "queue":
        created = queue_campaign(Path(args.campaign_dir), args.campaign_id)
        print(f"queued {len(created)} assets")
    elif args.cmd == "action":
        print(update_queue_item(args.queue_key, args.action, manual_url=args.manual_url, reason=args.reason))
    elif args.cmd == "list":
        for row in list_queue():
            print(row)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
