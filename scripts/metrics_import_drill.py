"""First metrics import drill without fabricating values."""
from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

import analytics
from hermes_store import STATE_DIR, utc_now_iso, write_text

DRILL_JSON = STATE_DIR / "metrics-import-drill.json"
DRILL_MD = STATE_DIR / "metrics-import-drill.md"


def run_drill(metric_date: str | None = None, campaign_id: str = "") -> dict[str, Any]:
    metric_date = metric_date or date.today().isoformat()
    campaign_id = campaign_id or "manual-metrics-drill"
    template = analytics.generate_daily_template(metric_date, campaign_id)
    validation_errors = analytics.validate_csv(template)
    dry_run = analytics.dry_run_import(template)
    report = analytics.report(metric_date, campaign_id)
    payload = {
        "generated_at": utc_now_iso(),
        "date": metric_date,
        "campaign_id": campaign_id,
        "template": str(template),
        "validation_errors": validation_errors,
        "dry_run": dry_run,
        "measured": report.get("measured", {}),
        "unavailable": report.get("unavailable", []),
        "blocked": report.get("blocked", []),
        "not_configured": report.get("not_configured", []),
        "fabricated_values": False,
    }
    write_text(DRILL_JSON, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    lines = [f"# Metrics Import Drill - {metric_date}", "", f"Campaign: {campaign_id}", f"Template: `{template}`", f"Validation errors: {len(validation_errors)}", f"Dry-run rows: {dry_run.get('would_import', 0)}", "", "## Status Buckets"]
    lines += [f"- measured: {len(payload['measured'])}", f"- unavailable: {len(payload['unavailable'])}", f"- blocked: {len(payload['blocked'])}", f"- not_configured: {len(payload['not_configured'])}", "", "No values were fabricated or imported during this drill."]
    write_text(DRILL_MD, "\n".join(lines) + "\n")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", dest="metric_date")
    parser.add_argument("--campaign-id", default="")
    args = parser.parse_args(argv)
    print(json.dumps(run_drill(args.metric_date, args.campaign_id), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
