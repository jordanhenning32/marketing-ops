from datetime import date, timedelta

import ops_tracker


def test_daily_ops_surfaces_top_video_signal(monkeypatch):
    state = ops_tracker.State()
    state.goal.target_date = date.today() + timedelta(days=10)
    dash = ops_tracker.compute_dashboard(state)

    monkeypatch.setattr(ops_tracker, "_video_activity", lambda: {"days_ago": 1, "done": 1})
    monkeypatch.setattr(ops_tracker, "_youtube_signal", lambda: {})
    monkeypatch.setattr(ops_tracker, "_engagement_signal", lambda: {
        "surfaced": 0,
        "drafted_waiting": 0,
        "last_scan_days": None,
    })
    monkeypatch.setattr(ops_tracker, "_real_lead_count", lambda: 0)
    monkeypatch.setattr(ops_tracker, "_email_provider", lambda: "none")
    monkeypatch.setattr(ops_tracker, "_editor_signal", lambda: {
        "lead_theme": "drawdown-guardian",
        "lead_avg": 26.6,
        "deemph_theme": "install-mechanics",
        "top_video_title": "Textbook. Didn't Move Anything. #Shorts",
        "top_video_views": 97,
        "top_video_delta": None,
        "winning_pattern": "drawdown-guardian + account-risk + risk-control with website CTA",
    })

    md = ops_tracker.render_daily_ops_markdown(dash, state)

    assert "top clip 97 views; theme avg 26.6 views/clip" in md
    assert "Textbook. Didn't Move Anything. #Shorts" in md
    assert "drawdown-guardian + account-risk + risk-control" in md
