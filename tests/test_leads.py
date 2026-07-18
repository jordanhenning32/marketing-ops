import leads
from concurrent.futures import ThreadPoolExecutor


def isolate_leads(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(leads, "STATE_DIR", state)
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")
    return state


def test_capture_lead_normalizes_dedupes_and_preserves_touch_history(tmp_path, monkeypatch):
    isolate_leads(tmp_path, monkeypatch)

    first, created = leads.capture_lead(
        email=" Trader@Example.COM ",
        campaign_id="campaign-a",
        consent=True,
        utm_source="x",
        utm_medium="social",
        utm_content="thread",
        landing_page="/lead/campaign-a",
        name=" Jordan ",
        interest="Bracket Boss",
        note=" Apex trader ",
    )
    second, created_again = leads.capture_lead(
        email="trader@example.com",
        campaign_id="campaign-b",
        consent=False,
        utm_source="linkedin",
        utm_medium="social",
        utm_content="post",
        landing_page="/lead/campaign-b",
        interest="Drawdown Guardian",
        note="Needs firm preset",
    )

    assert created is True
    assert created_again is False
    assert first["email"] == "trader@example.com"
    assert second["consent"] is True
    assert second["first_touch"]["campaign_id"] == "campaign-a"
    assert second["latest_touch"]["campaign_id"] == "campaign-b"
    assert second["name"] == "Jordan"
    assert second["interest"] == "Drawdown Guardian"
    assert second["lead_note"] == "Needs firm preset"
    assert len(leads.list_leads()) == 1


def test_export_csv_includes_public_site_lead_fields(tmp_path, monkeypatch):
    isolate_leads(tmp_path, monkeypatch)
    leads.capture_lead(
        email="beta@example.com",
        campaign_id="site-beta",
        consent=True,
        name="Beta Trader",
        interest="Early Access",
        note="Uses Apex",
    )

    out = leads.export_csv(tmp_path / "leads.csv")
    text = out.read_text(encoding="utf-8")

    assert "name,interest,lead_note" in text
    assert "Beta Trader,Early Access,Uses Apex" in text


def test_concurrent_lead_captures_do_not_lose_rows(tmp_path, monkeypatch):
    isolate_leads(tmp_path, monkeypatch)

    def capture(idx):
        return leads.capture_lead(
            email=f"burst-{idx}@example.test",
            campaign_id="burst",
            consent=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(capture, range(24)))

    assert len(leads.list_leads()) == 24


def test_capture_lead_requires_valid_email_and_campaign(tmp_path, monkeypatch):
    isolate_leads(tmp_path, monkeypatch)

    try:
        leads.capture_lead(email="bad", campaign_id="campaign-a")
    except ValueError as exc:
        assert "valid email" in str(exc)
    else:
        raise AssertionError("invalid email should fail")

    try:
        leads.capture_lead(email="ok@example.com", campaign_id="")
    except ValueError as exc:
        assert "campaign_id" in str(exc)
    else:
        raise AssertionError("missing campaign_id should fail")
