import json

import performance_training as pt


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_fixture_kit(content):
    campaign = content / "campaign-a"
    _write_json(campaign / "00-meta.json", {"product": "bb"})
    _write_json(
        campaign / "00-pipeline-manifest.json",
        {
            "slug": "campaign-a",
            "clips": [
                {
                    "index": 1,
                    "visual_title": "See Your R:R Before You Enter",
                    "visual_label": "BRACKET BOSS",
                    "duration_sec": 33,
                    "clip": "clips/01-risk.mp4",
                    "aspect_ratio": "9:16",
                    "width": 1080,
                    "height": 1920,
                }
            ],
        },
    )
    shorts = "\n".join(
        [
            '### Short 1 \\u2014 "See Your R:R Before You Enter"',
            "- **Source:** 00:02:09 \\u2192 00:02:42 (33 seconds)",
            '- **Hook (first 2 sec):** "2.38 R:R. $438 potential. Under $200 risk. All before I am in the trade."',
            "- **On-screen text:** RR locked in \\u2192 Risk confirmed \\u2192 THEN execute",
            "- **Outro CTA:** shadowedgetools.com for the NT8 add-on",
            "",
        ]
    )
    (campaign / "02-shorts-scripts.md").write_text(shorts.encode("utf-8").decode("unicode_escape"), encoding="utf-8")
    for platform in ("youtube", "tiktok"):
        _write_json(
            campaign / "distribution" / platform / "01" / "metadata.json",
            {
                "platform": platform,
                "title": "See Your R:R Before You Enter",
                "visual_title": "See Your R:R Before You Enter",
                "visual_label": "BRACKET BOSS",
                "clip_relpath": "clips/01-risk.mp4",
                "limits": {"aspect_label": "9:16"},
                "video_id": "VID_A" if platform == "youtube" else "",
                "platform_url": "https://www.youtube.com/shorts/VID_A" if platform == "youtube" else "",
            },
        )
    return campaign


def test_build_clip_index_extracts_creative_tags(tmp_path):
    content = tmp_path / "content"
    _write_fixture_kit(content)

    index = pt.build_clip_index(content)
    record = index["campaign-a|youtube|01"]

    assert record["duration_seconds"] == 33
    assert record["hook_type"] == "number_or_metric"
    assert record["product"] == "bracket_boss"
    assert record["topic"] == "risk_planning"
    assert record["opening_phrase"].startswith("2.38 R:R")
    assert record["cta"] == "shadowedgetools.com for the NT8 add-on"
    assert "vertical_short" in record["visual_style"]
    assert "text_overlay" in record["visual_style"]


def test_build_clip_index_prefers_sidecar_title_and_caption_cta(tmp_path):
    content = tmp_path / "content"
    campaign = _write_fixture_kit(content)
    clip_dir = campaign / "distribution" / "youtube" / "01"
    metadata_path = clip_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["title"] = "Short 1"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (campaign / "02-shorts-scripts.md").write_text(
        """### Short 1 - "Short 1"
- **Hook (first 2 sec):** Watch this order before you click.
- **On-screen text:** BRACKET BOSS
""",
        encoding="utf-8",
    )
    (clip_dir / "title.txt").write_text("Sidecar Winning Title", encoding="utf-8")
    (clip_dir / "caption.txt").write_text("See the tool at shadowedgetools.com", encoding="utf-8")

    index = pt.build_clip_index(content)
    record = index["campaign-a|youtube|01"]

    assert record["title"] == "Sidecar Winning Title"
    assert record["caption"] == "See the tool at shadowedgetools.com"
    assert record["cta"] == "See the tool at shadowedgetools.com"


def test_normalize_metric_record_accepts_aliases():
    row = {
        "platform": "tiktok",
        "campaign_slug": "campaign-a",
        "clip_index": "01",
        "video_views": "1,200",
        "avg_view_duration": "0:21",
        "average_percentage_viewed": "68%",
        "like_count": "88",
        "comment_count": 7,
        "link_clicks": "12",
        "sessions": "9",
        "orders": "2",
    }

    record = pt.normalize_metric_record(row, source="local.json")

    assert record["metric_platform"] == "tiktok"
    assert record["metrics"]["views"] == 1200
    assert record["metrics"]["average_view_duration_seconds"] == 21
    assert record["metrics"]["retention_rate"] == 0.68
    assert record["metrics"]["likes"] == 88
    assert record["metrics"]["comments"] == 7
    assert record["metrics"]["clicks"] == 12
    assert record["metrics"]["website_sessions"] == 9
    assert record["metrics"]["purchases"] == 2


def test_normalize_metric_record_expands_compact_counts():
    record = pt.normalize_metric_record({
        "platform": "tiktok",
        "campaign_slug": "campaign-a",
        "clip_index": 1,
        "play_count": "1.2K",
    })

    assert record["metrics"]["views"] == 1200


def test_classify_product_detects_both_products():
    assert pt.classify_product("Bracket Boss plus Drawdown Guardian") == "both"


def test_build_training_records_joins_platform_and_site_metrics(tmp_path):
    content = tmp_path / "content"
    _write_fixture_kit(content)
    youtube_stats = tmp_path / "youtube-video-stats.jsonl"
    youtube_stats.write_text(
        json.dumps(
            {
                "pulled_at": "2026-06-17T06:30:35",
                "video_id": "VID_A",
                "platform": "youtube",
                "title": "See Your R:R Before You Enter",
                "views": 50,
                "likes": 5,
                "comments": 1,
                "average_view_duration": "0:22",
                "retention": "67%",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    site_metrics = tmp_path / "site-metrics.json"
    _write_json(
        site_metrics,
        {
            "records": [
                {
                    "platform": "site",
                    "source_platform": "youtube",
                    "campaign_slug": "campaign-a",
                    "clip_index": 1,
                    "date": "2026-06-17",
                    "clicks": 4,
                    "website_sessions": 3,
                    "purchases": 1,
                }
            ]
        },
    )

    records = pt.build_training_records(content_dir=content, metric_paths=[youtube_stats, site_metrics])

    assert len(records) == 2
    youtube = next(r for r in records if r["metric_platform"] == "youtube")
    site = next(r for r in records if r["metric_platform"] == "site")
    assert youtube["platform"] == "youtube"
    assert youtube["metrics"]["views"] == 50
    assert youtube["derived"]["engagements"] == 6
    assert youtube["derived"]["average_view_ratio"] == round(22 / 33, 6)
    assert site["platform"] == "youtube"
    assert site["metrics"]["website_sessions"] == 3
    assert site["derived"]["purchase_rate"] == round(1 / 3, 6)


def test_metadata_metrics_and_site_metrics_are_training_input(tmp_path):
    content = tmp_path / "content"
    campaign = _write_fixture_kit(content)
    metadata_path = campaign / "distribution" / "tiktok" / "01" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["metrics"] = {"measured_at": "2026-06-17", "views": 77, "likes": 4}
    metadata["site_metrics"] = {"measured_at": "2026-06-17", "clicks": 5, "website_sessions": 3}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    records = pt.build_training_records(content_dir=content, metric_paths=[])

    tiktok = next(r for r in records if r["metric_platform"] == "tiktok")
    site = next(r for r in records if r["metric_platform"] == "site")
    assert tiktok["metrics"]["views"] == 77
    assert site["metrics"]["clicks"] == 5
    assert site["platform"] == "tiktok"


def test_metadata_stats_are_training_input(tmp_path):
    content = tmp_path / "content"
    campaign = _write_fixture_kit(content)
    metadata_path = campaign / "distribution" / "youtube" / "01" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["stats"] = {
        "pulled_at": "2026-06-17T08:00:00",
        "views": 9,
        "likes": 1,
        "comments": 0,
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    records = pt.build_training_records(content_dir=content, metric_paths=[])

    assert len(records) == 1
    assert records[0]["metric_platform"] == "youtube"
    assert records[0]["metrics"]["views"] == 9
    assert records[0]["product"] == "bracket_boss"
