"""TikTok profile stats -> Performance scoreboard.

Pulls follower count / total likes from the TikTok user.info endpoint and records
them into performance.jsonl, so the TikTok section of the scoreboard auto-fills
(and trends) the same way YouTube and GA4 do.

This is gated behind the user.info.stats scope (TIKTOK_STATS=1 + reconnect — see
tiktok_auth.stats_enabled). Until that scope is approved on the TikTok app the
pull is a safe no-op: is_configured() returns False and the TikTok section stays
manual-entry.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import tiktok_auth

TT_DIR = _ROOT / "state" / ".tiktok"
LAST_PULL_PATH = TT_DIR / "stats-last-pull.json"


def is_configured() -> bool:
    """True when TikTok is connected AND the stats scope is enabled."""
    return tiktok_auth.stats_enabled() and tiktok_auth.is_connected()


def pull(*, record: bool = True) -> dict:
    """Fetch profile stats and (optionally) record them for the scoreboard.

    Raises RuntimeError when the stats scope isn't enabled or nothing came back,
    so the refresh button can surface an actionable message."""
    if not tiktok_auth.stats_enabled():
        raise RuntimeError("TikTok stats scope not enabled — set TIKTOK_STATS=1 and reconnect")
    stats = tiktok_auth.fetch_user_stats()
    if not stats or stats.get("followers") is None:
        raise RuntimeError("TikTok returned no stats (reconnect to grant the user.info.stats scope)")

    summary = {
        "followers": stats.get("followers"),
        "likes": stats.get("likes"),
        "videos": stats.get("videos"),
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
    }
    TT_DIR.mkdir(parents=True, exist_ok=True)
    LAST_PULL_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if record:
        import analytics

        analytics.record_metric("tiktok_followers", summary["followers"], source="tiktok")
        if summary["likes"] is not None:
            analytics.record_metric("tiktok_likes", summary["likes"], source="tiktok")
    return summary


def last_pull() -> Optional[dict]:
    try:
        return json.loads(LAST_PULL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def last_pull_at() -> Optional[str]:
    lp = last_pull()
    return lp.get("pulled_at") if lp else None


def main(argv: Optional[list] = None) -> int:
    """CLI entry (`python tiktok_stats.py pull`) — used by a daily scheduled job."""
    try:
        from cli_io import configure_utf8_stdio

        configure_utf8_stdio()
    except Exception:
        pass
    try:
        s = pull()
        print(f"TikTok: {s['followers']} followers, {s['likes']} likes at {s['pulled_at']}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"TikTok stats pull failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
