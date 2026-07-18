import importlib
import threading
from pathlib import Path
from types import SimpleNamespace


def isolate_listening_state(tmp_path, monkeypatch):
    import listening
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(listening, "OPPORTUNITIES_PATH", state / "listening-opportunities.jsonl")
    monkeypatch.setattr(listening, "ENGAGEMENT_QUEUE_PATH", state / "engagement-queue.jsonl")
    monkeypatch.setattr(listening, "ENGAGEMENT_LOG_PATH", state / "engagement-log.jsonl")
    return state


def isolate_email_route_state(tmp_path, monkeypatch):
    import email_nurture
    import leads
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
    return state


def test_console_new_pages_render(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    for path in ["/leads", "/email-queue", "/performance", "/listening", "/engagement", "/lead/test-campaign"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Shadow Edge" in response.text or "Hermes" in response.text


def test_console_serves_favicon(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    favicon = client.get("/favicon.ico")
    today = client.get("/today")

    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/x-icon")
    assert b"\x00\x00\x01\x00" in favicon.content[:8]
    assert '/static/shadow-edge.ico' in today.text


def test_youtube_refresh_updates_editor_guidance(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import refresh_editor
    import youtube_stats
    from fastapi.testclient import TestClient

    refreshed = []
    regenerated = []
    monkeypatch.setattr(youtube_stats, "pull", lambda: {
        "video_count": 2,
        "tracked_count": 1,
        "pulled_at": "2026-06-17T12:00:00",
    })
    monkeypatch.setattr(refresh_editor, "main", lambda: refreshed.append(True) or 0)
    monkeypatch.setattr(console.ops_tracker, "run", lambda: regenerated.append(True) or Path("daily-ops/2026-06-19.md"))

    response = TestClient(console.app).post("/youtube/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert refreshed == [True]
    assert regenerated == [True]
    assert "Editor+refreshed" in response.headers["location"]
    assert "Today+regenerated" in response.headers["location"]


def test_email_queue_page_has_summary_filters_and_hides_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import email_nurture
    from fastapi.testclient import TestClient

    isolate_email_route_state(tmp_path, monkeypatch)
    email_nurture.write_jsonl(email_nurture.QUEUE_PATH, [
        {
            "queue_key": "fixture",
            "email": "fixture@example.com",
            "campaign_id": "cid",
            "step_id": "confirmation",
            "subject": "Fixture subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
        {
            "queue_key": "real",
            "email": "real@customer.test",
            "campaign_id": "cid",
            "step_id": "demo",
            "subject": "Real subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
            "due_at": "2099-01-01T00:00:00+00:00",
        },
        {
            "queue_key": "archived",
            "email": "archived@example.com",
            "campaign_id": "cid",
            "step_id": "demo",
            "subject": "Hidden subject",
            "status": "archived_fixture",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
    ])

    fixture = TestClient(console.app).get("/email-queue?filter=fixture")
    future = TestClient(console.app).get("/email-queue?filter=future")

    assert fixture.status_code == 200
    assert "Fixture/example.com" in fixture.text
    assert "fixture@example.com" in fixture.text
    assert "fixture/test domain" in fixture.text
    assert "Hidden subject" not in fixture.text
    assert "real@customer.test" not in fixture.text
    assert future.status_code == 200
    assert "Future" in future.text
    assert "real@customer.test" in future.text
    assert "fixture@example.com" not in future.text
    assert 'name="manual_url"' in future.text


def test_email_queue_action_handles_manual_sent_errors_and_preserves_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import email_nurture
    from fastapi.testclient import TestClient

    isolate_email_route_state(tmp_path, monkeypatch)
    email_nurture.write_jsonl(email_nurture.QUEUE_PATH, [{
        "queue_key": "real",
        "email": "real@customer.test",
        "campaign_id": "cid",
        "step_id": "demo",
        "subject": "Real subject",
        "status": "queued",
        "provider": "none",
        "created_at": "2026-06-01T00:00:00+00:00",
    }])

    client = TestClient(console.app)
    missing = client.post(
        "/email-queue/action",
        data={"queue_key": "real", "email": "real@customer.test", "action": "mark_sent", "queue_filter": "real"},
        follow_redirects=False,
    )
    saved = client.post(
        "/email-queue/action",
        data={"queue_key": "real", "email": "real@customer.test", "action": "mark_sent", "manual_url": "manual://sent", "queue_filter": "real"},
        follow_redirects=False,
    )
    row = email_nurture.read_jsonl(email_nurture.QUEUE_PATH)[0]

    assert missing.status_code == 303
    assert "filter=real" in missing.headers["location"]
    assert "sent+note%2FURL+is+required" in missing.headers["location"]
    assert saved.status_code == 303
    assert "filter=real" in saved.headers["location"]
    assert "msg=Email+queue+action+saved" in saved.headers["location"]
    assert row["status"] == "sent_manual"
    assert row["manual_url"] == "manual://sent"


def test_email_queue_page_groups_sequence_steps_by_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import email_nurture
    from fastapi.testclient import TestClient

    isolate_email_route_state(tmp_path, monkeypatch)
    email_nurture.write_jsonl(email_nurture.QUEUE_PATH, [
        {
            "queue_key": "lead|cid|confirmation",
            "email": "lead@customer.test",
            "campaign_id": "cid",
            "step_id": "confirmation",
            "subject": "Confirmation subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
        {
            "queue_key": "lead|cid|demo",
            "email": "lead@customer.test",
            "campaign_id": "cid",
            "step_id": "demo",
            "subject": "Demo subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
            "delay_days": 7,
        },
    ])

    response = TestClient(console.app).get("/email-queue?filter=queued")

    assert response.status_code == 200
    assert response.text.count('class="email-lead-group"') == 1
    assert "1 leads - 2 steps" in response.text
    assert "2 queued" in response.text
    assert "Confirmation subject" in response.text
    assert "Demo subject" in response.text


def test_email_queue_archive_fixtures_requires_confirmation_and_preserves_real_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import email_nurture
    from fastapi.testclient import TestClient

    isolate_email_route_state(tmp_path, monkeypatch)
    email_nurture.write_jsonl(email_nurture.QUEUE_PATH, [
        {
            "queue_key": "fixture",
            "email": "fixture@example.com",
            "campaign_id": "cid",
            "step_id": "confirmation",
            "subject": "Fixture subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
        {
            "queue_key": "real",
            "email": "real@customer.test",
            "campaign_id": "cid",
            "step_id": "demo",
            "subject": "Real subject",
            "status": "queued",
            "provider": "none",
            "created_at": "2026-06-01T00:00:00+00:00",
        },
    ])

    client = TestClient(console.app)
    missing = client.post("/email-queue/archive-fixtures", data={"confirm": ""}, follow_redirects=False)
    before = {row["queue_key"]: row for row in email_nurture.read_jsonl(email_nurture.QUEUE_PATH)}
    applied = client.post(
        "/email-queue/archive-fixtures",
        data={"confirm": "ARCHIVE_FIXTURE_EMAILS", "reason": "test cleanup"},
        follow_redirects=False,
    )
    after = {row["queue_key"]: row for row in email_nurture.read_jsonl(email_nurture.QUEUE_PATH)}

    assert missing.status_code == 303
    assert "ARCHIVE_FIXTURE_EMAILS" in missing.headers["location"]
    assert before["fixture"]["status"] == "queued"
    assert applied.status_code == 303
    assert "Archived+1+fixture" in applied.headers["location"]
    assert after["fixture"]["status"] == "archived_fixture"
    assert after["fixture"]["archive_reason"] == "test cleanup"
    assert after["real"]["status"] == "queued"


def test_email_queue_bulk_capture_requires_opt_in_and_adds_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import leads
    from fastapi.testclient import TestClient

    isolate_email_route_state(tmp_path, monkeypatch)
    client = TestClient(console.app)

    blocked = client.post(
        "/email-queue/capture-bulk",
        data={"emails": "buyer@customer.test", "campaign_id": "bulk-capture", "opt_in_confirm": ""},
        follow_redirects=False,
    )
    saved = client.post(
        "/email-queue/capture-bulk",
        data={
            "emails": "Buyer@Customer.test, second@customer.test buyer@customer.test",
            "campaign_id": "bulk-capture",
            "opt_in_confirm": "OPTED_IN",
        },
        follow_redirects=False,
    )
    rows = leads.list_leads()

    assert blocked.status_code == 303
    assert "OPTED_IN" in blocked.headers["location"]
    assert saved.status_code == 303
    assert "Captured+2+new" in saved.headers["location"]
    assert [row["email"] for row in rows] == ["buyer@customer.test", "second@customer.test"]
    assert all(row["consent"] is True for row in rows)


def test_email_queue_page_queues_bulk_blast_and_blocks_provider_send(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import email_nurture
    import leads
    from fastapi.testclient import TestClient

    _state = isolate_email_route_state(tmp_path, monkeypatch)
    (tmp_path / "config" / "email.yaml").write_text(
        """provider: resend
send_enabled: true
credential_env: EMAIL_PROVIDER_API_KEY
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("EMAIL_PROVIDER_API_KEY", "test-key")
    leads.write_jsonl(leads.LEADS_PATH, [
        {"email": "buyer@customer.test", "consent": True, "suppressed": False},
        {"email": "fixture@example.com", "consent": True, "suppressed": False},
    ])
    client = TestClient(console.app)

    page = client.get("/email-queue")
    blocked_queue = client.post(
        "/email-queue/blast/queue",
        data={
            "subject": "Launch news",
            "body": "Hello list.",
            "campaign_id": "launch",
            "blast_id": "launch-news",
            "confirm": "",
        },
        follow_redirects=False,
    )
    after_blocked = email_nurture.list_queue(enrich=False)
    queued = client.post(
        "/email-queue/blast/queue",
        data={
            "subject": "Launch news",
            "body": "Hello list.",
            "campaign_id": "launch",
            "blast_id": "launch-news",
            "confirm": email_nurture.BLAST_QUEUE_CONFIRM,
        },
        follow_redirects=False,
    )
    queued_page = client.get("/email-queue?filter=queued")
    sent = client.post(
        "/email-queue/blast/send",
        data={
            "campaign_id": "launch",
            "blast_id": "launch-news",
            "confirm": email_nurture.BLAST_SEND_CONFIRM,
        },
        follow_redirects=False,
    )
    rows = email_nurture.list_queue(enrich=False)

    assert page.status_code == 200
    assert 'action="/email-queue/capture-bulk"' in page.text
    assert "1 opted-in real recipients" in page.text
    assert blocked_queue.status_code == 303
    assert after_blocked == []
    assert queued.status_code == 303
    assert "Queued+1+blast" in queued.headers["location"]
    assert "Launch news" in queued_page.text
    assert "Recent Blasts" in queued_page.text
    assert sent.status_code == 303
    assert "Bulk+send+blocked" in sent.headers["location"]
    assert rows[0]["status"] == "queued"
    assert rows[0]["email"] == "buyer@customer.test"


def test_email_queue_template_tolerates_legacy_context(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")

    html = console.templates.env.get_template("email_queue.html").render(active="email_queue", queue=[{
        "queue_key": "legacy",
        "email": "legacy@example.com",
        "campaign_id": "cid",
        "step_id": "confirmation",
        "subject": "Legacy subject",
        "status": "queued",
        "provider": "none",
    }])

    assert "Email Queue" in html
    assert "Queued" in html
    assert "Legacy subject" in html
    assert "No email queue entries match this filter" not in html


def test_listening_scan_respects_draft_toggle(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    calls = []

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    def fake_run_scan(*, only=None, draft=True):
        calls.append({"only": only, "draft": draft})
        return {"ok": True}

    monkeypatch.setattr(console.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(listening, "run_scan", fake_run_scan)

    client = TestClient(console.app)
    off = client.post("/listening/scan", data={"source": "stocktwits"}, follow_redirects=False)
    on = client.post("/listening/scan", data={"source": "reddit", "draft": "1"}, follow_redirects=False)

    assert off.status_code == 303
    assert on.status_code == 303
    assert calls == [{"only": "stocktwits", "draft": False}, {"only": "reddit", "draft": True}]


def test_listening_scan_rejects_overlapping_runs(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    class HoldingThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            return None

    monkeypatch.setattr(console, "_listening_scan_lock", threading.Lock())
    monkeypatch.setattr(console.threading, "Thread", HoldingThread)
    monkeypatch.setattr(listening, "run_scan", lambda **kwargs: {"ok": True})

    client = TestClient(console.app)
    first = client.post("/listening/scan", data={"draft": "1"}, follow_redirects=False)
    second = client.post("/listening/scan", data={"draft": "1"}, follow_redirects=False)

    assert first.status_code == 303
    assert first.headers["location"] == "/listening?scan=started"
    assert second.status_code == 303
    assert second.headers["location"] == "/listening?scan=running"
    console._listening_scan_lock.release()


def test_engagement_action_redirects_unexpected_errors(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    def boom(*args, **kwargs):
        raise RuntimeError("state file locked")

    monkeypatch.setattr(listening, "set_queue_status", boom)

    client = TestClient(console.app)
    response = client.post(
        "/engagement/action",
        data={"item_id": "reddit:aaa", "action": "posted"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/engagement?err=state+file+locked"


def test_engagement_route_workflow_saves_edits_and_blocks_bad_post_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    isolate_listening_state(tmp_path, monkeypatch)
    listening.append_jsonl(
        listening.ENGAGEMENT_QUEUE_PATH,
        {
            "id": "reddit:route",
            "status": "drafted",
            "score": 0.91,
            "platform_name": "Reddit r/Trading",
            "url": "https://reddit.com/r/trading/comments/1",
            "their_post": "I keep revenge trading.",
            "draft_reply": "Original draft.",
            "approval_status": "pending",
            "compliance_status": "pass",
        },
    )

    client = TestClient(console.app)
    drafted = client.get("/engagement?status=drafted")
    assert drafted.status_code == 200
    assert "Approve first to unlock copy" in drafted.text
    assert "Copy reply" not in drafted.text

    approve = client.post(
        "/engagement/action",
        data={
            "item_id": "reddit:route",
            "action": "approved",
            "draft_reply": "A two-loss cooldown can stop one bad sequence becoming a max-loss day.",
        },
        follow_redirects=False,
    )
    assert approve.status_code == 303
    approved_page = client.get("/engagement?status=approved")
    assert "A two-loss cooldown" in approved_page.text
    assert "Copy reply" in approved_page.text

    bad_post = client.post(
        "/engagement/action",
        data={"item_id": "reddit:route", "action": "posted", "posted_url": "not a url"},
        follow_redirects=False,
    )
    assert bad_post.status_code == 303
    assert "posted_url+must+be+a+valid+http%28s%29+URL" in bad_post.headers["location"]

    posted = client.post(
        "/engagement/action",
        data={"item_id": "reddit:route", "action": "posted", "posted_url": "https://reddit.com/r/trading/comments/1/reply"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    posted_page = client.get("/engagement?status=posted")
    assert "https://reddit.com/r/trading/comments/1/reply" in posted_page.text


def test_listening_page_surfaces_bad_state_and_scan_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    isolate_listening_state(tmp_path, monkeypatch)
    monkeypatch.setattr(listening, "load_config", lambda: {"keywords": ["risk"], "sources": ["bad"]})
    monkeypatch.setattr(listening, "list_opportunities", lambda: (_ for _ in ()).throw(RuntimeError("bad opportunity state")))

    client = TestClient(console.app)
    page = client.get("/listening")
    assert page.status_code == 200
    assert "bad opportunity state" in page.text

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    def boom(*args, **kwargs):
        raise RuntimeError("scan failed loudly")

    monkeypatch.setattr(console.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(listening, "run_scan", boom)
    monkeypatch.setattr(listening, "load_config", lambda: {"keywords": ["risk"], "sources": {}})
    monkeypatch.setattr(listening, "list_opportunities", lambda: [])

    response = client.post("/listening/scan", data={"source": ""}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/listening")
    assert "scan failed loudly" in page.text


def test_listening_page_survives_last_scan_summary_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    isolate_listening_state(tmp_path, monkeypatch)
    monkeypatch.setattr(listening, "load_config", lambda: {"keywords": ["risk"], "sources": {}})
    monkeypatch.setattr(listening, "list_opportunities", lambda: [])
    monkeypatch.setattr(listening, "queue_items", lambda status=None: [])
    monkeypatch.setattr(listening, "last_scan_summary", lambda: (_ for _ in ()).throw(RuntimeError("summary unreadable")))

    response = TestClient(console.app).get("/listening")

    assert response.status_code == 200
    assert "summary unreadable" in response.text


def test_console_identity_and_root_redirect(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    identity = client.get("/console-id")
    assert identity.status_code == 200
    assert identity.json()["app"] == "shadow-edge-marketing-ops"
    assert identity.json()["home_path"] == "/today"
    assert identity.headers["access-control-allow-origin"] == "*"

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/today"


def test_today_page_has_daily_post_guidance_box(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import daily_posts
    from fastapi.testclient import TestClient

    monkeypatch.setattr(daily_posts, "load_posts", lambda: [
        {"platform": "x", "pillar": "risk", "status": "pending", "text": "Draft post."}
    ])
    monkeypatch.setattr(daily_posts, "posting_streak", lambda: 0)

    response = TestClient(console.app).get("/today")

    assert response.status_code == 200
    assert 'id="daily-post-guidance"' in response.text
    assert 'name="post_guidance"' in response.text
    assert "data-post-regen-form" in response.text


def test_today_generate_posts_passes_post_guidance(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")
    import daily_posts
    from fastapi.testclient import TestClient

    captured = {}

    def fake_generate_daily_posts(*, only=None, post_guidance=""):
        captured["only"] = only
        captured["post_guidance"] = post_guidance
        return {"generated": 1, "pillar": "risk-tip", "errors": []}

    monkeypatch.setattr(daily_posts, "generate_daily_posts", fake_generate_daily_posts)

    response = TestClient(console.app).post(
        "/today/generate-posts",
        data={
            "only": "x",
            "post_guidance": "  Hype Bracket Boss scale-outs.  ",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert captured == {"only": "x", "post_guidance": "Hype Bracket Boss scale-outs."}
    assert "Direction+applied" in response.headers["location"]


def test_listening_scan_status_tracks_running_then_done(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    import importlib
    import threading
    import time as _time
    console = importlib.import_module("console")
    import listening
    from fastapi.testclient import TestClient

    gate = threading.Event()

    def fake_scan(*args, **kwargs):
        gate.wait(timeout=5)
        return {"ran_at": "now", "found": 3, "new": 3, "retried": 0, "scored": True, "drafted": 2}

    monkeypatch.setattr(listening, "run_scan", fake_scan)
    client = TestClient(console.app)

    assert client.get("/listening/scan/status").json()["running"] is False

    resp = client.post("/listening/scan", data={"source": "", "draft": "1"}, follow_redirects=False)
    assert resp.status_code == 303

    running = client.get("/listening/scan/status").json()
    assert running["running"] is True
    assert running["elapsed_seconds"] >= 0

    gate.set()
    for _ in range(100):
        if not client.get("/listening/scan/status").json()["running"]:
            break
        _time.sleep(0.02)

    done = client.get("/listening/scan/status").json()
    assert done["running"] is False
    assert done["summary"]["drafted"] == 2


def test_video_upload_preflight_blocks_missing_anthropic_key(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    console = importlib.import_module("console")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    response = client.post(
        "/content/upload-video",
        data={"provider": "local", "product": "both", "whisper_model": "base"},
        files={"file": ("demo.mp4", b"not a real video", "video/mp4")},
    )

    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["error"]


def test_video_upload_rejects_empty_file(monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    console = importlib.import_module("console")
    monkeypatch.setattr(console, "_video_upload_blockers", lambda provider: [])
    monkeypatch.setattr(
        console.jobs_mod,
        "create_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("empty upload should not create a job")),
    )
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    response = client.post(
        "/content/upload-video",
        data={"provider": "local", "product": "both", "whisper_model": "base"},
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )

    assert response.status_code == 400
    assert "Upload was empty" in response.json()["error"]


def test_video_upload_captures_creator_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    console = importlib.import_module("console")
    monkeypatch.setattr(console, "INCOMING_VIDEOS_DIR", tmp_path / "incoming-videos")
    monkeypatch.setattr(console, "_video_upload_blockers", lambda provider: [])
    captured = {}

    def fake_create_job(**kwargs):
        captured["create"] = kwargs
        return SimpleNamespace(id="job123")

    def fake_run_job(job_id, target, *, args=(), kwargs=None):
        captured["run"] = {
            "job_id": job_id,
            "target": target,
            "args": args,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(console.jobs_mod, "create_job", fake_create_job)
    monkeypatch.setattr(console.jobs_mod, "run_job", fake_run_job)
    from fastapi.testclient import TestClient

    client = TestClient(console.app)
    response = client.post(
        "/content/upload-video",
        data={
            "provider": "local",
            "product": "both",
            "whisper_model": "base",
            "creator_guidance": "Favor discipline score.",
        },
        files={"file": ("demo.mp4", b"video bytes", "video/mp4")},
    )

    assert response.status_code == 200
    assert captured["create"]["meta"]["creator_guidance"] == "Favor discipline score."
    assert captured["run"]["kwargs"]["creator_guidance"] == "Favor discipline score."
