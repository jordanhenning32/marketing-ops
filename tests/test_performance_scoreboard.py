from __future__ import annotations

import importlib
import json
import sys
from datetime import date as real_date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient


class FrozenDate(real_date):
    current = real_date(2026, 7, 2)

    @classmethod
    def today(cls):
        return cls.current


@pytest.fixture
def perf_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DISABLE_WATCHER", "1")
    monkeypatch.delenv("TIKTOK_STATS", raising=False)
    monkeypatch.delenv("TIKTOK_DIRECT_POST", raising=False)

    analytics = importlib.import_module("analytics")
    console = importlib.import_module("console")
    leads = importlib.import_module("leads")
    tiktok_auth = importlib.import_module("tiktok_auth")
    tiktok_stats = importlib.import_module("tiktok_stats")
    ga4_stats = importlib.import_module("ga4_stats")

    state = tmp_path / "state"
    state.mkdir()

    monkeypatch.setattr(analytics, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(analytics, "PERFORMANCE_PATH", state / "performance.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(analytics, "date", FrozenDate)
    monkeypatch.setattr(console, "date", FrozenDate)
    monkeypatch.setattr(console, "STATE_DIR", state)
    monkeypatch.setattr(leads, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")

    monkeypatch.setattr(console.ops_tracker, "load_state", lambda: {})
    monkeypatch.setattr(console.ops_tracker, "compute_dashboard", lambda _state: {"goal": None})
    monkeypatch.setitem(
        sys.modules,
        "youtube_stats",
        SimpleNamespace(
            channel_summary=lambda: {},
            video_summary=lambda: [],
            last_pull_at=lambda: None,
        ),
    )
    monkeypatch.setattr(console.youtube_auth, "connection_state", lambda: "disconnected", raising=False)

    tt_dir = state / ".tiktok"
    monkeypatch.setattr(tiktok_auth, "TT_DIR", tt_dir)
    monkeypatch.setattr(tiktok_auth, "TOKEN_PATH", tt_dir / "token.json")
    monkeypatch.setattr(tiktok_auth, "STATE_PATH", tt_dir / "auth_state.json")
    monkeypatch.setattr(tiktok_stats, "TT_DIR", tt_dir)
    monkeypatch.setattr(tiktok_stats, "LAST_PULL_PATH", tt_dir / "stats-last-pull.json")

    ga4_dir = state / ".ga4"
    monkeypatch.setattr(ga4_stats, "GA4_DIR", ga4_dir)
    monkeypatch.setattr(ga4_stats, "LAST_PULL_PATH", ga4_dir / "last-pull.json")

    return SimpleNamespace(
        state=state,
        analytics=analytics,
        console=console,
        leads=leads,
        tiktok_auth=tiktok_auth,
        tiktok_stats=tiktok_stats,
        ga4_stats=ga4_stats,
    )


def _performance_rows(env):
    path = env.analytics.PERFORMANCE_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_leads_snapshot_once_per_day_and_trends(perf_env):
    FrozenDate.current = real_date(2026, 7, 2)
    perf_env.leads.capture_lead(email="a@example.com", campaign_id="cid", consent=True)
    perf_env.leads.capture_lead(email="b@example.com", campaign_id="cid", consent=True)

    first = perf_env.console._scoreboard()
    second = perf_env.console._scoreboard()

    leads_rows = [r for r in _performance_rows(perf_env) if r["metric"] == "leads"]
    assert first["leads"]["count"] == 2
    assert second["leads"]["count"] == 2
    assert len(leads_rows) == 1
    assert leads_rows[0]["date"] == "2026-07-02"
    assert leads_rows[0]["value"] == 2

    FrozenDate.current = real_date(2026, 7, 3)
    perf_env.leads.capture_lead(email="c@example.com", campaign_id="cid", consent=True)

    third = perf_env.console._scoreboard()

    leads_rows = [r for r in _performance_rows(perf_env) if r["metric"] == "leads"]
    assert [r["date"] for r in leads_rows] == ["2026-07-02", "2026-07-03"]
    assert third["leads"]["count"] == 3
    assert third["leads"]["trend"]["dir"] == "up"
    assert third["leads"]["trend"]["spark"]
    assert "1" in third["leads"]["trend"]["delta"]


def test_leads_snapshot_failure_falls_back_to_latest_recorded_value(perf_env, monkeypatch):
    perf_env.analytics.record_metric("leads", 5, metric_date="2026-07-01", source="leads")

    def fail_list_leads():
        raise RuntimeError("lead store unavailable")

    monkeypatch.setattr(perf_env.leads, "list_leads", fail_list_leads)

    scoreboard = perf_env.console._scoreboard()
    response = TestClient(perf_env.console.app).get("/performance")

    assert scoreboard["leads"]["count"] == 5
    assert response.status_code == 200
    assert "Email leads" in response.text


def test_tiktok_default_off_hides_auto_controls_and_refresh_redirects(perf_env, monkeypatch):
    monkeypatch.setattr(
        perf_env.tiktok_auth,
        "fetch_user_stats",
        lambda: (_ for _ in ()).throw(AssertionError("stats endpoint should not be called")),
    )

    assert perf_env.tiktok_auth.stats_enabled() is False
    assert perf_env.tiktok_stats.is_configured() is False
    scopes = perf_env.tiktok_auth.oauth_scopes()
    assert "user.info.basic" in scopes
    assert "video.upload" in scopes
    assert "video.publish" not in scopes
    assert "user.info.stats" not in scopes

    client = TestClient(perf_env.console.app)
    page = client.get("/performance")
    refresh = client.post("/performance/tiktok-refresh", follow_redirects=False)

    assert page.status_code == 200
    assert "enable TIKTOK_STATS" in page.text
    assert "Pull TikTok" not in page.text
    assert refresh.status_code == 303
    err = parse_qs(urlparse(refresh.headers["location"]).query)["err"][0]
    assert "TikTok pull failed" in err
    assert "TIKTOK_STATS=1" in err


def test_tiktok_stats_pull_records_followers_likes_only_and_page_controls(perf_env, monkeypatch):
    monkeypatch.setenv("TIKTOK_STATS", "1")
    perf_env.tiktok_auth.TT_DIR.mkdir(parents=True)
    perf_env.tiktok_auth.TOKEN_PATH.write_text(
        json.dumps({"access_token": "fake-token", "expires_at": 9999999999}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        perf_env.tiktok_auth,
        "fetch_user_stats",
        lambda: {"followers": 42, "likes": 99, "videos": 7},
    )

    summary = perf_env.tiktok_stats.pull()
    rows = _performance_rows(perf_env)
    metrics = [r["metric"] for r in rows]

    assert summary["followers"] == 42
    assert summary["likes"] == 99
    assert perf_env.tiktok_stats.LAST_PULL_PATH.exists()
    assert "tiktok_followers" in metrics
    assert "tiktok_likes" in metrics
    assert "tiktok_views" not in metrics

    client = TestClient(perf_env.console.app)
    page = client.get("/performance")
    refresh = client.post("/performance/tiktok-refresh", follow_redirects=False)

    assert page.status_code == 200
    assert "Pull TikTok" in page.text
    assert "Total likes" in page.text
    assert "42" in page.text
    assert "99" in page.text
    assert refresh.status_code == 303
    assert "TikTok%3A+42+followers%2C+99+total+likes" in refresh.headers["location"]


def test_tiktok_fetch_failures_and_record_false_do_not_write_metrics(perf_env, monkeypatch):
    assert perf_env.tiktok_auth.fetch_user_stats() == {}

    monkeypatch.setenv("TIKTOK_STATS", "1")
    monkeypatch.setattr(perf_env.tiktok_auth, "get_access_token", lambda: None)
    assert perf_env.tiktok_auth.fetch_user_stats() == {}

    class ErrorClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=500, json=lambda: {"error": "server_error"})

    monkeypatch.setattr(perf_env.tiktok_auth, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(perf_env.tiktok_auth.httpx, "Client", lambda *args, **kwargs: ErrorClient())
    assert perf_env.tiktok_auth.fetch_user_stats() == {}

    monkeypatch.setattr(
        perf_env.tiktok_auth,
        "fetch_user_stats",
        lambda: {"followers": 12, "likes": 34, "videos": 5},
    )
    summary = perf_env.tiktok_stats.pull(record=False)

    assert summary["followers"] == 12
    assert not perf_env.analytics.PERFORMANCE_PATH.exists()


def test_tiktok_stats_scope_does_not_disturb_posting_scopes(perf_env, monkeypatch):
    monkeypatch.setenv("TIKTOK_STATS", "1")
    scopes = perf_env.tiktok_auth.oauth_scopes()
    assert "user.info.stats" in scopes
    assert "video.upload" in scopes
    assert "video.publish" not in scopes

    monkeypatch.setenv("TIKTOK_DIRECT_POST", "1")
    scopes = perf_env.tiktok_auth.oauth_scopes()
    assert "user.info.stats" in scopes
    assert "video.upload" in scopes
    assert "video.publish" in scopes


def test_imported_scoreboard_metric_surfaces_on_performance_page(perf_env, tmp_path):
    csv_path = tmp_path / "scoreboard.csv"
    csv_path.write_text(
        "date,campaign_id,metric,value,source,status\n"
        "2026-07-01,cid,x_followers,321,manual_csv,measured\n",
        encoding="utf-8",
    )

    assert perf_env.analytics.validate_csv(csv_path) == []
    assert perf_env.analytics.import_csv(csv_path) == 1

    response = TestClient(perf_env.console.app).get("/performance")

    assert response.status_code == 200
    assert "321" in response.text


def test_csv_validation_accepts_scoreboard_metrics_and_rejects_unknown(perf_env, tmp_path):
    ok_csv = tmp_path / "ok.csv"
    ok_csv.write_text(
        "date,campaign_id,metric,value,source,status\n"
        "2026-07-01,cid,tiktok_followers,1,manual_csv,measured\n"
        "2026-07-01,cid,x_followers,2,manual_csv,measured\n"
        "2026-07-01,cid,leads,3,manual_csv,measured\n"
        "2026-07-01,cid,customers,4,manual_csv,measured\n"
        "2026-07-01,cid,revenue,5,manual_csv,measured\n"
        "2026-07-01,cid,website_visitors,6,manual_csv,measured\n",
        encoding="utf-8",
    )
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "date,campaign_id,metric,value,source,status\n"
        "2026-07-01,cid,not_a_real_metric,1,manual_csv,measured\n",
        encoding="utf-8",
    )

    assert perf_env.analytics.validate_csv(ok_csv) == []
    assert perf_env.analytics.validate_csv(bad_csv) == ["line 2: unknown metric not_a_real_metric"]


def test_performance_page_fallback_keys_when_stats_helpers_fail(perf_env, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "ga4_stats",
        SimpleNamespace(
            is_configured=lambda: (_ for _ in ()).throw(RuntimeError("ga4 failed")),
            last_pull=lambda: (_ for _ in ()).throw(RuntimeError("ga4 failed")),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tiktok_stats",
        SimpleNamespace(
            is_configured=lambda: (_ for _ in ()).throw(RuntimeError("tiktok failed")),
            last_pull_at=lambda: (_ for _ in ()).throw(RuntimeError("tiktok failed")),
        ),
    )

    response = TestClient(perf_env.console.app).get("/performance")

    assert response.status_code == 200
    assert "Email leads" in response.text
    assert "Pull TikTok" not in response.text


def test_performance_metric_bad_inputs_are_graceful(perf_env):
    client = TestClient(perf_env.console.app)

    missing_metric = client.post("/performance/metric", data={"value": "1"}, follow_redirects=False)
    huge_value = client.post(
        "/performance/metric",
        data={"metric": "x_followers", "value": "9" * 5000, "extra": "ignored"},
        follow_redirects=False,
    )
    non_numeric = client.post(
        "/performance/metric",
        data={"metric": "x_followers", "value": "not numeric"},
        follow_redirects=False,
    )

    assert missing_metric.status_code == 422
    assert huge_value.status_code == 303
    assert non_numeric.status_code == 303


def test_repeated_performance_renders_do_not_append_daily_lead_snapshots(perf_env):
    FrozenDate.current = real_date(2026, 7, 2)
    perf_env.leads.capture_lead(email="steady@example.com", campaign_id="cid", consent=True)
    client = TestClient(perf_env.console.app)

    for _ in range(75):
        response = client.get("/performance")
        assert response.status_code == 200

    leads_rows = [r for r in _performance_rows(perf_env) if r["metric"] == "leads"]
    assert len(leads_rows) == 1
    assert leads_rows[0]["value"] == 1


def test_real_performance_log_purge_integrity():
    path = Path(__file__).resolve().parents[1] / "state" / "performance.jsonl"
    rows = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"invalid JSON on line {idx}: {exc}") from exc

    # performance.jsonl is an append-only log that grows with every daily pull /
    # snapshot, so assert the post-purge floor (229 real rows) rather than a frozen
    # count that would false-fail the day after any legitimate refresh.
    assert len(rows) >= 229
    # Statuses must stay within the allowed vocabulary (no corruption).
    assert {row.get("status", "measured") for row in rows} <= {"measured", "unavailable", "blocked", "not_configured"}
    # Purge integrity — the real invariant: dead placeholder rows (status
    # "unavailable" with a blank value) must never be reintroduced.
    assert not [
        row
        for row in rows
        if row.get("status") == "unavailable" and str(row.get("value", "")).strip() == ""
    ]
