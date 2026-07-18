from pathlib import Path

import analytics
import distribution_queue
import email_nurture
import leads
import live_gates
from publishers import status_all


def isolate_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    config = tmp_path / "config"
    content = tmp_path / "content"
    state.mkdir()
    config.mkdir()
    content.mkdir()
    monkeypatch.setattr(distribution_queue, "STATE_DIR", state)
    monkeypatch.setattr(distribution_queue, "CONFIG_DIR", config)
    monkeypatch.setattr(distribution_queue, "QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(distribution_queue, "LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(email_nurture, "STATE_DIR", state)
    monkeypatch.setattr(email_nurture, "CONFIG_DIR", config)
    monkeypatch.setattr(email_nurture, "QUEUE_PATH", state / "email-queue.jsonl")
    monkeypatch.setattr(email_nurture, "EVENTS_PATH", state / "email-events.jsonl")
    monkeypatch.setattr(leads, "STATE_DIR", state)
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")
    monkeypatch.setattr(analytics, "STATE_DIR", state)
    monkeypatch.setattr(analytics, "PERFORMANCE_PATH", state / "performance.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_QUEUE_PATH", state / "publish-queue.jsonl", raising=False)
    monkeypatch.setattr(analytics, "PUBLISH_LOG_PATH", state / "publish-log.jsonl", raising=False)
    monkeypatch.setattr(live_gates, "CONFIG_DIR", config)
    return state, config, content


def test_publish_queue_entries_extract_copy_cta_utm_and_write_manual_kits(tmp_path, monkeypatch):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    monkeypatch.setattr(distribution_queue, "CONTENT_DIR", content, raising=False)
    campaign = content / "2026-05-30-test"
    campaign.mkdir()
    (campaign / "01-x-thread.md").write_text("Body copy\n\nCTA: Go here: https://shadowedgetools.com/checklist?utm_source=x&utm_medium=social&utm_campaign=c1&utm_content=x-thread\n", encoding="utf-8")
    (campaign / "08-compliance-check.md").write_text("- Status: pass\n", encoding="utf-8")

    queued = distribution_queue.queue_campaign(campaign, "c1")

    assert queued[0]["cta"].startswith("Go here")
    assert queued[0]["utm_url"].startswith("https://shadowedgetools.com/checklist?")
    assert "Body copy" in queued[0]["body_text"]
    assert queued[0]["approval_status"] == "pending"
    assert (campaign / "distribution" / "x" / "body.txt").exists()
    assert (campaign / "distribution" / "x" / "metadata.json").exists()


def test_publish_queue_actions_are_local_and_append_log(tmp_path, monkeypatch):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    distribution_queue.write_jsonl(distribution_queue.QUEUE_PATH, [{"queue_key": "k1", "status": "queued", "approval_status": "pending", "platform": "x"}])

    approved = distribution_queue.update_queue_item("k1", "approve")
    published = distribution_queue.update_queue_item("k1", "mark_published", manual_url="https://x.com/post/1")

    assert approved["approval_status"] == "approved"
    assert published["status"] == "published_manual"
    assert published["manual_url"] == "https://x.com/post/1"
    events = distribution_queue.read_jsonl(distribution_queue.LOG_PATH)
    assert [e["event"] for e in events] == ["approve", "mark_published"]


def test_email_queue_actions_and_suppression_are_append_only(tmp_path, monkeypatch):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    lead, _ = leads.capture_lead(email="a@example.com", campaign_id="c1", consent=True)
    email_nurture.queue_for_lead(lead, "c1")
    key = email_nurture.list_queue()[0]["queue_key"]

    skipped = email_nurture.update_queue_item(key, "skip", reason="not today")
    suppressed = email_nurture.suppress_lead("a@example.com", "unsubscribed")
    again = email_nurture.queue_for_lead({**lead, "suppressed": True}, "c2")

    assert skipped["status"] == "skipped"
    assert suppressed is True
    assert again == []
    assert any(e.get("event") == "email_skip" for e in email_nurture.read_jsonl(email_nurture.EVENTS_PATH))
    assert leads.list_leads()[0]["suppressed"] is True


def test_analytics_csv_import_schema_and_queue_status_reporting(tmp_path, monkeypatch):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    csv_path = tmp_path / "metrics.csv"
    csv_path.write_text("date,campaign_id,metric,value,source,status\n2026-05-30,c1,landing_page_clicks,12,manual_csv,measured\n2026-05-30,c1,revenue,,manual_csv,blocked\n", encoding="utf-8")
    distribution_queue.write_jsonl(distribution_queue.QUEUE_PATH, [{"campaign_id": "c1", "status": "queued"}, {"campaign_id": "c1", "status": "published_manual"}])

    count = analytics.import_csv(csv_path)
    summary = analytics.report("2026-05-30", "c1")

    assert count == 2
    assert summary["measured"]["landing_page_clicks"] == "12"
    assert "revenue" in summary["blocked"]
    assert summary["publish_queue"]["queued"] == 1
    assert summary["publish_queue"]["published_manual"] == 1


def test_live_gates_refuse_disabled_and_missing_credentials(monkeypatch, tmp_path):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    (config / "hermes.yaml").write_text("live_publish_enabled: false\nlive_email_enabled: false\n", encoding="utf-8")
    publish = live_gates.check_publish_gate(mode="live", platform="x", platform_config={"api_enabled": True, "credential_env": "X_API_KEY"}, queue_item={"approval_status": "approved", "compliance_status": "pass"})
    assert publish["allowed"] is False
    assert any("live_publish_enabled" in b for b in publish["blockers"])

    (config / "hermes.yaml").write_text("live_publish_enabled: true\nlive_email_enabled: true\n", encoding="utf-8")
    monkeypatch.delenv("X_API_KEY", raising=False)
    publish = live_gates.check_publish_gate(mode="live", platform="x", platform_config={"api_enabled": True, "credential_env": "X_API_KEY"}, queue_item={"approval_status": "approved", "compliance_status": "pass"})
    assert any("missing X_API_KEY" in b for b in publish["blockers"])


def test_publisher_status_reports_safe_fallbacks(monkeypatch):
    monkeypatch.delenv("X_API_KEY", raising=False)
    statuses = status_all({"x": {"api_enabled": False, "credential_env": "X_API_KEY", "publish_mode": "manual"}})
    assert statuses["x"]["supported"] is False
    assert statuses["x"]["safe_fallback_mode"] == "manual"
    assert statuses["x"]["docs_verified"] is False


def test_export_includes_first_and_latest_touch_fields(tmp_path, monkeypatch):
    state, config, content = isolate_state(tmp_path, monkeypatch)
    leads.capture_lead(email="a@example.com", campaign_id="c1", consent=True, utm_source="x")
    leads.capture_lead(email="a@example.com", campaign_id="c2", consent=True, utm_source="linkedin")
    out = tmp_path / "leads.csv"

    leads.export_csv(out)
    text = out.read_text(encoding="utf-8")

    assert "first_campaign_id" in text
    assert "first_utm_source" in text
    assert "latest_campaign_id" in text
    assert "c1" in text and "c2" in text
