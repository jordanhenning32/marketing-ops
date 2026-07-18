"""Consent-based email nurture queue. Sending is disabled unless explicitly wired."""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from hermes_store import CONFIG_DIR, CONTENT_DIR, STATE_DIR, append_jsonl, read_jsonl, utc_now_iso, write_jsonl

QUEUE_PATH = STATE_DIR / "email-queue.jsonl"
EVENTS_PATH = STATE_DIR / "email-events.jsonl"
DEFAULT_SEQUENCE_BODY_PATH = "content/email-sequences/risk-control-nurture.md"
FIXTURE_DOMAINS = {"example.com", "example.org", "example.net"}
STALE_DAYS = 14
SENT_STATUSES = {"sent", "sent_manual", "sent_provider"}
ARCHIVED_STATUSES = {"archived", "archived_duplicate", "archived_fixture"}
FILTER_LABELS = {
    "all": "All",
    "queued": "Queued",
    "due_now": "Due now",
    "future": "Future",
    "stale": "Stale",
    "fixture": "Fixture/example.com",
    "real": "Real domains",
    "skipped": "Skipped",
    "sent": "Sent",
}
URL_RE = re.compile(r"https?://[^\s<>)]+")
HEADING_RE = re.compile(r"^##\s+\d+\.\s+([a-z0-9_-]+)\b", re.IGNORECASE)
BLAST_QUEUE_CONFIRM = "QUEUE_BULK_BLAST"
BLAST_SEND_CONFIRM = "SEND_BULK_BLAST"
_QUEUE_LOCK = threading.RLock()

DEFAULT_SEQUENCE = [
    {"id": "confirmation", "delay_days": 0, "subject": "Confirmed: your Shadow Edge risk-control checklist"},
    {"id": "risk-rule", "delay_days": 1, "subject": "One rule before the first order"},
    {"id": "drawdown-guard", "delay_days": 3, "subject": "Where drawdown discipline breaks"},
    {"id": "order-lock", "delay_days": 5, "subject": "Removing one bad-click failure mode"},
    {"id": "demo", "delay_days": 7, "subject": "See the Shadow Edge workflow"},
]


def load_config() -> dict[str, Any]:
    path = CONFIG_DIR / "email.yaml"
    if yaml and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    data.setdefault("send_enabled", False)
    data.setdefault("provider", "none")
    data.setdefault("credential_env", "EMAIL_PROVIDER_API_KEY")
    data.setdefault("sequence", DEFAULT_SEQUENCE)
    data.setdefault("sequence_body_path", DEFAULT_SEQUENCE_BODY_PATH)
    return data


def list_queue(*, enrich: bool = True) -> list[dict[str, Any]]:
    rows = read_jsonl(QUEUE_PATH)
    if not enrich:
        return rows
    config = load_config()
    return [enrich_queue_item(row, config=config) for row in rows]


def _queue_key(email: str, campaign_id: str, step_id: str) -> str:
    return f"{email}|{campaign_id}|{step_id}"


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _due_at(created_at: str, delay_days: int) -> str:
    created = _parse_ts(created_at)
    if not created:
        return ""
    return (created + timedelta(days=int(delay_days or 0))).replace(microsecond=0).isoformat()


def _age_days(row: dict[str, Any], *, now: datetime | None = None) -> int:
    created = _parse_ts(row.get("created_at", ""))
    if not created:
        return 0
    now = now or datetime.now(timezone.utc)
    return max(0, (now - created).days)


def _email_domain(email: str) -> str:
    return (email or "").strip().lower().split("@")[-1] if "@" in (email or "") else ""


def is_fixture_email(email: str) -> bool:
    domain = _email_domain(email)
    local = (email or "").strip().lower().split("@")[0]
    return domain in FIXTURE_DOMAINS or local.startswith(("test-", "fixture-"))


def _config_body_path(config: dict[str, Any]) -> Path:
    configured = str(config.get("sequence_body_path") or DEFAULT_SEQUENCE_BODY_PATH)
    path = Path(configured)
    if not path.is_absolute():
        if configured.startswith("content/"):
            path = CONTENT_DIR.parent / configured
        else:
            path = CONTENT_DIR / configured
    return path


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(CONTENT_DIR.parent.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _preview(text: str, limit: int = 360) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _extract_utm_url(text: str) -> str:
    urls = URL_RE.findall(text or "")
    for url in urls:
        if "utm_" in url:
            return url.rstrip(".,")
    return ""


def _slug(value: str, fallback: str = "blast") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:48] or fallback


def _sequence_bodies(config: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    config = config or load_config()
    path = _config_body_path(config)
    if not path.exists():
        return {}
    sections: dict[str, dict[str, str]] = {}
    current_id = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_lines
        if not current_id:
            return
        subject = ""
        body_lines: list[str] = []
        for line in current_lines:
            stripped = line.strip()
            if stripped.lower().startswith("**subject:**"):
                subject = stripped.replace("**Subject:**", "").replace("**subject:**", "").strip()
                continue
            if stripped.lower().startswith("subject:"):
                subject = stripped.split(":", 1)[1].strip()
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        sections[current_id] = {
            "subject": subject.strip("* "),
            "body_preview": _preview(body),
            "body_path": _rel(path),
            "utm_url": _extract_utm_url(body),
        }
        current_id = ""
        current_lines = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(raw_line.strip())
        if match:
            flush()
            current_id = match.group(1)
            current_lines = []
            continue
        if current_id and raw_line.strip() == "---":
            flush()
            continue
        if current_id:
            current_lines.append(raw_line)
    flush()
    return sections


def _metadata_for_step(step_id: str, *, created_at: str, delay_days: int, config: dict[str, Any] | None = None) -> dict[str, str]:
    bodies = _sequence_bodies(config)
    step_body = bodies.get(step_id, {})
    return {
        "due_at": _due_at(created_at, delay_days),
        "body_preview": step_body.get("body_preview", ""),
        "body_path": step_body.get("body_path", ""),
        "utm_url": step_body.get("utm_url", ""),
    }


def enrich_queue_item(row: dict[str, Any], *, config: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    item = dict(row)
    config = config or load_config()
    status = item.get("status", "queued")
    item["status"] = status
    delay_days = int(item.get("delay_days", 0) or 0)
    item["delay_days"] = delay_days
    metadata = _metadata_for_step(str(item.get("step_id", "")), created_at=str(item.get("created_at", "")), delay_days=delay_days, config=config)
    for key, value in metadata.items():
        if not item.get(key):
            item[key] = value
    now = now or datetime.now(timezone.utc)
    due = _parse_ts(item.get("due_at", ""))
    item["age_days"] = _age_days(item, now=now)
    item["is_stale"] = status == "queued" and item["age_days"] >= STALE_DAYS
    item["is_fixture"] = is_fixture_email(str(item.get("email", "")))
    item["is_real_domain"] = bool(item.get("email")) and not item["is_fixture"]
    item["is_due_now"] = status == "queued" and (due is None or due <= now)
    item["is_future"] = status == "queued" and due is not None and due > now
    item["is_archived"] = status in ARCHIVED_STATUSES
    return item


def queue_summary(rows: list[dict[str, Any]] | None = None, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    rows = rows if rows is not None else list_queue()
    active = [enrich_queue_item(r, now=now) for r in rows if r.get("status", "queued") not in ARCHIVED_STATUSES]
    return {
        "all": len(active),
        "queued": sum(1 for r in active if r.get("status", "queued") == "queued"),
        "due_now": sum(1 for r in active if r.get("is_due_now")),
        "future": sum(1 for r in active if r.get("is_future")),
        "stale": sum(1 for r in active if r.get("is_stale")),
        "fixture": sum(1 for r in active if r.get("is_fixture")),
        "real": sum(1 for r in active if r.get("is_real_domain")),
        "skipped": sum(1 for r in active if r.get("status") == "skipped"),
        "sent": sum(1 for r in active if r.get("status") in SENT_STATUSES),
    }


def filter_queue(rows: list[dict[str, Any]], selected: str) -> list[dict[str, Any]]:
    selected = selected if selected in FILTER_LABELS else "queued"
    active = [r for r in rows if r.get("status", "queued") not in ARCHIVED_STATUSES]
    if selected == "all":
        return active
    if selected == "queued":
        return [r for r in active if r.get("status", "queued") == "queued"]
    if selected == "due_now":
        return [r for r in active if r.get("is_due_now")]
    if selected == "future":
        return [r for r in active if r.get("is_future")]
    if selected == "stale":
        return [r for r in active if r.get("is_stale")]
    if selected == "fixture":
        return [r for r in active if r.get("is_fixture")]
    if selected == "real":
        return [r for r in active if r.get("is_real_domain")]
    if selected == "skipped":
        return [r for r in active if r.get("status") == "skipped"]
    if selected == "sent":
        return [r for r in active if r.get("status") in SENT_STATUSES]
    return active


def group_by_lead(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("email", "")), str(row.get("campaign_id", "")))
        group = groups.setdefault(key, {
            "email": key[0],
            "campaign_id": key[1],
            "items": [],
            "queued_count": 0,
            "due_now_count": 0,
            "future_count": 0,
            "stale_count": 0,
            "sent_count": 0,
            "skipped_count": 0,
            "is_fixture": row.get("is_fixture", False),
            "next_due_at": "",
        })
        group["items"].append(row)
        status = row.get("status", "queued")
        if status == "queued":
            group["queued_count"] += 1
        if row.get("is_due_now"):
            group["due_now_count"] += 1
        if row.get("is_future"):
            group["future_count"] += 1
        if row.get("is_stale"):
            group["stale_count"] += 1
        if status in SENT_STATUSES:
            group["sent_count"] += 1
        if status == "skipped":
            group["skipped_count"] += 1
        due_at = row.get("due_at", "")
        if due_at and (not group["next_due_at"] or due_at < group["next_due_at"]):
            group["next_due_at"] = due_at
    sequence_order = {step.get("id"): idx for idx, step in enumerate(load_config().get("sequence", DEFAULT_SEQUENCE))}
    out = []
    for group in groups.values():
        group["items"] = sorted(group["items"], key=lambda item: (int(item.get("delay_days", 0) or 0), sequence_order.get(item.get("step_id"), 999), item.get("step_id", "")))
        out.append(group)
    return sorted(out, key=lambda group: (group.get("next_due_at") or "9999", group.get("email", "")))


def build_queue_view(selected_filter: str = "queued") -> dict[str, Any]:
    selected_filter = selected_filter if selected_filter in FILTER_LABELS else "queued"
    rows = list_queue()
    filtered = filter_queue(rows, selected_filter)
    return {
        "summary": queue_summary(rows),
        "filter": selected_filter,
        "filter_labels": FILTER_LABELS,
        "rows": filtered,
        "lead_groups": group_by_lead(filtered),
    }


def eligible_blast_recipients(*, include_fixtures: bool = False) -> list[dict[str, Any]]:
    import leads
    recipients: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lead in leads.list_leads():
        email = leads.normalize_email(str(lead.get("email", "")))
        if not email or email in seen:
            continue
        if not lead.get("consent") or lead.get("suppressed"):
            continue
        if is_fixture_email(email) and not include_fixtures:
            continue
        recipients.append({**lead, "email": email})
        seen.add(email)
    return sorted(recipients, key=lambda row: row.get("email", ""))


def queue_bulk_blast(
    *,
    subject: str,
    body: str,
    campaign_id: str,
    blast_id: str = "",
    include_fixtures: bool = False,
) -> dict[str, Any]:
    with _QUEUE_LOCK:
        subject = (subject or "").strip()
        body = (body or "").strip()
        campaign_id = (campaign_id or "").strip()
        if not subject:
            raise ValueError("blast subject is required")
        if not body:
            raise ValueError("blast body is required")
        if not campaign_id:
            raise ValueError("campaign_id is required")
        now = utc_now_iso()
        step_id = _slug(blast_id or f"blast-{now[:10]}-{subject}", fallback="blast")
        rows = list_queue(enrich=False)
        existing = {row.get("queue_key") for row in rows}
        recipients = eligible_blast_recipients(include_fixtures=include_fixtures)
        config = load_config()
        created: list[dict[str, Any]] = []
        for lead in recipients:
            key = _queue_key(lead["email"], campaign_id, step_id)
            if key in existing:
                continue
            row = {
                "queue_key": key,
                "email": lead["email"],
                "campaign_id": campaign_id,
                "step_id": step_id,
                "subject": subject,
                "body_text": body,
                "body_preview": _preview(body),
                "body_path": "",
                "utm_url": _extract_utm_url(body),
                "delay_days": 0,
                "status": "queued",
                "send_enabled": False,
                "created_at": now,
                "due_at": now,
                "provider": config.get("provider", "none"),
                "requires_unsubscribe": True,
                "blast_type": "manual_bulk",
                "blast_id": step_id,
            }
            rows.append(row)
            created.append(row)
        if created:
            write_jsonl(QUEUE_PATH, rows)
    append_jsonl(EVENTS_PATH, {
        "event": "email_bulk_blast_queued",
        "blast_id": step_id,
        "campaign_id": campaign_id,
        "recipient_count": len(recipients),
        "created": len(created),
        "include_fixtures": include_fixtures,
        "timestamp": now,
    })
    return {
        "blast_id": step_id,
        "campaign_id": campaign_id,
        "recipient_count": len(recipients),
        "created": len(created),
        "skipped_existing": len(recipients) - len(created),
        "subject": subject,
    }


def recent_blasts(limit: int = 6) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in list_queue():
        if row.get("blast_type") != "manual_bulk":
            continue
        key = (str(row.get("campaign_id", "")), str(row.get("blast_id") or row.get("step_id", "")))
        group = groups.setdefault(key, {
            "campaign_id": key[0],
            "blast_id": key[1],
            "subject": row.get("subject", ""),
            "created_at": row.get("created_at", ""),
            "queued": 0,
            "sent": 0,
            "skipped": 0,
            "total": 0,
        })
        group["total"] += 1
        if row.get("status") == "queued":
            group["queued"] += 1
        elif row.get("status") in SENT_STATUSES:
            group["sent"] += 1
        elif row.get("status") == "skipped":
            group["skipped"] += 1
        if row.get("created_at", "") > group.get("created_at", ""):
            group["created_at"] = row.get("created_at", "")
    return sorted(groups.values(), key=lambda row: row.get("created_at", ""), reverse=True)[:limit]


def queue_for_lead(lead: dict[str, Any], campaign_id: str | None = None) -> list[dict[str, Any]]:
    with _QUEUE_LOCK:
        if not lead.get("consent") or lead.get("suppressed"):
            append_jsonl(EVENTS_PATH, {"event": "nurture_skipped", "email": lead.get("email"), "reason": "no_consent_or_suppressed", "timestamp": utc_now_iso()})
            return []
        campaign_id = campaign_id or (lead.get("latest_touch") or {}).get("campaign_id") or "unknown"
        rows = list_queue(enrich=False)
        existing = {row.get("queue_key") for row in rows}
        created: list[dict[str, Any]] = []
        config = load_config()
        for step in config.get("sequence", DEFAULT_SEQUENCE):
            key = _queue_key(lead["email"], campaign_id, step["id"])
            if key in existing:
                continue
            created_at = utc_now_iso()
            delay_days = int(step.get("delay_days", 0))
            row = {
                "queue_key": key,
                "email": lead["email"],
                "campaign_id": campaign_id,
                "step_id": step["id"],
                "subject": step.get("subject", step["id"]),
                "delay_days": delay_days,
                "status": "queued",
                "send_enabled": False,
                "created_at": created_at,
                "provider": config.get("provider", "none"),
                "requires_unsubscribe": True,
            }
            row.update(_metadata_for_step(step["id"], created_at=created_at, delay_days=delay_days, config=config))
            rows.append(row)
            created.append(row)
            append_jsonl(EVENTS_PATH, {"event": "email_queued", "email": lead["email"], "campaign_id": campaign_id, "step_id": step["id"], "timestamp": row["created_at"]})
        if created:
            write_jsonl(QUEUE_PATH, rows)
        return created


def update_queue_item(queue_key: str, action: str, *, reason: str = "", manual_url: str = "") -> dict[str, Any]:
    with _QUEUE_LOCK:
        rows = list_queue(enrich=False)
        for idx, row in enumerate(rows):
            if row.get("queue_key") != queue_key:
                continue
            now = utc_now_iso()
            if action == "skip":
                row["status"] = "skipped"
                row["skip_reason"] = reason
                event = "email_skip"
            elif action == "mark_sent":
                if not manual_url:
                    raise ValueError("sent note/URL is required when marking email sent")
                row["status"] = "sent_manual"
                row["manual_url"] = manual_url
                row["sent_at"] = now
                event = "email_mark_sent"
            else:
                raise ValueError(f"unsupported email queue action: {action}")
            row["updated_at"] = now
            rows[idx] = row
            write_jsonl(QUEUE_PATH, rows)
            append_jsonl(EVENTS_PATH, {"event": event, "queue_key": queue_key, "email": row.get("email"), "campaign_id": row.get("campaign_id"), "reason": reason, "manual_url": manual_url, "timestamp": now})
            return enrich_queue_item(row)
        raise KeyError(f"email queue item not found: {queue_key}")


def archive_fixture_items(*, apply: bool = False, reason: str = "fixture/example.com queue cleanup") -> dict[str, Any]:
    rows = list_queue(enrich=False)
    now = utc_now_iso()
    candidates = [
        idx for idx, row in enumerate(rows)
        if row.get("status", "queued") == "queued" and is_fixture_email(str(row.get("email", "")))
    ]
    if apply:
        for idx in candidates:
            row = dict(rows[idx])
            row["status"] = "archived_fixture"
            row["archived_at"] = now
            row["archive_reason"] = reason
            rows[idx] = row
        write_jsonl(QUEUE_PATH, rows)
        append_jsonl(EVENTS_PATH, {
            "event": "email_archive_fixture_batch",
            "archived": len(candidates),
            "reason": reason,
            "timestamp": now,
        })
    return {"generated_at": now, "applied": apply, "archived": len(candidates), "reason": reason}


def suppress_lead(email: str, reason: str) -> bool:
    import leads
    changed = leads.suppress_lead(email, reason)
    if changed:
        append_jsonl(EVENTS_PATH, {"event": "lead_suppressed", "email": email.strip().lower(), "reason": reason, "timestamp": utc_now_iso()})
    return changed


def dry_run_send() -> dict[str, int]:
    queued = [row for row in list_queue() if row.get("status") == "queued"]
    append_jsonl(EVENTS_PATH, {"event": "dry_run_send", "queued_count": len(queued), "timestamp": utc_now_iso(), "sent": 0})
    return {"queued": len(queued), "sent": 0, "skipped": len(queued)}


class EmailProviderAdapter:
    provider = "base"
    supported = False

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def status(self) -> dict[str, Any]:
        credential_env = self.config.get("credential_env", "EMAIL_PROVIDER_API_KEY")
        return {
            "provider": self.provider,
            "supported": self.supported,
            "send_enabled": bool(self.config.get("send_enabled")),
            "credential_env": credential_env,
            "credential_present": bool(os.getenv(str(credential_env))),
        }

    def send(self, queue_item: dict[str, Any], lead: dict[str, Any], *, mode: str = "dry_run") -> dict[str, Any]:
        return {"sent": False, "provider": self.provider, "mode": mode, "blockers": [f"{self.provider} adapter is not implemented yet"]}


class NoopEmailProviderAdapter(EmailProviderAdapter):
    provider = "none"

    def send(self, queue_item: dict[str, Any], lead: dict[str, Any], *, mode: str = "dry_run") -> dict[str, Any]:
        return {"sent": False, "provider": self.provider, "mode": mode, "blockers": ["email provider is not configured"]}


class KitEmailProviderAdapter(EmailProviderAdapter):
    provider = "kit"


class ResendEmailProviderAdapter(EmailProviderAdapter):
    provider = "resend"


class MailchimpEmailProviderAdapter(EmailProviderAdapter):
    provider = "mailchimp"


ADAPTERS = {
    "none": NoopEmailProviderAdapter,
    "kit": KitEmailProviderAdapter,
    "convertkit": KitEmailProviderAdapter,
    "resend": ResendEmailProviderAdapter,
    "mailchimp": MailchimpEmailProviderAdapter,
}


def provider_adapter(config: dict[str, Any] | None = None) -> EmailProviderAdapter:
    config = config or load_config()
    provider = str(config.get("provider", "none")).strip().lower() or "none"
    adapter_cls = ADAPTERS.get(provider, NoopEmailProviderAdapter)
    return adapter_cls(config)


def provider_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    adapter = provider_adapter(config)
    status = adapter.status()
    status["live_send_blocked_by_default"] = True
    return status


def send_queue_item(queue_key: str, *, mode: str = "dry_run") -> dict[str, Any]:
    config = load_config()
    row = next((r for r in list_queue() if r.get("queue_key") == queue_key), None)
    if not row:
        raise KeyError(f"email queue item not found: {queue_key}")
    if row.get("status", "queued") != "queued":
        blockers = [f"queue item status is {row.get('status')}"]
        append_jsonl(EVENTS_PATH, {"event": "email_send_blocked", "queue_key": queue_key, "blockers": blockers, "timestamp": utc_now_iso()})
        return {"sent": False, "queue_key": queue_key, "blockers": blockers, "provider": config.get("provider", "none")}
    review_blockers = _live_send_review_blockers(row, mode=mode)
    if review_blockers:
        append_jsonl(EVENTS_PATH, {"event": "email_send_blocked", "queue_key": queue_key, "blockers": review_blockers, "timestamp": utc_now_iso()})
        return {"sent": False, "queue_key": queue_key, "blockers": review_blockers, "provider": config.get("provider", "none")}
    import leads
    lead = next((lead for lead in leads.list_leads() if lead.get("email") == row.get("email")), {})
    import live_gates
    gate = live_gates.check_email_gate(mode=mode, email_config=config, lead=lead)
    if not gate.get("allowed"):
        append_jsonl(EVENTS_PATH, {"event": "email_send_blocked", "queue_key": queue_key, "blockers": gate.get("blockers", []), "timestamp": utc_now_iso()})
        return {"sent": False, "queue_key": queue_key, "blockers": gate.get("blockers", []), "provider": config.get("provider", "none")}
    adapter = provider_adapter(config)
    return adapter.send(row, lead, mode=mode)


def _live_send_review_blockers(row: dict[str, Any], *, mode: str) -> list[str]:
    if mode != "live":
        return []
    blockers: list[str] = []
    if row.get("requires_unsubscribe") is not True:
        blockers.append("queue item is missing unsubscribe requirement")
    if row.get("approval_status") != "approved":
        blockers.append("queue item is not approved")
    if row.get("compliance_status") != "pass":
        blockers.append("queue item compliance_status is not pass")
    return blockers


def send_bulk_blast(*, campaign_id: str, blast_id: str, confirm: str, mode: str = "live") -> dict[str, Any]:
    if confirm != BLAST_SEND_CONFIRM:
        raise ValueError(f"type {BLAST_SEND_CONFIRM} to send a bulk blast")
    config = load_config()
    adapter = provider_adapter(config)
    if not adapter.status().get("supported"):
        blockers = [f"{adapter.provider} adapter is not implemented yet"]
        append_jsonl(EVENTS_PATH, {
            "event": "email_bulk_send_blocked",
            "campaign_id": campaign_id,
            "blast_id": blast_id,
            "blockers": blockers,
            "timestamp": utc_now_iso(),
        })
        return {"sent": 0, "attempted": 0, "blockers": blockers}
    rows = [
        row for row in list_queue()
        if row.get("campaign_id") == campaign_id
        and (row.get("blast_id") or row.get("step_id")) == blast_id
        and row.get("status") == "queued"
    ]
    if not rows:
        return {"sent": 0, "attempted": 0, "blockers": ["no queued blast rows found"]}
    results = [send_queue_item(row["queue_key"], mode=mode) for row in rows]
    sent = sum(1 for result in results if result.get("sent"))
    blockers = []
    for result in results:
        blockers.extend(result.get("blockers", []))
    return {"sent": sent, "attempted": len(results), "blockers": sorted(set(blockers))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dry-run")
    sub.add_parser("list")
    sub.add_parser("provider-status")
    send = sub.add_parser("send")
    send.add_argument("queue_key")
    send.add_argument("--mode", default="dry_run")
    bulk = sub.add_parser("queue-blast")
    bulk.add_argument("--subject", required=True)
    bulk.add_argument("--body", required=True)
    bulk.add_argument("--campaign-id", required=True)
    bulk.add_argument("--blast-id", default="")
    bulk.add_argument("--include-fixtures", action="store_true")
    bulk_send = sub.add_parser("send-blast")
    bulk_send.add_argument("--campaign-id", required=True)
    bulk_send.add_argument("--blast-id", required=True)
    bulk_send.add_argument("--confirm", required=True)
    bulk_send.add_argument("--mode", default="live")
    archive = sub.add_parser("archive-fixtures")
    archive.add_argument("--apply", action="store_true")
    action = sub.add_parser("action")
    action.add_argument("queue_key")
    action.add_argument("action", choices=["skip", "mark_sent"])
    action.add_argument("--reason", default="")
    action.add_argument("--manual-url", default="")
    sup = sub.add_parser("suppress")
    sup.add_argument("email")
    sup.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "dry-run":
        print(dry_run_send())
    elif args.cmd == "list":
        for row in list_queue():
            print(json.dumps(row, ensure_ascii=True, sort_keys=True))
    elif args.cmd == "provider-status":
        print(json.dumps(provider_status(), indent=2, ensure_ascii=False))
    elif args.cmd == "send":
        print(json.dumps(send_queue_item(args.queue_key, mode=args.mode), indent=2, ensure_ascii=False))
    elif args.cmd == "queue-blast":
        print(json.dumps(queue_bulk_blast(subject=args.subject, body=args.body, campaign_id=args.campaign_id, blast_id=args.blast_id, include_fixtures=args.include_fixtures), indent=2, ensure_ascii=False))
    elif args.cmd == "send-blast":
        print(json.dumps(send_bulk_blast(campaign_id=args.campaign_id, blast_id=args.blast_id, confirm=args.confirm, mode=args.mode), indent=2, ensure_ascii=False))
    elif args.cmd == "archive-fixtures":
        print(json.dumps(archive_fixture_items(apply=args.apply), indent=2, ensure_ascii=False))
    elif args.cmd == "action":
        print(update_queue_item(args.queue_key, args.action, reason=args.reason, manual_url=args.manual_url))
    elif args.cmd == "suppress":
        print(suppress_lead(args.email, args.reason))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
