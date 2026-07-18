import analytics
import leads


def isolate_analytics(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(analytics, "STATE_DIR", state)
    monkeypatch.setattr(analytics, "PERFORMANCE_PATH", state / "performance.jsonl")
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")
    return state


def test_report_separates_measured_and_unavailable_metrics(tmp_path, monkeypatch):
    isolate_analytics(tmp_path, monkeypatch)
    analytics.record_metric("website_sessions_by_source", "x:12", metric_date="2026-05-30")

    summary = analytics.report("2026-05-30")

    assert summary["measured"]["website_sessions_by_source"] == "x:12"
    assert "landing_page_clicks" in summary["unavailable"]
    assert "website_sessions_by_source" not in summary["unavailable"]


def test_report_includes_local_lead_count_without_estimating_other_metrics(tmp_path, monkeypatch):
    isolate_analytics(tmp_path, monkeypatch)
    leads.capture_lead(email="a@example.com", campaign_id="campaign-a", consent=True)
    leads.capture_lead(email="b@example.com", campaign_id="campaign-b", consent=True)

    summary = analytics.report("2026-05-30", campaign_id="campaign-a")

    assert summary["measured"]["email_captures"] == 1
    assert "revenue" in summary["unavailable"]


def test_scoreboard_keys_are_importable_metrics():
    """The performance scoreboard reads these keys; a CSV import / manual record
    targeting a scoreboard card must validate. Guards against the two metric
    vocabularies drifting apart again (which left the social cards permanently blank)."""
    import console

    section_keys = {m["key"] for s in console._SCOREBOARD_SECTIONS for m in s["metrics"]}
    missing = section_keys - analytics.KNOWN_METRICS
    assert not missing, f"scoreboard keys not accepted by the importer: {sorted(missing)}"
    # the two auto-snapshotted keys must be importable too
    assert {"leads", "customers"} <= analytics.KNOWN_METRICS


def test_scoreboard_key_guard_detects_new_unregistered_metric(monkeypatch):
    import console

    fake_metric = "scoreboard_metric_not_registered"
    monkeypatch.setattr(
        console,
        "_SCOREBOARD_SECTIONS",
        [
            *console._SCOREBOARD_SECTIONS,
            {"key": "guard", "label": "Guard", "source": "test", "metrics": [{"key": fake_metric, "label": "Guard"}]},
        ],
    )

    section_keys = {m["key"] for s in console._SCOREBOARD_SECTIONS for m in s["metrics"]}
    assert fake_metric in section_keys - analytics.KNOWN_METRICS


def test_import_accepts_scoreboard_metric(tmp_path, monkeypatch):
    isolate_analytics(tmp_path, monkeypatch)
    csv_path = tmp_path / "sb.csv"
    csv_path.write_text(
        "date,campaign_id,metric,value,source,status\n"
        "2026-06-01,cid,tiktok_followers,320,manual_csv,measured\n",
        encoding="utf-8",
    )
    assert analytics.validate_csv(csv_path) == []
    assert analytics.import_csv(csv_path) == 1
