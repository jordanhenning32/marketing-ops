"""Performance feedback training data for Shorts selection.

This module is intentionally local and deterministic. It reads the clip
distribution metadata we already produce, normalizes platform/site metrics, tags
each clip by creative pattern, and summarizes which patterns are winning.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"

METRIC_KEYS = [
    "views",
    "average_view_duration_sec",
    "retention_pct",
    "likes",
    "comments",
    "clicks",
    "website_sessions",
    "purchases",
    "delta_views",
]


_COMPACT_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _timestamp_to_seconds(value: str) -> float | None:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        return None
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?(?:[\.,]\d+)?", text):
        return _timestamp_to_seconds(text)
    cleaned = text.replace(",", "").replace("%", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kmb])?", cleaned, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix:
        number *= _COMPACT_MULTIPLIERS[suffix]
    return number


def _to_int(value: Any) -> int:
    number = _parse_float(value)
    return int(round(number)) if number is not None else 0


def _to_float(value: Any) -> float:
    number = _parse_float(value)
    return number if number is not None else 0.0


def _slug_label(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return cleaned or "unknown"


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


_SHORT_BLOCK_RE = re.compile(
    r"^###\s*Short\s*(?P<index>\d+)(?P<header>[^\n]*)\n"
    r"(?P<body>.*?)(?=^###\s*Short\s*\d+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_SHORT_FIELD_RE = re.compile(r"^\s*-\s*\*\*(?P<label>[^*]+?)\*\*\s*(?P<value>.*)$")


def _field_key(label: str) -> str:
    label = re.sub(r"\([^)]*\)", "", label or "")
    label = label.strip().strip(":").lower()
    return re.sub(r"[^a-z0-9]+", "_", label).strip("_")


def parse_shorts_script(path: Path) -> dict[int, dict[str, str]]:
    """Return generated Short fields by index from 02-shorts-scripts.md."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    clips: dict[int, dict[str, str]] = {}
    for match in _SHORT_BLOCK_RE.finditer(raw):
        idx = _to_int(match.group("index"))
        if idx <= 0:
            continue
        header = _first_nonempty(match.group("header")).strip(" -:")
        quoted = re.search(r'"([^"]+)"', header)
        title = quoted.group(1).strip() if quoted else header
        fields: dict[str, str] = {"title": title}
        for line in match.group("body").splitlines():
            field = _SHORT_FIELD_RE.match(line)
            if not field:
                continue
            key = _field_key(field.group("label"))
            value = _first_nonempty(field.group("value").lstrip(":"))
            if key:
                fields[key] = value
        clips[idx] = fields
    return clips


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def clip_context_for_metadata(meta_path: Path) -> dict[str, str]:
    """Load the generated Short block and local caption next to a metadata file."""
    context: dict[str, str] = {}
    try:
        clip_index = int(meta_path.parent.name)
    except ValueError:
        clip_index = 0
    try:
        run_dir = meta_path.parents[3]
    except IndexError:
        run_dir = None
    if run_dir and clip_index:
        context.update(parse_shorts_script(run_dir / "02-shorts-scripts.md").get(clip_index, {}))
    caption = _read_text(meta_path.parent / "caption.txt").strip()
    if caption:
        context["caption"] = caption
    title_txt = _read_text(meta_path.parent / "title.txt").strip()
    if title_txt:
        context.setdefault("title", title_txt)
    return context


def _read_sidecar(meta_path: Path, filename: str) -> str:
    try:
        return (meta_path.parent / filename).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _metadata_with_sidecars(meta_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(metadata)
    if not _first_nonempty(enriched.get("title"), enriched.get("title_used"), enriched.get("visual_title")):
        title = _read_sidecar(meta_path, "title.txt")
        if title:
            enriched["title"] = title
    if not _first_nonempty(enriched.get("caption"), enriched.get("description"), enriched.get("body_md")):
        caption = _read_sidecar(meta_path, "caption.txt")
        if caption:
            enriched["caption"] = caption
    return enriched


def _metric_sources(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [metadata]
    for key in ("stats", "metrics", "performance", "analytics", "site_metrics"):
        value = metadata.get(key)
        if isinstance(value, dict):
            sources.append(value)
    for platform in ("youtube", "tiktok", "instagram", "rumble", "site", "website"):
        value = metadata.get(platform)
        if isinstance(value, dict):
            sources.append(value)
            for nested_key in ("stats", "metrics", "performance", "analytics"):
                nested = value.get(nested_key)
                if isinstance(nested, dict):
                    sources.append(nested)
    return sources


def normalize_metrics(metadata: dict[str, Any]) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {key: 0 for key in METRIC_KEYS}
    aliases = {
        "views": ["views", "view_count", "video_views", "plays", "play_count"],
        "average_view_duration_sec": [
            "average_view_duration_sec",
            "avg_view_duration_sec",
            "average_view_duration_seconds",
            "avg_view_duration_seconds",
            "average_view_duration",
            "avg_view_duration",
            "averageViewDuration",
            "avg_watch_sec",
            "avg_watch_time",
            "avg_watch_time_seconds",
        ],
        "retention_pct": [
            "retention_pct",
            "retention",
            "retention_rate",
            "average_percentage_viewed",
            "avg_percent_viewed",
            "avg_percentage_viewed",
            "completion_rate",
        ],
        "likes": ["likes", "like_count"],
        "comments": ["comments", "comment_count", "replies"],
        "clicks": ["clicks", "link_clicks", "website_clicks", "landing_page_clicks"],
        "website_sessions": ["website_sessions", "sessions", "site_sessions"],
        "purchases": ["purchases", "orders", "sales", "conversions"],
        "delta_views": ["delta_views", "view_delta", "views_delta", "new_views"],
    }
    for source in _metric_sources(metadata):
        for canonical, names in aliases.items():
            for name in names:
                if name in source:
                    parsed = _parse_float(source.get(name))
                    if parsed is None:
                        continue
                    if canonical in {"views", "likes", "comments", "delta_views"}:
                        metrics[canonical] = int(round(parsed))
                    else:
                        metrics[canonical] = parsed
                    break
    return metrics


def opening_phrase(text: str, *, words: int = 7) -> str:
    tokens = re.findall(r"[A-Za-z0-9$']+", text or "")
    return " ".join(tokens[:words]).strip()


def classify_product(text: str) -> str:
    lower = (text or "").lower()
    has_bb = "bracket boss" in lower or "bracket" in lower
    has_dg = "drawdown guardian" in lower or "drawdown" in lower or "discipline score" in lower
    if has_bb and has_dg:
        return "both"
    if has_bb:
        return "bracket-boss"
    if has_dg:
        return "drawdown-guardian"
    if "ninjatrader" in lower or "nt8" in lower:
        return "ninjatrader"
    return "shadow-edge-tools"


def classify_hook_type(text: str) -> str:
    lower = (text or "").lower()
    if any(term in lower for term in ["discipline", "revenge", "hands off", "do not touch", "walk away", "lock", "cannot move"]):
        return "discipline"
    if any(term in lower for term in ["liquidation", "$", "risk", "blow", "account", "daily loss"]):
        return "account-risk"
    if any(term in lower for term in ["install", "setup", "how to", "walkthrough", "import"]):
        return "mechanics"
    if any(term in lower for term in ["filled", "target", "stop", "entry", "cancel", "invalidated"]):
        return "trade-decision"
    if any(term in lower for term in ["before", "after", "updates", "moved", "proof", "result"]):
        return "proof-payoff"
    return "generic"


def classify_topic(text: str) -> str:
    lower = (text or "").lower()
    if any(term in lower for term in ["liquidation", "drawdown", "daily loss", "risk"]):
        return "risk-control"
    if any(term in lower for term in ["discipline", "revenge", "hands off", "walk away"]):
        return "discipline"
    if any(term in lower for term in ["target", "stop", "entry", "order", "filled", "cancel"]):
        return "trade-management"
    if any(term in lower for term in ["install", "import", "setup", "ninjatrader"]):
        return "installation"
    if any(term in lower for term in ["automation", "automatic", "auto"]):
        return "automation"
    return "mixed"


def classify_cta(text: str) -> str:
    lower = (text or "").lower()
    stripped = lower.strip()
    if not stripped:
        return "none"
    if re.search(r"\b(no|without|missing)\s+(cta|call to action)\b", stripped) or stripped in {"none", "n/a", "na"}:
        return "none"
    if "shadowedgetools.com" in lower or "website" in lower:
        return "website"
    if "full" in lower or "channel" in lower or "watch" in lower:
        return "watch-full"
    return "generic"


def classify_visual_style(metadata: dict[str, Any], text: str) -> str:
    visual = _first_nonempty(metadata.get("visual_label"), metadata.get("visual_title"), metadata.get("title"))
    lower = f"{visual}\n{text}".lower()
    if re.search(r"[$#]?\d", lower):
        return "numeric-stakes"
    if "bracket boss" in lower or "drawdown guardian" in lower:
        return "product-label"
    if any(term in lower for term in ["install", "import", "setup", "ninjatrader"]):
        return "screen-walkthrough"
    if any(term in lower for term in ["rule", "never", "always", "hands off"]):
        return "rule-card"
    return "branded-generic"


def extract_clip_tags(metadata: dict[str, Any], clip_context: dict[str, str] | None = None) -> dict[str, Any]:
    clip_context = clip_context or {}
    title = _first_nonempty(
        clip_context.get("title"),
        metadata.get("title"),
        metadata.get("title_used"),
        metadata.get("visual_title"),
    )
    hook = _first_nonempty(clip_context.get("hook"), title)
    on_screen = _first_nonempty(clip_context.get("on_screen_text"), metadata.get("visual_title"))
    caption = _first_nonempty(
        clip_context.get("caption"),
        metadata.get("caption"),
        metadata.get("description"),
        metadata.get("body_md"),
    )
    cta_text = _first_nonempty(
        clip_context.get("outro_cta"),
        metadata.get("cta"),
        metadata.get("outro_cta"),
        metadata.get("caption"),
        metadata.get("description"),
    )
    qa = metadata.get("commercial_qa") if isinstance(metadata.get("commercial_qa"), dict) else {}
    notes = " ".join(qa.get("notes") or []) if isinstance(qa, dict) else ""
    text = "\n".join(
        part for part in [
            title,
            hook,
            on_screen,
            cta_text,
            caption,
            metadata.get("visual_label", ""),
            clip_context.get("source_quote", ""),
            notes,
        ]
        if isinstance(part, str) and part.strip()
    )
    duration = _to_float(metadata.get("duration_sec") or metadata.get("duration") or 0)
    if duration <= 0:
        limits = metadata.get("limits") if isinstance(metadata.get("limits"), dict) else {}
        duration = _to_float(limits.get("duration_sec") or 0)
    if duration <= 0:
        duration_bucket = "unknown"
    elif duration < 25:
        duration_bucket = "short"
    elif duration <= 45:
        duration_bucket = "medium"
    else:
        duration_bucket = "long"
    return {
        "hook_type": classify_hook_type(hook or title or text),
        "product": classify_product(text),
        "topic": classify_topic(text),
        "duration_sec": duration,
        "duration_bucket": duration_bucket,
        "cta_type": classify_cta(cta_text),
        "opening_phrase": opening_phrase(hook or title or caption),
        "visual_style": classify_visual_style(metadata, "\n".join([on_screen, text])),
        "hook": hook,
        "cta": cta_text,
        "on_screen_text": on_screen,
    }


def engagement_score(metrics: dict[str, float | int]) -> float:
    return round(
        _to_float(metrics.get("views"))
        + _to_float(metrics.get("delta_views")) * 1.5
        + _to_float(metrics.get("likes")) * 4
        + _to_float(metrics.get("comments")) * 8
        + _to_float(metrics.get("clicks")) * 6
        + _to_float(metrics.get("website_sessions")) * 10
        + _to_float(metrics.get("purchases")) * 60
        + _to_float(metrics.get("retention_pct")) * 0.8
        + _to_float(metrics.get("average_view_duration_sec")) * 0.4,
        2,
    )


def metadata_to_training_record(meta_path: Path, metadata: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata_with_sidecars(meta_path, metadata)
    clip_context = clip_context_for_metadata(meta_path)
    metrics = normalize_metrics(metadata)
    if not any(_to_float(metrics.get(key)) for key in METRIC_KEYS):
        return None
    try:
        clip_index = int(meta_path.parent.name)
    except ValueError:
        clip_index = metadata.get("clip_index")
    slug = meta_path.parents[3].name if len(meta_path.parents) >= 4 else metadata.get("slug")
    platform = metadata.get("platform") or meta_path.parents[1].name
    title = _first_nonempty(clip_context.get("title"), metadata.get("title"), metadata.get("title_used"), metadata.get("visual_title"))
    tags = extract_clip_tags(metadata, clip_context)
    return {
        "slug": slug,
        "clip_index": clip_index,
        "platform": platform,
        "metadata_path": str(meta_path),
        "title": title,
        "url": metadata.get("platform_url") or metadata.get("url"),
        "metrics": metrics,
        "tags": tags,
        "engagement_score": engagement_score(metrics),
    }


def youtube_video_to_training_record(video: dict[str, Any], content_dir: Path | None = None) -> dict[str, Any] | None:
    slug = video.get("slug")
    clip_index = _to_int(video.get("clip_index"))
    if not slug or clip_index <= 0:
        return None
    root = content_dir or CONTENT_DIR
    meta_path = root / str(slug) / "distribution" / "youtube" / f"{clip_index:02d}" / "metadata.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    metadata = _metadata_with_sidecars(meta_path, metadata)
    metadata.setdefault("platform", "youtube")
    metadata.setdefault("title", video.get("title"))
    metadata.setdefault("title_used", video.get("title"))
    metadata.setdefault("platform_url", video.get("url"))
    clip_context = clip_context_for_metadata(meta_path)

    metrics = normalize_metrics(metadata)
    video_metrics = normalize_metrics(video)
    for key in ("views", "likes", "comments", "delta_views"):
        if key in video:
            metrics[key] = video_metrics[key]
    if not any(_to_float(metrics.get(key)) for key in METRIC_KEYS):
        return None

    title = _first_nonempty(clip_context.get("title"), metadata.get("title"), metadata.get("title_used"), video.get("title"))
    tags = extract_clip_tags(metadata, clip_context)
    return {
        "slug": slug,
        "clip_index": clip_index,
        "platform": "youtube",
        "metadata_path": str(meta_path),
        "title": title,
        "url": metadata.get("platform_url") or video.get("url"),
        "metrics": metrics,
        "tags": tags,
        "engagement_score": engagement_score(metrics),
    }


def collect_youtube_training_records(videos: list[dict[str, Any]], content_dir: Path | None = None) -> list[dict[str, Any]]:
    records = [
        record
        for record in (youtube_video_to_training_record(video, content_dir) for video in videos)
        if record
    ]
    records.sort(key=lambda row: row["engagement_score"], reverse=True)
    return records


def dedupe_training_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for record in records:
        key = (record.get("platform"), record.get("slug"), record.get("clip_index"))
        if not all(key):
            passthrough.append(record)
            continue
        existing = by_key.get(key)
        if not existing or _to_float(record.get("engagement_score")) > _to_float(existing.get("engagement_score")):
            by_key[key] = record
    return sorted([*by_key.values(), *passthrough], key=lambda row: row["engagement_score"], reverse=True)


def collect_clip_training_records(content_dir: Path | None = None) -> list[dict[str, Any]]:
    root = content_dir or CONTENT_DIR
    records: list[dict[str, Any]] = []
    for meta_path in root.glob("*/distribution/*/*/metadata.json"):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        record = metadata_to_training_record(meta_path, metadata)
        if record:
            records.append(record)
    records.sort(key=lambda row: row["engagement_score"], reverse=True)
    return records


def _aggregate(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        value = row.get("tags", {}).get(key)
        if value:
            grouped[str(value)].append(row)
    out: list[dict[str, Any]] = []
    for value, rows in grouped.items():
        total_score = sum(_to_float(r.get("engagement_score")) for r in rows)
        views = sum(_to_int(r.get("metrics", {}).get("views")) for r in rows)
        purchases = sum(_to_int(r.get("metrics", {}).get("purchases")) for r in rows)
        clicks = sum(_to_int(r.get("metrics", {}).get("clicks")) for r in rows)
        out.append({
            "dimension": key,
            "value": value,
            "clips": len(rows),
            "avg_score": round(total_score / max(1, len(rows)), 2),
            "views": views,
            "clicks": clicks,
            "purchases": purchases,
        })
    return sorted(out, key=lambda row: (row["avg_score"], row["views"]), reverse=True)


def _dimension_summary(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    ranking = _aggregate(records, key)
    winners = ranking[:1]
    winner_values = {row["value"] for row in winners}
    losers = [
        row for row in sorted(ranking, key=lambda row: (row["avg_score"], row["views"]))
        if row["value"] not in winner_values
    ][:2] if len(ranking) > 1 else []
    return {
        "ranking": ranking,
        "winners": winners,
        "losers": losers,
    }


def summarize_training_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = ["hook_type", "product", "topic", "cta_type", "visual_style"]
    dimension_summaries = {dim: _dimension_summary(records, dim) for dim in dimensions}
    patterns = [row for dim in dimensions for row in _aggregate(records, dim)]
    winners = sorted(records, key=lambda row: row.get("engagement_score", 0), reverse=True)[:5]
    losers = sorted(records, key=lambda row: row.get("engagement_score", 0))[:5]
    top_patterns = sorted(patterns, key=lambda row: (row["avg_score"], row["views"]), reverse=True)[:6]
    weak_patterns = sorted(patterns, key=lambda row: (row["avg_score"], row["views"]))[:6]
    use_guidance: list[str] = []
    avoid_guidance: list[str] = []
    for dim in dimensions:
        summary = dimension_summaries[dim]
        if summary["winners"]:
            labels = ", ".join(row["value"].replace("-", " ") for row in summary["winners"])
            use_guidance.append(f"{dim.replace('_', ' ')}: {labels}")
        if summary["losers"]:
            labels = ", ".join(row["value"].replace("-", " ") for row in summary["losers"])
            avoid_guidance.append(f"{dim.replace('_', ' ')}: {labels}")
    lines: list[str] = []
    for dim in dimensions:
        summary = dimension_summaries[dim]
        if not summary["winners"]:
            continue
        winner = summary["winners"][0]
        label = winner["value"].replace("-", " ")
        line = (
            f"Favor {dim.replace('_', ' ')}: {label} "
            f"({winner['clips']} clips, avg score {winner['avg_score']}, {winner['views']} views)."
        )
        if summary["losers"]:
            loser = summary["losers"][0]
            loser_label = loser["value"].replace("-", " ")
            line += f" De-emphasize: {loser_label} (avg score {loser['avg_score']})."
        lines.append(line)
    if winners:
        win = winners[0]
        tags = win.get("tags", {})
        lines.append(
            "Best observed pattern: "
            f"{tags.get('product')} + {tags.get('hook_type')} + {tags.get('topic')} "
            f"with {tags.get('cta_type')} CTA."
        )
    return {
        "sample": {"clips": len(records)},
        "dimensions": dimension_summaries,
        "top_patterns": top_patterns,
        "weak_patterns": weak_patterns,
        "winners": winners,
        "losers": losers,
        "actionable_guidance": {
            "use": use_guidance,
            "avoid": avoid_guidance,
        },
        "prompt_lines": lines,
    }
