"""Canonical tracking contract and local event log."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from hermes_store import CONFIG_DIR, STATE_DIR, append_jsonl, read_jsonl, utc_now_iso, write_text

EVENTS_PATH = STATE_DIR / "events.jsonl"
TRACKING_CONFIG = CONFIG_DIR / "tracking.yaml"
CANONICAL_EVENTS = {
    "page_view",
    "lead_capture",
    "email_queued",
    "email_sent_manual",
    "publish_queued",
    "publish_approved",
    "publish_manual",
    "conversion",
    "revenue",
}
REQUIRED_FIELDS = {"event", "timestamp", "campaign_id", "source"}
UTM_FIELDS = ["utm_source", "utm_medium", "utm_campaign", "utm_content"]


def validate_event(row: dict[str, Any]) -> dict[str, Any]:
    event = str(row.get("event", "")).strip()
    if event not in CANONICAL_EVENTS:
        raise ValueError(f"unknown canonical event: {event}")
    payload = dict(row)
    payload.setdefault("timestamp", utc_now_iso())
    payload.setdefault("campaign_id", "")
    payload.setdefault("source", payload.get("utm_source", "manual"))
    missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
    if missing:
        raise ValueError("missing required event fields: " + ", ".join(missing))
    payload.setdefault("utm_campaign", payload.get("campaign_id", ""))
    for field in UTM_FIELDS:
        payload.setdefault(field, "")
    payload.setdefault("value", "")
    return payload


def append_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = validate_event(row)
    append_jsonl(EVENTS_PATH, payload)
    return payload


def write_default_config() -> Path:
    if TRACKING_CONFIG.exists():
        return TRACKING_CONFIG
    content = {
        "canonical_events": sorted(CANONICAL_EVENTS),
        "required_fields": sorted(REQUIRED_FIELDS),
        "utm_schema": {"utm_source": "platform name", "utm_medium": "social|shorts|video|email|partner|organic", "utm_campaign": "campaign id", "utm_content": "asset id"},
        "event_log": "state/events.jsonl",
        "manual_import": "state/manual-metrics/*.csv",
    }
    text = yaml.safe_dump(content, sort_keys=False) if yaml else json.dumps(content, indent=2)
    write_text(TRACKING_CONFIG, text)
    return TRACKING_CONFIG


def metric_to_event(metric: str) -> str:
    return {
        "website_sessions_by_source": "page_view",
        "landing_page_clicks": "page_view",
        "email_captures": "lead_capture",
        "email_opens_clicks_replies_unsubscribes": "email_sent_manual",
        "x_engagement": "publish_manual",
        "stocktwits_engagement": "publish_manual",
        "tiktok_engagement": "publish_manual",
        "linkedin_engagement": "publish_manual",
        "youtube_watch_metrics": "publish_manual",
        "conversions": "conversion",
        "revenue": "revenue",
    }.get(metric, "")


def tracking_status(metric_date: str | None = None) -> dict[str, Any]:
    write_default_config()
    rows = read_jsonl(EVENTS_PATH)
    if metric_date:
        rows = [r for r in rows if str(r.get("timestamp", "")).startswith(metric_date) or r.get("date") == metric_date]
    seen = sorted({r.get("event") for r in rows if r.get("event")})
    return {
        "config": str(TRACKING_CONFIG),
        "event_log": str(EVENTS_PATH),
        "canonical_events": sorted(CANONICAL_EVENTS),
        "events_recorded": len(rows),
        "events_seen": seen,
        "status": "ready_manual" if TRACKING_CONFIG.exists() else "needs_review",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    add = sub.add_parser("add")
    add.add_argument("event", choices=sorted(CANONICAL_EVENTS))
    add.add_argument("--campaign-id", required=True)
    add.add_argument("--source", default="manual")
    add.add_argument("--value", default="")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        print(write_default_config())
    elif args.cmd == "status":
        print(json.dumps(tracking_status(), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(append_event({"event": args.event, "campaign_id": args.campaign_id, "source": args.source, "value": args.value}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
