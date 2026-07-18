import json

import performance_feedback as pf


def _write_clip(content, slug, platform, idx, metadata):
    folder = content / slug / "distribution" / platform / f"{idx:02d}"
    folder.mkdir(parents=True)
    path = folder / "metadata.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path


def test_collect_clip_training_records_normalizes_metrics_and_tags(tmp_path, monkeypatch):
    content = tmp_path / "content"
    content.mkdir()
    _write_clip(content, "run-a", "youtube", 1, {
        "platform": "youtube",
        "title": "Bracket Boss: $973 Risk Line Before the Click",
        "visual_label": "BRACKET BOSS",
        "duration_sec": 31,
        "stats": {"views": 120, "likes": 8, "comments": 2, "average_view_duration_sec": 18, "retention_pct": 62},
        "site_metrics": {"clicks": 14, "website_sessions": 9, "purchases": 2},
    })
    _write_clip(content, "run-b", "tiktok", 2, {
        "platform": "tiktok",
        "title": "Generic setup walkthrough",
        "visual_label": "SHADOW EDGE TOOLS",
        "duration_sec": 42,
        "metrics": {"views": 30, "likes": 1, "comments": 0, "link_clicks": 1},
    })

    records = pf.collect_clip_training_records(content)

    assert len(records) == 2
    winner = records[0]
    assert winner["platform"] == "youtube"
    assert winner["metrics"]["views"] == 120
    assert winner["metrics"]["website_sessions"] == 9
    assert winner["metrics"]["purchases"] == 2
    assert winner["tags"]["product"] == "bracket-boss"
    assert winner["tags"]["hook_type"] == "account-risk"
    assert winner["tags"]["topic"] == "risk-control"
    assert winner["tags"]["cta_type"] == "none"
    assert winner["engagement_score"] > records[1]["engagement_score"]


def test_collect_clip_training_records_uses_generated_short_fields(tmp_path):
    content = tmp_path / "content"
    run = content / "run-a"
    run.mkdir(parents=True)
    (run / "02-shorts-scripts.md").write_text(
        """### Short 1 - "Lock Out Before You Chase"
- **Source:** 00:01:00 -> 00:01:32 (32 seconds)
- **Hook (first 2 sec):** Once you hit lock, you cannot move the stop. That is the point.
- **On-screen text:** DRAWDOWN GUARDIAN / LOCKED / HANDS OFF
- **Outro CTA:** See how it works at shadowedgetools.com
""",
        encoding="utf-8",
    )
    _write_clip(content, "run-a", "youtube", 1, {
        "platform": "youtube",
        "title": "Uploaded title can be less specific",
        "visual_label": "DRAWDOWN GUARDIAN",
        "stats": {"views": 80, "likes": 5},
    })

    records = pf.collect_clip_training_records(content)

    assert len(records) == 1
    tags = records[0]["tags"]
    assert records[0]["title"] == "Lock Out Before You Chase"
    assert tags["hook_type"] == "discipline"
    assert tags["cta_type"] == "website"
    assert tags["visual_style"] in {"numeric-stakes", "product-label", "rule-card"}
    assert tags["hook"].startswith("Once you hit lock")
    assert "DRAWDOWN GUARDIAN" in tags["on_screen_text"]


def test_normalize_metrics_handles_platform_aliases_without_bad_overwrites():
    metrics = pf.normalize_metrics({
        "metrics": {
            "video_views": "1,200",
            "avg_view_duration": "0:21",
            "average_percentage_viewed": "68%",
            "like_count": 88,
            "comment_count": 7,
        }
    })

    assert metrics["views"] == 1200
    assert metrics["average_view_duration_sec"] == 21
    assert metrics["retention_pct"] == 68
    assert metrics["likes"] == 88
    assert metrics["comments"] == 7

    assert pf.normalize_metrics({"stats": {"views": 100}, "analytics": {"views": "N/A"}})["views"] == 100


def test_extract_clip_tags_marks_missing_duration_and_explicit_no_cta():
    tags = pf.extract_clip_tags({"title": "Bracket Boss proof", "cta": "No CTA"})

    assert tags["duration_bucket"] == "unknown"
    assert tags["cta_type"] == "none"


def test_collect_youtube_training_records_uses_summary_metrics(tmp_path):
    content = tmp_path / "content"
    run = content / "run-a"
    run.mkdir(parents=True)
    (run / "02-shorts-scripts.md").write_text(
        """### Short 2 - "The $973 Line"
- **Hook (first 2 sec):** $973 to liquidation changes the trade.
- **On-screen text:** DRAWDOWN GUARDIAN / LIVE LIQUIDATION
- **Outro CTA:** shadowedgetools.com
""",
        encoding="utf-8",
    )
    _write_clip(content, "run-a", "youtube", 2, {
        "platform": "youtube",
        "title": "The $973 Line",
        "visual_label": "DRAWDOWN GUARDIAN",
    })

    records = pf.collect_youtube_training_records([
        {
            "video_id": "abc",
            "slug": "run-a",
            "clip_index": 2,
            "title": "The $973 Line #Shorts",
            "views": 144,
            "likes": 6,
            "comments": 1,
            "delta_views": 12,
            "url": "https://www.youtube.com/shorts/abc",
        }
    ], content)

    assert len(records) == 1
    assert records[0]["metrics"]["views"] == 144
    assert records[0]["metrics"]["delta_views"] == 12
    assert records[0]["tags"]["hook_type"] == "account-risk"
    assert records[0]["tags"]["cta_type"] == "website"


def test_summarize_training_records_names_winning_patterns(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    _write_clip(content, "run-a", "youtube", 1, {
        "title": "Drawdown Guardian stops the account blowup",
        "visual_label": "DRAWDOWN GUARDIAN",
        "duration_sec": 30,
        "stats": {"views": 200, "likes": 12, "comments": 3},
        "site_metrics": {"clicks": 18, "website_sessions": 11, "purchases": 1},
    })
    _write_clip(content, "run-b", "youtube", 2, {
        "title": "Install menu walkthrough",
        "visual_label": "NINJATRADER 8",
        "duration_sec": 50,
        "stats": {"views": 20, "likes": 0, "comments": 0},
    })

    summary = pf.summarize_training_records(pf.collect_clip_training_records(content))

    assert summary["sample"]["clips"] == 2
    assert summary["winners"][0]["tags"]["product"] == "drawdown-guardian"
    assert any(row["value"] == "account-risk" for row in summary["top_patterns"])
    assert summary["dimensions"]["hook_type"]["winners"][0]["value"] == "account-risk"
    assert any(row["value"] == "mechanics" for row in summary["dimensions"]["hook_type"]["losers"])
    assert "cta_type" in summary["dimensions"]
    assert "visual_style" in summary["dimensions"]
    assert any("Favor hook type: account risk" in line for line in summary["prompt_lines"])
    assert any("Best observed pattern" in line for line in summary["prompt_lines"])
