from __future__ import annotations

import os


def test_public_lead_nonlocal_unauth_requires_dev_override(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.delenv("PUBLIC_LEAD_API_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_LEAD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv("PUBLIC_LEAD_DEV_OVERRIDE", raising=False)

    from fastapi.testclient import TestClient
    import console

    client = TestClient(console.app, client=("203.0.113.10", 12345))
    response = client.post("/api/leads", json={"email": "test@example.com", "campaign_id": "campaign-a", "consent": False})
    assert response.status_code == 401


def test_public_lead_local_proxy_headers_still_require_token(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.delenv("PUBLIC_LEAD_API_TOKEN", raising=False)
    monkeypatch.delenv("PUBLIC_LEAD_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("PUBLIC_LEAD_DEV_OVERRIDE", raising=False)

    from fastapi.testclient import TestClient
    import console

    client = TestClient(console.app, client=("127.0.0.1", 12345))
    response = client.post(
        "/api/leads",
        json={"email": "test@example.com", "campaign_id": "campaign-a", "consent": True},
        headers={"x-forwarded-host": "www.shadowedgetools.com", "x-forwarded-proto": "https"},
    )
    assert response.status_code == 401


def test_public_lead_nonlocal_unauth_dev_override_reaches_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.delenv("PUBLIC_LEAD_API_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_LEAD_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("PUBLIC_LEAD_DEV_OVERRIDE", "1")

    from fastapi.testclient import TestClient
    import console
    import leads
    import email_nurture

    monkeypatch.setattr(leads, "STATE_DIR", tmp_path)
    monkeypatch.setattr(leads, "LEADS_PATH", tmp_path / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", tmp_path / "email-events.jsonl")
    monkeypatch.setattr(email_nurture, "QUEUE_PATH", tmp_path / "email-queue.jsonl")
    monkeypatch.setattr(email_nurture, "EVENTS_PATH", tmp_path / "email-events.jsonl")

    client = TestClient(console.app, client=("203.0.113.10", 12345))
    response = client.post("/api/leads", json={"email": "test@example.com", "campaign_id": "campaign-a", "consent": False})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_public_lead_string_false_does_not_queue_nurture(monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.setenv("PUBLIC_LEAD_API_TOKEN", "secret-token")

    from fastapi.testclient import TestClient
    import console
    import leads
    import email_nurture

    monkeypatch.setattr(leads, "STATE_DIR", tmp_path)
    monkeypatch.setattr(leads, "LEADS_PATH", tmp_path / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", tmp_path / "email-events.jsonl")
    monkeypatch.setattr(email_nurture, "QUEUE_PATH", tmp_path / "email-queue.jsonl")
    monkeypatch.setattr(email_nurture, "EVENTS_PATH", tmp_path / "email-events.jsonl")

    response = TestClient(console.app).post(
        "/api/leads",
        json={"email": "no-consent@example.com", "campaign_id": "campaign-a", "consent": "false"},
        headers={"x-shadowedge-token": "secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["consent"] is False
    assert response.json()["email_queue_created"] == 0
    assert leads.list_leads()[0]["consent"] is False
    assert email_nurture.list_queue(enrich=False) == []


def test_public_lead_malformed_json_returns_400(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.setenv("PUBLIC_LEAD_API_TOKEN", "secret-token")

    from fastapi.testclient import TestClient
    import console

    response = TestClient(console.app).post(
        "/api/leads",
        data="{not-json",
        headers={"content-type": "application/json", "x-shadowedge-token": "secret-token"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid JSON body"
