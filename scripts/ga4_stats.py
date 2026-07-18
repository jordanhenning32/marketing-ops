"""GA4 Data API pull -> Performance scoreboard.

Pulls visitors / page views / avg engagement time / top page from the Google
Analytics 4 Data API and records them into performance.jsonl, so the Website
section of the scoreboard auto-fills (and trends) the same way YouTube does.

One-time setup:
  1. Google Cloud: enable the "Google Analytics Data API", create a service
     account, download its JSON key.
  2. GA4 -> Admin -> Property Access Management -> add the service-account email
     as a Viewer on the property.
  3. Drop the key at state/.ga4/service-account.json (or point
     GA4_SERVICE_ACCOUNT_FILE at it). Set GA4_PROPERTY_ID in .env.

The key lives under state/.ga4/ (gitignored). We never commit it.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from cli_io import configure_utf8_stdio

try:
    from dotenv import load_dotenv

    for _c in (_ROOT / ".env", _ROOT / "config" / ".env"):
        if _c.exists():
            load_dotenv(_c, override=False)
except Exception:  # pragma: no cover
    pass

GA4_DIR = _ROOT / "state" / ".ga4"
LAST_PULL_PATH = GA4_DIR / "last-pull.json"


def property_id() -> str:
    return os.environ.get("GA4_PROPERTY_ID", "").strip()


def key_path() -> Path:
    override = os.environ.get("GA4_SERVICE_ACCOUNT_FILE", "").strip()
    return Path(override) if override else GA4_DIR / "service-account.json"


def is_configured() -> bool:
    return bool(property_id()) and key_path().exists()


def _client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    return BetaAnalyticsDataClient.from_service_account_file(str(key_path()))


def pull(*, days: int = 30, record: bool = True) -> dict:
    """Run a report and (optionally) record the metrics for the scoreboard."""
    if not property_id():
        raise RuntimeError("GA4_PROPERTY_ID not set in .env")
    if not key_path().exists():
        raise RuntimeError(f"GA4 service-account key not found at {key_path()}")

    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        OrderBy,
        RunReportRequest,
    )

    client = _client()
    prop = f"properties/{property_id()}"
    rng = [DateRange(start_date=f"{days}daysAgo", end_date="today")]

    totals = client.run_report(
        RunReportRequest(
            property=prop,
            date_ranges=rng,
            metrics=[
                Metric(name="activeUsers"),
                Metric(name="screenPageViews"),
                Metric(name="averageSessionDuration"),
            ],
        )
    )
    visitors = pageviews = avg = 0
    if totals.rows:
        mv = totals.rows[0].metric_values
        visitors = int(float(mv[0].value or 0))
        pageviews = int(float(mv[1].value or 0))
        avg = round(float(mv[2].value or 0))

    top = client.run_report(
        RunReportRequest(
            property=prop,
            date_ranges=rng,
            dimensions=[Dimension(name="pagePath")],
            metrics=[Metric(name="screenPageViews")],
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
            limit=5,
        )
    )
    top_pages = [
        {"path": r.dimension_values[0].value, "views": int(float(r.metric_values[0].value or 0))}
        for r in top.rows
    ]

    summary = {
        "visitors": visitors,
        "pageviews": pageviews,
        "avg_time": avg,
        "top_pages": top_pages,
        "top_page": top_pages[0]["path"] if top_pages else "",
        "days": days,
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
    }
    GA4_DIR.mkdir(parents=True, exist_ok=True)
    LAST_PULL_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if record:
        import analytics

        analytics.record_metric("website_visitors", visitors, source="ga4")
        analytics.record_metric("website_pageviews", pageviews, source="ga4")
        analytics.record_metric("website_avg_time", avg, source="ga4")
        if summary["top_page"]:
            analytics.record_metric("website_top_page", summary["top_page"], source="ga4")
    return summary


def last_pull() -> Optional[dict]:
    try:
        return json.loads(LAST_PULL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def main(argv: Optional[list] = None) -> int:
    """CLI entry (`python ga4_stats.py pull`) — used by the daily scheduled job."""
    configure_utf8_stdio()
    try:
        s = pull()
        print(
            f"GA4: {s['visitors']} visitors, {s['pageviews']} page views, "
            f"top {s['top_page']} (last {s['days']}d) at {s['pulled_at']}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"GA4 pull failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
