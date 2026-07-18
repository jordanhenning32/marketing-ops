"""Editor agent — reviews the YouTube performance feedback and steers the NEXT
video's editing.

This closes the back half of the feedback loop: `youtube_stats` collects what
each video did; the Editor reads that signal, works out which themes / titles /
angles are winning, and writes:

    * state/editorial-brief.md      — human-readable review (shown on /youtube)
    * state/editor-guidance.json    — machine-readable steering signal whose
                                      `prompt_block` the Content Multiplier injects
                                      into the next video's generation prompt.

It never touches already-published videos. The current channel sample is small,
so the brief is explicitly framed as directional (a lean, not a rule).
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from hermes_store import CONTENT_DIR, STATE_DIR, utc_now_iso, write_text
from .base import AgentResult, MarketingAgent, task
import performance_feedback

GUIDANCE_PATH = STATE_DIR / "editor-guidance.json"
BRIEF_PATH = STATE_DIR / "editorial-brief.md"

# Theme classification by title keywords, in priority order. The signal so far:
# discipline/lockout (Drawdown Guardian) wins; pure install/mechanics loses.
THEME_KEYWORDS: list[tuple[str, list[str]]] = [
    ("drawdown-guardian", ["drawdown guardian", "drawdown", "draw down", "lock", "auto-flat",
                            "auto flat", "daily loss", "discipline", "blow", "blew", "lockout", "flatten"]),
    ("bracket-boss", ["bracket boss", "bracket", "stop loss", "stop-loss", "risk siz",
                       "position siz", "scale-out", "scale out", "scale outs"]),
    ("install-mechanics", ["install", "installation", "setup", "set up", "how to",
                           "how scale", "walkthrough", "guide", "menu", "settings"]),
]
THEME_LABELS = {
    "drawdown-guardian": "Drawdown Guardian — the discipline / lockout angle",
    "bracket-boss": "Bracket Boss — risk-sizing / bracket discipline",
    "install-mechanics": "Install / how-it-works mechanics",
    "other": "Other / mixed",
}


def _classify(title: str) -> str:
    t = (title or "").lower()
    for theme, kws in THEME_KEYWORDS:
        if any(k in t for k in kws):
            return theme
    return "other"


def _theme_stats(videos: list[dict]) -> list[tuple[str, dict]]:
    """Aggregate views per theme, ranked by average views (avoids one viral video
    making a whole theme look strong on a single sample)."""
    agg: dict[str, dict] = defaultdict(lambda: {"videos": 0, "views": 0})
    for v in videos:
        s = agg[_classify(v.get("title", ""))]
        s["videos"] += 1
        s["views"] += int(v.get("views", 0) or 0)
    for s in agg.values():
        s["avg_views"] = round(s["views"] / max(1, s["videos"]), 1)
    return sorted(agg.items(), key=lambda kv: kv[1]["avg_views"], reverse=True)


def build_guidance(
    videos: list[dict],
    channel: dict,
    *,
    training_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ranked = _theme_stats(videos)
    top_videos = sorted(videos, key=lambda v: int(v.get("views", 0) or 0), reverse=True)
    channel_views = int((channel or {}).get("views", 0) or 0)
    feedback_training = performance_feedback.summarize_training_records(training_records or [])
    top_video_rows = [
        {
            "video_id": v.get("video_id"),
            "title": v.get("title", ""),
            "views": int(v.get("views", 0) or 0),
            "delta_views": v.get("delta_views"),
            "url": v.get("url"),
            "theme": _classify(v.get("title", "")),
            "tracked": bool(v.get("tracked", True)),
        }
        for v in top_videos[:5]
    ]

    # Best/worst non-"other" theme
    real = [(t, s) for t, s in ranked if t != "other"] or ranked or [
        ("other", {"videos": 0, "views": 0, "avg_views": 0})
    ]
    lead_theme, lead_stats = real[0]
    weak_theme = real[-1][0] if len(real) > 1 else None

    winning_titles = [v.get("title", "") for v in top_videos[:3] if v.get("title")]
    training_clips = int(feedback_training.get("sample", {}).get("clips", 0) or 0)
    confidence_basis = max(channel_views, training_clips * 50)
    confidence = "low" if confidence_basis < 300 else "medium" if confidence_basis < 2000 else "growing"

    lines = [
        "# PERFORMANCE GUIDANCE (from the editor reviewing live YouTube results)",
        f"# Sample is small ({len(videos)} videos / {channel_views} views) — treat this as a LEAN, not a rule.",
        f"- Lead with: {THEME_LABELS.get(lead_theme, lead_theme)}. It has the best avg views so far "
        f"({lead_stats['avg_views']}/video).",
        "- Open on the emotional stakes (the bad afternoon, revenge trading, almost blowing the account) "
        "BEFORE showing mechanics. The discipline/lockout story is what lands.",
    ]
    if weak_theme and weak_theme != lead_theme:
        lines.append(
            f"- De-emphasize standalone {THEME_LABELS.get(weak_theme, weak_theme)} clips — they underperform "
            "on their own; fold any mechanics into a discipline story instead."
        )
    if winning_titles:
        lines.append("- Titles that worked (mirror this punchy, outcome-first style): "
                     + "; ".join(f'"{t}"' for t in winning_titles))
    if top_video_rows:
        top = top_video_rows[0]
        lines.append(
            f"- Current top clip to study: \"{top['title']}\" ({top['views']} views). "
            "Repeat the proof/payoff shape, not just the topic label."
        )
    training_lines = feedback_training.get("prompt_lines") or []
    if training_lines:
        lines.append("- Clip-level training data from YouTube/TikTok/site metrics:")
        lines.extend(f"  - {line}" for line in training_lines)
    lines.append("- Keep the hook in the first ~2 seconds. Captions are burned in, so write short, "
                 "punchy spoken lines that read well on a muted phone.")
    prompt_block = "\n".join(lines)

    return {
        "generated_at": utc_now_iso(),
        "sample": {
            "videos": len(videos),
            "channel_views": channel_views,
            "subscribers": int((channel or {}).get("subscribers", 0) or 0),
        },
        "confidence": confidence,
        "theme_ranking": [{"theme": t, **s} for t, s in ranked],
        "lead_theme": lead_theme,
        "de_emphasize": weak_theme,
        "top_videos": top_video_rows,
        "winning_titles": winning_titles,
        "feedback_training": feedback_training,
        "performance_feedback": feedback_training,
        "prompt_block": prompt_block,
    }


def _render_brief(guidance: dict, top_videos: list[dict]) -> str:
    s = guidance["sample"]
    out = [
        f"# Editorial Brief",
        "",
        f"Reviewed {s['videos']} videos · {s['channel_views']} channel views · {s['subscribers']} subs · "
        f"confidence: **{guidance['confidence']}**",
        "",
        "## What's winning",
    ]
    for row in guidance["theme_ranking"]:
        out.append(f"- {THEME_LABELS.get(row['theme'], row['theme'])}: "
                   f"{row['avg_views']} avg views ({row['videos']} videos, {row['views']} total)")
    out += ["", "## Top videos"]
    for v in top_videos[:5]:
        out.append(f"- {int(v.get('views', 0))} views — {v.get('title', '(untitled)')}")
    training = guidance.get("feedback_training") or {}
    if training.get("top_patterns"):
        out += ["", "## Clip-level training signals"]
        for row in training["top_patterns"][:5]:
            out.append(
                f"- {row['dimension']}: {row['value']} "
                f"({row['clips']} clips, avg score {row['avg_score']}, {row['views']} views)"
            )
    out += ["", "## Direction for the next video", "", guidance["prompt_block"]]
    return "\n".join(out).strip() + "\n"


class EditorAgent(MarketingAgent):
    name = "editor"
    required_inputs = ["state/youtube-video-stats.jsonl (from the daily stats pull)"]
    outputs = ["state/editorial-brief.md", "state/editor-guidance.json", "state/agent-tasks.jsonl"]

    def run(self, context: dict) -> AgentResult:
        try:
            import youtube_stats
        except Exception as exc:  # noqa: BLE001 - never break the team run on an import
            return AgentResult(self.name, "blocked", {}, [f"youtube_stats unavailable: {exc}"],
                               [task(self.name, "Fix youtube_stats import", str(exc))])

        videos = youtube_stats.video_summary()
        channel = youtube_stats.channel_summary()
        try:
            metadata_records = performance_feedback.collect_clip_training_records(CONTENT_DIR)
            youtube_records = performance_feedback.collect_youtube_training_records(videos, CONTENT_DIR)
            training_records = performance_feedback.dedupe_training_records([*youtube_records, *metadata_records])
        except Exception:  # noqa: BLE001 - training data should never block the editor
            training_records = []

        if not videos and not training_records:
            blocker = "No YouTube stats yet — record/upload videos and let the daily pull run."
            return AgentResult(self.name, "ok", {"editor_review": "no-data"}, [blocker],
                               [task(self.name, "Get first YouTube stats",
                                     "Upload a video and run scripts/youtube_stats.py pull (or wait for the 04:55 Eastern task).",
                                     owner="Jordan")])

        guidance = build_guidance(videos, channel, training_records=training_records)
        top_videos = sorted(videos, key=lambda v: int(v.get("views", 0) or 0), reverse=True)

        write_text(GUIDANCE_PATH, json.dumps(guidance, indent=2, ensure_ascii=False) + "\n")
        write_text(BRIEF_PATH, _render_brief(guidance, top_videos))

        lead_label = THEME_LABELS.get(guidance["lead_theme"], guidance["lead_theme"])
        tasks = [
            task(self.name, "Lead the next video with the winning angle",
                 f"Editor review: lead with {lead_label}. Guidance auto-applied to the next video's "
                 "content generation; see state/editorial-brief.md.",
                 owner="Jordan", priority=2),
        ]
        return AgentResult(
            self.name, "ok",
            {
                "editorial_brief": str(BRIEF_PATH),
                "editor_guidance": str(GUIDANCE_PATH),
                "lead_theme": guidance["lead_theme"],
                "confidence": guidance["confidence"],
            },
            [],
            tasks,
        )
