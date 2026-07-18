"""Transcript cleanup — removes filler/stutters without paraphrasing or breaking timing."""
import video_pipeline as vp
from video_pipeline import TranscribedSegment, clean_transcript_segments, _clean_segment_text


def test_removes_nonlexical_filler():
    assert _clean_segment_text("Um, the drawdown was uh bad") == "The drawdown was bad"
    assert _clean_segment_text("Erm so it flattened") == "So it flattened"


def test_keeps_real_words_that_merely_contain_filler_substrings():
    # her/water/summer/yeah must survive (whole-word match only)
    text = "Her water was warmer in summer, yeah"
    assert _clean_segment_text(text) == text


def test_keeps_meaningful_discourse_words():
    # "so", "like", "a", "I" carry meaning and must not be stripped
    assert _clean_segment_text("So I like a tight stop") == "So I like a tight stop"


def test_collapses_immediate_stutter():
    # casing is left as-is for a lowercase mid-sentence fragment (no invented capital)
    assert _clean_segment_text("the the drawdown") == "the drawdown"
    assert _clean_segment_text("I I think it works") == "I think it works"


def test_strips_bracketed_noise():
    assert _clean_segment_text("Trading [Music] is risky") == "Trading is risky"


def test_pure_filler_collapses_to_empty():
    assert _clean_segment_text("Um... uh.") == ""
    assert _clean_segment_text("[Applause]") == ""


def test_does_not_capitalize_genuine_midsentence_fragment():
    # whisper hands us a lowercase mid-sentence fragment with no leading filler
    assert _clean_segment_text("was running against me") == "was running against me"


def test_clean_segments_preserves_timing_and_drops_empties():
    raw = [
        TranscribedSegment(0.0, 1.0, "Um."),
        TranscribedSegment(1.0, 3.0, "The the drawdown"),
        TranscribedSegment(3.0, 5.0, "was bad"),
    ]
    out = clean_transcript_segments(raw)
    assert [(s.start, s.end, s.text) for s in out] == [
        (1.0, 3.0, "The drawdown"),
        (3.0, 5.0, "was bad"),
    ]


def test_clean_is_idempotent():
    once = _clean_segment_text("Um, the the stop loss held")
    assert _clean_segment_text(once) == once
