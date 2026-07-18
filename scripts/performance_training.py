"""Local performance feedback records for clip-level training data.

This module is intentionally offline-only. It reads local content kit metadata,
platform metadata, and JSON/JSONL metric exports, then emits normalized records
that can be used later by an optimizer or prompt layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from hermes_store import CONTENT_DIR as HERMES_CONTENT_DIR
from hermes_store import STATE_DIR as HERMES_STATE_DIR
from hermes_store import write_jsonl

CONTENT_DIR = HERMES_CONTENT_DIR
STATE_DIR = HERMES_STATE_DIR
TRAINING_RECORDS_PATH = STATE_DIR / "performance-training-records.jsonl"
DEFAULT_METRIC_PATHS = (STATE_DIR / "youtube-video-stats.jsonl",)

METRIC_FIELDS = (
    "views",
    "average_view_duration_seconds",
    "retention_rate",
    "likes",
    "comments",
    "clicks",
    "website_sessions",
    "purchases",
)

_METRIC_ALIASES = {
    "views": ("views", "view_count", "video_views", "plays", "play_count"),
    "average_view_duration_seconds": (
        "average_view_duration_seconds",
        "average_view_duration_sec",
        "avg_view_duration_seconds",
        "avg_view_duration_sec",
        "average_view_duration",
        "avg_view_duration",
        "avg_watch_time",
        "avg_watch_time_seconds",
    ),
    "retention_rate": (
        "retention_rate",
        "retention",
        "average_percentage_viewed",
        "avg_percent_viewed",
        "avg_percentage_viewed",
        "completion_rate",
        "watch_completion_rate",
    ),
    "likes": ("likes", "like_count"),
    "comments": ("comments", "comment_count", "replies"),
    "clicks": ("clicks", "link_clicks", "website_clicks", "landing_page_clicks", "profile_clicks"),
    "website_sessions": ("website_sessions", "sessions", "site_sessions", "website_sessions_by_source"),
    "purchases": ("purchases", "purchase_count", "orders", "conversions", "conversion_count"),
}

_NESTED_METRIC_KEYS = ("metrics", "stats", "analytics", "performance", "site_metrics")
_PLATFORMS = {"youtube", "tiktok", "instagram", "rumble", "site", "website"}
_SHORTS_BLOCK_RE = re.compile(
    r"^###\s*Short\s*(?P<index>\d+)(?P<title_line>[^\n]*)\n"
    r"(?P<body>.*?)(?=^###\s*Short\s*\d+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_SOURCE_RE = re.compile(
    r"Source[^:]*:\*{0,2}\s*\[?(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[\.,]\d+)?)\]?"
    r"\s*(?:to|->|[^\w\s]+)\s*"
    r"\[?(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[\.,]\d+)?)\]?",
    re.IGNORECASE,
)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _iter_json_rows(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    if path.suffix.lower() == ".jsonl":
        yield from _read_jsonl(path)
        return

    value = _load_json(path)
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                yield row
        return
    if not isinstance(value, dict):
        return

    for key in ("records", "rows", "metrics", "data", "items", "videos", "clips"):
        nested = value.get(key)
        if isinstance(nested, list):
            for row in nested:
                if isinstance(row, dict):
                    yield row
            return

    if value and all(isinstance(v, dict) for v in value.values()):
        for key, row in value.items():
            merged = dict(row)
            if not any(merged.get(k) for k in ("video_id", "clip_id", "asset_id")):
                merged["asset_id"] = key
            yield merged
        return

    yield value


def _flatten_metric_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    flattened = {str(k).lower(): v for k, v in row.items()}
    for nested_key in _NESTED_METRIC_KEYS:
        nested = row.get(nested_key)
        if isinstance(nested, Mapping):
            for k, v in nested.items():
                flattened[str(k).lower()] = v
    return flattened


def _first(payload: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name.lower() in payload and payload[name.lower()] not in ("", None):
            return payload[name.lower()]
    return None


def _to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        try:
            return _ts_to_seconds(text)
        except ValueError:
            return None
    text = text.replace(",", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kmb])?", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    elif suffix == "b":
        number *= 1_000_000_000
    return number


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


def _to_rate(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    text = str(value)
    if "%" in text or number > 1:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _ts_to_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        h = 0
        m, s = parts
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError(f"invalid timestamp: {value}")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.strip().strip('"').strip("'")


def _norm_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _normal_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _clip_key(slug: str, platform: str, clip_index: int | str | None) -> str:
    idx = _parse_clip_index(clip_index)
    index_text = f"{idx:02d}" if idx is not None else "unknown"
    return f"{slug}|{_normalize_platform(platform)}|{index_text}"


def _parse_clip_index(value: Any) -> int | None:
    if value in ("", None):
        return None
    if isinstance(value, int):
        return value
    text = str(value)
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    return int(match.group(1))


def _normalize_platform(value: Any) -> str:
    platform = _norm_token(value)
    if platform == "website":
        return "site"
    return platform or "unknown"


def _short_field(body_md: str, label: str) -> str:
    label_re = re.escape(label)
    patterns = [
        rf"^\s*-\s*\*\*{label_re}[^:]*:\*\*\s*(.+)$",
        rf"^\s*-\s*{label_re}[^:]*:\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_md or "", flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return _clean_text(match.group(1))
    return ""


def _source_range(body_md: str) -> tuple[float, float] | None:
    match = _SOURCE_RE.search(body_md or "")
    if not match:
        return None
    try:
        start = _ts_to_seconds(match.group("start"))
        end = _ts_to_seconds(match.group("end"))
    except ValueError:
        return None
    if end <= start:
        return None
    return start, end


def _heading_title(index: int, title_line: str) -> str:
    quoted = re.search(r'"([^"]+)"', title_line or "")
    if quoted:
        return _clean_text(quoted.group(1))
    title = re.sub(r"^[\s:\-\u2013\u2014]+", "", title_line or "")
    return _clean_text(title) or f"Short {index}"


def parse_shorts_metadata(shorts_md: str) -> dict[int, dict[str, Any]]:
    """Extract deterministic creative metadata from a Shorts markdown file."""
    clips: dict[int, dict[str, Any]] = {}
    for block in _SHORTS_BLOCK_RE.finditer(shorts_md or ""):
        index = int(block.group("index"))
        body = block.group("body")
        source = _source_range(body)
        duration = (source[1] - source[0]) if source else None
        hook = _short_field(body, "Hook")
        on_screen_text = _short_field(body, "On-screen text")
        cta = _short_field(body, "Outro CTA")
        title = _heading_title(index, block.group("title_line"))
        clips[index] = {
            "clip_index": index,
            "title": title,
            "source_start_seconds": source[0] if source else None,
            "source_end_seconds": source[1] if source else None,
            "duration_seconds": duration,
            "hook_text": hook,
            "opening_phrase": opening_phrase(hook),
            "on_screen_text": on_screen_text,
            "cta": cta,
        }
    return clips


def opening_phrase(hook_text: str, *, max_words: int = 12) -> str:
    hook = _clean_text(hook_text)
    if not hook:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", hook, maxsplit=1)[0]
    words = first_sentence.split()
    if len(words) <= max_words:
        return first_sentence
    return " ".join(words[:max_words])


def classify_hook_type(hook_text: str, title: str = "") -> str:
    text = f"{hook_text} {title}".lower()
    if not text.strip():
        return "unknown"
    if "?" in hook_text:
        return "question"
    if re.search(r"[$%]?\d", text):
        return "number_or_metric"
    if any(term in text for term in ("before", "after", "difference", "instead", "against")):
        return "contrast"
    if any(term in text for term in ("liquidation", "blow", "risk", "loss", "account", "can't touch")):
        return "stakes"
    if any(term in text for term in ("rule", "no exceptions", "always", "never")):
        return "rule"
    if any(term in text for term in ("how", "here's how", "setup", "walkthrough", "tutorial", "set ")):
        return "how_to"
    if any(term in text for term in ("screen", "click", "placed", "hit lock", "order")):
        return "product_demo"
    return "statement"


def classify_product(*values: Any, fallback: str = "") -> str:
    joined = " ".join(_clean_text(v).lower() for v in values if v)
    fallback = fallback.lower()
    has_bracket_boss = (
        "bracket boss" in joined
        or any(term in joined for term in ("scale-out", "scale out", "break-even", "break even", "r:r", "rr locked"))
        or fallback in {"bb", "bracket_boss"}
    )
    has_drawdown_guardian = (
        "drawdown guardian" in joined
        or "draw down guardian" in joined
        or any(term in joined for term in ("liquidation", "drawdown", "discipline score", "auto flat"))
        or fallback in {"dg", "drawdown_guardian"}
    )
    if fallback in {"both", "all"}:
        return "both"
    if has_bracket_boss and has_drawdown_guardian:
        return "both"
    if has_bracket_boss:
        return "bracket_boss"
    if has_drawdown_guardian:
        return "drawdown_guardian"
    if "ninjatrader" in joined or "nt8" in joined:
        return "ninjatrader"
    if "shadow edge" in joined:
        return "shadow_edge_tools"
    return "unknown"


def classify_topic(*values: Any) -> str:
    text = " ".join(_clean_text(v).lower() for v in values if v)
    if any(term in text for term in ("liquidation", "drawdown", "daily loss", "auto flat")):
        return "drawdown_control"
    if any(term in text for term in ("scale", "break-even", "break even", "target", "contract riding")):
        return "trade_management"
    if any(term in text for term in ("install", "installation", "setup", "walkthrough", "settings", "menu")):
        return "setup_mechanics"
    if any(term in text for term in ("zone", "trend", "reversal", "ib ", "breakdown", "probability")):
        return "market_structure"
    if any(term in text for term in ("risk", "r:r", "stop", "position siz", "contracts")):
        return "risk_planning"
    if any(term in text for term in ("discipline", "lock function", "lockout", "walk away", "score")):
        return "discipline"
    return "general"


def classify_visual_style(*, aspect_ratio: str = "", width: Any = None, height: Any = None, visual_label: str = "", on_screen_text: str = "") -> str:
    tags: list[str] = []
    width_num = _to_float(width)
    height_num = _to_float(height)
    aspect = (aspect_ratio or "").lower()
    if "9:16" in aspect or (width_num and height_num and height_num > width_num):
        tags.append("vertical_short")
    if on_screen_text:
        tags.append("text_overlay")
    if visual_label:
        tags.append("branded_label")
    visual_blob = f"{visual_label} {on_screen_text}".lower()
    if any(term in visual_blob for term in ("ninjatrader", "trade", "risk", "chart", "order")):
        tags.append("screen_recording")
    return "+".join(tags) if tags else "unknown"


def _distribution_metadata_paths(campaign_dir: Path) -> Iterable[Path]:
    base = campaign_dir / "distribution"
    if not base.exists():
        return
    for platform_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for clip_dir in sorted(p for p in platform_dir.iterdir() if p.is_dir()):
            metadata_path = clip_dir / "metadata.json"
            if metadata_path.exists():
                yield metadata_path


def _manifest_clips(campaign_dir: Path) -> dict[int, dict[str, Any]]:
    manifest = _load_json(campaign_dir / "00-pipeline-manifest.json")
    if not isinstance(manifest, dict):
        return {}
    clips: dict[int, dict[str, Any]] = {}
    for item in manifest.get("clips") or []:
        if not isinstance(item, dict):
            continue
        idx = _parse_clip_index(item.get("index"))
        if idx is not None:
            clips[idx] = item
    return clips


def build_clip_index(content_dir: Path = CONTENT_DIR) -> dict[str, dict[str, Any]]:
    """Return platform clip metadata keyed by ``slug|platform|NN``."""
    content_dir = Path(content_dir)
    index: dict[str, dict[str, Any]] = {}
    if not content_dir.exists():
        return index

    for campaign_dir in sorted(p for p in content_dir.iterdir() if p.is_dir()):
        slug = campaign_dir.name
        campaign_meta = _load_json(campaign_dir / "00-meta.json") or {}
        manifest = _manifest_clips(campaign_dir)
        shorts_path = campaign_dir / "02-shorts-scripts.md"
        shorts = parse_shorts_metadata(shorts_path.read_text(encoding="utf-8") if shorts_path.exists() else "")

        for metadata_path in _distribution_metadata_paths(campaign_dir):
            metadata = _load_json(metadata_path)
            if not isinstance(metadata, dict):
                continue
            platform = _normalize_platform(metadata.get("platform") or metadata_path.parents[1].name)
            clip_index = _parse_clip_index(metadata_path.parent.name)
            if clip_index is None:
                continue
            manifest_clip = manifest.get(clip_index, {})
            script_clip = shorts.get(clip_index, {})
            sidecar_title = _read_text(metadata_path.parent / "title.txt")
            sidecar_caption = _read_text(metadata_path.parent / "caption.txt")
            title = (
                metadata.get("title")
                or metadata.get("title_used")
                or manifest_clip.get("visual_title")
                or script_clip.get("title")
                or f"Short {clip_index}"
            )
            if sidecar_title and _normal_title(title) in {"", f"short {clip_index}", "short"}:
                title = sidecar_title
            duration = (
                _to_float(manifest_clip.get("duration_sec"))
                or _to_float(script_clip.get("duration_seconds"))
                or _to_float(metadata.get("duration_seconds"))
                or _to_float(metadata.get("duration_sec"))
            )
            visual_label = metadata.get("visual_label") or manifest_clip.get("visual_label") or ""
            visual_title = metadata.get("visual_title") or manifest_clip.get("visual_title") or title
            hook = script_clip.get("hook_text", "")
            cta = (
                script_clip.get("cta")
                or metadata.get("cta")
                or metadata.get("outro_cta")
                or sidecar_caption
                or ""
            )
            on_screen_text = script_clip.get("on_screen_text", "")
            clip_relpath = metadata.get("clip_relpath") or manifest_clip.get("clip") or ""
            aspect_ratio = (
                manifest_clip.get("aspect_ratio")
                or (metadata.get("limits") or {}).get("aspect_label")
                or ""
            )
            record = {
                "clip_key": _clip_key(slug, platform, clip_index),
                "campaign_slug": slug,
                "campaign_product": campaign_meta.get("product", ""),
                "clip_index": clip_index,
                "platform": platform,
                "title": _clean_text(title),
                "visual_title": _clean_text(visual_title),
                "visual_label": _clean_text(visual_label),
                "clip_relpath": _clean_text(clip_relpath),
                "metadata_path": str(metadata_path),
                "video_id": _clean_text(metadata.get("video_id")),
                "platform_url": _clean_text(metadata.get("platform_url") or metadata.get("manual_url")),
                "uploaded_at": metadata.get("uploaded_at"),
                "duration_seconds": duration,
                "source_start_seconds": script_clip.get("source_start_seconds"),
                "source_end_seconds": script_clip.get("source_end_seconds"),
                "hook_text": hook,
                "opening_phrase": script_clip.get("opening_phrase", ""),
                "cta": _clean_text(cta),
                "caption": _clean_text(sidecar_caption or metadata.get("caption") or metadata.get("description")),
                "on_screen_text": on_screen_text,
                "hook_type": classify_hook_type(hook, str(title)),
                "product": classify_product(visual_label, title, hook, on_screen_text, cta, sidecar_caption, fallback=str(campaign_meta.get("product", ""))),
                "topic": classify_topic(title, hook, on_screen_text, cta, sidecar_caption),
                "visual_style": classify_visual_style(
                    aspect_ratio=str(aspect_ratio),
                    width=manifest_clip.get("width"),
                    height=manifest_clip.get("height"),
                    visual_label=str(visual_label),
                    on_screen_text=str(on_screen_text),
                ),
            }
            index[record["clip_key"]] = record
    return index


def normalize_metric_record(row: Mapping[str, Any], *, source: str = "local") -> dict[str, Any] | None:
    """Normalize one raw metric row into the clip-metric sidecar shape."""
    payload = _flatten_metric_payload(row)
    metrics: dict[str, int | float] = {}
    for field in METRIC_FIELDS:
        raw = _first(payload, _METRIC_ALIASES[field])
        if raw in ("", None):
            continue
        if field == "retention_rate":
            value = _to_rate(raw)
        elif field == "average_view_duration_seconds":
            value = _to_float(raw)
        elif field in {"views", "likes", "comments", "clicks", "website_sessions", "purchases"}:
            value = _to_int(raw)
        else:
            value = _to_float(raw)
        if value is not None:
            metrics[field] = value

    if not metrics:
        return None

    source_platform = _normalize_platform(
        row.get("source_platform") or row.get("utm_source") or payload.get("source_platform") or ""
    )
    platform = _normalize_platform(row.get("platform") or payload.get("platform") or "")
    if platform == "unknown":
        row_source = _clean_text(row.get("source") or source).lower()
        if row.get("video_id") or "youtube" in row_source:
            platform = "youtube"
        elif "tiktok" in row_source:
            platform = "tiktok"
        elif any(k in metrics for k in ("website_sessions", "purchases")):
            platform = "site"

    return {
        "source": source,
        "metric_platform": platform,
        "source_platform": "" if source_platform == "unknown" else source_platform,
        "campaign_slug": _clean_text(row.get("campaign_slug") or row.get("slug") or row.get("campaign_id")),
        "clip_index": _parse_clip_index(row.get("clip_index") or row.get("short_clip_index")),
        "clip_id": _clean_text(row.get("clip_id") or row.get("asset_id") or row.get("utm_content") or row.get("queue_key")),
        "video_id": _clean_text(row.get("video_id") or row.get("id")),
        "platform_url": _clean_text(row.get("platform_url") or row.get("url") or row.get("manual_url")),
        "title": _clean_text(row.get("title")),
        "measured_at": _clean_text(row.get("measured_at") or row.get("pulled_at") or row.get("date") or row.get("recorded_at")),
        "metrics": metrics,
    }


def load_metric_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        for row in _iter_json_rows(path):
            normalized = normalize_metric_record(row, source=str(path))
            if normalized:
                records.append(normalized)
    return records


def load_metadata_metric_records(content_dir: Path = CONTENT_DIR) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for metadata_path in sorted(Path(content_dir).glob("*/distribution/*/*/metadata.json")):
        metadata = _load_json(metadata_path)
        if not isinstance(metadata, dict):
            continue
        content_platform = _normalize_platform(metadata.get("platform") or metadata_path.parents[1].name)
        metric_sources = [
            ("stats", metadata.get("stats"), content_platform),
            ("metrics", metadata.get("metrics"), content_platform),
            ("performance", metadata.get("performance"), content_platform),
            ("analytics", metadata.get("analytics"), content_platform),
            ("site_metrics", metadata.get("site_metrics"), "site"),
        ]
        for source_name, payload, metric_platform in metric_sources:
            if not isinstance(payload, Mapping):
                continue
            row = {
                **payload,
                "platform": metric_platform,
                "source_platform": content_platform if metric_platform == "site" else "",
                "campaign_slug": metadata_path.parents[3].name,
                "clip_index": metadata_path.parent.name,
                "video_id": metadata.get("video_id"),
                "platform_url": metadata.get("platform_url") or metadata.get("manual_url"),
                "title": metadata.get("title") or metadata.get("title_used"),
                "measured_at": payload.get("pulled_at") or payload.get("measured_at") or metadata.get("uploaded_at"),
                "metric_source": source_name,
            }
            normalized = normalize_metric_record(row, source=f"{metadata_path}#{source_name}")
            if normalized:
                records.append(normalized)
    return records


def _clip_ref_candidates(metric: Mapping[str, Any]) -> list[tuple[str, str, int | None]]:
    candidates: list[tuple[str, str, int | None]] = []
    slug = _clean_text(metric.get("campaign_slug"))
    platform = _normalize_platform(metric.get("source_platform") or metric.get("metric_platform"))
    idx = _parse_clip_index(metric.get("clip_index"))
    if slug and idx is not None:
        candidates.append((slug, platform, idx))

    for raw in (metric.get("clip_id"),):
        text = _clean_text(raw)
        if not text:
            continue
        patterns = [
            r"^(?P<slug>[^|:]+)[|:](?P<platform>[a-zA-Z0-9_-]+)[|:](?:clip-)?(?P<idx>\d+)$",
            r"content[/\\](?P<slug>[^/\\]+)[/\\]distribution[/\\](?P<platform>[^/\\]+)[/\\](?P<idx>\d+)",
            r"^(?P<slug>.+?)[|:](?:clip-)?(?P<idx>\d+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            ref_slug = _clean_text(match.group("slug"))
            ref_platform = _normalize_platform(match.groupdict().get("platform") or platform)
            ref_idx = _parse_clip_index(match.group("idx"))
            if ref_slug and ref_idx is not None:
                candidates.append((ref_slug, ref_platform, ref_idx))
                break
    return candidates


def _alias_map(index: Mapping[str, dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for key, record in index.items():
        aliases[f"key:{key}"] = key
        if record.get("video_id"):
            aliases[f"video:{record['video_id']}"] = key
        if record.get("platform_url"):
            aliases[f"url:{record['platform_url']}"] = key
        if record.get("clip_relpath"):
            aliases[f"clip:{record['campaign_slug']}:{_norm_token(record['clip_relpath'])}"] = key
        if record.get("title"):
            aliases[f"title:{record['platform']}:{_normal_title(record['title'])}"] = key
    return aliases


def match_metric_record(metric: Mapping[str, Any], index: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    aliases = _alias_map(index)
    for slug, platform, idx in _clip_ref_candidates(metric):
        key = _clip_key(slug, platform, idx)
        if f"key:{key}" in aliases:
            return index[aliases[f"key:{key}"]]
        if platform in {"site", "unknown"}:
            for candidate_platform in ("youtube", "tiktok", "instagram", "rumble"):
                key = _clip_key(slug, candidate_platform, idx)
                if f"key:{key}" in aliases:
                    return index[aliases[f"key:{key}"]]
    if metric.get("video_id") and f"video:{metric['video_id']}" in aliases:
        return index[aliases[f"video:{metric['video_id']}"]]
    if metric.get("platform_url") and f"url:{metric['platform_url']}" in aliases:
        return index[aliases[f"url:{metric['platform_url']}"]]
    if metric.get("title"):
        metric_platform = _normalize_platform(metric.get("metric_platform"))
        title_key = f"title:{metric_platform}:{_normal_title(metric['title'])}"
        if title_key in aliases:
            return index[aliases[title_key]]
    return None


def _derived_metrics(metrics: Mapping[str, Any], duration_seconds: Any) -> dict[str, float | int]:
    derived: dict[str, float | int] = {}
    views = _to_float(metrics.get("views")) or 0.0
    likes = _to_float(metrics.get("likes")) or 0.0
    comments = _to_float(metrics.get("comments")) or 0.0
    clicks = _to_float(metrics.get("clicks")) or 0.0
    sessions = _to_float(metrics.get("website_sessions")) or 0.0
    purchases = _to_float(metrics.get("purchases")) or 0.0
    avg_view = _to_float(metrics.get("average_view_duration_seconds"))
    duration = _to_float(duration_seconds)

    engagements = int(likes + comments)
    derived["engagements"] = engagements
    if views > 0:
        derived["engagement_rate"] = round(engagements / views, 6)
        if clicks:
            derived["click_rate"] = round(clicks / views, 6)
    if sessions > 0 and purchases:
        derived["purchase_rate"] = round(purchases / sessions, 6)
    if avg_view is not None and duration and duration > 0:
        derived["average_view_ratio"] = round(min(avg_view / duration, 1.0), 6)
    return derived


def training_record_from_metric(creative: Mapping[str, Any], metric: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(metric.get("metrics") or {})
    measured_at = _clean_text(metric.get("measured_at")) or "undated"
    identity = json.dumps(
        {
            "clip_key": creative.get("clip_key"),
            "metric_platform": metric.get("metric_platform"),
            "measured_at": measured_at,
            "metrics": metrics,
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    return {
        "record_id": f"{creative.get('clip_key')}|{metric.get('metric_platform')}|{digest}",
        "campaign_slug": creative.get("campaign_slug", ""),
        "clip_key": creative.get("clip_key", ""),
        "clip_index": creative.get("clip_index"),
        "platform": creative.get("platform", metric.get("metric_platform", "")),
        "metric_platform": metric.get("metric_platform", ""),
        "metric_source": metric.get("source", ""),
        "measured_at": measured_at,
        "title": creative.get("title", metric.get("title", "")),
        "clip_relpath": creative.get("clip_relpath", ""),
        "duration_seconds": creative.get("duration_seconds"),
        "hook_type": creative.get("hook_type", "unknown"),
        "product": creative.get("product", "unknown"),
        "topic": creative.get("topic", "general"),
        "opening_phrase": creative.get("opening_phrase", ""),
        "hook_text": creative.get("hook_text", ""),
        "cta": creative.get("cta", ""),
        "visual_style": creative.get("visual_style", "unknown"),
        "metrics": metrics,
        "derived": _derived_metrics(metrics, creative.get("duration_seconds")),
    }


def build_training_records(
    *,
    content_dir: Path = CONTENT_DIR,
    metric_paths: Iterable[Path] | None = None,
    include_unmeasured: bool = False,
) -> list[dict[str, Any]]:
    index = build_clip_index(content_dir)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    metrics = load_metadata_metric_records(content_dir)
    selected_paths = list(DEFAULT_METRIC_PATHS if metric_paths is None else metric_paths)
    metrics.extend(load_metric_records(selected_paths))

    for metric in metrics:
        creative = match_metric_record(metric, index)
        if not creative:
            continue
        record = training_record_from_metric(creative, metric)
        fingerprint = json.dumps(
            {
                "clip_key": record["clip_key"],
                "metric_platform": record["metric_platform"],
                "measured_at": record["measured_at"],
                "metrics": record["metrics"],
            },
            sort_keys=True,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        records.append(record)

    if include_unmeasured:
        measured_keys = {record["clip_key"] for record in records}
        for creative in index.values():
            if creative["clip_key"] in measured_keys:
                continue
            placeholder = training_record_from_metric(
                creative,
                {
                    "metric_platform": creative.get("platform", ""),
                    "source": "content_metadata",
                    "measured_at": "",
                    "metrics": {},
                },
            )
            placeholder["training_eligible"] = False
            records.append(placeholder)

    records.sort(key=lambda r: (str(r.get("campaign_slug", "")), int(r.get("clip_index") or 0), str(r.get("platform", "")), str(r.get("measured_at", ""))))
    return records


def write_training_records_file(records: list[dict[str, Any]], path: Path = TRAINING_RECORDS_PATH) -> Path:
    write_jsonl(Path(path), records)
    return Path(path)


def build_and_write_training_records(
    *,
    content_dir: Path = CONTENT_DIR,
    metric_paths: Iterable[Path] | None = None,
    output_path: Path = TRAINING_RECORDS_PATH,
    include_unmeasured: bool = False,
) -> list[dict[str, Any]]:
    records = build_training_records(
        content_dir=content_dir,
        metric_paths=metric_paths,
        include_unmeasured=include_unmeasured,
    )
    write_training_records_file(records, output_path)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build offline clip performance training records")
    parser.add_argument("--content-dir", default=str(CONTENT_DIR))
    parser.add_argument("--output", default=str(TRAINING_RECORDS_PATH))
    parser.add_argument("--metric-path", action="append", default=[])
    parser.add_argument("--include-unmeasured", action="store_true")
    args = parser.parse_args(argv)

    metric_paths = [Path(p) for p in args.metric_path] if args.metric_path else None
    records = build_and_write_training_records(
        content_dir=Path(args.content_dir),
        metric_paths=metric_paths,
        output_path=Path(args.output),
        include_unmeasured=args.include_unmeasured,
    )
    print(f"wrote {len(records)} performance training records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
