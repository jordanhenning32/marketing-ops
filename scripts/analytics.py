"""Analytics import and daily measured/unavailable metric reporting."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from hermes_store import STATE_DIR, append_jsonl, read_jsonl, utc_now_iso
from leads import list_leads

PERFORMANCE_PATH = STATE_DIR / "performance.jsonl"
PUBLISH_QUEUE_PATH = STATE_DIR / "publish-queue.jsonl"
PUBLISH_LOG_PATH = STATE_DIR / "publish-log.jsonl"
REQUIRED_METRICS = [
    "website_sessions_by_source",
    "landing_page_clicks",
    "email_captures",
    "email_opens_clicks_replies_unsubscribes",
    "x_engagement",
    "stocktwits_engagement",
    "tiktok_engagement",
    "linkedin_engagement",
    "youtube_watch_metrics",
    "conversions",
    "revenue",
]


ALLOWED_STATUSES = {"measured", "unavailable", "blocked", "not_configured"}
REQUIRED_COLUMNS = {"date", "campaign_id", "metric", "value", "source", "status"}

# The performance scoreboard (console._SCOREBOARD_SECTIONS) reads these keys out
# of performance.jsonl. They live here too so a CSV import / manual `record` that
# targets a scoreboard card validates instead of being rejected as an "unknown
# metric" — the two vocabularies stayed divergent for a long time and that's why
# the TikTok/X/LinkedIn/Instagram cards never filled. Keep in sync with the
# scoreboard sections (test_analytics guards this).
SCOREBOARD_METRICS = [
    "revenue", "trials",
    "website_visitors", "website_pageviews", "website_avg_time", "website_top_page",
    "tiktok_followers", "tiktok_views", "tiktok_likes",
    "x_followers", "x_impressions", "x_interactions",
    "linkedin_followers", "linkedin_impressions", "linkedin_interactions",
    "instagram_followers", "instagram_views", "instagram_interactions",
    "leads", "customers",
]

# Everything a CSV import / manual record may target.
KNOWN_METRICS = set(REQUIRED_METRICS) | set(SCOREBOARD_METRICS)


def record_metric(metric: str, value: Any, *, source: str = "manual", metric_date: str | None = None, campaign_id: str = "", status: str = "measured") -> dict[str, Any]:
    row = {"metric": metric, "value": value, "source": source, "date": metric_date or date.today().isoformat(), "campaign_id": campaign_id, "status": status or "measured", "recorded_at": utc_now_iso()}
    append_jsonl(PERFORMANCE_PATH, row)
    return row


def validate_csv(path: Path) -> list[str]:
    errors: list[str] = []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            return ["missing required columns: " + ", ".join(sorted(missing))]
        for idx, row in enumerate(reader, start=2):
            metric = (row.get("metric") or "").strip()
            status = (row.get("status") or "").strip() or "measured"
            if metric not in KNOWN_METRICS:
                errors.append(f"line {idx}: unknown metric {metric}")
            if status not in ALLOWED_STATUSES:
                errors.append(f"line {idx}: invalid status {status}")
            event = (row.get("event") or "").strip()
            if event:
                try:
                    import tracking
                    tracking.validate_event({
                        "event": event,
                        "timestamp": (row.get("timestamp") or f"{row.get('date', '')}T00:00:00+00:00"),
                        "campaign_id": row.get("campaign_id", ""),
                        "source": row.get("source", "manual_csv"),
                        "utm_source": row.get("utm_source", ""),
                        "utm_medium": row.get("utm_medium", ""),
                        "utm_content": row.get("utm_content", ""),
                        "value": row.get("value", ""),
                    })
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"line {idx}: invalid event {event}: {exc}")
            if not row.get("date"):
                errors.append(f"line {idx}: missing date")
            if not row.get("campaign_id"):
                errors.append(f"line {idx}: missing campaign_id")
    return errors


def generate_template(path: Path, metric_date: str, campaign_id: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "campaign_id", "metric", "value", "source", "status", "event", "timestamp", "utm_source", "utm_medium", "utm_content", "notes"])
        writer.writeheader()
        for metric in REQUIRED_METRICS:
            writer.writerow({"date": metric_date, "campaign_id": campaign_id, "metric": metric, "value": "", "source": "manual_csv", "status": "unavailable", "event": "", "timestamp": "", "utm_source": "", "utm_medium": "", "utm_content": "", "notes": "fill value and set status=measured when known"})
    return path


def generate_daily_template(metric_date: str, campaign_id: str) -> Path:
    return generate_template(STATE_DIR / "manual-metrics" / f"{metric_date}-template.csv", metric_date, campaign_id)


def dry_run_import(path: Path) -> dict[str, Any]:
    errors = validate_csv(path)
    rows: list[dict[str, Any]] = []
    if not errors:
        with Path(path).open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("metric") or "").strip():
                    rows.append(dict(row))
    statuses = Counter((row.get("status") or "measured").strip() or "measured" for row in rows)
    return {"path": str(path), "errors": errors, "would_import": 0 if errors else len(rows), "status_counts": dict(statuses), "rows": rows[:5]}


def import_csv(path: Path) -> int:
    errors = validate_csv(path)
    if errors:
        raise ValueError("; ".join(errors))
    count = 0
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            metric = row.get("metric", "").strip()
            if not metric:
                continue
            record_metric(metric, row.get("value", ""), source=row.get("source", "manual_csv") or "manual_csv", metric_date=row.get("date") or None, campaign_id=row.get("campaign_id", ""), status=row.get("status", "measured") or "measured")
            event = (row.get("event") or "").strip()
            if event:
                try:
                    import tracking
                    tracking.append_event({
                        "event": event,
                        "timestamp": row.get("timestamp") or f"{row.get('date')}T00:00:00+00:00",
                        "campaign_id": row.get("campaign_id", ""),
                        "source": row.get("source", "manual_csv") or "manual_csv",
                        "utm_source": row.get("utm_source", ""),
                        "utm_medium": row.get("utm_medium", ""),
                        "utm_content": row.get("utm_content", ""),
                        "value": row.get("value", ""),
                        "metric": metric,
                    })
                except Exception:
                    raise
            count += 1
    return count


def publish_queue_summary(campaign_id: str = "") -> dict[str, int]:
    rows = [r for r in read_jsonl(PUBLISH_QUEUE_PATH) if not campaign_id or r.get("campaign_id") == campaign_id]
    return dict(Counter(r.get("status", "unknown") for r in rows))


def report(metric_date: str | None = None, campaign_id: str = "") -> dict[str, Any]:
    metric_date = metric_date or date.today().isoformat()
    rows = [r for r in read_jsonl(PERFORMANCE_PATH) if r.get("date") == metric_date and (not campaign_id or r.get("campaign_id") in {"", campaign_id})]
    measured: dict[str, Any] = {}
    blocked: list[str] = []
    not_configured: list[str] = []
    for r in rows:
        metric = r.get("metric")
        if not metric:
            continue
        status = r.get("status", "measured")
        if status == "blocked":
            if metric not in blocked:
                blocked.append(metric)
        elif status == "not_configured":
            if metric not in not_configured:
                not_configured.append(metric)
        elif status == "unavailable":
            continue
        elif status == "measured" and str(r.get("value", "")).strip() != "":
            measured[metric] = r.get("value")
    leads = list_leads()
    if "email_captures" not in measured and "email_captures" not in blocked and "email_captures" not in not_configured:
        if campaign_id:
            measured["email_captures"] = sum(1 for l in leads if (l.get("latest_touch") or {}).get("campaign_id") == campaign_id)
        else:
            measured["email_captures"] = len(leads)
    unavailable = [m for m in REQUIRED_METRICS if m not in measured and m not in blocked and m not in not_configured]
    return {"date": metric_date, "campaign_id": campaign_id, "measured": measured, "unavailable": unavailable, "blocked": blocked, "not_configured": not_configured, "publish_queue": publish_queue_summary(campaign_id)}


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [f"# Performance Report - {summary['date']}", "", "## Measured"]
    for k, v in summary["measured"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Publish Queue"]
    if summary.get("publish_queue"):
        for k, v in summary["publish_queue"].items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- none")
    lines += ["", "## Unavailable"]
    for item in summary["unavailable"]:
        lines.append(f"- {item}: unavailable")
    lines += ["", "## Blocked"]
    if summary.get("blocked"):
        for item in summary["blocked"]:
            lines.append(f"- {item}: blocked")
    else:
        lines.append("- none")
    lines += ["", "## Not Configured"]
    if summary.get("not_configured"):
        for item in summary["not_configured"]:
            lines.append(f"- {item}: not_configured")
    else:
        lines.append("- none")
    return "\n".join(lines).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    rpt = sub.add_parser("report")
    rpt.add_argument("--date", dest="metric_date")
    rpt.add_argument("--campaign-id", default="")
    imp = sub.add_parser("import-csv")
    imp.add_argument("path")
    val = sub.add_parser("validate-csv")
    val.add_argument("path")
    tmpl = sub.add_parser("template")
    tmpl.add_argument("path")
    tmpl.add_argument("--date", dest="metric_date", required=True)
    tmpl.add_argument("--campaign-id", required=True)
    daily_tmpl = sub.add_parser("daily-template")
    daily_tmpl.add_argument("--date", dest="metric_date", required=True)
    daily_tmpl.add_argument("--campaign-id", required=True)
    dry = sub.add_parser("dry-run-import")
    dry.add_argument("path")
    rec = sub.add_parser("record")
    rec.add_argument("metric")
    rec.add_argument("value")
    rec.add_argument("--date", dest="metric_date")
    rec.add_argument("--source", default="manual")
    rec.add_argument("--status", default="measured")
    args = parser.parse_args(argv)
    if args.cmd == "report":
        print(render_markdown(report(args.metric_date, args.campaign_id)))
    elif args.cmd == "import-csv":
        print(f"imported {import_csv(Path(args.path))} metrics")
    elif args.cmd == "validate-csv":
        errors = validate_csv(Path(args.path))
        if errors:
            print("\n".join(errors))
            return 1
        print("ok")
    elif args.cmd == "template":
        print(generate_template(Path(args.path), args.metric_date, args.campaign_id))
    elif args.cmd == "daily-template":
        print(generate_daily_template(args.metric_date, args.campaign_id))
    elif args.cmd == "dry-run-import":
        print(json.dumps(dry_run_import(Path(args.path)), indent=2, ensure_ascii=False))
    elif args.cmd == "record":
        print(record_metric(args.metric, args.value, source=args.source, metric_date=args.metric_date, status=args.status))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
