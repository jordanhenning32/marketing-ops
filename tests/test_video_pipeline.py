import gc
import json
import tracemalloc
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import video_pipeline


def _load_generated_json(root):
    json_paths = sorted(root.rglob("*.json"))
    assert json_paths
    return {
        path.relative_to(root).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in json_paths
    }


def _fake_rendered_qa_report(inputs, *, captions_burned=True):
    clips = [
        {
            "index": item["index"],
            "title": item.get("visual_title") or item.get("title", ""),
            "clip": str(item["clip_path"]),
            "score": 100,
            "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
            "status": "pass",
            "categories": {
                name: {"score": max_score, "max": max_score, "passed": True, "notes": []}
                for name, max_score in video_pipeline.RENDERED_QA_WEIGHTS.items()
            },
            "metrics": {"caption_cues": len(item.get("caption_cues") or [])},
            "reasons": [],
        }
        for item in inputs
    ]
    return {
        "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
        "passed": True,
        "manual_review_required": False,
        "approved_count": len(clips),
        "manual_review_count": 0,
        "failures": [],
        "clips": clips,
    }


def _assert_checklist_utm(url, *, source, campaign, content):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "shadowedgetools.com"
    assert parsed.path == "/checklist"
    assert query == {
        "utm_source": [source],
        "utm_medium": ["video"],
        "utm_campaign": [campaign],
        "utm_content": [content],
    }
    assert url.count("?") == 1
    assert "&&" not in url
    assert not url.endswith("&")


def _assert_release_ready_video_artifacts(out_dir, manifest, generated_json):
    commercial_qa = generated_json["00-commercial-qa.json"]
    rendered_qa = generated_json["00-rendered-qa.json"]
    clips = manifest["clips"]

    assert manifest["commercial_qa"] == commercial_qa
    assert manifest["rendered_qa"] == rendered_qa
    assert manifest["shorts_quality"]["quality_gate_status"] == "pass"
    assert manifest["platforms_prepared"] == video_pipeline.PLATFORMS
    assert len(commercial_qa["clips"]) == len(clips)
    assert len(rendered_qa["clips"]) == len(clips)

    long_form = manifest.get("long_form") or {}
    long_form_json_count = len((long_form.get("distribution") or {}))
    expected_json_count = 3 + (len(video_pipeline.PLATFORMS) * len(clips)) + long_form_json_count
    assert len(generated_json) == expected_json_count

    commercial_by_index = {item["index"]: item for item in commercial_qa["clips"]}
    rendered_by_index = {item["index"]: item for item in rendered_qa["clips"]}

    for clip in clips:
        clip_index = clip["index"]
        clip_path = out_dir / clip["clip"]
        assert clip_path.exists()
        assert clip["commercial_qa"] == commercial_by_index[clip_index]
        assert clip["rendered_qa"] == rendered_by_index[clip_index]
        assert clip["commercial_qa"]["status"] == "pass"
        assert clip["rendered_qa"]["status"] == "pass"
        assert set(clip["distribution"]) == set(video_pipeline.PLATFORMS)

        for platform in video_pipeline.PLATFORMS:
            kit_rel = clip["distribution"][platform]
            kit_dir = out_dir / kit_rel
            title_path = kit_dir / "title.txt"
            caption_path = kit_dir / "caption.txt"
            source_clip_path = kit_dir / "source-clip.txt"
            metadata_path = kit_dir / "metadata.json"

            assert kit_dir.is_dir()
            assert title_path.exists()
            assert caption_path.exists()
            assert source_clip_path.exists()
            assert metadata_path.exists()

            metadata = generated_json[f"{kit_rel}/metadata.json"]
            title = title_path.read_text(encoding="utf-8").strip()
            caption = caption_path.read_text(encoding="utf-8").strip()
            source_clip = source_clip_path.read_text(encoding="utf-8").strip()

            assert title
            assert caption
            assert metadata["platform"] == platform
            assert metadata["title"] == title
            assert metadata["clip_relpath"] == clip["clip"]
            assert (out_dir / metadata["clip_relpath"]).resolve() == clip_path.resolve()
            assert source_clip == str(clip_path.resolve())
            assert metadata["limits"] == video_pipeline.PLATFORM_LIMITS.get(platform, {})
            assert metadata["uploaded_at"] is None
            assert metadata["platform_url"] is None
            _assert_checklist_utm(
                metadata["website_url"],
                source=platform,
                campaign=out_dir.name,
                content=f"short-{clip_index:02d}",
            )
            assert "/?utm_" not in caption

            if platform == "youtube":
                assert "Tools and education only. Not financial advice." in caption
                assert "Free risk-control checklist: https://shadowedgetools.com/checklist" in caption
                assert metadata["website_url"].startswith("https://shadowedgetools.com/")
                assert "thumbnail_relpath" in metadata
                assert (out_dir / metadata["thumbnail_relpath"]).exists()
            else:
                assert "Free risk-control checklist: https://shadowedgetools.com/checklist" in caption
                assert metadata["website_url"].startswith("https://shadowedgetools.com/")
                assert "thumbnail_relpath" not in metadata
    if long_form.get("enabled"):
        youtube_rel = long_form["distribution"]["youtube"]
        long_meta = generated_json[f"{youtube_rel}/metadata.json"]
        long_caption = (out_dir / youtube_rel / "caption.txt").read_text(encoding="utf-8")
        assert long_meta["content_type"] == "long_form"
        _assert_checklist_utm(
            long_meta["website_url"],
            source="youtube",
            campaign=out_dir.name,
            content="long-form",
        )
        assert "utm_content=long-form" in long_meta["website_url"]
        assert long_caption.startswith("Free risk-control checklist: https://shadowedgetools.com/checklist")
        assert "/?utm_" not in long_caption
        assert "Full Shadow Edge Tools walkthrough" in long_caption


def test_overlay_title_split_matches_branded_two_line_style():
    title = video_pipeline._overlay_title_from_short('Short 1 - "Never Blow Another Account"')

    assert title == "Never Blow Another Account"
    assert video_pipeline._split_overlay_title(title) == ("NEVER BLOW", "ANOTHER ACCOUNT")


def test_overlay_label_detects_product_context():
    assert video_pipeline._overlay_label("Adding Drawdown Guardian", "") == "DRAWDOWN GUARDIAN"
    assert video_pipeline._overlay_label("Always Add First", "Bracket Boss goes on first") == "BRACKET BOSS"
    assert video_pipeline._overlay_label("Import option", "NinjaTrader 8 control panel") == "NINJATRADER 8"


def test_title_overlay_filter_escapes_ffmpeg_enable_commas():
    flt = video_pipeline._title_overlay_filter("Why Your Break-Even Stop Isn't at Zero")

    assert "between(t\\,0\\,4.25)" in flt
    assert "between(t,0,4.25)" not in flt
    assert "ISNT" in flt
    assert "ISN'T" not in flt


def test_vertical_shorts_filter_outputs_1080x1920_graph():
    flt = video_pipeline._vertical_shorts_filter("Never Blow Another Account")

    assert "scale=1080:1920:force_original_aspect_ratio=increase" in flt
    assert "crop=1080:1920" in flt
    assert "scale=1080:-2" in flt
    assert flt.endswith(",format=yuv420p[vout]")


def test_youtube_description_is_public_facing_not_internal_notes():
    short = video_pipeline.ShortRange(
        title='Short 1 - "Full Install in Under 2 Minutes"',
        start_sec=1,
        end_sec=107,
        body_md="\n".join([
            "- **Source:** 00:00:01 -> 00:01:47 (106 seconds)",
            '- **Hook (first 2 sec):** "Here is the exact install sequence."',
            '- **On-screen text:** "Control Panel -> Tools -> Import"',
            "- **Outro CTA:** Full guide at shadowedgetools.com",
        ]),
    )

    description = video_pipeline._youtube_description(
        short,
        "Full Install in Under 2 Minutes",
        website_url="https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=test&utm_content=short-01",
    )

    assert description.startswith("Free risk-control checklist: https://shadowedgetools.com/checklist")
    assert "Full Install in Under 2 Minutes" in description
    assert "In this clip:" in description
    assert "Here is the exact install sequence." in description
    assert "Control Panel -> Tools -> Import" in description
    assert "Tools and education only. Not financial advice." in description
    assert "utm_content=short-01" in description
    assert "#Shorts #NinjaTrader" in description
    assert "Source:" not in description
    assert "**" not in description


def test_distribution_kit_ctas_are_checklist_first_and_idempotent(tmp_path, monkeypatch):
    out_dir = tmp_path / "run"
    clip_path = out_dir / "clips" / "clip.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")
    campaign_id = "Launch Discipline Score"
    short = video_pipeline.ShortRange(
        title='Short 1 - "Do Not Move That Stop"',
        start_sec=0,
        end_sec=35,
        body_md="- **Hook:** The rule has to live inside the platform.",
    )

    monkeypatch.setattr(video_pipeline, "write_thumbnail", lambda _clip, path, **_kwargs: path.write_bytes(b"jpg"))

    first = video_pipeline.write_distribution_kit(out_dir, short, clip_path, short_index=1, campaign_id=campaign_id)
    second = video_pipeline.write_distribution_kit(out_dir, short, clip_path, short_index=1, campaign_id=campaign_id)

    assert first == second
    for platform, rel in second.items():
        kit_dir = out_dir / rel
        caption = (kit_dir / "caption.txt").read_text(encoding="utf-8")
        metadata = json.loads((kit_dir / "metadata.json").read_text(encoding="utf-8"))

        assert caption.count("Free risk-control checklist:") == 1
        assert "/?utm_" not in caption
        _assert_checklist_utm(
            metadata["website_url"],
            source=platform,
            campaign=campaign_id,
            content="short-01",
        )
        assert metadata["website_cta"] == f"Free risk-control checklist: {metadata['website_url']}"
        assert metadata["website_url"] in caption


def test_should_prepare_long_form_uses_strict_shorts_boundary():
    limit = video_pipeline.LONG_FORM_MIN_SECONDS

    assert video_pipeline.should_prepare_long_form(limit - 0.1) is False
    assert video_pipeline.should_prepare_long_form(limit) is False
    assert video_pipeline.should_prepare_long_form(limit + 0.1) is True


def test_long_form_youtube_kit_uses_checklist_cta(tmp_path, monkeypatch):
    out_dir = tmp_path / "run"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"video")
    monkeypatch.setattr(video_pipeline, "write_thumbnail", lambda _clip, path, **_kwargs: path.write_bytes(b"jpg"))

    info = video_pipeline.write_long_form_youtube_kit(
        out_dir,
        source_video,
        slug="discipline-score-walkthrough",
        product="both",
        duration_sec=video_pipeline.LONG_FORM_MIN_SECONDS + 60,
        shorts_count=3,
        campaign_id="Full Video Campaign",
    )

    kit_dir = out_dir / info["distribution"]["youtube"]
    caption = (kit_dir / "caption.txt").read_text(encoding="utf-8")
    metadata = json.loads((kit_dir / "metadata.json").read_text(encoding="utf-8"))

    assert info["enabled"] is True
    assert metadata["content_type"] == "long_form"
    assert caption.startswith("Free risk-control checklist: https://shadowedgetools.com/checklist")
    assert "#Shorts" not in caption
    assert "/?utm_" not in caption
    _assert_checklist_utm(
        metadata["website_url"],
        source="youtube",
        campaign="Full Video Campaign",
        content="long-form",
    )
    assert metadata["website_url"] in caption


def test_youtube_title_for_short_prefers_catchy_risk_hook():
    short = video_pipeline.ShortRange(
        title='Short 1 - "Generic Clip"',
        start_sec=0,
        end_sec=30,
        body_md="\n".join([
            '- **Hook (first 2 sec):** "$973 left until liquidation, then I walked away."',
            "- **Source quote:** Drawdown Guardian shows the liquidation distance before the trade.",
            "- **On-screen text:** $973 UNTIL LIQUIDATION",
        ]),
    )

    assert video_pipeline._youtube_title_for_short(short) == "$973 Until Liquidation. Walk Away."


def test_write_thumbnail_uses_designed_short_card_filter(tmp_path, monkeypatch):
    clip_path = tmp_path / "clip.mp4"
    thumb_path = tmp_path / "thumbnail.jpg"
    clip_path.write_bytes(b"fake video")
    captured = {}

    def fake_run_ffmpeg(args, **_kwargs):
        captured["args"] = args
        thumb_path.write_bytes(b"jpg")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(video_pipeline, "_run_ffmpeg", fake_run_ffmpeg)

    video_pipeline.write_thumbnail(
        clip_path,
        thumb_path,
        title="The Lock That Stops Revenge Trades",
        body_md="Drawdown Guardian and Bracket Boss protect the account.",
    )

    args = captured["args"]
    assert "-filter_complex" in args
    filter_arg = args[args.index("-filter_complex") + 1]
    assert "pad=1280:720" not in filter_arg
    assert "SHADOW EDGE" in filter_arg
    assert "WATCH BEFORE YOU TRADE" in filter_arg
    assert "DRAWDOWN GUARDIAN" in filter_arg
    assert "THE LOCK THAT" in filter_arg
    assert thumb_path.exists()


def test_youtube_kit_metadata_includes_thumbnail(tmp_path):
    out_dir = tmp_path / "run"
    clip_path = out_dir / "clips" / "clip.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")
    short = video_pipeline.ShortRange(
        title='Short 1 - "Never Blow Another Account"',
        start_sec=0,
        end_sec=5,
        body_md="- **Hook:** Professional clip",
    )
    captured = {}

    def fake_thumbnail(_clip_path, thumbnail_path, **kwargs):
        captured["kwargs"] = kwargs
        thumbnail_path.write_bytes(b"jpg")

    original = video_pipeline.write_thumbnail
    video_pipeline.write_thumbnail = fake_thumbnail
    try:
        video_pipeline.write_distribution_kit(out_dir, short, clip_path, short_index=1)
    finally:
        video_pipeline.write_thumbnail = original

    assert (out_dir / "distribution" / "youtube" / "01" / "thumbnail.jpg").exists()
    metadata = __import__("json").loads((out_dir / "distribution" / "youtube" / "01" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["thumbnail_relpath"] == "distribution/youtube/01/thumbnail.jpg"
    assert metadata["thumbnail_style"] == "designed_short_card"
    assert metadata["thumbnail_title"] == "Never Blow Another Account"
    assert metadata["website_url"].startswith("https://shadowedgetools.com/")
    assert "utm_content=short-01" in metadata["website_url"]
    assert "Free risk-control checklist: https://shadowedgetools.com/checklist" in (out_dir / "distribution" / "youtube" / "01" / "caption.txt").read_text(encoding="utf-8")
    assert captured["kwargs"]["title"] == "Never Blow Another Account"
    assert captured["kwargs"]["body_md"] == "- **Hook:** Professional clip"


def test_youtube_thumbnail_failure_does_not_abort_distribution_kit(tmp_path):
    out_dir = tmp_path / "run"
    clip_path = out_dir / "clips" / "clip.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")
    short = video_pipeline.ShortRange(
        title='Short 1 - "Never Blow Another Account"',
        start_sec=0,
        end_sec=30,
        body_md="- **Hook:** Professional clip",
    )

    def broken_thumbnail(_clip_path, _thumbnail_path, **_kwargs):
        raise RuntimeError("ffmpeg thumbnail failed")

    original = video_pipeline.write_thumbnail
    video_pipeline.write_thumbnail = broken_thumbnail
    try:
        video_pipeline.write_distribution_kit(out_dir, short, clip_path, short_index=1)
    finally:
        video_pipeline.write_thumbnail = original

    metadata = __import__("json").loads((out_dir / "distribution" / "youtube" / "01" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["thumbnail_error"] == "ffmpeg thumbnail failed"
    assert "thumbnail_relpath" not in metadata


def test_parse_shorts_ranges_accepts_common_source_timestamp_styles():
    md = """
### Short 1 - "Break Even Discipline"
- **Source:** [00:10] -> [00:40]
- **Hook:** Move break even only after the trade proves it.

### Short 2 - "Bracket Boss Maps the Trade"
- **Source:** 00:01:20 to 00:01:55
- **Hook:** Bracket Boss maps stop target and entry before emotion hits.

### Short 3 - "Risk Gets Real"
- **Source:** 00:02:00 — 00:02:30
- **Hook:** The liquidation line makes the risk obvious.
"""

    ranges = video_pipeline.parse_shorts_ranges(md)

    assert [(s.start_sec, s.end_sec) for s in ranges] == [(10, 40), (80, 115), (120, 150)]


def test_parse_shorts_ranges_rejects_invalid_or_reversed_timestamps():
    md = """
### Short 1 - "Bad Clock"
- **Source:** 00:99 -> 01:10

### Short 2 - "Backwards"
- **Source:** 00:02:00 -> 00:01:30
"""

    assert video_pipeline.parse_shorts_ranges(md) == []


def test_repair_short_ranges_to_duration_clamps_markdown_source():
    md = """
### Short 1 - "The Lock"
- **Score:** 8/10
- **Source:** 00:00:50 -> 00:01:31 (41 seconds)
- **Hook:** It can flatten before you breach it.

### Short 2 - "Inside The Plan"
- **Score:** 8/10
- **Source:** 00:00:10 -> 00:00:40 (30 seconds)
- **Hook:** Follow the plan before pressure hits.
"""
    shorts = video_pipeline.parse_shorts_ranges(md)

    repaired, meta = video_pipeline.repair_short_ranges_to_duration(shorts, 85.6)

    assert meta["attempted"] is True
    assert meta["clamped_indexes"] == [1]
    assert meta["dropped_indexes"] == []
    assert repaired[0].end_sec == 85.0
    assert "00:00:50 -> 00:01:25 (35 seconds)" in repaired[0].body_md
    assert "00:01:31" not in repaired[0].body_md
    assert repaired[1].end_sec == 40


def test_env_flag_treats_falsey_words_as_disabled(monkeypatch):
    for value in ("0", "false", "False", "no", "off"):
        monkeypatch.setenv("SHORTS_QUALITY_GATE", value)
        assert video_pipeline._env_flag("SHORTS_QUALITY_GATE", True) is False

    monkeypatch.delenv("SHORTS_QUALITY_GATE", raising=False)
    assert video_pipeline._env_flag("SHORTS_QUALITY_GATE", True) is True


def _synthesize_source(path, seconds=8):
    return video_pipeline._run_ffmpeg([
        "-y",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ])


def test_rendered_qa_passes_clean_vertical_render(tmp_path):
    try:
        video_pipeline.ffmpeg_exe()
    except Exception:  # pragma: no cover
        pytest.skip("ffmpeg binary not available")

    src = tmp_path / "src.mp4"
    res = _synthesize_source(src, 8)
    if res.returncode != 0 or not src.exists() or src.stat().st_size == 0:
        pytest.skip(f"could not synthesize source clip: {(res.stderr or '')[:200]}")

    clip = tmp_path / "clip.mp4"
    cues = [
        video_pipeline.CaptionCue(4.5, 5.4, "risk rule changes"),
        video_pipeline.CaptionCue(5.4, 6.0, "watch the stop"),
    ]
    video_pipeline.cut_clip(
        src,
        0,
        6,
        clip,
        title_overlay="Risk Rule Changes",
        title_body_md="- **Outro CTA:** See the full workflow at shadowedgetools.com",
        caption_cues=cues,
    )

    qa = video_pipeline.rendered_clip_qa(
        clip,
        index=1,
        title="Risk Rule Changes",
        visual_label="BRACKET BOSS",
        cta="See the full workflow at shadowedgetools.com",
        caption_cues=cues,
        captions_burned=True,
    )

    assert qa["status"] == "pass"
    assert qa["score"] >= video_pipeline.RENDERED_QA_THRESHOLD
    assert qa["metrics"]["width"] == video_pipeline.SHORTS_WIDTH
    assert qa["metrics"]["height"] == video_pipeline.SHORTS_HEIGHT
    assert qa["categories"]["audio"]["passed"] is True
    assert qa["categories"]["captions"]["passed"] is True


def test_rendered_qa_flags_black_silent_square_render(tmp_path):
    try:
        video_pipeline.ffmpeg_exe()
    except Exception:  # pragma: no cover
        pytest.skip("ffmpeg binary not available")

    clip = tmp_path / "bad.mp4"
    res = video_pipeline._run_ffmpeg([
        "-y",
        "-f", "lavfi", "-i", "color=c=black:size=640x640:rate=30:duration=2",
        "-f", "lavfi", "-i", "aevalsrc=0:d=2",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(clip),
    ])
    if res.returncode != 0 or not clip.exists() or clip.stat().st_size == 0:
        pytest.skip(f"could not synthesize bad clip: {(res.stderr or '')[:200]}")

    qa = video_pipeline.rendered_clip_qa(
        clip,
        index=1,
        title="",
        visual_label="",
        cta="",
        caption_cues=[],
        captions_burned=True,
    )

    assert qa["status"] == "manual_review"
    assert qa["score"] < video_pipeline.RENDERED_QA_THRESHOLD
    assert qa["categories"]["crop"]["passed"] is False
    assert qa["categories"]["black_frames"]["passed"] is False
    assert qa["categories"]["audio"]["passed"] is False
    assert qa["categories"]["captions"]["passed"] is False


def test_process_video_job_real_render_e2e_writes_distribution_kits(tmp_path, monkeypatch):
    try:
        video_pipeline.ffmpeg_exe()
    except Exception:  # pragma: no cover
        pytest.skip("ffmpeg binary not available")

    video_path = tmp_path / "source.mp4"
    res = _synthesize_source(video_path, 72)
    if res.returncode != 0 or not video_path.exists() or video_path.stat().st_size == 0:
        pytest.skip(f"could not synthesize source clip: {(res.stderr or '')[:200]}")

    content_dir = tmp_path / "content"
    out_dir = content_dir / "real-render-e2e-output"
    starts = [0, 17, 34, 51]

    def short_block(idx, start):
        return f"""### Short {idx} - "Risk Decision {idx} With Bracket Boss"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 15)} (15 seconds)
- **Hook (first 2 sec):** "Risk decision {idx} changes before the click."
- **Why this clip works:** Bracket Boss makes the stop target and risk decision visible before entry.
- **Source quote:** Bracket Boss maps the stop target before risk decision {idx}.
- **On-screen text:** RISK DECISION {idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""

    shorts_md = "\n\n---\n\n".join(
        short_block(idx, start)
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 15,
            text=f"Bracket Boss maps the stop target before risk decision {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]
    job_updates = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        video_pipeline.jobs,
        "update_job",
        lambda _job_id, **kwargs: job_updates.append(kwargs),
    )

    video_pipeline.process_video_job(
        "job-real-render",
        video_path,
        slug="real-render-e2e",
        transcribe_provider="local",
        burn_captions=True,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    rendered_qa = json.loads((out_dir / "00-rendered-qa.json").read_text(encoding="utf-8"))
    commercial_qa = json.loads((out_dir / "00-commercial-qa.json").read_text(encoding="utf-8"))

    assert job_updates[-1]["state"] == "done"
    assert manifest["captions_burned"] is True
    assert manifest["shorts_quality"]["quality_gate_status"] == "pass"
    assert manifest["shorts_quality"]["quality_failures"] == []
    assert manifest["commercial_qa"] == commercial_qa
    assert manifest["rendered_qa"] == rendered_qa
    assert rendered_qa["passed"] is True
    assert rendered_qa["approved_count"] == 4
    assert len(manifest["clips"]) == 4
    assert all(clip["caption_cues"] > 0 for clip in manifest["clips"])
    assert all(clip["rendered_qa"]["score"] >= video_pipeline.RENDERED_QA_THRESHOLD for clip in manifest["clips"])

    for clip in manifest["clips"]:
        clip_path = out_dir / clip["clip"]
        assert clip_path.exists() and clip_path.stat().st_size > 0
        for platform in video_pipeline.PLATFORMS:
            kit_dir = out_dir / clip["distribution"][platform]
            assert (kit_dir / "caption.txt").exists()
            assert (kit_dir / "title.txt").exists()
            assert (kit_dir / "source-clip.txt").read_text(encoding="utf-8").strip() == str(clip_path.resolve())
            metadata = json.loads((kit_dir / "metadata.json").read_text(encoding="utf-8"))
            assert metadata["clip_relpath"] == clip["clip"]
            assert metadata["visual_label"] == "BRACKET BOSS"
        assert (out_dir / clip["distribution"]["youtube"] / "thumbnail.jpg").exists()


def test_process_video_job_mocked_e2e_requires_high_quality_shorts(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-output"

    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Quality Trade Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)
- **Hook (first 2 sec):** "The risk rule changes the next decision."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** Bracket Boss maps the stop target and risk before decision number {idx}.
- **On-screen text:** RISK RULE #{idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )

    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=f"Bracket Boss maps the stop target and risk before decision number {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]
    job_updates = []
    captured_multiplier_kwargs = {}

    def fake_process_transcript(_path, **kwargs):
        captured_multiplier_kwargs.update(kwargs)
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        video_pipeline.jobs,
        "update_job",
        lambda _job_id, **kwargs: job_updates.append(kwargs),
    )

    video_pipeline.process_video_job(
        "job-1",
        video_path,
        slug="mock-e2e",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
        creator_guidance="  Favor discipline score and liquidation risk.  ",
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    sidecar = json.loads((out_dir / "00-commercial-qa.json").read_text(encoding="utf-8"))
    assert len(manifest["clips"]) == 10
    assert manifest["shorts_quality"]["quality_gate_passed"] is True
    assert manifest["shorts_quality"]["quality_failures"] == []
    assert manifest["shorts_quality"]["script_errors"] == []
    assert manifest["shorts_quality"]["quality_gate_enabled"] is True
    assert manifest["shorts_quality"]["commercial_qa"]["passed"] is True
    assert manifest["shorts_quality"]["commercial_qa"]["approved_count"] == 10
    assert manifest["commercial_qa"] == sidecar
    assert manifest["shorts_quality"]["rendered_qa"]["passed"] is True
    assert manifest["rendered_qa"]["approved_count"] == 10
    assert (out_dir / "00-rendered-qa.json").exists()
    assert manifest["commercial_qa"]["manual_review_count"] == 0
    assert (out_dir / "00-commercial-qa.json").exists()
    generated_json = _load_generated_json(out_dir)
    assert "00-pipeline-manifest.json" in generated_json
    assert "00-commercial-qa.json" in generated_json
    assert "00-rendered-qa.json" in generated_json
    assert len([path for path in generated_json if path.endswith("/metadata.json")]) == (
        (len(video_pipeline.PLATFORMS) * 10) + 1
    )
    _assert_release_ready_video_artifacts(out_dir, manifest, generated_json)
    assert all(clip["commercial_qa"]["score"] >= 85 for clip in manifest["clips"])
    first_clip_qa = manifest["clips"][0]["commercial_qa"]
    category_scores = first_clip_qa.get("category_scores") or first_clip_qa.get("categories")
    assert set(category_scores) == set(video_pipeline.COMMERCIAL_QA_WEIGHTS)
    assert all((out_dir / clip["clip"]).exists() for clip in manifest["clips"])
    assert job_updates[-1]["state"] == "done"
    assert captured_multiplier_kwargs["creator_guidance"] == "Favor discipline score and liquidation risk."


def test_process_video_job_repairs_short_source_that_exceeds_duration(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-duration-repair"
    shorts_md = """### Short 1 - "The Lock That Stops Revenge Trading"
- **Score:** 8/10
- **Source:** 00:00:50 -> 00:01:31 (41 seconds)
- **Hook (first 2 sec):** "The score lives inside Drawdown Guardian."
- **Why this clip works:** Drawdown Guardian shows the exact distance to the trailing drawdown line before a funded account breach.
- **Source quote:** Drawdown Guardian tracks the exact distance to your trailing drawdown line before breach.
- **On-screen text:** TRAILING DRAWDOWN LINE
- **Outro CTA:** shadowedgetools.com

---

### Short 2 - "Why Traders Blow Accounts"
- **Score:** 8/10
- **Source:** 00:00:00 -> 00:01:00 (60 seconds)
- **Hook (first 2 sec):** "Most traders do not blow accounts because they cannot read a chart."
- **Why this clip works:** The clip ties discipline, process, and account risk to a concrete product workflow.
- **Source quote:** Bracket Boss maps the stop target and risk before the pressure decision.
- **On-screen text:** PROCESS BEFORE PRESSURE
- **Outro CTA:** shadowedgetools.com
"""
    segments = [
        video_pipeline.TranscribedSegment(
            start=0,
            end=60,
            text="Bracket Boss maps the stop target and risk before the pressure decision.",
        ),
        video_pipeline.TranscribedSegment(
            start=50,
            end=85.6,
            text="Drawdown Guardian tracks the exact distance to your trailing drawdown line before breach.",
        ),
    ]
    cut_ranges = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, start, end, output_path, **_kwargs):
        cut_ranges.append((start, end))
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 85.6)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    video_pipeline.process_video_job(
        "job-duration-repair",
        video_path,
        slug="mock-e2e-duration-repair",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    final_shorts = (out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    qa = manifest["shorts_quality"]
    assert qa["quality_gate_status"] == "pass"
    assert qa["script_errors"] == []
    assert qa["duration_repair"]["clamped_indexes"] == [1]
    assert qa["duration_repair"]["dropped_indexes"] == []
    assert "00:00:50 -> 00:01:25 (35 seconds)" in final_shorts
    assert "00:01:31" not in final_shorts
    assert (out_dir / "02-shorts-scripts.pre-duration-repair.md").exists()
    assert len(manifest["clips"]) == 2
    assert cut_ranges[0] == (50, 85.0)


def test_process_video_job_repeated_runs_reset_exports_and_keep_memory_bounded(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "repeat-e2e-output"
    current_count = {"value": 8}
    job_updates = []

    def shorts_md(count):
        return "\n\n---\n\n".join(
            f"""### Short {idx} - "Repeatable Risk Decision {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts((idx - 1) * 18)} -> {video_pipeline._fmt_ts((idx - 1) * 18 + 15)} (15 seconds)
- **Hook (first 2 sec):** "Risk decision {idx} changes before the click."
- **Why this clip works:** Bracket Boss and Drawdown Guardian make the risk-control payoff visible before entry.
- **Source quote:** Bracket Boss maps the stop target and risk decision {idx} before entry.
- **On-screen text:** RISK DECISION {idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
            for idx in range(1, count + 1)
        )

    segments = [
        video_pipeline.TranscribedSegment(
            start=(idx - 1) * 18,
            end=(idx - 1) * 18 + 15,
            text=f"Bracket Boss maps the stop target and risk decision {idx} before entry.",
        )
        for idx in range(1, 9)
    ]

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md(current_count["value"]), encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 150)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        video_pipeline.jobs,
        "update_job",
        lambda _job_id, **kwargs: job_updates.append(kwargs),
    )

    tracemalloc.start()
    try:
        for count in (8, 6, 6):
            current_count["value"] = count
            video_pipeline.process_video_job(
                f"job-repeat-{count}",
                video_path,
                slug="repeat-e2e",
                transcribe_provider="local",
                burn_captions=False,
                clean_transcript=False,
            )
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            assert current < 8 * 1024 * 1024
            assert peak < 24 * 1024 * 1024

            manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
            generated_json = _load_generated_json(out_dir)
            _assert_release_ready_video_artifacts(out_dir, manifest, generated_json)
            assert len(manifest["clips"]) == count
            assert len(list((out_dir / "clips").glob("*.mp4"))) == count
            for platform in video_pipeline.PLATFORMS:
                platform_root = out_dir / "distribution" / platform
                assert sorted(path.name for path in platform_root.iterdir()) == [
                    f"{idx:02d}" for idx in range(1, count + 1)
                ]
    finally:
        tracemalloc.stop()

    done_updates = [update for update in job_updates if update.get("state") == "done"]
    assert len(done_updates) == 3


def test_process_video_job_blocks_failed_rendered_qa_before_distribution(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-rendered-qa-blocked"

    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Rendered QA Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)
- **Hook (first 2 sec):** "The risk rule changes the next decision."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** Bracket Boss maps the stop target and risk before decision number {idx}.
- **On-screen text:** RISK RULE #{idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=f"Bracket Boss maps the stop target and risk before decision number {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]
    distribution_calls = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        output_path.write_bytes(b"fake clip")

    def fake_rendered_qa_report(inputs, *, captions_burned=True):
        clips = []
        for item in inputs:
            failed = item["index"] == 1
            clips.append({
                "index": item["index"],
                "title": item["visual_title"],
                "clip": str(item["clip_path"]),
                "score": 40 if failed else 100,
                "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
                "status": "manual_review" if failed else "pass",
                "categories": {},
                "metrics": {},
                "reasons": ["black_frames: black frames dominate the rendered clip"] if failed else [],
            })
        return {
            "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
            "passed": False,
            "manual_review_required": True,
            "approved_count": len(clips) - 1,
            "manual_review_count": 1,
            "failures": [f"clip 1: rendered QA score 40 below {video_pipeline.RENDERED_QA_THRESHOLD}"],
            "clips": clips,
        }

    def fail_distribution(*_args, **_kwargs):
        distribution_calls.append("distribution")
        raise AssertionError("distribution kit should not be written when rendered QA gate blocks")

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline, "write_distribution_kit", fail_distribution)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Rendered video QA gate failed"):
        video_pipeline.process_video_job(
            "job-rendered-qa-blocked",
            video_path,
            slug="mock-e2e-rendered-qa-blocked",
            transcribe_provider="local",
            burn_captions=False,
            clean_transcript=False,
        )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    sidecar = json.loads((out_dir / "00-rendered-qa.json").read_text(encoding="utf-8"))
    assert manifest["shorts_quality"]["quality_gate_status"] == "blocked"
    assert manifest["shorts_quality"]["rendered_qa_gate_enabled"] is True
    assert manifest["shorts_quality"]["rendered_qa"]["manual_review_count"] == 1
    assert manifest["rendered_qa"] == sidecar
    assert manifest["clips"][0]["rendered_qa"]["status"] == "manual_review"
    assert manifest["clips"][0]["distribution"] == {}
    assert distribution_calls == []
    assert not (out_dir / "distribution").exists()


def test_process_video_job_allows_rendered_qa_manual_review_when_gate_disabled(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-rendered-qa-manual"

    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Rendered Manual Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)
- **Hook (first 2 sec):** "The risk rule changes the next decision."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** Bracket Boss maps the stop target and risk before decision number {idx}.
- **On-screen text:** RISK RULE #{idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=f"Bracket Boss maps the stop target and risk before decision number {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]
    distribution_calls = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    def fake_rendered_qa_report(inputs, *, captions_burned=True):
        clips = []
        for item in inputs:
            failed = item["index"] == 1
            clips.append({
                "index": item["index"],
                "title": item["visual_title"],
                "clip": str(item["clip_path"]),
                "score": 40 if failed else 100,
                "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
                "status": "manual_review" if failed else "pass",
                "categories": {},
                "metrics": {},
                "reasons": ["black_frames: black frames dominate the rendered clip"] if failed else [],
            })
        return {
            "threshold": video_pipeline.RENDERED_QA_THRESHOLD,
            "passed": False,
            "manual_review_required": True,
            "approved_count": len(clips) - 1,
            "manual_review_count": 1,
            "failures": [f"clip 1: rendered QA score 40 below {video_pipeline.RENDERED_QA_THRESHOLD}"],
            "clips": clips,
        }

    def track_distribution(*args, **kwargs):
        distribution_calls.append(kwargs.get("short_index"))
        return original_write_distribution_kit(*args, **kwargs)

    original_write_distribution_kit = video_pipeline.write_distribution_kit
    monkeypatch.setenv("RENDERED_QA_GATE", "0")
    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline, "write_distribution_kit", track_distribution)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    video_pipeline.process_video_job(
        "job-rendered-qa-manual",
        video_path,
        slug="mock-e2e-rendered-qa-manual",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    assert manifest["shorts_quality"]["quality_gate_status"] == "manual_review"
    assert manifest["shorts_quality"]["manual_review_required"] is True
    assert manifest["shorts_quality"]["rendered_qa_gate_enabled"] is False
    assert manifest["shorts_quality"]["rendered_qa"]["manual_review_count"] == 1
    assert manifest["clips"][0]["rendered_qa"]["status"] == "manual_review"
    assert len(distribution_calls) == 10
    assert all(clip["distribution"] for clip in manifest["clips"])
    assert (out_dir / "distribution" / "youtube" / "01" / "metadata.json").exists()


def test_shorts_quality_report_flags_too_few_early_clustered_clips():
    shorts = [
        video_pipeline.ShortRange(f"Short {i}", i * 60, i * 60 + 30, "")
        for i in range(6)
    ]

    report = video_pipeline.shorts_quality_report(shorts, 17 * 60)

    assert report["target_min"] == 10
    assert report["count"] == 6
    assert any("only 6 shorts found" in warning for warning in report["warnings"])
    assert any("start after halfway" in warning for warning in report["warnings"])
    assert any("weak timeline coverage" in warning for warning in report["warnings"])
    assert video_pipeline.shorts_quality_failures(report) == []


def test_process_video_job_blocks_structural_quality_failures_before_export(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-structural-blocked"

    starts = [40, 130, 250, 390, 470, 560]
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Structural QA Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + (80 if idx == 1 else 30))} ({80 if idx == 1 else 30} seconds)
- **Hook (first 2 sec):** "The risk rule changes the next decision."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** Bracket Boss maps the stop target and risk before decision number {idx}.
- **On-screen text:** RISK RULE #{idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=f"Bracket Boss maps the stop target and risk before decision number {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]
    export_calls = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fail_export(*_args, **_kwargs):
        export_calls.append("export")
        raise AssertionError("exports should not run when structural quality gate blocks")

    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fail_export)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", fail_export)
    monkeypatch.setattr(video_pipeline, "write_distribution_kit", fail_export)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Shorts quality gate failed"):
        video_pipeline.process_video_job(
            "job-structural-blocked",
            video_path,
            slug="mock-e2e-structural-blocked",
            transcribe_provider="local",
            burn_captions=False,
            clean_transcript=False,
        )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    assert manifest["clips"] == []
    assert manifest["shorts_quality"]["quality_gate_status"] == "blocked"
    assert any("exceed 70 seconds" in failure or "too long" in failure for failure in manifest["shorts_quality"]["quality_failures"])
    assert export_calls == []
    assert not (out_dir / "clips").exists()
    assert not (out_dir / "distribution").exists()


def test_process_video_job_mocked_e2e_filters_failed_commercial_qa_metadata(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-commercial-qa"

    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]

    def short_block(
        idx,
        start,
        *,
        score="9/10",
        title=None,
        hook="The risk rule changes the next decision.",
        why="Bracket Boss and Drawdown Guardian make the risk-control payoff visible.",
        quote=None,
        on_screen=None,
        cta="See the full workflow at shadowedgetools.com",
    ):
        title = title or f"Commercial QA Moment {idx}"
        quote = quote or f"Bracket Boss maps the stop target and risk before decision number {idx}."
        on_screen = on_screen or f"RISK RULE #{idx}"
        lines = [
            f'### Short {idx} - "{title}"',
            f"- **Score:** {score}",
            f"- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)",
            f'- **Hook (first 2 sec):** "{hook}"',
            f"- **Why this clip works:** {why}",
            f"- **Source quote:** {quote}",
            f"- **On-screen text:** {on_screen}",
        ]
        if cta is not None:
            lines.append(f"- **Outro CTA:** {cta}")
        return "\n".join(lines)

    overrides = {
        2: {
            "score": "6/10",
            "title": "Generic Market Update",
            "hook": "Welcome back before we begin this generic overview.",
            "why": "Generic summary.",
            "quote": "This risk-free setup guarantees profit every day.",
            "on_screen": "MARKET UPDATE WITH A VERY LONG AND UNCLEAR LINE THAT WILL NOT FIT WELL",
            "cta": None,
        },
        3: {"cta": None},
        4: {
            "title": "Commercial QA Moment 1",
            "quote": "Bracket Boss maps the stop target and risk before decision number 1.",
        },
    }
    shorts_md = "\n\n---\n\n".join(
        short_block(idx, start, **overrides.get(idx, {}))
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=f"Bracket Boss maps the stop target and risk before decision number {idx}.",
        )
        for idx, start in enumerate(starts, start=1)
    ]

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setenv("SHORTS_QUALITY_GATE", "0")
    monkeypatch.setenv("COMMERCIAL_QA_GATE", "0")
    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    video_pipeline.process_video_job(
        "job-qa",
        video_path,
        slug="mock-e2e-commercial-qa",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    sidecar = json.loads((out_dir / "00-commercial-qa.json").read_text(encoding="utf-8"))
    qa = manifest["shorts_quality"]
    assert qa["quality_gate_enabled"] is False
    assert qa["commercial_qa_gate_enabled"] is False
    assert qa["quality_gate_passed"] is False
    assert qa["quality_gate_status"] == "manual_review"
    assert qa["manual_review_required"] is True
    assert set(qa["script_errors"]).issubset(set(qa["quality_failures"]))
    repair = qa["commercial_qa_repair"]
    assert repair["attempted"] is True
    assert repair["succeeded"] is True
    assert repair["original_count"] == 10
    assert repair["repaired_count"] == len(manifest["clips"])
    assert repair["dropped_indexes"]
    assert qa["commercial_qa"]["manual_review_count"] == 0
    assert sidecar["manual_review_required"] is False
    assert sidecar["manual_review_count"] == qa["commercial_qa"]["manual_review_count"]
    assert (out_dir / "00-commercial-qa.json").exists()
    assert (out_dir / "02-shorts-scripts.pre-pipeline-qa.md").exists()
    generated_json = _load_generated_json(out_dir)
    assert "00-pipeline-manifest.json" in generated_json
    assert "00-commercial-qa.json" in generated_json
    assert any("missing Outro CTA" in error for error in qa["script_errors"])
    assert any("duplicate" in error for error in qa["script_errors"])
    final_shorts = (out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    assert "risk-free setup guarantees profit" not in final_shorts
    assert 0 < len(manifest["clips"]) < 10
    assert all(clip["commercial_qa"]["status"] == "pass" for clip in manifest["clips"])


def test_process_video_job_mocked_e2e_blocks_failed_commercial_qa_when_gate_enabled(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-commercial-qa-blocked"
    shorts_md = """### Short 1 - "Weak Commercial Clip"
- **Score:** 4/10
- **Source:** 00:00:40 -> 00:01:10 (30 seconds)
- **Hook (first 2 sec):** "Generic update."
- **Why this clip works:** It does not create a clear risk-control payoff.
- **Source quote:** This risk-free Bracket Boss setup guarantees profit.
- **On-screen text:** UPDATE
- **Outro CTA:** shadowedgetools.com
"""
    segments = [
        video_pipeline.TranscribedSegment(
            start=40,
            end=70,
            text="This risk-free Bracket Boss setup guarantees profit.",
        )
    ]

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    export_calls = []

    def fail_cut_clip(*_args, **_kwargs):
        export_calls.append("cut_clip")
        raise AssertionError("cut_clip should not run when commercial QA gate blocks")

    def fail_distribution_kit(*_args, **_kwargs):
        export_calls.append("write_distribution_kit")
        raise AssertionError("distribution kit should not be written when commercial QA gate blocks")

    def fail_thumbnail(*_args, **_kwargs):
        export_calls.append("write_thumbnail")
        raise AssertionError("thumbnail should not be written when commercial QA gate blocks")

    monkeypatch.delenv("SHORTS_QUALITY_GATE", raising=False)
    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fail_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_distribution_kit", fail_distribution_kit)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fail_thumbnail)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Shorts quality gate failed"):
        video_pipeline.process_video_job(
            "job-qa-blocked",
            video_path,
            slug="mock-e2e-commercial-qa-blocked",
            transcribe_provider="local",
            burn_captions=False,
            clean_transcript=False,
        )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    sidecar = json.loads((out_dir / "00-commercial-qa.json").read_text(encoding="utf-8"))
    assert manifest["clips"] == []
    assert manifest["shorts_quality"]["quality_gate_status"] == "blocked"
    assert manifest["shorts_quality"]["commercial_qa_gate_enabled"] is True
    assert sidecar["manual_review_required"] is True
    assert (out_dir / "00-commercial-qa.json").exists()
    assert export_calls == []
    assert not (out_dir / "clips").exists()
    assert not (out_dir / "distribution").exists()


def test_commercial_qa_gate_filters_bad_clip_when_good_clips_remain(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-commercial-only-blocked"

    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]
    quotes = [
        f"Bracket Boss maps the stop target and risk before decision number {idx}."
        for idx in range(1, 11)
    ]
    quotes[1] = "This risk-free setup guarantees profit with Bracket Boss today."
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Risk Control Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)
- **Hook (first 2 sec):** "The risk decision changes trade {idx}."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** {quotes[idx - 1]}
- **On-screen text:** RISK DECISION {idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=quotes[idx - 1],
        )
        for idx, start in enumerate(starts, start=1)
    ]
    cut_calls = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        cut_calls.append(output_path.name)
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setenv("SHORTS_QUALITY_GATE", "0")
    monkeypatch.setenv("COMMERCIAL_QA_GATE", "1")
    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    video_pipeline.process_video_job(
        "job-commercial-only-filtered",
        video_path,
        slug="mock-e2e-commercial-only-blocked",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    generated_json = _load_generated_json(out_dir)
    assert len(manifest["clips"]) == 9
    assert manifest["shorts_quality"]["quality_gate_enabled"] is False
    assert manifest["shorts_quality"]["commercial_qa_gate_enabled"] is True
    assert manifest["shorts_quality"]["script_errors"] == []
    assert manifest["shorts_quality"]["quality_gate_status"] == "pass"
    assert manifest["shorts_quality"]["commercial_qa"]["manual_review_count"] == 0
    repair = manifest["shorts_quality"]["commercial_qa_repair"]
    assert repair["succeeded"] is True
    assert repair["dropped_indexes"] == [2]
    assert repair["original_count"] == 10
    assert repair["repaired_count"] == 9
    assert len(cut_calls) == 9
    assert (out_dir / "02-shorts-scripts.pre-pipeline-qa.md").exists()
    final_shorts = (out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    assert "risk-free setup guarantees profit" not in final_shorts
    _assert_release_ready_video_artifacts(out_dir, manifest, generated_json)


def test_pipeline_filters_exact_failed_run_shape_without_blocking(tmp_path, monkeypatch):
    video_path = tmp_path / "session.mp4"
    video_path.write_bytes(b"fake video bytes")
    content_dir = tmp_path / "content"
    out_dir = content_dir / "mock-e2e-commercial-qa-near-miss"

    starts = [40, 95, 150, 215, 280, 345, 410, 475, 680, 870]
    quotes = [
        f"Bracket Boss maps the stop target and risk before decision number {idx}."
        for idx in range(1, 11)
    ]
    quotes[0] = "This Bracket Boss setup shows $392 profit before the risk decision."
    quotes[5] = "This Drawdown Guardian example shows $300 profit on $200 risk."
    shorts_md = "\n\n---\n\n".join(
        f"""### Short {idx} - "Risk Control Moment {idx}"
- **Score:** 9/10
- **Source:** {video_pipeline._fmt_ts(start)} -> {video_pipeline._fmt_ts(start + 30)} (30 seconds)
- **Hook (first 2 sec):** "The risk decision changes trade {idx}."
- **Why this clip works:** Bracket Boss and Drawdown Guardian turn risk control into a visible trading payoff.
- **Source quote:** {quotes[idx - 1]}
- **On-screen text:** RISK DECISION {idx}
- **Outro CTA:** See the full workflow at shadowedgetools.com
"""
        for idx, start in enumerate(starts, start=1)
    )
    segments = [
        video_pipeline.TranscribedSegment(
            start=start,
            end=start + 30,
            text=quotes[idx - 1],
        )
        for idx, start in enumerate(starts, start=1)
    ]
    cut_calls = []

    def fake_process_transcript(_path, **_kwargs):
        out_dir.mkdir(parents=True)
        (out_dir / "02-shorts-scripts.md").write_text(shorts_md, encoding="utf-8")
        return SimpleNamespace(out_dir=out_dir, error=None)

    def fake_cut_clip(_video_path, _start, _end, output_path, **_kwargs):
        cut_calls.append(output_path.name)
        output_path.write_bytes(b"fake clip")

    def fake_thumbnail(_clip_path, thumbnail_path, **_kwargs):
        thumbnail_path.write_bytes(b"fake thumbnail")

    monkeypatch.setenv("SHORTS_QUALITY_GATE", "1")
    monkeypatch.setenv("COMMERCIAL_QA_GATE", "1")
    monkeypatch.setattr(video_pipeline, "CONTENT_DIR", content_dir)
    monkeypatch.setattr(video_pipeline, "probe_duration", lambda _path: 17 * 60)
    monkeypatch.setattr(video_pipeline, "transcribe_local", lambda *_args, **_kwargs: (segments, "en"))
    monkeypatch.setattr(video_pipeline.content_multiplier, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(video_pipeline, "cut_clip", fake_cut_clip)
    monkeypatch.setattr(video_pipeline, "write_thumbnail", fake_thumbnail)
    monkeypatch.setattr(video_pipeline, "rendered_qa_report", _fake_rendered_qa_report)
    monkeypatch.setattr(video_pipeline.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(video_pipeline.jobs, "update_job", lambda *_args, **_kwargs: None)

    video_pipeline.process_video_job(
        "job-commercial-qa-near-miss",
        video_path,
        slug="mock-e2e-commercial-qa-near-miss",
        transcribe_provider="local",
        burn_captions=False,
        clean_transcript=False,
    )

    manifest = json.loads((out_dir / "00-pipeline-manifest.json").read_text(encoding="utf-8"))
    generated_json = _load_generated_json(out_dir)
    qa = manifest["shorts_quality"]
    repair = qa["commercial_qa_repair"]
    assert qa["quality_gate_status"] == "pass"
    assert any("start after halfway" in warning for warning in qa["warnings"])
    assert any("only 8 shorts found" in warning for warning in qa["warnings"])
    assert qa["quality_failures"] == []
    assert repair["succeeded"] is True
    assert repair["dropped_indexes"] == [1, 6]
    assert repair["original_count"] == 10
    assert repair["repaired_count"] == 8
    assert len(manifest["clips"]) == 8
    assert len(cut_calls) == 8
    assert (out_dir / "02-shorts-scripts.pre-pipeline-qa.md").exists()
    final_shorts = (out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    assert "$392 profit" not in final_shorts
    assert "$300 profit" not in final_shorts
    assert all(clip["commercial_qa"]["status"] == "pass" for clip in manifest["clips"])
    _assert_release_ready_video_artifacts(out_dir, manifest, generated_json)


def _internal_note_short():
    return video_pipeline.ShortRange(
        title='Short 1 - "The Lock Keeps You Disciplined"',
        start_sec=50,
        end_sec=85,
        body_md="\n".join([
            "- **Score:** 8/10",
            "- **Source:** 00:00:50 -> 00:01:25 (35 seconds)",
            '- **Hook (first 2 sec):** "The lock stops you after a red trade."',
            '- **On-screen text:** "Order Lock engaged"',
            "- **Why this clip works:** product-payoff clip, names both tools",
            "- **Outro CTA:** Full guide at shadowedgetools.com",
        ]),
    )


def test_social_caption_is_viewer_facing_not_strategy():
    cta = "Free risk-control checklist: https://shadowedgetools.com/checklist?utm_source=tiktok"
    caption = video_pipeline._social_caption(
        _internal_note_short(), "The Lock Keeps You Disciplined", cta
    )
    # leads with the hook, includes on-screen value + the checklist CTA
    assert caption.startswith("The lock stops you after a red trade.")
    assert "Order Lock engaged" in caption
    assert cta in caption
    # internal production notes must NOT leak into a public caption
    assert "Score" not in caption
    assert "8/10" not in caption
    assert "Why this clip works" not in caption
    assert "product-payoff" not in caption
    assert "00:00:50" not in caption


def test_clean_caption_for_posting_strips_internal_notes():
    raw = "\n".join([
        "The Lock Keeps You Disciplined",
        "",
        "Free risk-control checklist: https://shadowedgetools.com/checklist?utm_source=tiktok",
        "",
        "- **Score:** 8/10",
        "- **Source:** 00:00:50 -> 00:01:25 (35 seconds)",
        '- **Hook (first 2 sec):** "The lock stops you after a red trade."',
        '- **On-screen text:** "Order Lock engaged"',
        "- **Why this clip works:** product-payoff clip, names both tools",
        "- **Outro CTA:** See it at shadowedgetools.com",
        "",
        "#daytrader #propfirm",
    ])
    out = video_pipeline.clean_caption_for_posting(raw)
    # internal notes dropped entirely
    assert "Score" not in out and "8/10" not in out
    assert "Source:" not in out and "00:00:50" not in out
    assert "Why this clip works" not in out and "product-payoff" not in out
    # viewer-facing copy kept, with **Label:** prefixes removed
    assert "The lock stops you after a red trade." in out
    assert "Order Lock engaged" in out
    assert "See it at shadowedgetools.com" in out
    assert "The Lock Keeps You Disciplined" in out
    assert "Free risk-control checklist:" in out
    assert "#daytrader #propfirm" in out
    assert "**Hook" not in out and "On-screen text:" not in out


def test_clean_caption_leaves_clean_text_intact():
    clean = (
        "The lock stops you after a red trade.\n\n"
        "Free risk-control checklist: https://shadowedgetools.com/checklist\n\n"
        "#daytrader #propfirm"
    )
    assert video_pipeline.clean_caption_for_posting(clean) == clean
