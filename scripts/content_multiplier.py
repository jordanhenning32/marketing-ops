"""Shadow Edge Tools — Content Multiplier.

Takes one long-form transcript (Descript export) and produces a week of
cross-platform content drafts via the Anthropic API. Outputs are written
as markdown files inside `content/<week>-<slug>/`.

Supported transcript inputs:
- Plain text (`.txt`)  — no timestamps
- SubRip (`.srt`)      — timestamps preserved; Shorts output uses them
- Word doc (`.docx`)   — text extracted via python-docx if available

Run from the CLI:
    python scripts/content_multiplier.py path/to/transcript.srt \
        --slug discipline-score --product both

Or from the console (web UI uploads land here via `process_transcript()`).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Load env from repo/shared Shadow Edge locations on import.
try:
    from dotenv import load_dotenv
    _env_root = Path(__file__).resolve().parent.parent
    for candidate in (
        _env_root / ".env",
        _env_root.parent / ".ENV",
        _env_root.parent / ".env",
        Path("E:/futures-bot/.env"),
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
INCOMING_DIR = CONTENT_DIR / "incoming"
PROCESSED_DIR = CONTENT_DIR / "incoming" / ".processed"
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
# The Editor agent writes this from live YouTube results; it steers the next video.
EDITOR_GUIDANCE_PATH = STATE_DIR / "editor-guidance.json"

DEFAULT_MODEL = os.environ.get("CONTENT_MULTIPLIER_MODEL", "claude-sonnet-4-6")
PREMIUM_MODEL = os.environ.get("CONTENT_MULTIPLIER_PREMIUM_MODEL", "claude-opus-4-7")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_OUTPUT_TOKENS = _int_env("CONTENT_MULTIPLIER_MAX_TOKENS", 10000)
SHORTS_EDITOR_PASS = os.environ.get("CONTENT_MULTIPLIER_SHORTS_PASS", "1") != "0"
SHORTS_MAX_CANDIDATES = _int_env("CONTENT_MULTIPLIER_SHORTS_CANDIDATES", 72)
COMMERCIAL_QA_MIN_SCORE = _int_env("COMMERCIAL_QA_MIN_SCORE", 85)
LLM_MAX_ATTEMPTS = max(1, _int_env("CONTENT_MULTIPLIER_LLM_MAX_ATTEMPTS", 5))
LLM_RETRY_BASE_SECONDS = max(0.0, _float_env("CONTENT_MULTIPLIER_LLM_RETRY_BASE_SECONDS", 3.0))
LLM_RETRY_MAX_SECONDS = max(0.0, _float_env("CONTENT_MULTIPLIER_LLM_RETRY_MAX_SECONDS", 30.0))
_TRANSIENT_LLM_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMMessageCreateError(RuntimeError):
    def __init__(self, attempts: int, original: Exception):
        self.attempts = attempts
        self.original = original
        suffix = "" if attempts == 1 else "s"
        super().__init__(f"Anthropic request failed after {attempts} attempt{suffix}: {original}")


def _response_retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = None
    if headers is not None:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


def _is_retryable_llm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code in _TRANSIENT_LLM_STATUS_CODES:
        return True

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if response_status in _TRANSIENT_LLM_STATUS_CODES:
        return True

    body = getattr(exc, "body", None) or getattr(exc, "error", None)
    if isinstance(body, dict):
        error = body.get("error")
        error_type = error.get("type") if isinstance(error, dict) else body.get("type")
        if error_type in {"overloaded_error", "rate_limit_error"}:
            return True

    text = str(exc).lower()
    return (
        "overloaded_error" in text
        or ("overloaded" in text and "529" in text)
        or "rate_limit_error" in text
        or "temporarily unavailable" in text
    )


def _llm_retry_delay(attempt: int, exc: Exception) -> float:
    retry_after = _response_retry_after_seconds(exc)
    if retry_after is not None:
        return min(retry_after, LLM_RETRY_MAX_SECONDS)
    return min(LLM_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), LLM_RETRY_MAX_SECONDS)


def _create_message_with_retries(client, **kwargs):
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if attempt >= LLM_MAX_ATTEMPTS or not _is_retryable_llm_error(exc):
                raise LLMMessageCreateError(attempt, exc) from exc
            delay = _llm_retry_delay(attempt, exc)
            if delay > 0:
                time.sleep(delay)

    raise RuntimeError("unreachable LLM retry state")


def _is_transient_llm_failure(exc: Exception) -> bool:
    if isinstance(exc, LLMMessageCreateError):
        return _is_retryable_llm_error(exc.original)
    return _is_retryable_llm_error(exc)


# ---------------------------------------------------------------------------
# Transcript ingestion
# ---------------------------------------------------------------------------

@dataclass
class TranscriptSegment:
    start_sec: float
    end_sec: float
    text: str

    @staticmethod
    def fmt_ts(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class Transcript:
    text: str                        # full plain-text body
    segments: list[TranscriptSegment]  # empty if no timestamps
    source_filename: str
    duration_sec: float | None

    @property
    def has_timestamps(self) -> bool:
        return bool(self.segments)


@dataclass
class ClipCandidate:
    start_sec: float
    end_sec: float
    text: str
    score: int
    reasons: list[str]

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


_SRT_TIME_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*$"
)


def _ts_to_seconds(ts: str) -> float:
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 2:
        h = "0"
        m, rest = parts
    else:
        h, m, rest = parts
    s = float(rest)
    return int(h) * 3600 + int(m) * 60 + s


def parse_srt(raw: str) -> list[TranscriptSegment]:
    """Line-by-line SRT parser. Each block is:
        <index>\n<start --> end>\n<text lines>\n\n
    Avoids regex backtracking on multi-line bodies.
    """
    out: list[TranscriptSegment] = []
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return out

    # Split on blank-line boundaries (one or more)
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        # First line is usually an index; if it's a time line we use it directly.
        time_line = lines[1] if _SRT_TIME_RE.match(lines[1]) else (
            lines[0] if _SRT_TIME_RE.match(lines[0]) else None
        )
        if not time_line:
            continue
        text_lines = lines[lines.index(time_line) + 1:]
        text = " ".join(text_lines).strip()
        if not text:
            continue
        m = _SRT_TIME_RE.match(time_line)
        out.append(
            TranscriptSegment(
                start_sec=_ts_to_seconds(m.group(1)),
                end_sec=_ts_to_seconds(m.group(2)),
                text=text,
            )
        )
    return out


def load_transcript(path: Path) -> Transcript:
    """Detect format and load. Returns Transcript with segments populated for SRT."""
    ext = path.suffix.lower()
    raw = ""
    segments: list[TranscriptSegment] = []

    if ext == ".srt":
        raw = path.read_text(encoding="utf-8", errors="replace")
        segments = parse_srt(raw)
        text = " ".join(s.text for s in segments)
    elif ext == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError(
                "docx transcript requires python-docx. Run: pip install python-docx"
            ) from exc
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:  # .txt or anything else — treat as plain text
        text = path.read_text(encoding="utf-8", errors="replace")

    duration = segments[-1].end_sec if segments else None
    return Transcript(
        text=text,
        segments=segments,
        source_filename=path.name,
        duration_sec=duration,
    )


def srt_for_prompt(segments: list[TranscriptSegment], chunk_sec: float = 30.0) -> str:
    """Render SRT segments into compact `[hh:mm:ss] text` chunks for the LLM.

    Groups segments into ~30s chunks so the model sees coherent beats with
    timestamps without drowning in fragments.
    """
    chunks: list[str] = []
    cur_text: list[str] = []
    cur_start: float | None = None
    last_end: float = 0.0
    for seg in segments:
        if cur_start is None:
            cur_start = seg.start_sec
        cur_text.append(seg.text)
        last_end = seg.end_sec
        if (last_end - cur_start) >= chunk_sec:
            chunks.append(
                f"[{TranscriptSegment.fmt_ts(cur_start)} → {TranscriptSegment.fmt_ts(last_end)}] "
                + " ".join(cur_text)
            )
            cur_text, cur_start = [], None
    if cur_text and cur_start is not None:
        chunks.append(
            f"[{TranscriptSegment.fmt_ts(cur_start)} → {TranscriptSegment.fmt_ts(last_end)}] "
            + " ".join(cur_text)
        )
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# LLM call + output parsing
# ---------------------------------------------------------------------------

OUTPUT_SPEC = """\
Produce EXACTLY six sections, in order, each delimited like this:

<<<<FILE: 01-x-thread.md>>>>
(content for X thread)

<<<<FILE: 02-shorts-scripts.md>>>>
(content for Shorts scripts)

(... and so on for 03-blog-post.md, 04-email.md, 05-reddit-post.md, 06-linkedin-post.md)

No preamble before the first marker. No trailing commentary after the last section.

# Section specs

## 01-x-thread.md
A single X/Twitter thread, 8–12 tweets. Each tweet on its own line, prefixed
"1/", "2/", etc. Each tweet ≤ 270 characters. Hook tweet first. End with a
soft CTA pointing to ONE of: shadowedgetools.com, the lead magnet, or the
YouTube video. No emojis except 🎯 once at most.

## 02-shorts-scripts.md
Use the Shorts target count from the user prompt. The clips must be ranked from
strongest to weakest and drawn from the full transcript, not clustered near the
start. Target 20-45 seconds each; 15-60 seconds is acceptable when the moment
needs it. Do not use clips over 70 seconds.

Hard rejects:
- Generic intro/outro, housekeeping, platform navigation, or setup-only clips
- A clip that needs missing context from outside its timestamp range
- A clip whose hook is only a topic label instead of tension, stakes, surprise,
  objection, mistake avoided, or visible before/after payoff
- Duplicate clips that make the same point with different words

For each:

### Short N — "title"
- **Score:** N/10
- **Source:** HH:MM:SS → HH:MM:SS  (length seconds)
- **Hook (first 2 sec):** ...
- **Why this clip works:** one concrete reason tied to stakes/curiosity/payoff
- **Source quote:** exact spoken line or tight paraphrase from the timestamp
- **On-screen text:** ...
- **Outro CTA:** one short line

If the transcript has timestamps, use the EXACT timestamps from the source.
If not, write "Source: estimate <minute> mark" instead.

## 03-blog-post.md
1200–1500 word SEO blog post. H1 with long-tail keyword. 4–6 H2 sections.
Frontmatter:
---
title: ...
slug: ...
description: 150–160 chars meta description
target_keyword: ...
---

End with one CTA paragraph.

## 04-email.md
Newsletter email. Three subject line options at top, then the body.
Format:
**Subject options:**
1. ...
2. ...
3. ...

---
(body, ~250 words, conversational, one CTA at the end)

## 05-reddit-post.md
A Reddit post for the most relevant subreddit. Format:
**Target subreddit:** r/...
**Title:** ...
---
(body, value-first, NO LINKS, NO product mentions unless the topic naturally
demands it. Plain text, no markdown formatting beyond paragraph breaks.)

## 06-linkedin-post.md
A 280–350 word LinkedIn post. Story-driven, business-angle take on the same
topic. Plain text, no headers.
"""


def load_editor_guidance() -> str:
    """Read the Editor agent's performance guidance (its `prompt_block`), if any.

    Returns "" when no guidance has been generated yet, so generation still works
    on day one before any YouTube results exist."""
    try:
        data = json.loads(EDITOR_GUIDANCE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return (data.get("prompt_block") or "").strip()


def shorts_target_range(duration_sec: float | None) -> tuple[int, int]:
    if not duration_sec or duration_sec <= 0:
        return (8, 10)
    if duration_sec < 5 * 60:
        return (4, 6)
    if duration_sec < 12 * 60:
        return (6, 8)
    if duration_sec < 25 * 60:
        return (10, 12)
    return (12, 16)


def shorts_selection_brief(transcript: Transcript) -> str:
    target_min, target_max = shorts_target_range(transcript.duration_sec)
    lines = [
        "## Shorts selection brief",
        "",
        f"Shorts target: {target_min}-{target_max} ranked clips.",
        "Prioritize moments with visible stakes, a rule decision, order placed/canceled, mistake avoided,",
        "before/after contrast, emotional discipline, or a concrete product payoff.",
        "Avoid bland tutorial summaries. Each selected clip must work cold for a viewer who has not seen the long video.",
    ]
    if transcript.duration_sec:
        duration = transcript.duration_sec
        lines.insert(2, f"Source duration: {TranscriptSegment.fmt_ts(duration)} ({duration:.0f} seconds).")
        if duration >= 12 * 60:
            third = duration / 3
            lines.extend([
                "",
                "Timeline coverage requirement for this long recording:",
                f"- First third: 00:00:00 to {TranscriptSegment.fmt_ts(third)}",
                f"- Middle third: {TranscriptSegment.fmt_ts(third)} to {TranscriptSegment.fmt_ts(third * 2)}",
                f"- Final third: {TranscriptSegment.fmt_ts(third * 2)} to {TranscriptSegment.fmt_ts(duration)}",
                "- Include at least 3 viable clips from each third unless that third truly has no usable moment.",
                "- Include at least 4 clips that start after the halfway point.",
                "- Do not stop mining once the first few obvious moments are found.",
            ])
    return "\n".join(lines).strip()


_CLIP_SCORE_RULES: list[tuple[str, int, list[str]]] = [
    ("money/risk stakes", 5, ["$", "liquidation", "drawdown", "daily loss", "risk", "prop firm", "account"]),
    ("trade decision", 5, ["filled", "fill", "target", "stop", "cancel", "canceled", "invalid", "invalidated", "break even", "breakeven", "entry", "order"]),
    ("discipline tension", 4, ["discipline", "walk away", "don't touch", "do not touch", "force", "revenge", "against the trend", "no exceptions"]),
    ("product payoff", 4, ["bracket boss", "drawdown guardian", "auto", "automation", "score", "tracked", "managed"]),
    ("rule/lesson", 3, ["rule", "because", "that's why", "the reason", "what that means", "if ", "then ", "always", "never"]),
    ("specific numbers", 2, ["contracts", "points", "ticks", "percent", "%", "1:1", "2:1", "70%"]),
]
_CLIP_PENALTY_TERMS = [
    "welcome",
    "like and subscribe",
    "in this video",
    "let's get started",
    "make sure you",
    "before we begin",
]


def _has_intro_setup_opening(text: str) -> bool:
    opening = " ".join((text or "").lower().split()[:14])
    return any(term in opening for term in _CLIP_PENALTY_TERMS)


def _clip_score(text: str) -> tuple[int, list[str]]:
    lower = (text or "").lower()
    if _has_intro_setup_opening(text):
        return 0, ["intro/setup hard reject"]
    score = 0
    reasons: list[str] = []
    for label, weight, terms in _CLIP_SCORE_RULES:
        hits = [term for term in terms if term in lower]
        if hits:
            score += weight + min(2, len(hits) - 1)
            reasons.append(label)
    if re.search(r"\$\s*\d", text or ""):
        score += 4
        if "money/risk stakes" not in reasons:
            reasons.append("money/risk stakes")
    if re.search(r"\b\d+(\.\d+)?\s*(r|rr|contracts?|points?|ticks?)\b", lower):
        score += 2
        if "specific numbers" not in reasons:
            reasons.append("specific numbers")
    penalties = [term for term in _CLIP_PENALTY_TERMS if term in lower]
    if penalties:
        score -= 5
        reasons.append("intro/setup penalty")
    return min(20, max(0, score)), reasons[:5]


def _overlap_ratio(a: ClipCandidate, b: ClipCandidate) -> float:
    overlap = max(0.0, min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec))
    return overlap / max(1.0, min(a.duration_sec, b.duration_sec))


def mine_clip_candidates(
    transcript: Transcript,
    *,
    max_candidates: int = SHORTS_MAX_CANDIDATES,
) -> list[ClipCandidate]:
    """Free local first pass: find likely Shorts moments before spending LLM taste.

    The miner intentionally over-selects. The dedicated Shorts editor pass gets
    this ranked list plus the transcript, then makes the final taste call.
    """
    if not transcript.segments:
        return []

    raw: list[ClipCandidate] = []
    segments = transcript.segments
    for i in range(0, len(segments), 2):
        start = segments[i].start_sec
        parts: list[str] = []
        end = start
        emitted = False
        for j in range(i, len(segments)):
            end = segments[j].end_sec
            duration = end - start
            if duration > 68:
                break
            parts.append(segments[j].text)
            if duration < 18:
                continue
            text = " ".join(parts).strip()
            if not text:
                continue
            score, reasons = _clip_score(text)
            if duration >= 24 and score > 0:
                # Prefer natural-ish endings, but do not require punctuation from
                # speech-to-text output.
                natural_end = text[-1:] in ".?!" or duration >= 36
                if natural_end:
                    raw.append(ClipCandidate(start, end, text, score, reasons))
                    emitted = True
                    break
        if not emitted:
            single_text = segments[i].text.strip()
            score, reasons = _clip_score(single_text)
            if score > 0:
                duration_limit = transcript.duration_sec or segments[-1].end_sec
                expanded_start = max(0.0, min(segments[i].start_sec, max(0.0, duration_limit - 24.0)))
                expanded_end = min(duration_limit, max(segments[i].end_sec + 12.0, expanded_start + 24.0))
                if expanded_end - expanded_start >= 15:
                    range_text = " ".join(
                        seg.text for seg in segments
                        if seg.end_sec > expanded_start and seg.start_sec < expanded_end
                    ).strip() or single_text
                    raw.append(ClipCandidate(expanded_start, expanded_end, range_text, score, reasons))

    duration = transcript.duration_sec or (segments[-1].end_sec if segments else 0)
    ranked = sorted(raw, key=lambda c: (c.score, c.duration_sec), reverse=True)

    def add_candidate(selected: list[ClipCandidate], cand: ClipCandidate) -> bool:
        if any(_overlap_ratio(cand, existing) > 0.55 for existing in selected):
            return False
        selected.append(cand)
        return True

    selected: list[ClipCandidate] = []
    if duration >= 12 * 60:
        third = duration / 3
        quota = min(3, max(1, max_candidates // 3))
        # Reserve slots for each third before filling by global score. This keeps
        # a dense opening from starving later trade/payoff moments.
        for idx in range(3):
            lo = idx * third
            hi = duration if idx == 2 else (idx + 1) * third
            pool = [c for c in ranked if lo <= c.start_sec < hi and c not in selected]
            for cand in pool:
                add_candidate(selected, cand)
                existing = sum(1 for c in selected if lo <= c.start_sec < hi)
                if existing >= quota or len(selected) >= max_candidates:
                    break
    for cand in ranked:
        if len(selected) >= max_candidates:
            break
        add_candidate(selected, cand)

    return sorted(selected, key=lambda c: c.score, reverse=True)[:max_candidates]


def _snippet(text: str, limit: int = 420) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def clip_candidates_for_prompt(candidates: list[ClipCandidate]) -> str:
    if not candidates:
        return "No local candidates were mined; use the transcript directly."
    lines = []
    for idx, cand in enumerate(candidates, start=1):
        reasons = ", ".join(cand.reasons) if cand.reasons else "speech moment"
        score = min(cand.score, 20)
        lines.append(
            f"C{idx:02d} | score={score}/20 | "
            f"{TranscriptSegment.fmt_ts(cand.start_sec)} -> {TranscriptSegment.fmt_ts(cand.end_sec)} "
            f"({cand.duration_sec:.0f}s) | reasons: {reasons}\n"
            f"quote: {_snippet(cand.text)}"
        )
    return "\n\n".join(lines)


def build_shorts_editor_prompts(
    transcript: Transcript,
    *,
    slug: str,
    product: str,
    brand_voice: str,
    guardrails: str,
    editor_guidance: str = "",
    creator_guidance: str = "",
    candidates: list[ClipCandidate] | None = None,
) -> tuple[str, str]:
    if not transcript.segments:
        raise ValueError("Shorts editor pass requires a timestamped transcript")
    candidates = candidates if candidates is not None else mine_clip_candidates(transcript)
    system = f"""\
You are the senior short-form editor for Shadow Edge Tools. Your only job is to
select the highest-quality clips from one timestamped trading video.

You are ruthless. A clip must work cold on Shorts/Reels/TikTok: clear stakes in
the first two seconds, one idea, visible/understandable payoff, no bland setup,
no duplicate angles, no generic tutorial summaries.

# BRAND VOICE
{brand_voice}

# HARD GUARDRAILS
{guardrails}

# OUTPUT FORMAT
Return exactly one section and no commentary:

<<<<FILE: 02-shorts-scripts.md>>>>

For each selected clip:

### Short N - "title"
- **Score:** N/10
- **Source:** HH:MM:SS -> HH:MM:SS (length seconds)
- **Hook (first 2 sec):** ...
- **Why this clip works:** ...
- **Source quote:** ...
- **On-screen text:** ...
- **Outro CTA:** ...
"""
    guidance_block = ""
    if creator_guidance.strip():
        guidance_block += (
            "## Creator direction for this run\n\n"
            "Follow this direction when choosing Shorts, hooks, titles, and CTAs unless it conflicts with guardrails.\n\n"
            f"{creator_guidance.strip()}\n\n"
        )
    if editor_guidance.strip():
        guidance_block += "## Performance guidance\n\n" + editor_guidance.strip() + "\n\n"
    user = f"""\
Topic slug: {slug}
Target product focus: {product}

{guidance_block}{shorts_selection_brief(transcript)}

## Local candidate moments

These were mined for stakes, decisions, rules, product payoff, and specific
numbers. Prefer the best candidates, but you may adjust boundaries by up to
10 seconds if the transcript shows a cleaner hook or payoff nearby.

{clip_candidates_for_prompt(candidates)}

## Source transcript with timestamps

{srt_for_prompt(transcript.segments, chunk_sec=20.0)}
"""
    return system, user


def build_prompts(
    transcript: Transcript,
    *,
    slug: str,
    product: str,
    brand_voice: str,
    guardrails: str,
    editor_guidance: str = "",
    creator_guidance: str = "",
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt)."""
    system = f"""\
You are the Content Multiplier for Shadow Edge Tools, a NinjaTrader 8 add-on
business sold to futures prop traders. Your job is to take ONE long-form
transcript Jordan recorded and produce a week of cross-platform content
drafts in his voice.

# BRAND VOICE
{brand_voice}

# HARD GUARDRAILS — these are non-negotiable
{guardrails}

# OUTPUT FORMAT
{OUTPUT_SPEC}
"""

    if transcript.has_timestamps:
        body_block = "## Source transcript (with timestamps)\n\n" + srt_for_prompt(transcript.segments)
        ts_note = "Use the exact timestamps from the transcript in the Shorts section."
    else:
        body_block = "## Source transcript (no timestamps)\n\n" + transcript.text
        ts_note = "No timestamps were provided. For Shorts, estimate clip boundaries based on content beats."

    guidance_block = ""
    if creator_guidance.strip():
        guidance_block += (
            "## Creator direction for this run\n\n"
            "Follow this direction when selecting Shorts, writing hooks/titles, and framing the supporting content unless it conflicts with guardrails.\n\n"
            f"{creator_guidance.strip()}\n\n"
        )
    if editor_guidance.strip():
        guidance_block += (
            "## Performance guidance — weight Shorts selection, titles, and hooks toward this\n\n"
            f"{editor_guidance.strip()}\n"
        )

    user = f"""\
Topic slug: {slug}
Target product focus: {product}
Source filename: {transcript.source_filename}

{ts_note}

{guidance_block}{shorts_selection_brief(transcript)}

{body_block}
"""
    return system, user


_FILE_MARKER_RE = re.compile(r"<<<<FILE:\s*(?P<name>[\w\-.]+)\s*>>>>")
_SHORTS_BLOCK_RE = re.compile(
    r"###\s*(?P<title>Short[^\n]*?)\n"
    r"(?P<body>.*?)(?=^###\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_SHORT_SOURCE_RE = re.compile(
    r"Source:\*{0,2}\s*(\d{2}:\d{2}:\d{2})\s*(?:[→\->]+|to)\s*(\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)
_SHORT_SOURCE_RE = re.compile(
    r"Source:\*{0,2}\s*\[?((?:\d{2}:)?\d{2}:\d{2})\]?\s*(?:[^\w\s]+|to)\s*\[?((?:\d{2}:)?\d{2}:\d{2})\]?",
    re.IGNORECASE,
)

_REQUIRED_SHORT_FIELDS = [
    "Score",
    "Source",
    "Hook",
    "Why this clip works",
    "Source quote",
    "On-screen text",
    "Outro CTA",
]
_PRODUCT_OR_PAYOFF_TERMS = [
    "bracket boss", "drawdown guardian", "liquidation", "discipline score",
    "target", "stop", "cancel", "canceled", "invalid", "invalidated",
    "break even", "breakeven", "filled", "entry", "risk", "drawdown",
    "managed", "automatic", "automation", "edge", "bad decision",
]
_COMMERCIAL_QA_WEIGHTS = {
    "hook": 15,
    "clarity": 12,
    "product_proof": 15,
    "visual_quality": 12,
    "audio_captions": 10,
    "cta": 10,
    "compliance": 14,
    "uniqueness": 12,
}
_HOOK_STAKE_TERMS = [
    "liquidation", "risk", "stop", "target", "invalid", "cancel", "filled",
    "break even", "drawdown", "discipline", "bad decision", "edge", "account",
    "counter-trend", "counter trend", "market gets loud", "before the click",
    "bracket", "score moved", "plan was followed",
]
_PRODUCT_TERMS = [
    "bracket boss", "drawdown guardian", "shadow edge", "tool", "tools",
    "product", "feature", "automation", "automatic", "discipline score",
]
_PROOF_ACTION_TERMS = [
    "maps", "mapped", "updates", "adjusted", "moved", "filled", "placed",
    "invalidated", "cancel", "canceled", "managed", "protect", "control",
    "walk away", "hands off", "before", "after", "without manual",
]
_GENERIC_CLIP_TERMS = [
    "market update", "generic", "overview", "talking about", "quick tip",
    "welcome back", "getting started", "before we begin", "like and subscribe",
    "in this video", "we talk about",
]
_FILLER_OR_DEAD_AIR_TERMS = [
    "welcome back", "before we begin", "make sure to like", "subscribe",
    "um", "uh", "pause", "wait a second", "hang on", "let me pull this up",
    "in this video", "we talk about",
]
_COMPLIANCE_RISK_TERMS = [
    "guaranteed profit", "guarantee profit", "risk free", "no risk",
    "can't lose", "cannot lose", "will make money", "make you profitable",
    "guaranteed funded", "pass your eval", "pass the eval", "daily profits",
    "profit every day", "secret strategy", "guarantees profit", "risk-free",
]


def _short_field(body_md: str, label: str) -> str:
    label_re = re.escape(label)
    patterns = [
        rf"^\s*-\s*\*\*{label_re}[^:]*:\*\*\s*(.+)$",
        rf"^\s*-\s*{label_re}[^:]*:\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_md, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def _norm_for_dupe(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _timestamp_parts_valid(ts: str) -> bool:
    parts = ts.split(":")
    if len(parts) == 2:
        m, ss = parts
    elif len(parts) == 3:
        _h, m, ss = parts
    else:
        return False
    return int(m) < 60 and int(ss) < 60


def _source_range(body_md: str) -> tuple[float, float] | None:
    match = _SHORT_SOURCE_RE.search(body_md)
    if not match:
        return None
    if not _timestamp_parts_valid(match.group(1)) or not _timestamp_parts_valid(match.group(2)):
        return None
    start = _ts_to_seconds(match.group(1))
    end = _ts_to_seconds(match.group(2))
    if end <= start:
        return None
    return start, end


def _word_count(value: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$]+", value or ""))


def _has_any(value: str, terms: list[str]) -> bool:
    haystack = (value or "").lower()
    return any(term in haystack for term in terms)


def _short_title_key(title: str) -> str:
    return _norm_for_dupe(re.sub(r"^Short\s*\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE))


def _score_commercial_clip(
    *,
    idx: int,
    title: str,
    body: str,
    duplicate_title: bool,
    duplicate_quote: bool,
    overlapping: bool,
) -> dict:
    title_clean = re.sub(r"^Short\s*\d+\s*[-:]\s*", "", title, flags=re.IGNORECASE).strip().strip('"')
    hook = _short_field(body, "Hook")
    why = _short_field(body, "Why this clip works")
    quote = _short_field(body, "Source quote")
    onscreen = _short_field(body, "On-screen text")
    cta = _short_field(body, "Outro CTA")
    editor_score_text = _short_field(body, "Score")
    source = _source_range(body)
    length = (source[1] - source[0]) if source else 0.0
    haystack = f"{title_clean}\n{body}".lower()
    notes: list[str] = []
    categories: dict[str, int] = {}

    hook_score = 0
    if _word_count(hook) >= 5:
        hook_score += 7
    else:
        notes.append("hook is too short or missing")
    hook_has_stake = _has_any(hook, _HOOK_STAKE_TERMS) or bool(re.search(r"[$\d]", hook or ""))
    if hook_has_stake:
        hook_score += 5
    else:
        notes.append("hook lacks a clear stake, number, or decision")
    if not _has_any(hook, _FILLER_OR_DEAD_AIR_TERMS):
        hook_score += 3
    else:
        notes.append("hook starts with setup or dead air")
    categories["hook"] = min(_COMMERCIAL_QA_WEIGHTS["hook"], hook_score)

    clarity_score = 0
    if _word_count(title_clean) >= 3:
        clarity_score += 3
    else:
        notes.append("title is too vague")
    if _word_count(why) >= 5:
        clarity_score += 4
    else:
        notes.append("why-this-works is too thin")
    if _word_count(quote) >= 6:
        clarity_score += 3
    else:
        notes.append("source quote is too thin")
    if not _has_any(f"{title_clean}\n{why}\n{quote}", _GENERIC_CLIP_TERMS):
        clarity_score += 2
    else:
        notes.append("clip reads generic or introductory")
    categories["clarity"] = min(_COMMERCIAL_QA_WEIGHTS["clarity"], clarity_score)

    proof_score = 0
    if _has_any(haystack, _PRODUCT_TERMS):
        proof_score += 6
    else:
        notes.append("missing explicit product/tool proof")
    if _has_any(haystack, _PRODUCT_OR_PAYOFF_TERMS):
        proof_score += 4
    else:
        notes.append("missing trading payoff or decision proof")
    if _has_any(haystack, _PROOF_ACTION_TERMS):
        proof_score += 3
    else:
        notes.append("missing visible action or before/after proof")
    if _has_any(f"{title_clean}\n{onscreen}", _HOOK_STAKE_TERMS + _PRODUCT_TERMS) or re.search(r"[$\d]", onscreen or ""):
        proof_score += 2
    else:
        notes.append("title/on-screen text does not surface the payoff")
    categories["product_proof"] = min(_COMMERCIAL_QA_WEIGHTS["product_proof"], proof_score)

    visual_score = 0
    if 18 <= length <= 55:
        visual_score += 4
    elif 15 <= length <= 60:
        visual_score += 3
    else:
        notes.append("source duration is outside the release-quality range")
    if onscreen:
        visual_score += 2
    else:
        notes.append("missing on-screen text")
    if onscreen and len(onscreen) <= 60 and 1 <= _word_count(onscreen) <= 9:
        visual_score += 3
    elif onscreen:
        notes.append("on-screen text is too long for a Short")
    if _has_any(f"{title_clean}\n{why}\n{onscreen}", _HOOK_STAKE_TERMS + _PRODUCT_TERMS + _PROOF_ACTION_TERMS):
        visual_score += 3
    else:
        notes.append("visual copy does not carry product or payoff context")
    categories["visual_quality"] = min(_COMMERCIAL_QA_WEIGHTS["visual_quality"], visual_score)

    audio_score = 0
    quote_words = _word_count(quote)
    if 6 <= quote_words <= 32:
        audio_score += 4
    else:
        notes.append("source quote is not caption-friendly")
    if not _has_any(f"{hook}\n{quote}", _FILLER_OR_DEAD_AIR_TERMS):
        audio_score += 3
    else:
        notes.append("audio likely includes filler or setup")
    if quote and not _has_any(quote, _GENERIC_CLIP_TERMS):
        audio_score += 2
    else:
        notes.append("source quote sounds generic")
    if length >= 20:
        audio_score += 1
    categories["audio_captions"] = min(_COMMERCIAL_QA_WEIGHTS["audio_captions"], audio_score)

    cta_score = 0
    if cta:
        cta_score += 3
    else:
        notes.append("missing outro CTA")
    if "shadowedgetools.com" in (cta or "").lower():
        cta_score += 4
    elif _has_any(cta, ["see how", "full workflow", "full breakdown", "full session", "watch the full", "channel"]):
        cta_score += 3
    else:
        notes.append("CTA does not point viewers to the product or next step")
    if not _has_any(cta, ["guaranteed", "profit", "risk free"]):
        cta_score += 3
    categories["cta"] = min(_COMMERCIAL_QA_WEIGHTS["cta"], cta_score)

    compliance_score = _COMMERCIAL_QA_WEIGHTS["compliance"]
    risky_terms = [term for term in _COMPLIANCE_RISK_TERMS if term in haystack]
    if risky_terms:
        compliance_score = 0
        notes.append("compliance risk: " + ", ".join(risky_terms[:3]))
    elif "financial advice" in haystack and "not financial advice" not in haystack:
        compliance_score = max(0, compliance_score - 6)
        notes.append("financial-advice language needs a disclaimer")
    categories["compliance"] = compliance_score

    uniqueness_score = _COMMERCIAL_QA_WEIGHTS["uniqueness"]
    if duplicate_title:
        uniqueness_score -= 5
        notes.append("duplicate title angle")
    if duplicate_quote:
        uniqueness_score -= 5
        notes.append("duplicate source quote")
    if overlapping:
        uniqueness_score -= 2
        notes.append("overlaps another selected Short; schedule on a different day")
    if _has_any(title_clean, _GENERIC_CLIP_TERMS):
        uniqueness_score -= 3
        notes.append("title angle is generic")
    categories["uniqueness"] = max(0, uniqueness_score)

    total = sum(categories.values())
    release_caps: list[int] = []
    score_match = re.search(r"\b(\d{1,2})\s*/\s*10\b", editor_score_text or "")
    if score_match and int(score_match.group(1)) < 7:
        notes.append("editor score is below release threshold")
        release_caps.append(74)
    if not cta:
        release_caps.append(84)
    if categories["compliance"] == 0:
        release_caps.append(60)
    if duplicate_title or duplicate_quote:
        release_caps.append(82)
    if not source or not (15 <= length <= 60):
        release_caps.append(80)
    if not hook_has_stake:
        release_caps.append(84)
    if categories["hook"] < 10:
        release_caps.append(84)
    if categories["product_proof"] < 10:
        release_caps.append(84)
    if release_caps:
        total = min(total, min(release_caps))
    status = "approved" if total >= COMMERCIAL_QA_MIN_SCORE else "manual_review"
    return {
        "index": idx,
        "title": title.strip(),
        "score": total,
        "threshold": COMMERCIAL_QA_MIN_SCORE,
        "status": status,
        "category_scores": categories,
        "notes": notes,
    }


def commercial_qa_report(
    shorts_md: str,
    *,
    duration_sec: float | None = None,
    min_score: int | None = None,
) -> dict:
    """Commercial release scorecard for Shorts scripts.

    The score is deterministic and intentionally conservative: a Short should
    have a cold-open hook, clear product/payoff proof, clean captions/CTA, no
    compliance risk, and a distinct angle before it is exported.
    """
    threshold = COMMERCIAL_QA_MIN_SCORE if min_score is None else int(min_score)
    blocks = list(_SHORTS_BLOCK_RE.finditer(shorts_md or ""))
    if not blocks:
        return {
            "threshold": threshold,
            "approved_count": 0,
            "manual_review_count": 0,
            "clips": [],
            "failures": ["no Shorts blocks found for commercial QA"],
            "passed": False,
        }

    title_counts: dict[str, int] = {}
    quote_counts: dict[str, int] = {}
    ranges: list[tuple[int, float, float]] = []
    for idx, block in enumerate(blocks, start=1):
        title_key = _short_title_key(block.group("title"))
        title_counts[title_key] = title_counts.get(title_key, 0) + 1
        quote_key = _norm_for_dupe(_short_field(block.group("body"), "Source quote"))
        if quote_key:
            quote_counts[quote_key] = quote_counts.get(quote_key, 0) + 1
        source = _source_range(block.group("body"))
        if source:
            ranges.append((idx, source[0], source[1]))

    overlapping_indexes: set[int] = set()
    for pos, (idx_a, start_a, end_a) in enumerate(ranges):
        for idx_b, start_b, end_b in ranges[pos + 1:]:
            overlap = max(0.0, min(end_a, end_b) - max(start_a, start_b))
            if overlap / max(1.0, min(end_a - start_a, end_b - start_b)) > 0.5:
                overlapping_indexes.update({idx_a, idx_b})

    clips: list[dict] = []
    failures: list[str] = []
    for idx, block in enumerate(blocks, start=1):
        body = block.group("body")
        title = block.group("title").strip()
        quote_key = _norm_for_dupe(_short_field(body, "Source quote"))
        clip = _score_commercial_clip(
            idx=idx,
            title=title,
            body=body,
            duplicate_title=title_counts.get(_short_title_key(title), 0) > 1,
            duplicate_quote=bool(quote_key and quote_counts.get(quote_key, 0) > 1),
            overlapping=idx in overlapping_indexes,
        )
        if clip["score"] < threshold:
            clip["status"] = "manual_review"
            failures.append(
                f"short {idx}: commercial QA score {clip['score']}/100 below {threshold}"
            )
        source = _source_range(body)
        if duration_sec and source and source[1] > duration_sec + 1:
            failures.append(f"short {idx}: commercial QA source exceeds duration")
            clip["status"] = "manual_review"
            clip["notes"].append("source exceeds transcript duration")
        clips.append(clip)

    approved_count = sum(1 for clip in clips if clip["status"] == "approved")
    manual_review_count = len(clips) - approved_count
    return {
        "threshold": threshold,
        "approved_count": approved_count,
        "manual_review_count": manual_review_count,
        "clips": clips,
        "failures": failures,
        "passed": not failures,
    }


def commercial_qa_failures(report: dict) -> list[str]:
    return list(report.get("failures") or [])


def validate_shorts_script(shorts_md: str, *, duration_sec: float | None = None) -> list[str]:
    """Deterministic quality checks for generated Shorts scripts.

    This does not judge taste; it enforces the structure a human editor needs:
    enough clips, complete rationale, usable durations, distinct titles/quotes,
    and at least some trading-product or trade-decision proof in each clip.
    """
    errors: list[str] = []
    blocks = list(_SHORTS_BLOCK_RE.finditer(shorts_md or ""))
    if not blocks:
        return ["no Shorts blocks found"]

    seen_titles: set[str] = set()
    seen_quotes: set[str] = set()
    ranges: list[tuple[str, float, float]] = []
    for idx, block in enumerate(blocks, start=1):
        title = block.group("title").strip()
        body = block.group("body")
        title_key = _norm_for_dupe(re.sub(r"^Short\s*\d+\s*[-—:]\s*", "", title, flags=re.IGNORECASE))
        if title_key in seen_titles:
            errors.append(f"short {idx}: duplicate title")
        seen_titles.add(title_key)

        for label in _REQUIRED_SHORT_FIELDS:
            if not _short_field(body, label):
                errors.append(f"short {idx}: missing {label}")

        score = _short_field(body, "Score")
        if score and not re.search(r"\b([7-9]|10)\s*/\s*10\b", score):
            errors.append(f"short {idx}: score must be 7/10 or better")

        source = _source_range(body)
        if not source:
            errors.append(f"short {idx}: invalid Source timestamp range")
        else:
            start, end = source
            length = end - start
            ranges.append((f"short {idx}", start, end))
            if length < 15:
                errors.append(f"short {idx}: clip is too short ({length:.0f}s)")
            if length > 60:
                errors.append(f"short {idx}: clip is too long ({length:.0f}s)")
            if duration_sec and (start < 0 or end > duration_sec + 1):
                errors.append(f"short {idx}: Source exceeds transcript duration")

        hook = _short_field(body, "Hook")
        if hook and len(_norm_for_dupe(hook).split()) < 5:
            errors.append(f"short {idx}: hook is too thin")

        quote = _short_field(body, "Source quote")
        quote_key = _norm_for_dupe(quote)
        if quote_key:
            if quote_key in seen_quotes:
                errors.append(f"short {idx}: duplicate Source quote")
            seen_quotes.add(quote_key)
            if len(quote_key.split()) < 6:
                errors.append(f"short {idx}: Source quote is too thin")

        haystack = f"{title}\n{body}".lower()
        if not any(term in haystack for term in _PRODUCT_OR_PAYOFF_TERMS):
            errors.append(f"short {idx}: missing product, trade-decision, or payoff proof")

    return errors


def _short_editor_score(body_md: str) -> int:
    match = re.search(r"\b(\d{1,2})\s*/\s*10\b", _short_field(body_md, "Score") or "")
    if not match:
        return 0
    return min(10, int(match.group(1)))


def _replace_short_source_range(body_md: str, start: float, end: float) -> str:
    replacement = (
        f"- **Source:** {TranscriptSegment.fmt_ts(start)} -> "
        f"{TranscriptSegment.fmt_ts(end)} ({end - start:.0f} seconds)"
    )
    lines = []
    replaced = False
    for line in body_md.splitlines():
        if not replaced and re.match(r"^\s*-\s*(?:\*\*)?Source\b", line, flags=re.IGNORECASE):
            lines.append(replacement)
            replaced = True
        else:
            lines.append(line)
    return "\n".join(lines)


def _renumber_short_title(title: str, idx: int) -> str:
    title = title.strip()
    if re.match(r"^Short\s+\d+\b", title, flags=re.IGNORECASE):
        return re.sub(r"^Short\s+\d+\b", f"Short {idx}", title, count=1, flags=re.IGNORECASE)
    return f"Short {idx} - {title.strip() or 'Selected Trading Moment'}"


def _format_repaired_shorts(selected: list[dict]) -> str:
    repaired_blocks = [
        f"### {_renumber_short_title(row['title'], idx)}\n{row['body'].strip()}"
        for idx, row in enumerate(selected, start=1)
    ]
    repaired = "\n\n---\n\n".join(repaired_blocks).strip()
    if repaired:
        repaired += "\n"
    return repaired


def repair_shorts_script(shorts_md: str, *, duration_sec: float | None = None) -> tuple[str, dict]:
    """Deterministically salvage repairable Shorts editor output.

    The repair is intentionally conservative: it only removes or trims bad
    selections, never invents new clips or improves weak scores. If the remaining
    set cannot satisfy validation, the caller still fails closed.
    """
    blocks = list(_SHORTS_BLOCK_RE.finditer(shorts_md or ""))
    target_min, target_max = shorts_target_range(duration_sec)
    notes: list[str] = []
    candidates: list[dict] = []
    for original_idx, block in enumerate(blocks, start=1):
        title = block.group("title").strip()
        body = block.group("body").strip()
        score = _short_editor_score(body)
        if score < 7:
            notes.append(f"dropped short {original_idx}: score {score}/10 below release threshold")
            continue
        source = _source_range(body)
        if not source:
            notes.append(f"dropped short {original_idx}: invalid Source timestamp range")
            continue
        start, end = source
        if duration_sec:
            end = min(end, duration_sec)
        if end - start > 60:
            end = start + 60
            notes.append(f"trimmed short {original_idx}: Source range capped at 60 seconds")
        if end - start < 15:
            notes.append(f"dropped short {original_idx}: clip is too short after repair")
            continue
        repaired_body = _replace_short_source_range(body, start, end)
        qa = _score_commercial_clip(
            idx=original_idx,
            title=title,
            body=repaired_body,
            duplicate_title=False,
            duplicate_quote=False,
            overlapping=False,
        )
        candidates.append({
            "original_idx": original_idx,
            "title": title,
            "body": repaired_body,
            "start": start,
            "end": end,
            "score": score,
            "qa_score": int(qa.get("score", 0)),
            "title_key": _short_title_key(title),
            "quote_key": _norm_for_dupe(_short_field(repaired_body, "Source quote")),
        })

    candidates.sort(key=lambda row: (-row["score"], -row["qa_score"], row["start"], row["original_idx"]))
    selected: list[dict] = []
    seen_titles: set[str] = set()
    seen_quotes: set[str] = set()
    for candidate in candidates:
        if candidate["title_key"] in seen_titles:
            notes.append(f"dropped short {candidate['original_idx']}: duplicate title after repair")
            continue
        if candidate["quote_key"] and candidate["quote_key"] in seen_quotes:
            notes.append(f"dropped short {candidate['original_idx']}: duplicate Source quote after repair")
            continue
        selected.append(candidate)
        seen_titles.add(candidate["title_key"])
        if candidate["quote_key"]:
            seen_quotes.add(candidate["quote_key"])

    selected = selected[:target_max]
    selected.sort(key=lambda row: row["original_idx"])
    repaired = _format_repaired_shorts(selected)
    repair_errors = validate_shorts_script(repaired, duration_sec=duration_sec)
    for _ in range(len(selected)):
        bad_positions: dict[int, list[str]] = {}
        for error in repair_errors:
            match = re.match(r"short\s+(\d+):\s*(.+)", error, flags=re.IGNORECASE)
            if match:
                bad_positions.setdefault(int(match.group(1)) - 1, []).append(match.group(2))
        if not bad_positions:
            break
        for pos in sorted(bad_positions, reverse=True):
            if 0 <= pos < len(selected):
                row = selected.pop(pos)
                notes.append(
                    f"dropped short {row['original_idx']}: " + "; ".join(bad_positions[pos][:3])
                )
        repaired = _format_repaired_shorts(selected)
        repair_errors = validate_shorts_script(repaired, duration_sec=duration_sec)
    return repaired, {
        "attempted": True,
        "target_min": target_min,
        "target_max": target_max,
        "original_count": len(blocks),
        "repaired_count": len(selected),
        "notes": notes,
        "validation_errors": repair_errors,
        "succeeded": not repair_errors,
    }


def repair_commercial_qa_script(shorts_md: str, *, duration_sec: float | None = None) -> tuple[str, dict]:
    """Drop commercial-QA failures when enough release-ready clips remain."""
    report = commercial_qa_report(shorts_md, duration_sec=duration_sec)
    failed_indexes = {
        int(clip.get("index", 0))
        for clip in report.get("clips", [])
        if clip.get("status") != "approved" or int(clip.get("score", 0)) < int(report.get("threshold", COMMERCIAL_QA_MIN_SCORE))
    }
    blocks = list(_SHORTS_BLOCK_RE.finditer(shorts_md or ""))
    target_min, target_max = shorts_target_range(duration_sec)
    selected: list[dict] = []
    notes: list[str] = []
    for idx, block in enumerate(blocks, start=1):
        if idx in failed_indexes:
            clip = next((item for item in report.get("clips", []) if int(item.get("index", 0)) == idx), {})
            notes.append(
                f"dropped short {idx}: commercial QA score {clip.get('score', 0)} below "
                f"{report.get('threshold', COMMERCIAL_QA_MIN_SCORE)}"
            )
            continue
        selected.append({
            "original_idx": idx,
            "title": block.group("title").strip(),
            "body": block.group("body").strip(),
        })
    selected = selected[:target_max]
    repaired = _format_repaired_shorts(selected)
    validation_errors = validate_shorts_script(repaired, duration_sec=duration_sec)
    repaired_report = commercial_qa_report(repaired, duration_sec=duration_sec) if not validation_errors else {}
    commercial_failures = commercial_qa_failures(repaired_report) if repaired_report else []
    return repaired, {
        "attempted": bool(failed_indexes),
        "target_min": target_min,
        "target_max": target_max,
        "original_count": len(blocks),
        "repaired_count": len(selected),
        "dropped_indexes": sorted(failed_indexes),
        "notes": notes,
        "validation_errors": validation_errors,
        "commercial_failures": commercial_failures,
        "commercial_qa": repaired_report,
        "succeeded": bool(failed_indexes) and not validation_errors and not commercial_failures,
    }


def split_output(raw: str) -> dict[str, str]:
    """Split the model's response into {filename: content}."""
    parts: dict[str, str] = {}
    matches = list(_FILE_MARKER_RE.finditer(raw))
    if not matches:
        return parts
    for i, m in enumerate(matches):
        name = m.group("name").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        parts[name] = raw[start:end].strip() + "\n"
    return parts


# ---------------------------------------------------------------------------
# Commercial QA data model
# ---------------------------------------------------------------------------

COMMERCIAL_QA_THRESHOLD = 85


@dataclass
class CommercialQACriterionScore:
    score: int
    max_score: int

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "max_score": self.max_score,
        }


@dataclass
class CommercialQAShortScore:
    index: int
    title: str
    total: int
    threshold: int
    passed: bool
    criteria: dict[str, CommercialQACriterionScore]
    notes: list[str]
    status: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "total": self.total,
            "threshold": self.threshold,
            "passed": self.passed,
            "criteria": {key: score.to_dict() for key, score in self.criteria.items()},
            "notes": self.notes,
            "status": self.status,
        }


@dataclass
class CommercialQAReportModel:
    threshold: int
    passed: bool
    shorts: list[CommercialQAShortScore]
    errors: list[str]
    approved_count: int
    manual_review_count: int

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "passed": self.passed,
            "short_count": len(self.shorts),
            "approved_count": self.approved_count,
            "manual_review_count": self.manual_review_count,
            "errors": self.errors,
            "shorts": [short.to_dict() for short in self.shorts],
        }


class ShortsEditorPassError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw: str = "",
        shorts: str = "",
        validation_errors: list[str] | None = None,
        commercial_failures: list[str] | None = None,
        repair: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.shorts = shorts
        self.validation_errors = validation_errors or []
        self.commercial_failures = commercial_failures or []
        self.repair = repair or {}


def score_shorts_commercial_qa(
    shorts_md: str,
    *,
    duration_sec: float | None = None,
    threshold: int = COMMERCIAL_QA_THRESHOLD,
) -> CommercialQAReportModel:
    """Return a typed Commercial QA scorecard for proposed Shorts.

    This is a deterministic/no-paid-call wrapper around `commercial_qa_report()`.
    It scores each proposed Short across hook, clarity, product proof, visual
    quality, audio/captions, CTA, compliance, and uniqueness with a total
    0-100 score and an 85-point default threshold.
    """
    raw = commercial_qa_report(
        shorts_md,
        duration_sec=duration_sec,
        min_score=threshold,
    )
    shorts: list[CommercialQAShortScore] = []
    for clip in raw.get("clips", []):
        category_scores = clip.get("category_scores") or {}
        criteria = {
            name: CommercialQACriterionScore(
                score=int(category_scores.get(name, 0)),
                max_score=int(_COMMERCIAL_QA_WEIGHTS[name]),
            )
            for name in _COMMERCIAL_QA_WEIGHTS
        }
        total = int(clip.get("score", 0))
        shorts.append(
            CommercialQAShortScore(
                index=int(clip.get("index", len(shorts) + 1)),
                title=str(clip.get("title", "")),
                total=total,
                threshold=int(raw.get("threshold", threshold)),
                passed=total >= threshold and clip.get("status") == "approved",
                criteria=criteria,
                notes=list(clip.get("notes") or []),
                status=str(clip.get("status", "manual_review")),
            )
        )
    return CommercialQAReportModel(
        threshold=int(raw.get("threshold", threshold)),
        passed=bool(raw.get("passed", False)),
        shorts=shorts,
        errors=list(raw.get("failures") or []),
        approved_count=int(raw.get("approved_count", 0)),
        manual_review_count=int(raw.get("manual_review_count", 0)),
    )


def run_shorts_editor_pass(
    client,
    *,
    transcript: Transcript,
    slug: str,
    product: str,
    brand_voice: str,
    guardrails: str,
    editor_guidance: str,
    creator_guidance: str = "",
    model: str,
) -> tuple[str, dict]:
    candidates = mine_clip_candidates(transcript)
    system, user = build_shorts_editor_prompts(
        transcript,
        slug=slug,
        product=product,
        brand_voice=brand_voice,
        guardrails=guardrails,
        editor_guidance=editor_guidance,
        creator_guidance=creator_guidance,
        candidates=candidates,
    )
    msg = _create_message_with_retries(
        client,
        model=model,
        max_tokens=min(MAX_OUTPUT_TOKENS, 7000),
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    )
    sections = split_output(raw)
    shorts = sections.get("02-shorts-scripts.md")
    if not shorts:
        raise RuntimeError("Shorts editor response did not contain <<<<FILE: 02-shorts-scripts.md>>>>")
    validation_errors = validate_shorts_script(shorts, duration_sec=transcript.duration_sec)
    repair_meta: dict[str, object] | None = None
    if validation_errors:
        repaired, repair_meta = repair_shorts_script(shorts, duration_sec=transcript.duration_sec)
        if repair_meta.get("succeeded"):
            shorts = repaired
        else:
            raise ShortsEditorPassError(
                "Shorts editor validation failed: " + "; ".join(validation_errors[:12]),
                raw=raw,
                shorts=shorts,
                validation_errors=validation_errors,
                repair=repair_meta,
            )
    commercial_qa = commercial_qa_report(shorts, duration_sec=transcript.duration_sec)
    commercial_failures = commercial_qa_failures(commercial_qa)
    if commercial_failures:
        commercial_repaired, commercial_repair_meta = repair_commercial_qa_script(
            shorts,
            duration_sec=transcript.duration_sec,
        )
        if commercial_repair_meta.get("succeeded"):
            shorts = commercial_repaired
            commercial_qa = commercial_repair_meta["commercial_qa"]
            commercial_failures = []
            if repair_meta:
                repair_meta.setdefault("notes", [])
                repair_meta["commercial_qa_repair"] = commercial_repair_meta
            else:
                repair_meta = {
                    "attempted": True,
                    "succeeded": True,
                    "validation_errors": [],
                    "notes": [],
                    "commercial_qa_repair": commercial_repair_meta,
                }
        else:
            raise ShortsEditorPassError(
                "Shorts commercial QA failed: " + "; ".join(commercial_failures[:12]),
                raw=raw,
                shorts=shorts,
                commercial_failures=commercial_failures,
                repair=commercial_repair_meta,
            )
    meta: dict[str, object] = {
        "candidate_count": len(candidates),
        "model": model,
        "raw": raw,
        "validation_errors": validation_errors,
        "commercial_qa": commercial_qa,
    }
    if repair_meta:
        meta["repair"] = repair_meta
        meta["repaired"] = True
    if getattr(msg, "usage", None):
        meta["usage"] = {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        }
    return shorts, meta


# ---------------------------------------------------------------------------
# Multiplier entrypoint
# ---------------------------------------------------------------------------

@dataclass
class MultiplierResult:
    out_dir: Path
    files: list[str]
    model: str
    raw_response_path: Path
    error: str | None = None


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s\-]", "", s.strip().lower())
    s = re.sub(r"\s+", "-", s)
    return s[:60].strip("-") or "untitled"


def _output_dir(slug: str) -> Path:
    today = date.today()
    iso = today.isocalendar()
    folder_name = f"W{iso.week:02d}-{slug}-{today.isoformat()}"
    return CONTENT_DIR / folder_name


def process_transcript(
    transcript_path: Path,
    *,
    slug: str | None = None,
    product: str = "both",
    model: str | None = None,
    premium: bool = False,
    editor_guidance: str | None = None,
    creator_guidance: str = "",
) -> MultiplierResult:
    """Run the multiplier on one transcript file. Returns paths to outputs.

    `slug` defaults to the filename stem if not given. `product` is one of
    'bb', 'dg', 'both'. `premium=True` switches to the Opus model. `editor_guidance`
    defaults to whatever the Editor agent last wrote (so the next video is steered
    by live YouTube results); pass "" to explicitly disable steering.
    """
    transcript = load_transcript(transcript_path)
    if not slug:
        slug = _slugify(transcript_path.stem)
    chosen_model = model or (PREMIUM_MODEL if premium else DEFAULT_MODEL)
    if editor_guidance is None:
        editor_guidance = load_editor_guidance()
    creator_guidance = (creator_guidance or "").strip()[:2000]

    brand_voice = _read(CONFIG_DIR / "brand-voice.md")
    guardrails = _read(CONFIG_DIR / "guardrails.md")
    system, user = build_prompts(
        transcript,
        slug=slug,
        product=product,
        brand_voice=brand_voice,
        guardrails=guardrails,
        editor_guidance=editor_guidance,
        creator_guidance=creator_guidance,
    )

    out_dir = _output_dir(slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save the source transcript and a meta sidecar before calling the API
    (out_dir / "00-source.txt").write_text(transcript.text, encoding="utf-8")
    if transcript.has_timestamps:
        (out_dir / "00-source.srt").write_text(
            transcript_path.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
        )
    meta = {
        "slug": slug,
        "product": product,
        "model": chosen_model,
        "source_filename": transcript.source_filename,
        "has_timestamps": transcript.has_timestamps,
        "editor_guidance_applied": bool(editor_guidance.strip()),
        "creator_guidance_applied": bool(creator_guidance),
        "duration_sec": transcript.duration_sec,
        "run_started_at": datetime.now().isoformat(timespec="seconds"),
    }
    if creator_guidance:
        meta["creator_guidance"] = creator_guidance
        (out_dir / "00-creator-guidance.md").write_text(creator_guidance + "\n", encoding="utf-8")
    (out_dir / "00-meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # Call Anthropic
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        return MultiplierResult(
            out_dir=out_dir,
            files=[],
            model=chosen_model,
            raw_response_path=out_dir / "00-error.txt",
            error=f"anthropic SDK not installed: {exc}",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return MultiplierResult(
            out_dir=out_dir,
            files=[],
            model=chosen_model,
            raw_response_path=out_dir / "00-error.txt",
            error="ANTHROPIC_API_KEY missing - populate marketing-ops/.env or the parent .ENV",
        )

    client = Anthropic(api_key=api_key)
    try:
        msg = _create_message_with_retries(
            client,
            model=chosen_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except LLMMessageCreateError as exc:
        error = str(exc)
        (out_dir / "00-error.txt").write_text(error, encoding="utf-8")
        meta["run_completed_at"] = datetime.now().isoformat(timespec="seconds")
        meta["api_error"] = error
        meta["api_attempts"] = exc.attempts
        (out_dir / "00-meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        return MultiplierResult(
            out_dir=out_dir,
            files=[],
            model=chosen_model,
            raw_response_path=out_dir / "00-error.txt",
            error=error,
        )
    raw = "".join(
        block.text for block in msg.content if getattr(block, "type", "") == "text"
    )

    raw_path = out_dir / "00-raw-response.md"
    raw_path.write_text(raw, encoding="utf-8")

    sections = split_output(raw)
    if not sections:
        return MultiplierResult(
            out_dir=out_dir,
            files=[],
            model=chosen_model,
            raw_response_path=raw_path,
            error="Model response did not contain <<<<FILE: ...>>>> markers. See 00-raw-response.md.",
        )

    written: list[str] = []
    for name, body in sections.items():
        # Sanitize filename — only allow simple basenames
        safe_name = re.sub(r"[^\w\-.]", "_", name)
        (out_dir / safe_name).write_text(body, encoding="utf-8")
        written.append(safe_name)

    if SHORTS_EDITOR_PASS and transcript.has_timestamps:
        shorts_model = os.environ.get("CONTENT_MULTIPLIER_SHORTS_MODEL") or chosen_model
        try:
            shorts_body, shorts_meta = run_shorts_editor_pass(
                client,
                transcript=transcript,
                slug=slug,
                product=product,
                brand_voice=brand_voice,
                guardrails=guardrails,
                editor_guidance=editor_guidance,
                creator_guidance=creator_guidance,
                model=shorts_model,
            )
        except Exception as exc:  # noqa: BLE001
            transient_llm_failure = _is_transient_llm_failure(exc)
            meta["shorts_editor_pass"] = False
            meta["shorts_editor_status"] = "failed_nonfatal" if transient_llm_failure else "failed"
            meta["shorts_editor_error"] = str(exc)
            if isinstance(exc, LLMMessageCreateError):
                meta["shorts_editor_api_attempts"] = exc.attempts
            if transient_llm_failure:
                meta["shorts_editor_note"] = "Continuing with first-pass shorts after transient LLM failure."
            if isinstance(exc, ShortsEditorPassError):
                if exc.raw:
                    (out_dir / "00-shorts-editor-response.md").write_text(exc.raw, encoding="utf-8")
                if exc.shorts:
                    (out_dir / "00-shorts-editor-rejected.md").write_text(exc.shorts, encoding="utf-8")
                meta["shorts_editor"] = {
                    "validation_errors": exc.validation_errors,
                    "commercial_failures": exc.commercial_failures,
                    "repair": exc.repair,
                }
            meta["run_completed_at"] = datetime.now().isoformat(timespec="seconds")
            if getattr(msg, "usage", None):
                meta["usage"] = {
                    "input_tokens": msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                }
            meta["files"] = sorted(written)
            (out_dir / "00-meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
            if transient_llm_failure:
                return MultiplierResult(
                    out_dir=out_dir,
                    files=sorted(written),
                    model=chosen_model,
                    raw_response_path=raw_path,
                )
            return MultiplierResult(
                out_dir=out_dir,
                files=sorted(written),
                model=chosen_model,
                raw_response_path=raw_path,
                error=f"Shorts editor pass failed: {exc}",
            )

        shorts_raw = str(shorts_meta.pop("raw", ""))
        (out_dir / "00-shorts-editor-response.md").write_text(shorts_raw, encoding="utf-8")
        (out_dir / "02-shorts-scripts.md").write_text(shorts_body, encoding="utf-8")
        if "02-shorts-scripts.md" not in written:
            written.append("02-shorts-scripts.md")
        meta["shorts_editor_pass"] = True
        meta["shorts_editor_status"] = "repaired" if shorts_meta.get("repaired") else "succeeded"
        meta["shorts_editor"] = shorts_meta
    else:
        meta["shorts_editor_pass"] = False
        meta["shorts_editor_status"] = "skipped"

    # Update meta with completion + token usage if available
    meta["run_completed_at"] = datetime.now().isoformat(timespec="seconds")
    if getattr(msg, "usage", None):
        meta["usage"] = {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        }
    meta["files"] = sorted(written)
    (out_dir / "00-meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    return MultiplierResult(
        out_dir=out_dir,
        files=sorted(written),
        model=chosen_model,
        raw_response_path=raw_path,
    )


# ---------------------------------------------------------------------------
# Folder watcher (called from console.py background thread or CLI)
# ---------------------------------------------------------------------------

def scan_incoming() -> list[MultiplierResult]:
    """Process every transcript currently in content/incoming/ (recursively
    one level). Returns results, leaves consumed files in `.processed/`.
    """
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    results: list[MultiplierResult] = []
    for entry in sorted(INCOMING_DIR.iterdir()):
        if entry.is_dir():
            continue
        if entry.suffix.lower() not in {".txt", ".srt", ".docx"}:
            continue
        try:
            result = process_transcript(entry)
            results.append(result)
            # Move source into .processed/ so we don't re-run on next scan
            target = PROCESSED_DIR / entry.name
            if target.exists():
                target = PROCESSED_DIR / f"{entry.stem}-{datetime.now():%Y%m%d-%H%M%S}{entry.suffix}"
            entry.rename(target)
        except Exception as exc:  # noqa: BLE001
            results.append(
                MultiplierResult(
                    out_dir=INCOMING_DIR,
                    files=[],
                    model="",
                    raw_response_path=Path(),
                    error=f"{entry.name}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    p = argparse.ArgumentParser(description="Shadow Edge Content Multiplier")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="Process a single transcript file")
    run.add_argument("transcript", type=Path)
    run.add_argument("--slug", default=None)
    run.add_argument("--product", default="both", choices=["bb", "dg", "both"])
    run.add_argument("--premium", action="store_true", help="Use the Opus model")

    sub.add_parser("scan", help="Process all files in content/incoming/")

    args = p.parse_args()

    if args.cmd == "run":
        result = process_transcript(
            args.transcript,
            slug=args.slug,
            product=args.product,
            premium=args.premium,
        )
    elif args.cmd == "scan":
        results = scan_incoming()
        if not results:
            print("No new transcripts in content/incoming/")
            return 0
        for result in results:
            _print_result(result)
        return 0
    else:
        p.print_help()
        return 1

    return _print_result(result)


def _print_result(result: MultiplierResult) -> int:
    if result.error:
        print(f"ERROR ({result.out_dir}): {result.error}", file=sys.stderr)
        return 2
    print(f"OK ({result.model})")
    print(f"  Output: {result.out_dir}")
    for f in result.files:
        print(f"    - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
