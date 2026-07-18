"""Lead capture state and CSV export for Shadow Edge."""
from __future__ import annotations

import argparse
import re
import threading
from pathlib import Path
from typing import Any

from hermes_store import STATE_DIR, append_jsonl, read_jsonl, utc_now_iso, write_csv, write_jsonl

LEADS_PATH = STATE_DIR / "leads.jsonl"
EMAIL_EVENTS_PATH = STATE_DIR / "email-events.jsonl"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LEADS_LOCK = threading.RLock()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))


def list_leads() -> list[dict[str, Any]]:
    return read_jsonl(LEADS_PATH)


def capture_lead(
    *,
    email: str,
    campaign_id: str,
    utm_source: str = "",
    utm_medium: str = "",
    utm_content: str = "",
    consent: bool = False,
    landing_page: str = "",
    user_agent: str = "",
    name: str = "",
    interest: str = "",
    note: str = "",
) -> tuple[dict[str, Any], bool]:
    with _LEADS_LOCK:
        normalized = normalize_email(email)
        if not is_valid_email(normalized):
            raise ValueError("valid email is required")
        if not campaign_id:
            raise ValueError("campaign_id is required")
        now = utc_now_iso()
        rows = list_leads()
        latest = {
            "campaign_id": campaign_id,
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_content": utm_content,
            "landing_page": landing_page,
            "user_agent": user_agent,
            "interest": _clean(interest, 160),
            "note": _clean(note, 1000),
            "timestamp": now,
        }
        for idx, row in enumerate(rows):
            if row.get("email") == normalized:
                row.setdefault("first_touch", row.get("latest_touch", latest))
                row["latest_touch"] = latest
                row["updated_at"] = now
                if _clean(name, 160):
                    row["name"] = _clean(name, 160)
                if _clean(interest, 160):
                    row["interest"] = _clean(interest, 160)
                if _clean(note, 1000):
                    row["lead_note"] = _clean(note, 1000)
                row["consent"] = bool(row.get("consent") or consent)
                if consent and not row.get("consent_timestamp"):
                    row["consent_timestamp"] = now
                rows[idx] = row
                write_jsonl(LEADS_PATH, rows)
                append_jsonl(EMAIL_EVENTS_PATH, {"event": "lead_updated", "email": normalized, "campaign_id": campaign_id, "timestamp": now, "consent": bool(consent)})
                return row, False
        row = {
            "email": normalized,
            "name": _clean(name, 160),
            "created_at": now,
            "updated_at": now,
            "consent": bool(consent),
            "consent_source": landing_page or campaign_id,
            "consent_timestamp": now if consent else "",
            "suppressed": False,
            "suppression_reason": "",
            "interest": _clean(interest, 160),
            "lead_note": _clean(note, 1000),
            "first_touch": latest,
            "latest_touch": latest,
        }
        rows.append(row)
        write_jsonl(LEADS_PATH, rows)
        append_jsonl(EMAIL_EVENTS_PATH, {"event": "lead_created", "email": normalized, "campaign_id": campaign_id, "timestamp": now, "consent": bool(consent)})
        return row, True


def _clean(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:max_length]


def suppress_lead(email: str, reason: str) -> bool:
    with _LEADS_LOCK:
        normalized = normalize_email(email)
        rows = list_leads()
        changed = False
        for row in rows:
            if row.get("email") == normalized:
                row["suppressed"] = True
                row["suppression_reason"] = reason
                row["updated_at"] = utc_now_iso()
                changed = True
        if changed:
            write_jsonl(LEADS_PATH, rows)
            append_jsonl(EMAIL_EVENTS_PATH, {"event": "suppressed", "email": normalized, "reason": reason, "timestamp": utc_now_iso()})
        return changed


def export_csv(path: Path) -> Path:
    rows = list_leads()
    flat = []
    for row in rows:
        first = row.get("first_touch") or {}
        latest = row.get("latest_touch") or {}
        flat.append({
            "email": row.get("email"),
            "name": row.get("name"),
            "interest": row.get("interest"),
            "lead_note": row.get("lead_note"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "consent": row.get("consent"),
            "consent_timestamp": row.get("consent_timestamp"),
            "suppressed": row.get("suppressed"),
            "suppression_reason": row.get("suppression_reason"),
            "first_campaign_id": first.get("campaign_id"),
            "first_utm_source": first.get("utm_source"),
            "first_utm_medium": first.get("utm_medium"),
            "first_utm_content": first.get("utm_content"),
            "first_landing_page": first.get("landing_page"),
            "latest_campaign_id": latest.get("campaign_id"),
            "latest_utm_source": latest.get("utm_source"),
            "latest_utm_medium": latest.get("utm_medium"),
            "latest_utm_content": latest.get("utm_content"),
            "latest_landing_page": latest.get("landing_page"),
        })
    write_csv(path, flat)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("email")
    add.add_argument("--campaign-id", required=True)
    add.add_argument("--consent", action="store_true")
    add.add_argument("--utm-source", default="manual")
    add.add_argument("--utm-medium", default="organic")
    add.add_argument("--utm-content", default="cli")
    exp = sub.add_parser("export")
    exp.add_argument("path")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        lead, created = capture_lead(email=args.email, campaign_id=args.campaign_id, consent=args.consent, utm_source=args.utm_source, utm_medium=args.utm_medium, utm_content=args.utm_content, landing_page="cli")
        print(("created" if created else "updated") + ": " + lead["email"])
    elif args.cmd == "export":
        print(export_csv(Path(args.path)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
