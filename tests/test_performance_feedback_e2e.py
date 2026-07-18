import csv
import json
import sys
from types import ModuleType, SimpleNamespace

import analytics
import content_multiplier as cm
import leads
import performance_feedback as pf
import youtube_stats
from agents import editor_agent
from agents.editor_agent import EditorAgent


def _write_distribution_kit(content, slug, platform, idx, metadata, caption):
    folder = content / slug / "distribution" / platform / f"{idx:02d}"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / "caption.txt").write_text(caption, encoding="utf-8")
    (folder / "title.txt").write_text(metadata.get("title", ""), encoding="utf-8")
    return folder


def _isolate(tmp_path, monkeypatch):
    state = tmp_path / "state"
    content = tmp_path / "content"
    config = tmp_path / "config"
    state.mkdir()
    content.mkdir()
    config.mkdir()

    monkeypatch.setattr(analytics, "STATE_DIR", state)
    monkeypatch.setattr(analytics, "PERFORMANCE_PATH", state / "performance.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(analytics, "PUBLISH_LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(leads, "LEADS_PATH", state / "leads.jsonl")
    monkeypatch.setattr(leads, "EMAIL_EVENTS_PATH", state / "email-events.jsonl")

    monkeypatch.setattr(editor_agent, "CONTENT_DIR", content)
    monkeypatch.setattr(editor_agent, "GUIDANCE_PATH", state / "editor-guidance.json")
    monkeypatch.setattr(editor_agent, "BRIEF_PATH", state / "editorial-brief.md")

    monkeypatch.setattr(cm, "CONTENT_DIR", content)
    monkeypatch.setattr(cm, "CONFIG_DIR", config)
    monkeypatch.setattr(cm, "STATE_DIR", state)
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", state / "editor-guidance.json")
    (config / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (config / "guardrails.md").write_text("tools and education only", encoding="utf-8")

    return state, content, config


def _import_local_metrics(csv_path, campaign_id):
    rows = [
        {
            "date": "2026-06-17",
            "campaign_id": campaign_id,
            "metric": "youtube_watch_metrics",
            "value": "views=260 likes=18 comments=5",
            "source": "local_youtube_export",
            "status": "measured",
        },
        {
            "date": "2026-06-17",
            "campaign_id": campaign_id,
            "metric": "tiktok_engagement",
            "value": "views=640 likes=55 shares=12",
            "source": "local_tiktok_export",
            "status": "measured",
        },
        {
            "date": "2026-06-17",
            "campaign_id": campaign_id,
            "metric": "website_sessions_by_source",
            "value": "youtube:11;tiktok:34",
            "source": "local_site_export",
            "status": "measured",
        },
        {
            "date": "2026-06-17",
            "campaign_id": campaign_id,
            "metric": "landing_page_clicks",
            "value": "45",
            "source": "local_site_export",
            "status": "measured",
        },
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "campaign_id", "metric", "value", "source", "status"])
        writer.writeheader()
        writer.writerows(rows)
    return analytics.import_csv(csv_path)


def _install_fake_anthropic(monkeypatch, captured):
    raw = """<<<<FILE: 01-x-thread.md>>>>
Thread

<<<<FILE: 02-shorts-scripts.md>>>>
Shorts

<<<<FILE: 03-blog-post.md>>>>
Blog

<<<<FILE: 04-email.md>>>>
Email

<<<<FILE: 05-reddit-post.md>>>>
Reddit

<<<<FILE: 06-linkedin-post.md>>>>
LinkedIn
"""

    class FakeMessages:
        def create(self, **kwargs):
            captured["user_prompt"] = kwargs["messages"][0]["content"]
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=raw)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            )

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_performance_feedback_training_data_e2e(tmp_path, monkeypatch):
    _state, content, _config = _isolate(tmp_path, monkeypatch)
    slug = "e2e-performance-feedback"

    imported = _import_local_metrics(tmp_path / "metrics.csv", slug)
    report = analytics.report("2026-06-17", campaign_id=slug)

    assert imported == 4
    assert report["measured"]["youtube_watch_metrics"] == "views=260 likes=18 comments=5"
    assert report["measured"]["tiktok_engagement"] == "views=640 likes=55 shares=12"
    assert report["measured"]["website_sessions_by_source"] == "youtube:11;tiktok:34"
    assert report["measured"]["landing_page_clicks"] == "45"

    _write_distribution_kit(
        content,
        slug,
        "youtube",
        1,
        {
            "platform": "youtube",
            "title": "$973 liquidation line before the bad click",
            "visual_label": "DRAWDOWN GUARDIAN",
            "duration_sec": 32,
            "stats": {"views": 260, "likes": 18, "comments": 5, "retention_pct": 71},
            "site_metrics": {"clicks": 22, "website_sessions": 16, "purchases": 1},
        },
        "The account-risk moment: discipline score rose, lockout held, and the CTA points to shadowedgetools.com",
    )
    _write_distribution_kit(
        content,
        slug,
        "tiktok",
        1,
        {
            "platform": "tiktok",
            "title": "$973 liquidation line before the bad click",
            "visual_label": "DRAWDOWN GUARDIAN",
            "duration_sec": 32,
            "metrics": {"views": 640, "likes": 55, "comments": 9, "link_clicks": 23},
            "site_metrics": {"website_sessions": 34},
        },
        "Same hook repurposed for TikTok. Viewers clicked through to shadowedgetools.com",
    )
    _write_distribution_kit(
        content,
        slug,
        "youtube",
        2,
        {
            "platform": "youtube",
            "title": "NinjaTrader install menu walkthrough",
            "visual_label": "PLATFORM SETUP",
            "duration_sec": 52,
            "stats": {"views": 18, "likes": 0, "comments": 0},
        },
        "A setup-only walkthrough with no CTA.",
    )

    training_records = pf.collect_clip_training_records(content)
    assert {row["platform"] for row in training_records} == {"youtube", "tiktok"}
    assert training_records[0]["tags"]["hook_type"] == "account-risk"
    assert training_records[0]["tags"]["cta_type"] == "website"
    assert training_records[-1]["tags"]["hook_type"] == "mechanics"

    monkeypatch.setattr(youtube_stats, "video_summary", lambda: [
        {
            "video_id": "yt-winner",
            "title": "$973 liquidation lockout kept the trade disciplined",
            "views": 260,
            "slug": slug,
            "clip_index": 1,
            "tracked": True,
        },
        {
            "video_id": "yt-loser",
            "title": "NinjaTrader install menu walkthrough",
            "views": 18,
            "slug": slug,
            "clip_index": 2,
            "tracked": True,
        },
    ])
    monkeypatch.setattr(youtube_stats, "channel_summary", lambda: {"subscribers": 4, "views": 278, "videos": 2})

    result = EditorAgent().run({"date": "2026-06-17", "campaign_id": slug, "dry_run": True})
    guidance_path = editor_agent.GUIDANCE_PATH
    guidance = json.loads(guidance_path.read_text(encoding="utf-8"))

    assert result.status == "ok"
    assert guidance["lead_theme"] == "drawdown-guardian"
    assert guidance["feedback_training"]["sample"]["clips"] == 3
    assert guidance["feedback_training"]["winners"][0]["platform"] == "tiktok"
    assert guidance["feedback_training"]["losers"][0]["tags"]["hook_type"] == "mechanics"
    assert "Clip-level training data from YouTube/TikTok/site metrics" in guidance["prompt_block"]
    assert "Favor hook type: account risk" in guidance["prompt_block"]
    assert "De-emphasize: mechanics" in guidance["prompt_block"]
    assert "Favor product: drawdown guardian" in guidance["prompt_block"]
    assert "De-emphasize: ninjatrader" in guidance["prompt_block"]
    assert "Favor cta type: website" in guidance["prompt_block"]
    assert "De-emphasize: none" in guidance["prompt_block"]
    assert "Favor visual style: numeric stakes" in guidance["prompt_block"]
    assert "De-emphasize: screen walkthrough" in guidance["prompt_block"]

    captured = {}
    _install_fake_anthropic(monkeypatch, captured)
    transcript = tmp_path / "future.txt"
    transcript.write_text("This next recording has a live risk-control moment.", encoding="utf-8")

    generated = cm.process_transcript(transcript, slug="future-shorts", editor_guidance=None)

    assert generated.error is None
    assert "Clip-level training data from YouTube/TikTok/site metrics" in captured["user_prompt"]
    assert "Favor hook type: account risk" in captured["user_prompt"]
    assert "De-emphasize: mechanics" in captured["user_prompt"]
    assert "Favor product: drawdown guardian" in captured["user_prompt"]
    assert "De-emphasize: ninjatrader" in captured["user_prompt"]
    assert "Favor cta type: website" in captured["user_prompt"]
    assert "De-emphasize: none" in captured["user_prompt"]
    assert "Favor visual style: numeric stakes" in captured["user_prompt"]
    assert "De-emphasize: screen walkthrough" in captured["user_prompt"]
    meta = json.loads((generated.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["editor_guidance_applied"] is True
