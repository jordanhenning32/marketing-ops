"""Tests for the Editor agent — reviews YouTube feedback, steers the next video."""
import json

import youtube_stats
import performance_feedback
from agents import editor_agent
from agents.editor_agent import EditorAgent, build_guidance, _classify

VIDEOS = [
    {"video_id": "a", "title": "The Lock. The One Feature That Keeps You Disciplined #Shorts", "views": 46},
    {"video_id": "b", "title": "Drawdown Guardian — Never Blow Another Account", "views": 18},
    {"video_id": "c", "title": "How Scale-Outs Work in Bracket Boss", "views": 1},
    {"video_id": "d", "title": "Full Install in Under 2 Minutes", "views": 4},
]
CHANNEL = {"subscribers": 3, "views": 69, "videos": 4}
TRAINING_RECORDS = [
    {
        "title": "Bracket Boss: $973 Risk Line Before the Click",
        "metrics": {"views": 140, "likes": 9, "comments": 2, "clicks": 11, "website_sessions": 7, "purchases": 1},
        "tags": {
            "hook_type": "account-risk",
            "product": "bracket-boss",
            "topic": "risk-control",
            "duration_bucket": "medium",
            "cta_type": "website",
            "opening_phrase": "Bracket Boss $973 Risk Line",
            "visual_style": "numeric-stakes",
        },
        "engagement_score": 290,
    },
    {
        "title": "Install menu walkthrough",
        "metrics": {"views": 12, "likes": 0, "comments": 0, "clicks": 0, "website_sessions": 0, "purchases": 0},
        "tags": {
            "hook_type": "mechanics",
            "product": "ninjatrader",
            "topic": "installation",
            "duration_bucket": "long",
            "cta_type": "none",
            "opening_phrase": "Install menu walkthrough",
            "visual_style": "screen-walkthrough",
        },
        "engagement_score": 12,
    },
]


def test_classify_themes():
    assert _classify("The Lock keeps you disciplined") == "drawdown-guardian"
    assert _classify("How Scale-Outs Work in Bracket Boss") == "bracket-boss"
    assert _classify("Full Install in Under 2 Minutes") == "install-mechanics"
    assert _classify("Random unrelated clip") == "other"


def test_build_guidance_picks_winner_and_flags_loser():
    g = build_guidance(VIDEOS, CHANNEL)
    assert g["lead_theme"] == "drawdown-guardian"   # 32 avg beats bracket(1)/install(4)
    assert g["confidence"] == "low"                 # 69 channel views -> small sample
    assert "Drawdown Guardian" in g["prompt_block"]
    assert g["winning_titles"][0].startswith("The Lock")
    assert g["top_videos"][0]["title"].startswith("The Lock")
    assert g["top_videos"][0]["views"] == 46
    assert "Current top clip to study" in g["prompt_block"]
    assert g["de_emphasize"] == "bracket-boss"      # lowest avg views


def test_build_guidance_includes_clip_training_patterns():
    g = build_guidance(VIDEOS, CHANNEL, training_records=TRAINING_RECORDS)

    assert g["feedback_training"]["sample"]["clips"] == 2
    assert g["performance_feedback"]["sample"]["clips"] == 2
    assert g["feedback_training"]["dimensions"]["hook_type"]["winners"][0]["value"] == "account-risk"
    assert any(row["value"] == "account-risk" for row in g["feedback_training"]["top_patterns"])
    assert "Clip-level training data" in g["prompt_block"]
    assert "Favor hook type: account risk" in g["prompt_block"]
    assert "Favor cta type: website" in g["prompt_block"]
    assert "Favor visual style: numeric stakes" in g["prompt_block"]
    assert "Best observed pattern: bracket-boss + account-risk + risk-control" in g["prompt_block"]


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(editor_agent, "GUIDANCE_PATH", tmp_path / "editor-guidance.json")
    monkeypatch.setattr(editor_agent, "BRIEF_PATH", tmp_path / "editorial-brief.md")
    monkeypatch.setattr(editor_agent, "CONTENT_DIR", tmp_path / "content")


def test_editor_run_writes_brief_and_guidance(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube_stats, "video_summary", lambda: VIDEOS)
    monkeypatch.setattr(youtube_stats, "channel_summary", lambda: CHANNEL)
    monkeypatch.setattr(performance_feedback, "collect_clip_training_records", lambda _content_dir=None: TRAINING_RECORDS)
    monkeypatch.setattr(performance_feedback, "collect_youtube_training_records", lambda _videos, _content_dir=None: [])

    res = EditorAgent().run({"date": "2026-06-17", "dry_run": True})

    assert res.status == "ok"
    assert res.outputs["lead_theme"] == "drawdown-guardian"
    assert (tmp_path / "editor-guidance.json").exists()
    assert (tmp_path / "editorial-brief.md").exists()
    g = json.loads((tmp_path / "editor-guidance.json").read_text(encoding="utf-8"))
    assert g["lead_theme"] == "drawdown-guardian"
    assert g["feedback_training"]["sample"]["clips"] == 2
    assert "account risk" in g["prompt_block"]
    assert "Clip-level training signals" in (tmp_path / "editorial-brief.md").read_text(encoding="utf-8")
    assert res.tasks  # emits at least one editorial task


def test_editor_run_no_data_is_graceful(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube_stats, "video_summary", lambda: [])
    monkeypatch.setattr(youtube_stats, "channel_summary", lambda: {})
    monkeypatch.setattr(performance_feedback, "collect_clip_training_records", lambda _content_dir=None: [])
    monkeypatch.setattr(performance_feedback, "collect_youtube_training_records", lambda _videos, _content_dir=None: [])

    res = EditorAgent().run({"date": "2026-06-17", "dry_run": True})

    assert res.status == "ok"
    assert res.blockers                                   # explains there's no data
    assert not (tmp_path / "editor-guidance.json").exists()  # no misleading guidance written
