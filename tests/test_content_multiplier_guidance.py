"""The content multiplier should fold the Editor's guidance into the next video's
generation prompt (and work fine when there's no guidance yet)."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

import content_multiplier as cm
from content_multiplier import Transcript, TranscriptSegment, build_prompts


def _transcript():
    return Transcript(text="hello world", segments=[], source_filename="x.txt", duration_sec=0)


def test_load_editor_guidance_reads_prompt_block(tmp_path, monkeypatch):
    p = tmp_path / "editor-guidance.json"
    p.write_text(json.dumps({"prompt_block": "LEAN INTO DISCIPLINE"}), encoding="utf-8")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", p)
    assert cm.load_editor_guidance() == "LEAN INTO DISCIPLINE"


def test_load_editor_guidance_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "nope.json")
    assert cm.load_editor_guidance() == ""


def test_build_prompts_injects_guidance():
    _system, user = build_prompts(
        _transcript(), slug="s", product="both",
        brand_voice="bv", guardrails="gr", editor_guidance="LEAN INTO DISCIPLINE",
    )
    assert "Performance guidance" in user
    assert "LEAN INTO DISCIPLINE" in user


def test_build_prompts_injects_creator_guidance():
    _system, user = build_prompts(
        _transcript(),
        slug="s",
        product="both",
        brand_voice="bv",
        guardrails="gr",
        creator_guidance="Favor discipline score and funded-account risk.",
    )

    assert "Creator direction for this run" in user
    assert "Favor discipline score" in user


def test_shorts_editor_prompt_injects_guidance():
    transcript = Transcript(
        text="",
        segments=[
            TranscriptSegment(0, 12, "We have $973 to liquidation before this trade."),
            TranscriptSegment(12, 30, "Drawdown Guardian shows the discipline score moving."),
        ],
        source_filename="x.srt",
        duration_sec=30,
    )

    _system, user = cm.build_shorts_editor_prompts(
        transcript,
        slug="s",
        product="both",
        brand_voice="bv",
        guardrails="gr",
        editor_guidance="Favor hook type: account risk. Favor cta type: website.",
    )

    assert "Performance guidance" in user
    assert "Favor hook type: account risk" in user
    assert "Favor cta type: website" in user
    assert "Local candidate moments" in user


def test_shorts_editor_prompt_injects_creator_guidance():
    transcript = Transcript(
        text="",
        segments=[
            TranscriptSegment(0, 12, "We have $973 to liquidation before this trade."),
            TranscriptSegment(12, 30, "Drawdown Guardian shows the discipline score moving."),
        ],
        source_filename="x.srt",
        duration_sec=30,
    )

    _system, user = cm.build_shorts_editor_prompts(
        transcript,
        slug="s",
        product="both",
        brand_voice="bv",
        guardrails="gr",
        creator_guidance="Prefer the liquidation-risk clip.",
    )

    assert "Creator direction for this run" in user
    assert "Prefer the liquidation-risk clip" in user


def test_build_prompts_without_guidance_has_no_block():
    _system, user = build_prompts(
        _transcript(), slug="s", product="both", brand_voice="bv", guardrails="gr",
    )
    assert "Performance guidance" not in user


def test_process_transcript_retries_overloaded_anthropic_request(tmp_path, monkeypatch):
    raw = """<<<<FILE: 01-x-thread.md>>>>
Thread draft

<<<<FILE: 02-shorts-scripts.md>>>>
Shorts draft
"""

    class FakeOverloadedError(Exception):
        status_code = 529

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise FakeOverloadedError("Error code: 529 - overloaded_error: Overloaded")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=raw)],
                usage=SimpleNamespace(input_tokens=12, output_tokens=34),
            )

    class FakeAnthropic:
        messages_obj = None

        def __init__(self, api_key):
            self.messages = FakeMessages()
            FakeAnthropic.messages_obj = self.messages

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "LLM_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(cm, "LLM_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(cm, "LLM_RETRY_MAX_SECONDS", 0.0)
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    transcript = tmp_path / "source.txt"
    transcript.write_text("A discipline score walkthrough.", encoding="utf-8")

    result = cm.process_transcript(transcript, slug="retry-test", editor_guidance="")

    assert result.error is None
    assert FakeAnthropic.messages_obj.calls == 2
    assert (result.out_dir / "00-raw-response.md").read_text(encoding="utf-8") == raw
    assert (result.out_dir / "01-x-thread.md").exists()
    assert (result.out_dir / "02-shorts-scripts.md").exists()
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["usage"] == {"input_tokens": 12, "output_tokens": 34}


def test_process_transcript_records_creator_guidance(tmp_path, monkeypatch):
    raw = """<<<<FILE: 01-x-thread.md>>>>
Thread draft

<<<<FILE: 02-shorts-scripts.md>>>>
Shorts draft
"""

    class FakeMessages:
        def create(self, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=raw)],
                usage=SimpleNamespace(input_tokens=12, output_tokens=34),
            )

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    transcript = tmp_path / "source.txt"
    transcript.write_text("A discipline score walkthrough.", encoding="utf-8")

    result = cm.process_transcript(
        transcript,
        slug="creator-brief",
        editor_guidance="",
        creator_guidance="Push drawdown risk.",
    )

    assert result.error is None
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["creator_guidance_applied"] is True
    assert meta["creator_guidance"] == "Push drawdown risk."
    assert (result.out_dir / "00-creator-guidance.md").read_text(encoding="utf-8") == "Push drawdown risk.\n"


def test_process_transcript_records_api_error_after_retry_exhaustion(tmp_path, monkeypatch):
    class FakeOverloadedError(Exception):
        status_code = 529

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            raise FakeOverloadedError("Error code: 529 - overloaded_error: Overloaded")

    class FakeAnthropic:
        messages_obj = None

        def __init__(self, api_key):
            self.messages = FakeMessages()
            FakeAnthropic.messages_obj = self.messages

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "LLM_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cm, "LLM_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(cm, "LLM_RETRY_MAX_SECONDS", 0.0)
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    transcript = tmp_path / "source.txt"
    transcript.write_text("A discipline score walkthrough.", encoding="utf-8")

    result = cm.process_transcript(transcript, slug="retry-fails", editor_guidance="")

    assert result.error.startswith("Anthropic request failed after 2 attempts:")
    assert FakeAnthropic.messages_obj.calls == 2
    assert result.raw_response_path.name == "00-error.txt"
    assert (result.out_dir / "00-error.txt").exists()
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["api_attempts"] == 2
    assert meta["api_error"] == result.error


def test_long_video_prompt_demands_more_shorts_and_timeline_coverage():
    transcript = Transcript(
        text="",
        segments=[],
        source_filename="long.srt",
        duration_sec=17 * 60,
    )

    _system, user = build_prompts(
        transcript, slug="s", product="both", brand_voice="bv", guardrails="gr",
    )

    assert "Shorts target: 10-12 ranked clips." in user
    assert "Timeline coverage requirement" in user
    assert "Include at least 4 clips that start after the halfway point." in user


def test_clip_miner_prioritizes_real_trade_moments_over_intro():
    transcript = Transcript(
        text="",
        segments=[
            TranscriptSegment(0, 8, "Welcome back today we are getting started."),
            TranscriptSegment(8, 16, "Before we begin make sure the platform is open."),
            TranscriptSegment(120, 132, "We have $973 to liquidation and this trade risks $200."),
            TranscriptSegment(132, 144, "Bracket Boss has the stop target and entry already mapped."),
            TranscriptSegment(144, 156, "If price cracks the level the setup is invalidated and we cancel."),
            TranscriptSegment(620, 632, "The target filled and I did not touch the stop."),
            TranscriptSegment(632, 644, "Drawdown Guardian moved the discipline score to 53 automatically."),
            TranscriptSegment(644, 656, "That is the whole payoff of following the rule."),
        ],
        source_filename="x.srt",
        duration_sec=17 * 60,
    )

    candidates = cm.mine_clip_candidates(transcript, max_candidates=5)

    assert candidates
    assert "$973" in candidates[0].text or "target filled" in candidates[0].text.lower()
    assert all("Welcome back" not in c.text for c in candidates)


def test_clip_miner_expands_sparse_high_value_segments():
    transcript = Transcript(
        text="",
        segments=[
            TranscriptSegment(40, 48, "We have $973 to liquidation and this setup risks $200."),
            TranscriptSegment(130, 138, "Price cracked the level, setup invalidated, cancel the order."),
        ],
        source_filename="x.srt",
        duration_sec=17 * 60,
    )

    candidates = cm.mine_clip_candidates(transcript, max_candidates=5)

    assert candidates
    assert candidates[0].duration_sec >= 20
    assert "$973" in candidates[0].text


def test_shorts_editor_prompt_uses_candidates_and_single_file_output():
    transcript = Transcript(
        text="",
        segments=[
            TranscriptSegment(120, 132, "We have $973 to liquidation and this trade risks $200."),
            TranscriptSegment(132, 144, "Bracket Boss has the stop target and entry already mapped."),
            TranscriptSegment(144, 156, "If price cracks the level the setup is invalidated and we cancel."),
        ],
        source_filename="x.srt",
        duration_sec=17 * 60,
    )

    system, user = cm.build_shorts_editor_prompts(
        transcript,
        slug="s",
        product="both",
        brand_voice="bv",
        guardrails="gr",
    )

    assert "<<<<FILE: 02-shorts-scripts.md>>>>" in system
    assert "Local candidate moments" in user
    assert "score=" in user
    assert "$973" in user


def test_shorts_editor_requires_timestamped_transcript():
    with pytest.raises(ValueError, match="timestamped transcript"):
        cm.build_shorts_editor_prompts(
            Transcript(text="plain transcript", segments=[], source_filename="x.txt", duration_sec=0),
            slug="s",
            product="both",
            brand_voice="bv",
            guardrails="gr",
        )


def test_clip_miner_reserves_timeline_coverage_for_long_videos():
    segments = []
    for base in (60, 120, 420, 480, 760, 820):
        segments.extend([
            TranscriptSegment(
                base,
                base + 12,
                "Bracket Boss maps the entry stop target and risk before the click.",
            ),
            TranscriptSegment(
                base + 12,
                base + 24,
                "The target filled while I did not touch the stop, which proves the rule.",
            ),
        ])
    transcript = Transcript(text="", segments=segments, source_filename="x.srt", duration_sec=17 * 60)

    candidates = cm.mine_clip_candidates(transcript, max_candidates=6)
    third = transcript.duration_sec / 3
    thirds = [
        sum(1 for c in candidates if 0 <= c.start_sec < third),
        sum(1 for c in candidates if third <= c.start_sec < third * 2),
        sum(1 for c in candidates if c.start_sec >= third * 2),
    ]

    assert thirds == [2, 2, 2]


def test_process_transcript_overwrites_first_pass_shorts_with_editor_pass(tmp_path, monkeypatch):
    first_pass = """<<<<FILE: 01-x-thread.md>>>>
1/ broad thread

<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "Weak Early Clip"
- **Score:** 4/10
- **Source:** 00:00:00 -> 00:00:30 (30 seconds)
- **Hook (first 2 sec):** Weak setup.
- **Why this clip works:** It does not.
- **Source quote:** Welcome back.
- **On-screen text:** INTRO
- **Outro CTA:** shadowedgetools.com

<<<<FILE: 03-blog-post.md>>>>
Blog

<<<<FILE: 04-email.md>>>>
Email

<<<<FILE: 05-reddit-post.md>>>>
Reddit

<<<<FILE: 06-linkedin-post.md>>>>
LinkedIn
"""
    second_pass = """<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "The $973 Line"
- **Score:** 9/10
- **Source:** 00:00:40 -> 00:01:10 (30 seconds)
- **Hook (first 2 sec):** "$973 to liquidation changes the trade."
- **Why this clip works:** Concrete account-risk stakes.
- **Source quote:** We have $973 to liquidation and this setup risks $200.
- **On-screen text:** $973 LEFT
- **Outro CTA:** shadowedgetools.com

---

### Short 2 - "Cancel When the Edge Dies"
- **Score:** 9/10
- **Source:** 00:02:10 -> 00:02:40 (30 seconds)
- **Hook (first 2 sec):** "The setup invalidated, so the order is gone."
- **Why this clip works:** A clear trading decision with Bracket Boss discipline stakes.
- **Source quote:** Price cracked the level, setup invalidated, cancel the order.
- **On-screen text:** EDGE GONE = ORDER GONE
- **Outro CTA:** Full session at shadowedgetools.com

---

### Short 3 - "Bracket Boss Before the Click"
- **Score:** 8/10
- **Source:** 00:04:00 -> 00:04:32 (32 seconds)
- **Hook (first 2 sec):** "Entry, stop, and target are set before the click."
- **Why this clip works:** Shows Bracket Boss as process control.
- **Source quote:** Bracket Boss has the stop target and entry already mapped.
- **On-screen text:** PLAN BEFORE THE ORDER
- **Outro CTA:** shadowedgetools.com

---

### Short 4 - "Break Even at One to One"
- **Score:** 8/10
- **Source:** 00:05:50 -> 00:06:20 (30 seconds)
- **Hook (first 2 sec):** "Counter-trend means break even at one to one."
- **Why this clip works:** A reusable trading rule tied to risk control.
- **Source quote:** This is counter trend, so break even goes at one to one.
- **On-screen text:** COUNTER-TREND RULE
- **Outro CTA:** Full breakdown at shadowedgetools.com

---

### Short 5 - "Do Not Interfere"
- **Score:** 8/10
- **Source:** 00:07:10 -> 00:07:42 (32 seconds)
- **Hook (first 2 sec):** "Once the bracket is placed, your job is not interfering."
- **Why this clip works:** Emotional discipline plus Bracket Boss automation.
- **Source quote:** Once the order is placed I can walk away and let it manage.
- **On-screen text:** SET IT. DO NOT TOUCH IT.
- **Outro CTA:** shadowedgetools.com

---

### Short 6 - "Filled Means Hands Off"
- **Score:** 9/10
- **Source:** 00:09:00 -> 00:09:32 (32 seconds)
- **Hook (first 2 sec):** "The trade filled; now doing nothing is the test."
- **Why this clip works:** Real Bracket Boss post-entry tension with discipline payoff.
- **Source quote:** We are filled now and I am not touching the stop.
- **On-screen text:** FILLED. HANDS OFF.
- **Outro CTA:** Watch the full trade at shadowedgetools.com

---

### Short 7 - "Discipline Score Moved"
- **Score:** 9/10
- **Source:** 00:10:40 -> 00:11:12 (32 seconds)
- **Hook (first 2 sec):** "The score moved because the plan was followed."
- **Why this clip works:** Drawdown Guardian provides feedback on behavior.
- **Source quote:** Drawdown Guardian moved the discipline score to 53 automatically.
- **On-screen text:** DISCIPLINE SCORE: 53
- **Outro CTA:** shadowedgetools.com

---

### Short 8 - "Target Hit, Stop Untouched"
- **Score:** 9/10
- **Source:** 00:12:05 -> 00:12:35 (30 seconds)
- **Hook (first 2 sec):** "Target hit, and the stop never moved."
- **Why this clip works:** A clean Bracket Boss before-after payoff.
- **Source quote:** The target filled and I did not touch the stop.
- **On-screen text:** TARGET HIT / STOP UNTOUCHED
- **Outro CTA:** Full session at shadowedgetools.com

---

### Short 9 - "Liquidation Updates Live"
- **Score:** 8/10
- **Source:** 00:14:10 -> 00:14:42 (32 seconds)
- **Hook (first 2 sec):** "The liquidation number updates after the trade."
- **Why this clip works:** Concrete Drawdown Guardian account-safety payoff.
- **Source quote:** The liquidation line adjusted after the trade without manual math.
- **On-screen text:** LIQUIDATION UPDATES LIVE
- **Outro CTA:** shadowedgetools.com

---

### Short 10 - "Fewer Bad Decisions"
- **Score:** 9/10
- **Source:** 00:15:30 -> 00:16:05 (35 seconds)
- **Hook (first 2 sec):** "The product is fewer bad decisions when the market gets loud."
- **Why this clip works:** Turns features into the buyer outcome.
- **Source quote:** The edge is fewer bad decisions when the market gets loud.
- **On-screen text:** FEWER BAD DECISIONS
- **Outro CTA:** See how it works at shadowedgetools.com
"""

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            text = first_pass if self.calls == 1 else second_pass
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=20),
            )

    class FakeAnthropic:
        messages_obj = None

        def __init__(self, api_key):
            self.messages = FakeMessages()
            FakeAnthropic.messages_obj = self.messages

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    srt = tmp_path / "source.srt"
    srt.write_text(
        "\n\n".join([
            "1\n00:00:40,000 --> 00:00:48,000\nWe have $973 to liquidation and this setup risks $200.\n",
            "2\n00:02:10,000 --> 00:02:18,000\nPrice cracked the level, setup invalidated, cancel the order.\n",
            "3\n00:04:00,000 --> 00:04:08,000\nBracket Boss has the stop target and entry already mapped.\n",
            "4\n00:05:50,000 --> 00:05:58,000\nThis is counter trend, so break even goes at one to one.\n",
            "5\n00:07:10,000 --> 00:07:18,000\nOnce the order is placed I can walk away and let it manage.\n",
            "6\n00:09:00,000 --> 00:09:08,000\nWe are filled now and I am not touching the stop.\n",
            "7\n00:10:40,000 --> 00:10:48,000\nDrawdown Guardian moved the discipline score to 53 automatically.\n",
            "8\n00:12:05,000 --> 00:12:13,000\nThe target filled and I did not touch the stop.\n",
            "9\n00:14:10,000 --> 00:14:18,000\nThe liquidation line adjusted after the trade without manual math.\n",
            "10\n00:15:30,000 --> 00:15:38,000\nThe edge is fewer bad decisions when the market gets loud.\n",
            "11\n00:17:00,000 --> 00:17:40,000\nFinal recap of the trading product workflow.\n",
        ]),
        encoding="utf-8",
    )

    result = cm.process_transcript(srt, slug="quality-test", editor_guidance="")

    assert result.error is None
    assert FakeAnthropic.messages_obj.calls == 2
    final_shorts = (result.out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    assert "The $973 Line" in final_shorts
    assert "Weak Early Clip" not in final_shorts
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["shorts_editor_pass"] is True
    assert meta["shorts_editor"]["candidate_count"] > 0
    assert (result.out_dir / "00-shorts-editor-response.md").exists()


def test_process_transcript_records_failed_shorts_editor_metadata(tmp_path, monkeypatch):
    first_pass = """<<<<FILE: 01-x-thread.md>>>>
Thread

<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "Weak Early Clip"
- **Score:** 4/10
- **Source:** 00:00:00 -> 00:00:30 (30 seconds)
- **Hook (first 2 sec):** Weak setup.
- **Why this clip works:** It does not.
- **Source quote:** Welcome back to the channel with generic setup.
- **On-screen text:** INTRO
- **Outro CTA:** shadowedgetools.com
"""
    bad_second_pass = """<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "Still Weak"
- **Score:** 4/10
- **Source:** 00:00:30 -> 00:00:35 (5 seconds)
- **Hook (first 2 sec):** Weak.
"""

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            text = first_pass if self.calls == 1 else bad_second_pass
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=11, output_tokens=22),
            )

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    srt = tmp_path / "source.srt"
    srt.write_text(
        "1\n00:00:30,000 --> 00:00:45,000\nBracket Boss maps the entry stop target and risk before the click.\n",
        encoding="utf-8",
    )

    result = cm.process_transcript(srt, slug="failed-quality-test", editor_guidance="")

    assert result.error.startswith("Shorts editor pass failed:")
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["shorts_editor_pass"] is False
    assert meta["shorts_editor_status"] == "failed"
    assert "Shorts editor validation failed" in meta["shorts_editor_error"]
    assert meta["shorts_editor"]["validation_errors"]
    assert meta["shorts_editor"]["repair"]["succeeded"] is False
    assert (result.out_dir / "00-shorts-editor-response.md").exists()
    assert (result.out_dir / "00-shorts-editor-rejected.md").exists()
    assert meta["files"] == ["01-x-thread.md", "02-shorts-scripts.md"]
    assert meta["usage"] == {"input_tokens": 11, "output_tokens": 22}
    assert "run_completed_at" in meta


def test_process_transcript_keeps_first_pass_when_shorts_editor_is_overloaded(tmp_path, monkeypatch):
    first_pass = """<<<<FILE: 01-x-thread.md>>>>
Thread

<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "First Pass Clip"
- **Score:** 8/10
- **Source:** 00:00:30 -> 00:01:00 (30 seconds)
- **Hook (first 2 sec):** Risk is visible before the trade.
- **Why this clip works:** It shows the account protection moment clearly.
- **Source quote:** Bracket Boss maps the entry stop target and risk before the click.
- **On-screen text:** RISK BEFORE ENTRY
- **Outro CTA:** shadowedgetools.com
"""

    class FakeOverloadedError(Exception):
        status_code = 529

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=first_pass)],
                    usage=SimpleNamespace(input_tokens=11, output_tokens=22),
                )
            raise FakeOverloadedError("Error code: 529 - overloaded_error: Overloaded")

    class FakeAnthropic:
        messages_obj = None

        def __init__(self, api_key):
            self.messages = FakeMessages()
            FakeAnthropic.messages_obj = self.messages

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "LLM_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cm, "LLM_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(cm, "LLM_RETRY_MAX_SECONDS", 0.0)
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    srt = tmp_path / "source.srt"
    srt.write_text(
        "1\n00:00:30,000 --> 00:01:00,000\nBracket Boss maps the entry stop target and risk before the click.\n",
        encoding="utf-8",
    )

    result = cm.process_transcript(srt, slug="editor-overload", editor_guidance="")

    assert result.error is None
    assert FakeAnthropic.messages_obj.calls == 3
    assert result.files == ["01-x-thread.md", "02-shorts-scripts.md"]
    assert "First Pass Clip" in (result.out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["shorts_editor_pass"] is False
    assert meta["shorts_editor_status"] == "failed_nonfatal"
    assert meta["shorts_editor_api_attempts"] == 2
    assert "Continuing with first-pass shorts" in meta["shorts_editor_note"]
    assert meta["usage"] == {"input_tokens": 11, "output_tokens": 22}


def test_validate_shorts_script_rejects_enough_clips_without_quality_fields():
    bad = "\n\n---\n\n".join(
        f"""### Short {i} - "Generic Clip {i}"
- **Source:** 00:{i:02d}:00 -> 00:{i:02d}:30 (30 seconds)
- **Hook (first 2 sec):** Generic trading topic with no edge.
- **On-screen text:** MARKET UPDATE
- **Outro CTA:** shadowedgetools.com
"""
        for i in range(1, 11)
    )

    errors = cm.validate_shorts_script(bad, duration_sec=17 * 60)

    assert not any(error.startswith("only 10 shorts") for error in errors)
    assert any("missing Score" in error for error in errors)
    assert any("missing Why this clip works" in error for error in errors)
    assert any("missing Source quote" in error for error in errors)


def _commercial_short_block(
    idx: int,
    start_sec: int,
    *,
    title: str | None = None,
    score: str = "9/10",
    hook: str | None = None,
    why: str | None = None,
    quote: str | None = None,
    on_screen: str | None = None,
    cta: str | None = "See the full workflow at shadowedgetools.com",
) -> str:
    title = title or f"Risk Control Moment {idx}"
    hook = hook or f"Risk rule {idx} changes the next trading decision."
    why = why or "Bracket Boss and Drawdown Guardian make the risk-control payoff visible before emotion takes over."
    quote = quote or f"Bracket Boss maps the stop target and risk before decision number {idx}."
    on_screen = on_screen or f"RISK RULE #{idx}"
    lines = [
        f'### Short {idx} - "{title}"',
        f"- **Score:** {score}",
        f"- **Source:** {TranscriptSegment.fmt_ts(start_sec)} -> {TranscriptSegment.fmt_ts(start_sec + 30)} (30 seconds)",
        f'- **Hook (first 2 sec):** "{hook}"',
        f"- **Why this clip works:** {why}",
        f"- **Source quote:** {quote}",
        f"- **On-screen text:** {on_screen}",
    ]
    if cta is not None:
        lines.append(f"- **Outro CTA:** {cta}")
    return "\n".join(lines)


def _commercial_shorts_script(overrides: dict[int, dict] | None = None) -> str:
    starts = [40, 130, 250, 390, 470, 560, 650, 740, 850, 930]
    overrides = overrides or {}
    return "\n\n---\n\n".join(
        _commercial_short_block(idx, start, **overrides.get(idx, {}))
        for idx, start in enumerate(starts, start=1)
    )


def test_validate_shorts_script_accepts_high_quality_commercial_shorts():
    errors = cm.validate_shorts_script(
        _commercial_shorts_script(),
        duration_sec=17 * 60,
    )

    assert errors == []


def test_repair_shorts_script_drops_low_score_but_keeps_overlapping_editor_picks():
    starts = [40, 45, 130, 220, 310, 400, 490, 580, 670, 760, 850, 940]
    bad = "\n\n---\n\n".join(
        _commercial_short_block(
            idx,
            start,
            score="6/10" if idx == 12 else "9/10",
        )
        for idx, start in enumerate(starts, start=1)
    )

    repaired, meta = cm.repair_shorts_script(bad, duration_sec=17 * 60)

    assert meta["succeeded"] is True
    assert meta["original_count"] == 12
    assert meta["repaired_count"] == 11
    assert any("score 6/10" in note for note in meta["notes"])
    assert not any("overlaps stronger short" in note for note in meta["notes"])
    assert cm.validate_shorts_script(repaired, duration_sec=17 * 60) == []
    assert "### Short 11" in repaired
    assert "### Short 12" not in repaired
    assert "6/10" not in repaired


def test_repair_shorts_script_drops_thin_hook_and_low_score_but_keeps_overlap():
    starts = [40, 45, 130, 220, 310, 400, 490, 580, 670, 760, 850, 940]
    bad = "\n\n---\n\n".join(
        _commercial_short_block(
            idx,
            start,
            score="6/10" if idx == 12 else "9/10",
            hook="Wait." if idx == 4 else None,
        )
        for idx, start in enumerate(starts, start=1)
    )

    repaired, meta = cm.repair_shorts_script(bad, duration_sec=17 * 60)

    assert meta["succeeded"] is True
    assert meta["original_count"] == 12
    assert meta["repaired_count"] == 10
    assert any("score 6/10" in note for note in meta["notes"])
    assert any("hook is too thin" in note for note in meta["notes"])
    assert not any("overlaps stronger short" in note for note in meta["notes"])
    assert cm.validate_shorts_script(repaired, duration_sec=17 * 60) == []
    assert "Wait." not in repaired
    assert "6/10" not in repaired


def test_repair_commercial_qa_script_drops_weak_commercial_clip_when_enough_remain():
    starts = [40, 45, 130, 220, 310, 400, 490, 580, 670, 760, 850]
    bad = "\n\n---\n\n".join(
        _commercial_short_block(
            idx,
            start,
            hook="In this video we talk about trading today." if idx == 4 else None,
        )
        for idx, start in enumerate(starts, start=1)
    )

    repaired, meta = cm.repair_commercial_qa_script(bad, duration_sec=17 * 60)

    assert meta["succeeded"] is True
    assert meta["original_count"] == 11
    assert meta["repaired_count"] == 10
    assert meta["dropped_indexes"] == [4]
    assert cm.validate_shorts_script(repaired, duration_sec=17 * 60) == []
    assert cm.commercial_qa_report(repaired, duration_sec=17 * 60)["passed"] is True
    assert "In this video we talk about trading today" not in repaired


def test_process_transcript_repairs_shorts_editor_validation_errors(tmp_path, monkeypatch):
    first_pass = """<<<<FILE: 02-shorts-scripts.md>>>>
### Short 1 - "Initial Draft"
- **Score:** 9/10
- **Source:** 00:00:40 -> 00:01:10 (30 seconds)
- **Hook (first 2 sec):** Risk rule one changes before the click.
- **Why this clip works:** Bracket Boss and Drawdown Guardian make risk visible.
- **Source quote:** Bracket Boss maps the stop target and risk before decision one.
- **On-screen text:** RISK RULE 1
- **Outro CTA:** shadowedgetools.com
"""
    starts = [40, 45, 130, 220, 310, 400, 490, 580, 670, 760, 850, 940]
    repairable_shorts = "\n\n---\n\n".join(
        _commercial_short_block(
            idx,
            start,
            score="6/10" if idx == 12 else "9/10",
            hook="Wait." if idx == 4 else None,
        )
        for idx, start in enumerate(starts, start=1)
    )
    second_pass = f"<<<<FILE: 02-shorts-scripts.md>>>>\n{repairable_shorts}"

    class FakeMessages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            text = first_pass if self.calls == 1 else second_pass
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=11, output_tokens=22),
            )

    class FakeAnthropic:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    fake_mod = ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cm, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cm, "EDITOR_GUIDANCE_PATH", tmp_path / "state" / "editor-guidance.json")
    cm.CONTENT_DIR.mkdir()
    cm.CONFIG_DIR.mkdir()
    cm.STATE_DIR.mkdir()
    (cm.CONFIG_DIR / "brand-voice.md").write_text("direct trader voice", encoding="utf-8")
    (cm.CONFIG_DIR / "guardrails.md").write_text("no financial advice", encoding="utf-8")
    srt = tmp_path / "source.srt"
    srt.write_text(
        "1\n00:17:00,000 --> 00:17:40,000\nBracket Boss maps the stop target and risk before the click.\n",
        encoding="utf-8",
    )

    result = cm.process_transcript(srt, slug="repair-quality-test", editor_guidance="")

    assert result.error is None
    final_shorts = (result.out_dir / "02-shorts-scripts.md").read_text(encoding="utf-8")
    assert cm.validate_shorts_script(final_shorts, duration_sec=17 * 60 + 40) == []
    assert "6/10" not in final_shorts
    assert "### Short 10" in final_shorts
    meta = json.loads((result.out_dir / "00-meta.json").read_text(encoding="utf-8"))
    assert meta["shorts_editor_pass"] is True
    assert meta["shorts_editor_status"] == "repaired"
    assert meta["shorts_editor"]["repaired"] is True
    assert meta["shorts_editor"]["repair"]["succeeded"] is True
    assert not any("overlaps stronger short" in note for note in meta["shorts_editor"]["repair"]["notes"])


@pytest.mark.parametrize(
    ("case_overrides", "expected"),
    [
        ({1: {"score": "6/10"}}, "score must be 7/10 or better"),
        (
            {
                2: {
                    "score": "6/10",
                    "hook": "Risk-free guaranteed profits are the product promise.",
                    "why": "This is a compliance-risk commercial claim and should not clear the score threshold.",
                    "quote": "Bracket Boss guarantees profit when the market gets loud.",
                }
            },
            "score must be 7/10 or better",
        ),
        ({3: {"cta": None}}, "missing Outro CTA"),
        (
            {
                4: {
                    "title": "Risk Control Moment 1",
                    "quote": "Bracket Boss maps the stop target and risk before decision number 1.",
                }
            },
            "duplicate",
        ),
    ],
)
def test_validate_shorts_script_rejects_commercial_qa_failures(case_overrides, expected):
    errors = cm.validate_shorts_script(
        _commercial_shorts_script(case_overrides),
        duration_sec=17 * 60,
    )

    assert any(expected in error for error in errors)


def test_commercial_qa_report_approves_release_ready_shorts():
    report = cm.commercial_qa_report(_commercial_shorts_script(), duration_sec=17 * 60)

    assert report["passed"] is True
    assert report["approved_count"] == 10
    assert report["manual_review_count"] == 0
    assert report["failures"] == []
    assert all(clip["score"] >= 85 for clip in report["clips"])
    assert set(report["clips"][0]["category_scores"]) == {
        "hook",
        "clarity",
        "product_proof",
        "visual_quality",
        "audio_captions",
        "cta",
        "compliance",
        "uniqueness",
    }


def test_commercial_qa_report_sends_weak_or_risky_shorts_to_review():
    report = cm.commercial_qa_report(
        _commercial_shorts_script({
            2: {
                "title": "Generic Market Update",
                "hook": "Welcome back before we begin this generic overview.",
                "why": "Generic summary.",
                "quote": "This risk free setup guarantees profit every day.",
                "on_screen": "MARKET UPDATE WITH A VERY LONG AND UNCLEAR LINE THAT WILL NOT FIT WELL",
                "cta": None,
            }
        }),
        duration_sec=17 * 60,
    )

    clip = report["clips"][1]
    assert report["passed"] is False
    assert report["manual_review_count"] == 1
    assert clip["status"] == "manual_review"
    assert clip["score"] < 85
    assert any("compliance risk" in note for note in clip["notes"])
    assert any("commercial QA score" in failure for failure in report["failures"])
