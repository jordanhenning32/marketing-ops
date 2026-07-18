import email_nurture
import leads
import pytest
from concurrent.futures import ThreadPoolExecutor


def isolate_email(tmp_path, monkeypatch):
    state = tmp_path / "state"
    config = tmp_path / "config"
    content = tmp_path / "content"
    state.mkdir()
    config.mkdir()
    content.mkdir()
    monkeypatch.setattr(email_nurture, "STATE_DIR", state)
    monkeypatch.setattr(email_nurture, "CONFIG_DIR", config)
    monkeypatch.setattr(email_nurture, "CONTENT_DIR", content)
    monkeypatch.setattr(email_nurture, "QUEUE_PATH", state / "email-queue.jsonl")
    monkeypatch.setattr(email_nurture, "EVENTS_PATH", state / "email-events.jsonl")
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")
    return state, config, content


def test_queue_for_lead_requires_consent_and_not_suppressed(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)

    assert email_nurture.queue_for_lead({"email": "a@example.com", "consent": False}, "c1") == []
    assert email_nurture.queue_for_lead({"email": "b@example.com", "consent": True, "suppressed": True}, "c1") == []
    assert email_nurture.list_queue() == []


def test_queue_for_lead_is_idempotent_and_send_disabled(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    lead = {"email": "a@example.com", "consent": True, "suppressed": False}

    first = email_nurture.queue_for_lead(lead, "campaign-a")
    second = email_nurture.queue_for_lead(lead, "campaign-a")

    assert len(first) == len(email_nurture.DEFAULT_SEQUENCE)
    assert second == []
    assert len(email_nurture.list_queue()) == len(email_nurture.DEFAULT_SEQUENCE)
    assert all(row["send_enabled"] is False for row in email_nurture.list_queue())
    assert email_nurture.dry_run_send()["sent"] == 0


def test_queue_items_include_due_body_and_utm_metadata(tmp_path, monkeypatch):
    _state, config, content = isolate_email(tmp_path, monkeypatch)
    sequence = content / "email-sequences" / "risk-control-nurture.md"
    sequence.parent.mkdir(parents=True)
    sequence.write_text(
        """# Sequence

## 1. confirmation - delay 0 days
**Subject:** Confirmed subject

Hello from the sequence.
https://shadowedgetools.com/checklist?utm_source=email&utm_content=confirmation
""",
        encoding="utf-8",
    )
    (config / "email.yaml").write_text(
        """provider: none
send_enabled: false
sequence_body_path: content/email-sequences/risk-control-nurture.md
sequence:
  - id: confirmation
    delay_days: 2
    subject: Confirmed subject
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(email_nurture, "utc_now_iso", lambda: "2026-06-01T10:00:00+00:00")

    row = email_nurture.queue_for_lead({"email": "real@customer.test", "consent": True}, "campaign-a")[0]

    assert row["due_at"] == "2026-06-03T10:00:00+00:00"
    assert row["body_path"] == "content/email-sequences/risk-control-nurture.md"
    assert "Hello from the sequence" in row["body_preview"]
    assert row["utm_url"].endswith("utm_content=confirmation")


def test_queue_summary_grouping_and_fixture_archive_are_safe(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    lead = {"email": "fixture@example.com", "consent": True, "suppressed": False}
    email_nurture.queue_for_lead(lead, "campaign-a")

    view = email_nurture.build_queue_view("fixture")
    assert view["summary"]["fixture"] == len(email_nurture.DEFAULT_SEQUENCE)
    assert view["summary"]["real"] == 0
    assert len(view["lead_groups"]) == 1
    assert view["lead_groups"][0]["queued_count"] == len(email_nurture.DEFAULT_SEQUENCE)

    dry_run = email_nurture.archive_fixture_items(apply=False)
    assert dry_run["archived"] == len(email_nurture.DEFAULT_SEQUENCE)
    assert all(row["status"] == "queued" for row in email_nurture.list_queue(enrich=False))

    applied = email_nurture.archive_fixture_items(apply=True)
    assert applied["archived"] == len(email_nurture.DEFAULT_SEQUENCE)
    assert all(row["status"] == "archived_fixture" for row in email_nurture.list_queue(enrich=False))
    assert email_nurture.build_queue_view("queued")["summary"]["queued"] == 0


def test_provider_status_and_send_remain_gated(tmp_path, monkeypatch):
    _state, config, _content = isolate_email(tmp_path, monkeypatch)
    (config / "email.yaml").write_text(
        """provider: resend
send_enabled: true
credential_env: EMAIL_PROVIDER_API_KEY
sequence:
  - id: confirmation
    delay_days: 0
    subject: Confirmed subject
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("EMAIL_PROVIDER_API_KEY", "test-key")
    row = email_nurture.queue_for_lead({"email": "real@customer.test", "consent": True}, "campaign-a")[0]

    status = email_nurture.provider_status()
    blocked = email_nurture.send_queue_item(row["queue_key"], mode="dry_run")

    assert status["provider"] == "resend"
    assert status["credential_present"] is True
    assert blocked["sent"] is False
    assert any("dry-run" in blocker for blocker in blocked["blockers"])


def test_send_queue_item_refuses_non_queued_status(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    row = email_nurture.queue_for_lead({"email": "real@customer.test", "consent": True}, "campaign-a")[0]
    email_nurture.update_queue_item(row["queue_key"], "skip", reason="not ready")

    blocked = email_nurture.send_queue_item(row["queue_key"], mode="live")

    assert blocked["sent"] is False
    assert blocked["blockers"] == ["queue item status is skipped"]


def test_queue_bulk_blast_uses_consented_real_leads_only(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    leads.write_jsonl(leads.LEADS_PATH, [
        {"email": "Buyer@Customer.test", "consent": True, "suppressed": False},
        {"email": "buyer@customer.test", "consent": True, "suppressed": False},
        {"email": "no-consent@customer.test", "consent": False, "suppressed": False},
        {"email": "suppressed@customer.test", "consent": True, "suppressed": True},
        {"email": "fixture@example.com", "consent": True, "suppressed": False},
    ])

    result = email_nurture.queue_bulk_blast(
        subject="Launch update",
        body="Hello launch list.\nhttps://shadowedgetools.com/demo?utm_source=email&utm_campaign=launch",
        campaign_id="launch",
        blast_id="launch-update",
    )
    second = email_nurture.queue_bulk_blast(
        subject="Launch update",
        body="Hello launch list.\nhttps://shadowedgetools.com/demo?utm_source=email&utm_campaign=launch",
        campaign_id="launch",
        blast_id="launch-update",
    )
    rows = email_nurture.list_queue(enrich=False)

    assert result["recipient_count"] == 1
    assert result["created"] == 1
    assert second["created"] == 0
    assert rows[0]["email"] == "buyer@customer.test"
    assert rows[0]["blast_type"] == "manual_bulk"
    assert rows[0]["body_preview"] == "Hello launch list. https://shadowedgetools.com/demo?utm_source=email&utm_campaign=launch"
    assert rows[0]["utm_url"].endswith("utm_campaign=launch")
    assert rows[0]["requires_unsubscribe"] is True


def test_send_bulk_blast_stays_provider_gated(tmp_path, monkeypatch):
    _state, config, _content = isolate_email(tmp_path, monkeypatch)
    (config / "email.yaml").write_text(
        """provider: resend
send_enabled: true
credential_env: EMAIL_PROVIDER_API_KEY
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("EMAIL_PROVIDER_API_KEY", "test-key")
    leads.write_jsonl(leads.LEADS_PATH, [
        {"email": "buyer@customer.test", "consent": True, "suppressed": False},
    ])
    queued = email_nurture.queue_bulk_blast(
        subject="Launch update",
        body="Hello launch list.",
        campaign_id="launch",
        blast_id="launch-update",
    )

    with pytest.raises(ValueError):
        email_nurture.send_bulk_blast(campaign_id="launch", blast_id=queued["blast_id"], confirm="")
    blocked = email_nurture.send_bulk_blast(
        campaign_id="launch",
        blast_id=queued["blast_id"],
        confirm=email_nurture.BLAST_SEND_CONFIRM,
    )

    assert blocked["sent"] == 0
    assert blocked["attempted"] == 0
    assert blocked["blockers"] == ["resend adapter is not implemented yet"]
    assert email_nurture.list_queue(enrich=False)[0]["status"] == "queued"


def test_concurrent_queue_for_lead_does_not_duplicate_sequence(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    lead = {"email": "burst@customer.test", "consent": True, "suppressed": False}

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _idx: email_nurture.queue_for_lead(lead, "burst"), range(16)))

    rows = email_nurture.list_queue(enrich=False)
    assert len(rows) == len(email_nurture.DEFAULT_SEQUENCE)
    assert len({row["queue_key"] for row in rows}) == len(email_nurture.DEFAULT_SEQUENCE)


def test_live_send_requires_review_compliance_and_unsubscribe_metadata(tmp_path, monkeypatch):
    isolate_email(tmp_path, monkeypatch)
    row = email_nurture.queue_for_lead({"email": "real@customer.test", "consent": True}, "campaign-a")[0]

    blocked = email_nurture.send_queue_item(row["queue_key"], mode="live")

    assert blocked["sent"] is False
    assert "queue item is not approved" in blocked["blockers"]
    assert "queue item compliance_status is not pass" in blocked["blockers"]
