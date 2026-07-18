"""Tests for burned-in subtitle (caption) support in the video pipeline.

The pure-Python cue logic is unit-tested directly. The render test synthesizes a
source clip with ffmpeg (lavfi) and actually cuts it with title + captions, which
is the only thing that proves the filtergraph string is valid ffmpeg syntax.
"""
import pytest

import video_pipeline as vp
from video_pipeline import CaptionCue, TranscribedSegment


def _seg(start, end, text):
    return TranscribedSegment(start=start, end=end, text=text)


# --- cue splitting ----------------------------------------------------------

def test_split_segment_groups_words_and_covers_span():
    seg = _seg(10.0, 16.0, "one two three four five six seven eight")  # 8 words
    cues = vp._split_segment_into_cues(seg)
    assert len(cues) == 2  # 6 + 2 at CAPTION_MAX_WORDS_PER_CUE=6
    # contiguous, covering the full source span
    assert cues[0][0] == 10.0
    assert cues[-1][1] == pytest.approx(16.0, abs=0.01)
    assert cues[0][1] == cues[1][0]  # no gap between sub-cues
    assert cues[0][2] == "one two three four five six"
    assert cues[1][2] == "seven eight"


def test_split_segment_empty_text_yields_nothing():
    assert vp._split_segment_into_cues(_seg(0.0, 2.0, "   ")) == []


# --- windowing / shifting / floor / de-overlap ------------------------------

def test_clip_caption_cues_drops_under_title_and_keeps_visible():
    segments = [
        _seg(1.0, 3.0, "should be hidden"),          # entirely under the title card
        _seg(5.0, 6.0, "visible one two"),           # one cue, after the card
        _seg(7.0, 11.0, "aaa bbb ccc ddd eee fff ggg hhh"),  # 8 words -> 2 cues
    ]
    cues = vp.clip_caption_cues(segments, clip_start=0.0, clip_end=12.0)

    texts = [c.text for c in cues]
    assert "should be hidden" not in texts
    assert "visible one two" in texts
    assert len(cues) == 3  # 1 + 2

    # all start at/after the title-card floor, sorted, never overlapping
    assert all(c.start >= vp.TITLE_DURATION_SEC for c in cues)
    for a, b in zip(cues, cues[1:]):
        assert a.start <= b.start
        assert a.end <= b.start + 0.0011  # rounding tolerance


def test_clip_caption_cues_shifts_to_clip_local_time():
    segments = [_seg(15.0, 16.0, "shift test phrase")]
    cues = vp.clip_caption_cues(segments, clip_start=10.0, clip_end=20.0)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(5.0, abs=0.001)   # 15 - 10
    assert cues[0].end == pytest.approx(6.0, abs=0.001)     # 16 - 10


def test_clip_caption_cues_min_duration_does_not_exceed_next():
    # two back-to-back tiny cues: the first is padded but must not overlap the second
    segments = [_seg(5.0, 5.1, "tiny"), _seg(5.1, 5.2, "next")]
    cues = vp.clip_caption_cues(segments, clip_start=0.0, clip_end=12.0)
    assert len(cues) == 2
    assert cues[0].end <= cues[1].start + 0.0011


# --- filter string shape ----------------------------------------------------

def test_caption_drawtext_carries_time_window_and_text():
    s = vp._caption_drawtext(CaptionCue(4.25, 6.0, "stop loss matters"))
    assert s.startswith("drawtext=")
    assert r"between(t\,4.25\,6.0)" in s
    assert "stop loss matters" in s
    assert "fontcolor=white" in s


def test_vertical_filter_appends_captions_only_when_present():
    cues = [CaptionCue(4.3, 5.0, "hello world")]
    with_caps = vp._vertical_shorts_filter("Title", "body", cues)
    without = vp._vertical_shorts_filter("Title", "body", None)
    assert "hello world" in with_caps
    assert "hello world" not in without
    # both still terminate in the named output pad
    assert with_caps.endswith("[vout]") and without.endswith("[vout]")


# --- real render (proves the filtergraph is valid ffmpeg) -------------------

def _synthesize_source(path, seconds=8):
    return vp._run_ffmpeg([
        "-y",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ])


def test_cut_clip_renders_with_burned_captions(tmp_path):
    try:
        vp.ffmpeg_exe()
    except Exception:  # pragma: no cover
        pytest.skip("ffmpeg binary not available")

    src = tmp_path / "src.mp4"
    res = _synthesize_source(src, 8)
    if res.returncode != 0 or not src.exists() or src.stat().st_size == 0:
        pytest.skip(f"could not synthesize source clip: {(res.stderr or '')[:200]}")

    out = tmp_path / "clip.mp4"
    cues = [
        CaptionCue(4.5, 5.4, "stop loss matters"),
        CaptionCue(5.4, 6.0, "lock it in"),
    ]
    # raises RuntimeError if the filtergraph (title + captions) is invalid
    vp.cut_clip(src, 0.0, 6.0, out, title_overlay="Discipline Test", title_body_md="x", caption_cues=cues)

    assert out.exists() and out.stat().st_size > 0
    assert 5.0 < vp.probe_duration(out) < 7.0


# --- funnel: bare homepage -> tracked /checklist ---------------------------

def test_retarget_rewrites_bare_homepage_to_tracked_checklist():
    tracked = "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=W26"
    for raw in [
        "See the full workflow at shadowedgetools.com",
        "Learn more: https://shadowedgetools.com",
        "visit www.shadowedgetools.com today",
    ]:
        out = vp._retarget_bare_site_links(raw, tracked)
        assert tracked in out
        # no bare homepage mention survives (i.e. domain always precedes the /checklist path)
        assert "shadowedgetools.com" in out
        assert out.count("shadowedgetools.com") == out.count("shadowedgetools.com/checklist")


def test_retarget_leaves_already_tracked_links_untouched():
    tracked = "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=W26"
    line = f"Free risk-control checklist: {tracked}"
    assert vp._retarget_bare_site_links(line, tracked) == line


def test_retarget_catches_homepage_with_trailing_slash():
    tracked = "https://shadowedgetools.com/checklist?utm_source=youtube"
    out = vp._retarget_bare_site_links("More: https://www.shadowedgetools.com/", tracked)
    assert out == f"More: {tracked}"


def test_retarget_preserves_real_deep_links():
    # Product/guide deep links are intentional and higher-intent — never rewrite them.
    tracked = "https://shadowedgetools.com/checklist?utm_source=youtube"
    for deep in [
        "https://www.shadowedgetools.com/products/bracket-boss",
        "shadowedgetools.com/products/drawdown-guardian",
        "https://www.shadowedgetools.com/guides",
    ]:
        line = f"See {deep} for details"
        assert vp._retarget_bare_site_links(line, tracked) == line


def test_retarget_handles_empty():
    assert vp._retarget_bare_site_links("", "https://x/checklist") == ""
