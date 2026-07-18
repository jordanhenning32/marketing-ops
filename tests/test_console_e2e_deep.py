import importlib
import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _goal():
    return {
        "customers": 0,
        "target_customers": 500,
        "customers_remaining": 500,
        "days_remaining": None,
        "sales_per_day_required": None,
    }


@pytest.fixture()
def isolated_console(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    console = importlib.import_module("console")

    state = tmp_path / "state"
    content = tmp_path / "content"
    daily = tmp_path / "daily-ops"
    config = tmp_path / "config"
    for folder in (state, content, daily, config, content / "jobs", content / "incoming", content / "incoming-videos"):
        folder.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(console, "STATE_DIR", state)
    monkeypatch.setattr(console, "CONTENT_DIR", content)
    monkeypatch.setattr(console, "DAILY_OPS_DIR", daily)
    monkeypatch.setattr(console, "INCOMING_DIR", content / "incoming")
    monkeypatch.setattr(console, "INCOMING_VIDEOS_DIR", content / "incoming-videos")

    import affiliates
    import analytics
    import daily_posts
    import email_nurture
    import ga4_stats
    import leads
    import listening
    import tiktok_auth
    import tracking
    import youtube_stats

    monkeypatch.setattr(console.jobs_mod, "JOBS_DIR", content / "jobs")
    monkeypatch.setattr(affiliates, "STATE_PATH", state / "affiliate-applications.json")
    monkeypatch.setattr(analytics, "PERFORMANCE_PATH", state / "performance.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(leads, "STATE_DIR", state)
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")
    monkeypatch.setattr(email_nurture, "STATE_DIR", state)
    monkeypatch.setattr(email_nurture, "CONFIG_DIR", config)
    monkeypatch.setattr(email_nurture, "CONTENT_DIR", content)
    monkeypatch.setattr(email_nurture, "QUEUE_PATH", state / "email-queue.jsonl")
    monkeypatch.setattr(email_nurture, "EVENTS_PATH", state / "email-events.jsonl")
    monkeypatch.setattr(tracking, "STATE_DIR", state)
    monkeypatch.setattr(tracking, "EVENTS_PATH", state / "events.jsonl")
    monkeypatch.setattr(listening, "STATE_DIR", state)
    monkeypatch.setattr(listening, "OPPORTUNITIES_PATH", state / "listening-opportunities.jsonl")
    monkeypatch.setattr(listening, "ENGAGEMENT_QUEUE_PATH", state / "engagement-queue.jsonl")
    monkeypatch.setattr(listening, "ENGAGEMENT_LOG_PATH", state / "engagement-log.jsonl")
    monkeypatch.setattr(ga4_stats, "GA4_DIR", state / ".ga4")
    monkeypatch.setattr(ga4_stats, "LAST_PULL_PATH", state / ".ga4" / "last-pull.json")

    fake_state = SimpleNamespace(partnerships=[], influencers=[])

    def fake_run():
        path = daily / f"{date.today().isoformat()}.md"
        path.write_text("# Daily Ops\n", encoding="utf-8")
        return path

    monkeypatch.setattr(console.ops_tracker, "load_state", lambda: fake_state)
    monkeypatch.setattr(console.ops_tracker, "compute_dashboard", lambda *_args, **_kwargs: {"goal": _goal()})
    monkeypatch.setattr(console.ops_tracker, "pick_todays_partner", lambda _dash: None)
    monkeypatch.setattr(console.ops_tracker, "run", fake_run)
    monkeypatch.setattr(console, "_outreach_summary", lambda: None)
    monkeypatch.setattr(daily_posts, "load_posts", lambda *args, **kwargs: [])
    monkeypatch.setattr(daily_posts, "posting_streak", lambda: 0)

    yt_status = SimpleNamespace(
        connected=False,
        has_client_secret=False,
        channel_id=None,
        channel_title=None,
        last_refreshed=None,
        error=None,
    )
    monkeypatch.setattr(console.youtube_auth, "get_status", lambda: yt_status)
    monkeypatch.setattr(console.youtube_auth, "is_connected", lambda: False)
    monkeypatch.setattr(console.youtube_auth, "has_client_secret", lambda: False)
    monkeypatch.setattr(console.youtube_auth, "disconnect", lambda: None)
    monkeypatch.setattr(youtube_stats, "channel_summary", lambda: {})
    monkeypatch.setattr(youtube_stats, "video_summary", lambda: [])
    monkeypatch.setattr(youtube_stats, "last_pull_at", lambda: None)

    monkeypatch.setattr(tiktok_auth, "get_status", lambda: {"connected": False})
    monkeypatch.setattr(tiktok_auth, "is_connected", lambda: False)
    monkeypatch.setattr(tiktok_auth, "has_credentials", lambda: False)
    monkeypatch.setattr(tiktok_auth, "disconnect", lambda: None)

    monkeypatch.setattr(ga4_stats, "is_configured", lambda: False)
    monkeypatch.setattr(ga4_stats, "last_pull", lambda: None)

    monkeypatch.setattr(listening, "load_config", lambda: {"keywords": [], "sources": {}})
    monkeypatch.setattr(listening, "source_health", lambda _cfg: {})
    monkeypatch.setattr(listening, "list_opportunities", lambda: [])
    monkeypatch.setattr(listening, "queue_items", lambda status=None: [])
    monkeypatch.setattr(listening, "last_scan_summary", lambda: None)

    return SimpleNamespace(
        console=console,
        client=TestClient(console.app),
        state=state,
        content=content,
        daily=daily,
        config=config,
    )


def _write_video_run(content_root, *, slug="safe-run", with_long_form=False):
    run = content_root / slug
    run.mkdir()
    (run / "00-meta.json").write_text(json.dumps({"campaign_id": slug}), encoding="utf-8")
    (run / "01-draft.md").write_text("Draft body", encoding="utf-8")
    (run / "clips").mkdir()
    (run / "clips" / "01-demo.mp4").write_bytes(b"fake mp4")
    for platform in ("youtube", "tiktok", "instagram", "rumble"):
        kit = run / "distribution" / platform / "01"
        kit.mkdir(parents=True)
        (kit / "title.txt").write_text(f"{platform} title\n", encoding="utf-8")
        (kit / "caption.txt").write_text(f"{platform} caption", encoding="utf-8")
        metadata = {"uploaded_at": "2026-06-20T10:00:00", "platform_url": "https://youtu.be/demo"} if platform == "youtube" else {}
        (kit / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    long_form = {"enabled": False}
    if with_long_form:
        (run / "source.mp4").write_bytes(b"full video")
        kit = run / "distribution" / "youtube" / "00"
        kit.mkdir(parents=True, exist_ok=True)
        website_url = f"https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign={slug}&utm_content=long-form"
        (kit / "title.txt").write_text("Full Discipline Score Walkthrough\n", encoding="utf-8")
        (kit / "caption.txt").write_text(f"Free risk-control checklist: {website_url}\n\nFull walkthrough.", encoding="utf-8")
        (kit / "source-clip.txt").write_text(str((run / "source.mp4").resolve()), encoding="utf-8")
        (kit / "thumbnail.jpg").write_bytes(b"jpg")
        (kit / "metadata.json").write_text(
            json.dumps({
                "content_type": "long_form",
                "title": "Full Discipline Score Walkthrough",
                "website_url": website_url,
                "uploaded_at": None,
                "platform_url": None,
                "thumbnail_relpath": "distribution/youtube/00/thumbnail.jpg",
            }),
            encoding="utf-8",
        )
        long_form = {
            "enabled": True,
            "title": "Full Discipline Score Walkthrough",
            "duration_sec": 240,
            "distribution": {"youtube": "distribution/youtube/00"},
            "website_url": website_url,
        }
    manifest = {
        "clips": [
            {
                "index": 1,
                "title": "Demo clip",
                "start": "00:00",
                "end": "00:30",
                "duration_sec": 30,
                "clip": "clips/01-demo.mp4",
                "distribution": {
                    "youtube": "distribution/youtube/01",
                    "tiktok": "distribution/tiktok/01",
                    "instagram": "distribution/instagram/01",
                    "rumble": "distribution/rumble/01",
                },
            }
        ],
        "long_form": long_form,
    }
    (run / "00-pipeline-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/", 307),
        ("/console-id", 200),
        ("/health", 200),
        ("/pipeline", 200),
        ("/state", 200),
        ("/today", 200),
        ("/docs/final-operator-handoff", 200),
        ("/affiliates", 200),
        ("/affiliates/discover", 200),
        ("/content", 200),
        ("/content/youtube/settings", 200),
        ("/tiktok", 200),
        ("/leads", 200),
        ("/leads/export.csv", 200),
        ("/lead/test-campaign", 200),
        ("/email-queue", 200),
        ("/listening", 200),
        ("/listening/scan/status", 200),
        ("/engagement", 200),
        ("/performance", 200),
        ("/youtube", 200),
        ("/unsubscribe/route-sweep@example.com", 200),
    ],
)
def test_console_get_route_sweep_renders_without_real_state(isolated_console, path, status):
    response = isolated_console.client.get(path, follow_redirects=False)

    assert response.status_code == status, path
    assert "Traceback" not in response.text


def test_content_routes_reject_path_traversal_slug(isolated_console, tmp_path):
    marker = tmp_path / "root-leak.md"
    marker.write_text("root leak should never render", encoding="utf-8")

    detail = isolated_console.client.get("/content/%2E%2E")
    clip = isolated_console.client.get("/content/clip/%2E%2E/root-leak.md")
    save = isolated_console.client.post(
        "/content/%2E%2E/save",
        data={"filename": "root-leak.md", "content": "changed"},
        follow_redirects=False,
    )

    assert detail.status_code == 404
    assert "root leak should never render" not in detail.text
    assert clip.status_code == 404
    assert save.status_code in {303, 404}
    assert marker.read_text(encoding="utf-8") == "root leak should never render"


def test_content_distribution_actions_round_trip_and_degrade_when_disconnected(isolated_console):
    run = _write_video_run(isolated_console.content)
    client = isolated_console.client

    detail = client.get("/content/safe-run")
    saved = client.post(
        "/content/safe-run/clip/1/kit/youtube/save",
        data={"title": "Edited title", "caption": "Edited caption"},
    )
    posted = client.post("/content/safe-run/clip/1/mark-posted", data={"channel": "x"})
    youtube = client.post("/content/safe-run/clip/1/upload-youtube", data={"privacy": "public"})
    tiktok = client.post("/content/safe-run/clip/1/upload-tiktok")
    clip = client.get("/content/clip/safe-run/clips/01-demo.mp4")

    assert detail.status_code == 200
    assert "1/5 distributed" in detail.text
    assert saved.json() == {"ok": True}
    assert (run / "distribution" / "youtube" / "01" / "title.txt").read_text(encoding="utf-8") == "Edited title\n"
    assert (run / "distribution" / "youtube" / "01" / "caption.txt").read_text(encoding="utf-8") == "Edited caption"
    assert posted.status_code == 200
    assert posted.json()["posted"] is True
    assert youtube.status_code == 400
    assert "YouTube not connected" in youtube.json()["error"]
    assert tiktok.status_code == 400
    assert "TikTok not connected" in tiktok.json()["error"]
    assert clip.status_code == 200


def test_content_detail_full_video_card_renders_only_when_long_form_kit_exists(isolated_console):
    _write_video_run(isolated_console.content, slug="short-only", with_long_form=False)
    _write_video_run(isolated_console.content, slug="long-ready", with_long_form=True)
    client = isolated_console.client

    short_detail = client.get("/content/short-only")
    long_detail = client.get("/content/long-ready")

    assert short_detail.status_code == 200
    assert "Full video for YouTube" not in short_detail.text
    assert long_detail.status_code == 200
    assert "Full video for YouTube" in long_detail.text
    assert "distribution/youtube/00" in long_detail.text
    assert "https://shadowedgetools.com/checklist?utm_source=youtube" in long_detail.text


def test_performance_csv_import_bad_input_redirects_with_error(isolated_console):
    response = isolated_console.client.post(
        "/performance/import-csv",
        files={"file": ("bad.csv", b"date,metric\n2026-06-20,revenue\n", "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/performance?err=")
    assert not (isolated_console.state / "manual-metrics" / "bad.csv").exists()


def test_integration_refresh_routes_fail_gracefully_when_unconfigured(isolated_console, monkeypatch):
    import ga4_stats
    import youtube_stats

    monkeypatch.setattr(ga4_stats, "pull", lambda: (_ for _ in ()).throw(RuntimeError("GA4 key not found")))
    monkeypatch.setattr(youtube_stats, "pull", lambda: (_ for _ in ()).throw(RuntimeError("YouTube not connected")))

    yt_start = isolated_console.client.get("/content/youtube/auth/start", follow_redirects=False)
    tt_start = isolated_console.client.get("/tiktok/auth/start", follow_redirects=False)
    ga4 = isolated_console.client.post("/performance/ga4-refresh", follow_redirects=False)
    yt = isolated_console.client.post("/youtube/refresh", follow_redirects=False)

    assert yt_start.status_code == 303
    assert yt_start.headers["location"] == "/youtube"
    assert tt_start.status_code == 303
    assert "TIKTOK_CLIENT_KEY" in tt_start.headers["location"]
    assert ga4.status_code == 303
    assert "err=GA4+pull+failed" in ga4.headers["location"]
    assert yt.status_code == 303
    assert "err=YouTube+not+connected" in yt.headers["location"]


def test_daily_posts_cli_configures_utf8_stdio(monkeypatch):
    import daily_posts

    class FakeStdout:
        def __init__(self):
            self.reconfigured = []
            self.buffer = []

        def reconfigure(self, **kwargs):
            self.reconfigured.append(kwargs)

        def write(self, text):
            self.buffer.append(text)

        def flush(self):
            return None

    fake = FakeStdout()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(daily_posts, "load_posts", lambda: [
        {"status": "pending", "platform": "x", "pillar": "risk", "text": "Smart quotes and arrows -> ok"}
    ])

    assert daily_posts.main(["show"]) == 0
    assert fake.reconfigured
    assert fake.reconfigured[0]["encoding"].lower() == "utf-8"
