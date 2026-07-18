import content_multiplier as cm


HIGH_QUALITY_SHORTS = """
### Short 1 - "The $973 Line"
- **Score:** 9/10
- **Source:** 00:00:40 -> 00:01:10 (30 seconds)
- **Hook (first 2 sec):** "$973 to liquidation changes the trade."
- **Why this clip works:** Concrete account-risk stakes tied to the liquidation number and Drawdown Guardian.
- **Source quote:** We have $973 to liquidation and this setup risks $200.
- **On-screen text:** $973 LEFT
- **Outro CTA:** See the full risk workflow at shadowedgetools.com

---

### Short 2 - "Bracket Boss Before the Click"
- **Score:** 9/10
- **Source:** 00:04:00 -> 00:04:32 (32 seconds)
- **Hook (first 2 sec):** "Entry, stop, and target are set before the click."
- **Why this clip works:** Shows Bracket Boss turning a trade idea into a visible risk plan.
- **Source quote:** Bracket Boss has the stop target and entry already mapped.
- **On-screen text:** PLAN BEFORE THE ORDER
- **Outro CTA:** Watch the full session on the channel
"""


def test_commercial_qa_scores_each_short_with_threshold_and_criteria():
    report = cm.score_shorts_commercial_qa(HIGH_QUALITY_SHORTS)

    assert report.threshold == 85
    assert report.passed is True
    assert len(report.shorts) == 2
    assert all(short.total >= 85 for short in report.shorts)
    assert all(short.passed for short in report.shorts)
    assert set(report.shorts[0].criteria) == {
        "hook",
        "clarity",
        "product_proof",
        "visual_quality",
        "audio_captions",
        "cta",
        "compliance",
        "uniqueness",
    }
    compliance = report.shorts[0].criteria["compliance"]
    assert compliance.score == compliance.max_score
    assert report.to_dict()["short_count"] == 2


def test_commercial_qa_flags_weak_non_compliant_duplicate_shorts():
    bad = """
### Short 1 - "Market Update"
- **Score:** 8/10
- **Source:** 00:00:00 -> 00:00:10 (10 seconds)
- **Hook (first 2 sec):** In this video we talk about trading.
- **Why this clip works:** It is useful.
- **Source quote:** Welcome back.
- **On-screen text:** MARKET UPDATE
- **Outro CTA:** Buy now before it is too late for guaranteed profit.

---

### Short 2 - "Market Update"
- **Score:** 8/10
- **Source:** 00:00:02 -> 00:00:12 (10 seconds)
- **Hook (first 2 sec):** In this video we talk about trading.
- **Why this clip works:** It is useful.
- **Source quote:** Welcome back.
- **On-screen text:** MARKET UPDATE
- **Outro CTA:** Buy now before it is too late for guaranteed profit.
"""

    report = cm.score_shorts_commercial_qa(bad)

    assert report.passed is False
    assert len(report.shorts) == 2
    assert all(short.total < 85 for short in report.shorts)
    assert all(short.criteria["compliance"].score == 0 for short in report.shorts)
    assert all(short.criteria["uniqueness"].score < 10 for short in report.shorts)
    assert any("compliance risk" in note for note in report.shorts[0].notes)


def test_commercial_qa_blocks_generic_hooks_even_when_other_fields_are_good():
    report = cm.score_shorts_commercial_qa(
        HIGH_QUALITY_SHORTS.replace(
            '"$973 to liquidation changes the trade."',
            "In this video we talk about trading today.",
        )
    )

    assert report.passed is False
    assert report.shorts[0].total < 85
    assert any("hook lacks a clear stake" in note for note in report.shorts[0].notes)
    assert any("audio likely includes filler" in note for note in report.shorts[0].notes)


def test_commercial_qa_allows_overlapping_shorts_as_scheduling_note():
    report = cm.score_shorts_commercial_qa(
        HIGH_QUALITY_SHORTS.replace(
            "00:04:00 -> 00:04:32 (32 seconds)",
            "00:00:50 -> 00:01:20 (30 seconds)",
        )
    )

    assert report.passed is True
    assert all(short.total >= 85 for short in report.shorts)
    assert any("schedule on a different day" in note for short in report.shorts for note in short.notes)


def test_commercial_qa_blocks_bad_clip_duration_bounds():
    too_short = cm.score_shorts_commercial_qa(
        HIGH_QUALITY_SHORTS.replace(
            "00:00:40 -> 00:01:10 (30 seconds)",
            "00:00:40 -> 00:00:50 (10 seconds)",
        )
    )
    too_long = cm.score_shorts_commercial_qa(
        HIGH_QUALITY_SHORTS.replace(
            "00:00:40 -> 00:01:10 (30 seconds)",
            "00:00:40 -> 00:01:50 (70 seconds)",
        )
    )

    assert too_short.passed is False
    assert too_short.shorts[0].total < 85
    assert too_long.passed is False
    assert too_long.shorts[0].total < 85
    assert any("source duration is outside" in note for note in too_short.shorts[0].notes)
    assert any("source duration is outside" in note for note in too_long.shorts[0].notes)


def test_commercial_qa_returns_report_error_when_no_shorts_found():
    report = cm.score_shorts_commercial_qa("No shorts here.")

    assert report.passed is False
    assert report.shorts == []
    assert report.errors == ["no Shorts blocks found for commercial QA"]
