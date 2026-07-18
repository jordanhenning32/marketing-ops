"""Backfill tracked /checklist links into ALREADY-PUBLISHED YouTube videos.

The early uploads went out with a dead, untracked homepage link (or none), so
their views send zero attributable traffic to the site. This walks every video on
the authenticated channel and rewrites its description so it funnels to the free
checklist (lead magnet) AND is attributable in GA4:

  - any bare "shadowedgetools.com" mention  -> the tracked /checklist link
  - a video with NO site link at all         -> prepends the tracked CTA line

It preserves title, category, tags, and privacy — only the description changes.
Idempotent: a video already carrying the tracked link is left untouched.

Usage:
  python youtube_backfill.py            # DRY RUN — shows what would change
  python youtube_backfill.py --apply    # writes the changes via the YouTube API
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import youtube_auth
import youtube_stats
from video_pipeline import _retarget_bare_site_links

try:
    from cli_io import configure_utf8_stdio
except Exception:  # pragma: no cover
    def configure_utf8_stdio():  # type: ignore
        pass

# Static tracked lead-magnet link for backfilled videos. utm_source=youtube keeps
# GA4 attribution consistent with new uploads; a dedicated campaign lets you see
# how much the backfill itself moved.
TRACKED_LINK = "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=youtube_backfill"
CTA_LINE = f"Free NinjaTrader risk-control checklist (2-min setup): {TRACKED_LINK}"


def _new_description(current: str) -> str:
    """Return the funnel-corrected description, or the input unchanged if already good."""
    current = current or ""
    # 1) Rewrite any bare homepage mention to the tracked link.
    updated = _retarget_bare_site_links(current, TRACKED_LINK)
    # 2) If the tracked /checklist link still isn't present, prepend the CTA.
    if "shadowedgetools.com/checklist" not in updated:
        updated = f"{CTA_LINE}\n\n{updated}".strip()
    return updated[:5000]


def _plan(yt) -> list[dict]:
    """Build the change plan for every video on the channel."""
    video_ids = youtube_stats.channel_upload_ids(yt)
    plan: list[dict] = []
    for batch in youtube_stats._chunked(video_ids):
        resp = yt.videos().list(part="snippet", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            snip = item.get("snippet", {})
            old = snip.get("description", "")
            new = _new_description(old)
            plan.append({
                "id": item["id"],
                "title": snip.get("title", ""),
                "categoryId": snip.get("categoryId"),
                "tags": snip.get("tags"),
                "old": old,
                "new": new,
                "changed": new != old,
            })
    return plan


def _build_client():
    creds = youtube_auth.get_authenticated_credentials()
    if not creds:
        raise RuntimeError("YouTube not connected — visit /youtube and click Connect.")
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def backfill(*, apply: bool = False) -> dict:
    """Plan (and optionally write) the funnel-corrected descriptions.

    Returns {total, changed, applied, errors, titles} so both the CLI and the
    console button can report the outcome. When apply is False nothing is written."""
    yt = _build_client()
    plan = _plan(yt)
    changed = [p for p in plan if p["changed"]]
    summary: dict = {
        "total": len(plan),
        "changed": len(changed),
        "applied": 0,
        "errors": [],
        "titles": [p["title"] for p in changed],
    }
    if not apply:
        return summary
    for p in changed:
        body = {"id": p["id"], "snippet": {
            "title": p["title"][:100] or "Shadow Edge Tools",
            "categoryId": p["categoryId"] or "22",
            "description": p["new"],
        }}
        if p["tags"]:
            body["snippet"]["tags"] = p["tags"]
        try:
            yt.videos().update(part="snippet", body=body).execute()
            summary["applied"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"{p['id']}: {exc}")
    return summary


def run(*, apply: bool = False) -> int:
    try:
        s = backfill(apply=apply)
    except Exception as exc:  # noqa: BLE001
        print(exc)
        return 1

    print(f"{s['total']} videos on channel · {s['changed']} need a funnel fix")
    for t in s["titles"]:
        print(f"  ● {t[:70]}")
    if not s["changed"]:
        print("Nothing to backfill — every video already carries the tracked link.")
        return 0
    if not apply:
        print(f"\nDRY RUN. Re-run with --apply to update these {s['changed']} descriptions.")
        return 0
    for e in s["errors"]:
        print(f"  ✗ {e}")
    print(f"\nBackfilled {s['applied']}/{s['changed']} videos.")
    return 0 if s["applied"] == s["changed"] else 1


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    args = ap.parse_args(argv)
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
