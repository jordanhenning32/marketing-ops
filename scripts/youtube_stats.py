"""YouTube performance feedback loop.

Pulls per-video + channel statistics from the YouTube Data API v3 (covered by
the `youtube.readonly` scope we already hold) and folds them back into the
system so the daily loop can SEE what is working:

    * append-only time series  -> state/youtube-video-stats.jsonl
                                  state/youtube-channel-stats.jsonl
    * write-back into each clip -> content/<run>/distribution/youtube/<NN>/metadata.json  ("stats" block)
    * roll-up into analytics    -> state/performance.jsonl  (metric: youtube_watch_metrics)

The loop covers the WHOLE channel (via the uploads playlist), not just clips the
pipeline uploaded, so videos posted by hand still show up. Deltas are computed
against the previous snapshot, which is the actual feedback signal: "this video
gained N views since the last pull".

Scope note: views / likes / comments come from the Data API and need no extra
permission. Watch-time, average-view-duration and traffic sources require the
YouTube *Analytics* API (scope `yt-analytics.readonly`), which we do NOT hold;
add that scope and re-auth to unlock them. This module is honest about that and
only reports what the current scope actually returns.

CLI:
    python scripts/youtube_stats.py pull      # fetch + store + write-back
    python scripts/youtube_stats.py show      # print the latest table (no fetch)
    python scripts/youtube_stats.py videos    # list locally-tracked uploads
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from googleapiclient.discovery import build

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import youtube_auth  # noqa: E402

ROOT = _HERE.parent
CONTENT_DIR = ROOT / "content"
STATE_DIR = ROOT / "state"
VIDEO_STATS_PATH = STATE_DIR / "youtube-video-stats.jsonl"
CHANNEL_STATS_PATH = STATE_DIR / "youtube-channel-stats.jsonl"

# Data API allows up to 50 ids per videos.list / playlistItems page.
_BATCH = 50


# --- small jsonl helpers (kept local so this module stands alone) -----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- discovery: what have we uploaded? --------------------------------------

def local_video_index() -> dict[str, dict[str, Any]]:
    """Map video_id -> local clip info, scanning every YouTube kit metadata.

    This is the bridge that lets channel-wide stats attach back to the run /
    clip that produced them, so a hot video points at its source content.
    """
    index: dict[str, dict[str, Any]] = {}
    for meta_path in CONTENT_DIR.glob("*/distribution/youtube/*/metadata.json"):
        try:
            md = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vid = md.get("video_id")
        if not vid:
            continue
        # content/<slug>/distribution/youtube/<NN>/metadata.json
        try:
            clip_index = int(meta_path.parent.name)
        except ValueError:
            clip_index = None
        slug = meta_path.parents[3].name
        index[vid] = {
            "slug": slug,
            "clip_index": clip_index,
            "metadata_path": meta_path,
            "title_used": md.get("title_used"),
            "platform_url": md.get("platform_url"),
            "uploaded_at": md.get("uploaded_at"),
            "privacy": md.get("privacy"),
        }
    return index


# --- API calls --------------------------------------------------------------

def _client():
    creds = youtube_auth.get_authenticated_credentials()
    if not creds:
        raise RuntimeError(
            "YouTube not connected — visit /youtube and click Connect "
            "(or run scripts/youtube_auth.py)."
        )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _chunked(items: list[str], size: int = _BATCH) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def channel_upload_ids(yt) -> list[str]:
    """Every video_id on the authenticated channel, via its uploads playlist."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        return []
    uploads = (
        items[0].get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads:
        return []
    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            yt.playlistItems()
            .list(part="contentDetails", playlistId=uploads, maxResults=_BATCH, pageToken=page_token)
            .execute()
        )
        for it in resp.get("items", []):
            vid = (it.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_stats(yt, video_ids: list[str]) -> dict[str, dict[str, Any]]:
    """video_id -> {title, published_at, privacy, views, likes, comments, favorites}."""
    out: dict[str, dict[str, Any]] = {}
    ids = [v for v in dict.fromkeys(video_ids) if v]  # dedupe, drop falsy
    for batch in _chunked(ids):
        resp = (
            yt.videos()
            .list(part="snippet,statistics,status", id=",".join(batch))
            .execute()
        )
        for v in resp.get("items", []):
            st = v.get("statistics", {})
            sn = v.get("snippet", {})
            out[v["id"]] = {
                "title": sn.get("title", ""),
                "published_at": sn.get("publishedAt"),
                "privacy": (v.get("status") or {}).get("privacyStatus"),
                "views": _to_int(st.get("viewCount")),
                "likes": _to_int(st.get("likeCount")),
                "comments": _to_int(st.get("commentCount")),
                "favorites": _to_int(st.get("favoriteCount")),
            }
    return out


def fetch_channel_stats(yt) -> dict[str, Any]:
    ch = yt.channels().list(part="snippet,statistics", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        return {}
    st = items[0].get("statistics", {})
    sn = items[0].get("snippet", {})
    return {
        "channel_id": items[0].get("id"),
        "title": sn.get("title", ""),
        "subscribers": _to_int(st.get("subscriberCount")),
        "views": _to_int(st.get("viewCount")),
        "videos": _to_int(st.get("videoCount")),
    }


# --- the feedback loop ------------------------------------------------------

def _latest_by_video(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r.get("video_id", ""), []).append(r)
    for vid in by:
        by[vid].sort(key=lambda r: r.get("pulled_at", ""))
    return by


def pull(*, write_back: bool = True, record_analytics: bool = True) -> dict[str, Any]:
    """Fetch current stats for the whole channel, store a snapshot, fold back in."""
    yt = _client()
    local_idx = local_video_index()

    # Whole-channel coverage, falling back to locally-tracked ids if the
    # playlist lookup is unavailable for any reason.
    try:
        channel_ids = channel_upload_ids(yt)
    except Exception:  # noqa: BLE001 - never let enumeration kill the pull
        channel_ids = []
    all_ids = list(dict.fromkeys([*channel_ids, *local_idx.keys()]))

    stats = fetch_video_stats(yt, all_ids)
    channel = fetch_channel_stats(yt)

    prev = _latest_by_video(_read_jsonl(VIDEO_STATS_PATH))
    pulled_at = _now()

    snapshots: list[dict[str, Any]] = []
    for vid, s in stats.items():
        local = local_idx.get(vid, {})
        prev_rows = prev.get(vid)
        prev_views = prev_rows[-1].get("views") if prev_rows else None
        row = {
            "pulled_at": pulled_at,
            "video_id": vid,
            "title": local.get("title_used") or s["title"],
            "url": local.get("platform_url") or f"https://www.youtube.com/shorts/{vid}",
            "privacy": s.get("privacy") or local.get("privacy"),
            "published_at": s.get("published_at"),
            "uploaded_at": local.get("uploaded_at"),
            "slug": local.get("slug"),
            "clip_index": local.get("clip_index"),
            "tracked": vid in local_idx,
            "views": s["views"],
            "likes": s["likes"],
            "comments": s["comments"],
            "favorites": s["favorites"],
            "delta_views": (s["views"] - prev_views) if prev_views is not None else None,
        }
        snapshots.append(row)
        _append_jsonl(VIDEO_STATS_PATH, row)

    if channel:
        _append_jsonl(CHANNEL_STATS_PATH, {"pulled_at": pulled_at, **channel})

    if write_back:
        _write_back_metadata(snapshots, local_idx)

    if record_analytics and snapshots:
        _record_analytics_rollup(channel, snapshots)

    snapshots.sort(key=lambda r: r["views"], reverse=True)
    return {
        "pulled_at": pulled_at,
        "channel": channel,
        "videos": snapshots,
        "video_count": len(snapshots),
        "tracked_count": sum(1 for r in snapshots if r["tracked"]),
    }


def _write_back_metadata(snapshots: list[dict[str, Any]], local_idx: dict[str, dict[str, Any]]) -> None:
    for row in snapshots:
        local = local_idx.get(row["video_id"])
        if not local:
            continue
        path: Path = local["metadata_path"]
        try:
            md = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        md["stats"] = {
            "pulled_at": row["pulled_at"],
            "views": row["views"],
            "likes": row["likes"],
            "comments": row["comments"],
            "delta_views": row["delta_views"],
        }
        path.write_text(json.dumps(md, indent=2), encoding="utf-8")


def _record_analytics_rollup(channel: dict[str, Any], snapshots: list[dict[str, Any]]) -> None:
    """Surface a single measured line on /performance so YouTube stops reading
    as 'unavailable'. Honest scope: this is Data-API engagement, not watch-time."""
    try:
        import analytics
    except Exception:  # noqa: BLE001
        return
    total_views = channel.get("views") if channel else sum(r["views"] for r in snapshots)
    total_likes = sum(r["likes"] for r in snapshots)
    subs = channel.get("subscribers") if channel else "?"
    value = (
        f"views={total_views} likes={total_likes} subs={subs} "
        f"across {len(snapshots)} videos (Data API: views/likes/comments)"
    )
    try:
        analytics.record_metric(
            "youtube_watch_metrics", value, source="youtube_api", status="measured"
        )
    except Exception:  # noqa: BLE001 - analytics is a nice-to-have, never block the pull
        pass


# --- read-side accessors (for console / CLI, no network) --------------------

def video_summary() -> list[dict[str, Any]]:
    """Latest snapshot per video + delta vs the prior snapshot, hottest first."""
    by = _latest_by_video(_read_jsonl(VIDEO_STATS_PATH))
    out: list[dict[str, Any]] = []
    for vid, rows in by.items():
        if not vid or not rows:
            continue
        latest = dict(rows[-1])
        if len(rows) >= 2:
            latest["delta_views"] = latest.get("views", 0) - rows[-2].get("views", 0)
        else:
            latest.setdefault("delta_views", None)  # template always reads this field
        out.append(latest)
    out.sort(key=lambda r: r.get("views", 0), reverse=True)
    return out


def channel_summary() -> dict[str, Any]:
    rows = _read_jsonl(CHANNEL_STATS_PATH)
    if not rows:
        return {}
    rows.sort(key=lambda r: r.get("pulled_at", ""))
    latest = dict(rows[-1])
    if len(rows) >= 2:
        latest["delta_views"] = latest.get("views", 0) - rows[-2].get("views", 0)
        latest["delta_subscribers"] = latest.get("subscribers", 0) - rows[-2].get("subscribers", 0)
    else:
        # template always reads these; guarantee them on the first snapshot
        latest.setdefault("delta_views", None)
        latest.setdefault("delta_subscribers", None)
    return latest


def last_pull_at() -> str | None:
    rows = _read_jsonl(VIDEO_STATS_PATH)
    return max((r.get("pulled_at", "") for r in rows), default=None) or None


# --- CLI --------------------------------------------------------------------

def _print_table(videos: list[dict[str, Any]], channel: dict[str, Any]) -> None:
    if channel:
        d = channel.get("delta_views")
        delta = f"  ({d:+d} since last pull)" if isinstance(d, int) else ""
        print(
            f"CHANNEL {channel.get('title','')}: subs={channel.get('subscribers')} "
            f"views={channel.get('views')} videos={channel.get('videos')}{delta}\n"
        )
    print(f"{'VIEWS':>6} {'+/-':>5} {'LIKES':>5}  TITLE")
    for v in videos:
        d = v.get("delta_views")
        delta = f"{d:+d}" if isinstance(d, int) else "·"
        flag = "" if v.get("tracked", True) else "  [untracked]"
        print(f"{v.get('views',0):>6} {delta:>5} {v.get('likes',0):>5}  {(v.get('title') or '')[:54]}{flag}")


def main(argv: list[str] | None = None) -> int:
    # Video titles can carry smart quotes / emoji; the Windows console defaults
    # to cp1252 and would crash on them. Never let presentation kill the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="YouTube performance feedback loop")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull", help="fetch live stats, store snapshot, write back")
    sub.add_parser("show", help="print the latest stored table (no fetch)")
    sub.add_parser("videos", help="list locally-tracked uploads")
    args = parser.parse_args(argv)

    if args.cmd == "pull":
        result = pull()
        _print_table(result["videos"], channel_summary())
        print(
            f"\nStored snapshot at {result['pulled_at']} · "
            f"{result['video_count']} videos ({result['tracked_count']} tracked locally)"
        )
    elif args.cmd == "show":
        videos = video_summary()
        if not videos:
            print("No stored stats yet. Run: python scripts/youtube_stats.py pull")
            return 0
        _print_table(videos, channel_summary())
        print(f"\nLast pull: {last_pull_at()}")
    elif args.cmd == "videos":
        idx = local_video_index()
        if not idx:
            print("No locally-tracked YouTube uploads found.")
            return 0
        for vid, info in idx.items():
            print(f"{vid}  {info.get('slug')}/clip-{info.get('clip_index')}  {info.get('title_used') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
